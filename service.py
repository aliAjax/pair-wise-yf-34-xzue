"""机型与计划数据层及业务规则：仓储、计划生命周期、续航放行编排。计算规则见 endurance.py。"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from endurance import WIND_FACTOR_MAX, WIND_FACTOR_MIN, assess_endurance, wind_factor_valid

ROLES = {"viewer", "operator", "airspace_reviewer", "commander", "auditor"}
ACTIVE_STATUSES = {"submitted", "approved"}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, details: Any = None):
        super().__init__(message); self.status, self.code, self.message, self.details = status, code, message, details


def utcnow() -> datetime: return datetime.now(timezone.utc)
def iso(value: datetime | None = None) -> str: return (value or utcnow()).replace(microsecond=0).isoformat().replace("+00:00", "Z")
def parse_time(value: str | None) -> datetime:
    if not value: raise ApiError(400, "time_required", "必须提供 ISO 8601 时间")
    try: parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc: raise ApiError(400, "invalid_time", f"时间格式错误: {value}") from exc
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def route_bbox(route: list[list[float]]) -> tuple[float, float, float, float]:
    xs = [float(point[0]) for point in route]; ys = [float(point[1]) for point in route]
    return min(xs), min(ys), max(xs), max(ys)


def boxes_overlap(a: tuple[float, float, float, float], b: tuple[float, float, float, float], buffer: float = 0.0) -> bool:
    return a[0] <= b[2] + buffer and a[2] + buffer >= b[0] and a[1] <= b[3] + buffer and a[3] + buffer >= b[1]


def times_overlap(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool: return a_start < b_end and b_start < a_end


def validate_route(route: Any) -> list[list[float]]:
    if not isinstance(route, list) or len(route) < 2: raise ApiError(400, "invalid_route", "航线至少需要两个经纬度点")
    normalized: list[list[float]] = []
    for point in route:
        if not isinstance(point, list) or len(point) != 2 or not all(isinstance(v, (int, float)) for v in point): raise ApiError(400, "invalid_route_point", "每个航线点必须是 [经度,纬度]")
        lon, lat = float(point[0]), float(point[1])
        if not -180 <= lon <= 180 or not -90 <= lat <= 90: raise ApiError(400, "invalid_coordinates", "经纬度超出范围")
        normalized.append([lon, lat])
    return normalized


def validate_point(value: Any, label: str = "备用降落点") -> tuple[float, float]:
    if not isinstance(value, list) or len(value) != 2 or not all(isinstance(v, (int, float)) for v in value): raise ApiError(400, "invalid_point", f"{label}必须是 [经度,纬度]")
    lon, lat = float(value[0]), float(value[1])
    if not -180 <= lon <= 180 or not -90 <= lat <= 90: raise ApiError(400, "invalid_coordinates", "经纬度超出范围")
    return lon, lat


class Repository:
    def __init__(self, path: str | Path):
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row; self.conn.execute("PRAGMA foreign_keys=ON"); self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS restrictions(
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, kind TEXT NOT NULL, min_lon REAL NOT NULL, min_lat REAL NOT NULL,
            max_lon REAL NOT NULL, max_lat REAL NOT NULL, min_altitude REAL NOT NULL DEFAULT 0, max_altitude REAL NOT NULL,
            starts_at TEXT NOT NULL, ends_at TEXT NOT NULL, reason TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS aircraft_types(
            id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL UNIQUE, name TEXT NOT NULL DEFAULT '',
            cruise_speed_kmh REAL NOT NULL, endurance_minutes REAL NOT NULL, return_minutes REAL NOT NULL,
            created_by TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS flight_plans(
            id INTEGER PRIMARY KEY AUTOINCREMENT, operator_id TEXT NOT NULL, callsign TEXT NOT NULL, drone_model TEXT NOT NULL,
            aircraft_type_id INTEGER REFERENCES aircraft_types(id), alternate_lon REAL, alternate_lat REAL,
            payload_kg REAL NOT NULL, route_json TEXT NOT NULL, starts_at TEXT NOT NULL, ends_at TEXT NOT NULL, max_altitude REAL NOT NULL,
            population_risk INTEGER NOT NULL, emergency_plan TEXT NOT NULL, region TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft',
            revision INTEGER NOT NULL DEFAULT 1, created_by TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(operator_id,callsign,starts_at)
        );
        CREATE TABLE IF NOT EXISTS approvals(
            id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER NOT NULL REFERENCES flight_plans(id), plan_revision INTEGER NOT NULL,
            reviewer TEXT NOT NULL, decision TEXT NOT NULL, reason TEXT NOT NULL, offline_id TEXT UNIQUE,
            override_kind TEXT, wind_factor REAL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS notifications(
            id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER NOT NULL REFERENCES flight_plans(id), kind TEXT NOT NULL,
            message TEXT NOT NULL, created_at TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS audit_log(
            id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER, actor TEXT NOT NULL, role TEXT NOT NULL, action TEXT NOT NULL,
            detail_json TEXT NOT NULL, created_at TEXT NOT NULL
        );
        """)
        self._migrate()

    def _migrate(self) -> None:
        """为既有数据库补齐续航放行新增列（新库建表时已包含）。"""
        additions = {
            "flight_plans": {
                "aircraft_type_id": "aircraft_type_id INTEGER REFERENCES aircraft_types(id)",
                "alternate_lon": "alternate_lon REAL",
                "alternate_lat": "alternate_lat REAL",
            },
            "approvals": {"wind_factor": "wind_factor REAL"},
        }
        for table, columns in additions.items():
            existing = {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}
            for column, ddl in columns.items():
                if column not in existing: self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")

    @contextmanager
    def tx(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try: yield self.conn; self.conn.execute("COMMIT")
        except Exception: self.conn.execute("ROLLBACK"); raise

    @staticmethod
    def audit(conn: sqlite3.Connection, plan_id: int | None, actor: str, role: str, action: str, detail: dict[str, Any]) -> None:
        conn.execute("INSERT INTO audit_log(plan_id,actor,role,action,detail_json,created_at) VALUES(?,?,?,?,?,?)",
                     (plan_id, actor, role, action, json.dumps(detail, ensure_ascii=False, sort_keys=True), iso()))

    @staticmethod
    def notify(conn: sqlite3.Connection, plan_id: int, kind: str, message: str) -> None:
        conn.execute("INSERT INTO notifications(plan_id,kind,message,created_at) VALUES(?,?,?,?)", (plan_id, kind, message, iso()))


class DroneAirspaceService:
    def __init__(self, path: str | Path): self.repo = Repository(path)

    @staticmethod
    def identity(headers: Any) -> tuple[str, str, str]:
        actor, role, operator = headers.get("X-User-Id", "").strip(), headers.get("X-Role", "").strip(), headers.get("X-Operator", "").strip()
        if not actor or role not in ROLES: raise ApiError(401, "unauthorized", "需要 X-User-Id 和有效 X-Role")
        if role == "operator" and not operator: raise ApiError(401, "operator_required", "运营方角色必须提供 X-Operator")
        return actor, role, operator

    @staticmethod
    def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None: return dict(row) if row else None

    def create_restriction(self, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"airspace_reviewer", "commander"}: raise ApiError(403, "restriction_forbidden", "只有空域审核员或指挥官可以维护限制")
        name, kind, reason = str(body.get("name", "")).strip(), str(body.get("kind", "")).strip(), str(body.get("reason", "")).strip()
        if kind not in {"no_fly", "temporary_limit"} or not name or not reason: raise ApiError(400, "invalid_restriction", "名称、类型和原因必填")
        try:
            min_lon, min_lat, max_lon, max_lat = map(float, (body.get("min_lon"), body.get("min_lat"), body.get("max_lon"), body.get("max_lat")))
            min_alt, max_alt = float(body.get("min_altitude", 0)), float(body.get("max_altitude"))
        except (TypeError, ValueError): raise ApiError(400, "invalid_restriction", "空域范围和高度必须为数字")
        start, end = parse_time(body.get("starts_at")), parse_time(body.get("ends_at"))
        if min_lon >= max_lon or min_lat >= max_lat or min_alt < 0 or max_alt <= min_alt or end <= start:
            raise ApiError(400, "invalid_restriction", "空域范围、高度或时间无效")
        with self.repo.tx() as conn:
            cur = conn.execute("""INSERT INTO restrictions(name,kind,min_lon,min_lat,max_lon,max_lat,min_altitude,max_altitude,starts_at,ends_at,reason,created_at)
                                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (name, kind, min_lon, min_lat, max_lon, max_lat, min_alt, max_alt, iso(start), iso(end), reason, iso()))
            return dict(conn.execute("SELECT * FROM restrictions WHERE id=?", (cur.lastrowid,)).fetchone())

    def create_aircraft_type(self, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"airspace_reviewer", "commander"}: raise ApiError(403, "aircraft_type_forbidden", "只有空域审核员或指挥官可以登记机型资料")
        code, name = str(body.get("code", "")).strip().upper(), str(body.get("name", "")).strip()
        try:
            cruise, endurance, ret = float(body.get("cruise_speed_kmh")), float(body.get("endurance_minutes")), float(body.get("return_minutes"))
        except (TypeError, ValueError): raise ApiError(400, "invalid_aircraft_type", "巡航速度、满电时长和安全返航分钟必须为数字")
        if not code or cruise <= 0 or endurance <= 0 or ret < 0 or ret >= endurance:
            raise ApiError(400, "invalid_aircraft_type", "机型代码必填，巡航速度和满电时长必须为正，安全返航分钟必须小于满电时长")
        with self.repo.tx() as conn:
            try:
                cur = conn.execute("INSERT INTO aircraft_types(code,name,cruise_speed_kmh,endurance_minutes,return_minutes,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                                   (code, name, cruise, endurance, ret, actor, iso()))
            except sqlite3.IntegrityError as exc: raise ApiError(409, "aircraft_type_exists", "该机型资料已登记") from exc
            Repository.audit(conn, None, actor, role, "aircraft_type_created", {"code": code, "cruise_speed_kmh": cruise, "endurance_minutes": endurance, "return_minutes": ret})
            return dict(conn.execute("SELECT * FROM aircraft_types WHERE id=?", (cur.lastrowid,)).fetchone())

    def list_aircraft_types(self, role: str) -> dict[str, Any]:
        return {"aircraft_types": [dict(r) for r in self.repo.conn.execute("SELECT * FROM aircraft_types ORDER BY code")]}

    def _aircraft_type(self, conn: sqlite3.Connection, body: dict[str, Any]) -> sqlite3.Row:
        type_id, code = body.get("aircraft_type_id"), body.get("aircraft_type")
        if type_id is not None:
            row = conn.execute("SELECT * FROM aircraft_types WHERE id=?", (type_id,)).fetchone()
        elif code not in (None, ""):
            row = conn.execute("SELECT * FROM aircraft_types WHERE code=?", (str(code).strip().upper(),)).fetchone()
        else: raise ApiError(400, "aircraft_type_required", "必须选择已登记机型（aircraft_type 或 aircraft_type_id）")
        if not row: raise ApiError(404, "aircraft_type_not_found", "所选机型未登记，请先维护机型资料")
        return row

    def create_plan(self, actor: str, role: str, operator: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "operator": raise ApiError(403, "plan_forbidden", "只有运营方可以创建飞行计划")
        required = ("callsign", "drone_model", "starts_at", "ends_at", "emergency_plan", "region")
        if any(body.get(key) in (None, "") for key in required): raise ApiError(400, "missing_fields", "飞行计划字段不完整")
        route = validate_route(body.get("route")); start, end = parse_time(body["starts_at"]), parse_time(body["ends_at"])
        if body.get("alternate_point") is None: raise ApiError(400, "missing_fields", "必须选择备用降落点 alternate_point")
        alt_lon, alt_lat = validate_point(body["alternate_point"])
        try: payload, altitude = float(body.get("payload_kg")), float(body.get("max_altitude"))
        except (TypeError, ValueError): raise ApiError(400, "invalid_numbers", "payload_kg 和 max_altitude 必须为数字")
        risk = body.get("population_risk")
        if not 0 <= payload <= 25 or altitude <= 0 or not isinstance(risk, int) or not 0 <= risk <= 5:
            raise ApiError(400, "invalid_plan", "载荷、高度或人口风险无效")
        if end <= start or start <= utcnow(): raise ApiError(400, "invalid_time", "飞行时间必须在未来且结束晚于开始")
        bbox = route_bbox(route)
        with self.repo.tx() as conn:
            aircraft = self._aircraft_type(conn, body)
            try:
                cur = conn.execute("""INSERT INTO flight_plans(operator_id,callsign,drone_model,aircraft_type_id,alternate_lon,alternate_lat,payload_kg,route_json,starts_at,ends_at,max_altitude,population_risk,emergency_plan,region,created_by,created_at,updated_at)
                                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                   (operator, str(body["callsign"]).upper(), body["drone_model"], aircraft["id"], alt_lon, alt_lat, payload, json.dumps(route), iso(start), iso(end), altitude, risk, body["emergency_plan"], body["region"], actor, iso(), iso()))
            except sqlite3.IntegrityError as exc: raise ApiError(409, "plan_duplicate", "同一运营方、呼号和起飞时间的计划已存在") from exc
            plan_id = cur.lastrowid; Repository.audit(conn, plan_id, actor, role, "plan_created", {"bbox": bbox, "revision": 1, "aircraft_type": aircraft["code"], "alternate_point": [alt_lon, alt_lat]})
            return self.get_plan(plan_id, role, operator)

    def _plan_row(self, conn: sqlite3.Connection, plan_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM flight_plans WHERE id=?", (plan_id,)).fetchone()
        if not row: raise ApiError(404, "plan_not_found", "飞行计划不存在")
        return row

    @staticmethod
    def _route(row: sqlite3.Row) -> list[list[float]]: return json.loads(row["route_json"])

    def check_conflicts(self, plan_id: int, role: str, operator: str) -> dict[str, Any]:
        with self.repo.tx() as conn:
            plan = self._plan_row(conn, plan_id)
            if role == "operator" and plan["operator_id"] != operator: raise ApiError(403, "plan_forbidden", "不能查看其他运营方计划")
            if role not in {"operator", "airspace_reviewer", "commander", "auditor", "viewer"}: raise ApiError(403, "check_forbidden", "无权检查冲突")
            return self._conflict_report(conn, plan)

    def _conflict_report(self, conn: sqlite3.Connection, plan: sqlite3.Row) -> dict[str, Any]:
        route = self._route(plan); bbox = route_bbox(route); start, end = parse_time(plan["starts_at"]), parse_time(plan["ends_at"])
        hard: list[dict[str, Any]] = []; blocking: list[dict[str, Any]] = []
        if plan["payload_kg"] > 25: hard.append({"code": "payload_limit", "message": "载荷超过 25kg 硬限制"})
        if plan["max_altitude"] > 120: hard.append({"code": "altitude_limit", "message": "常规计划高度不得超过 120m"})
        if plan["population_risk"] > 3: blocking.append({"code": "population_risk", "risk": plan["population_risk"], "message": "人口风险超过常规批准阈值"})
        for restriction in conn.execute("SELECT * FROM restrictions WHERE status='active'"):
            rbox = (restriction["min_lon"], restriction["min_lat"], restriction["max_lon"], restriction["max_lat"])
            if not boxes_overlap(bbox, rbox): continue
            if not times_overlap(start, end, parse_time(restriction["starts_at"]), parse_time(restriction["ends_at"])): continue
            altitude_overlap = plan["max_altitude"] > restriction["min_altitude"] and restriction["max_altitude"] > 0
            if altitude_overlap:
                item = {"code": "airspace_restriction", "restriction_id": restriction["id"], "name": restriction["name"], "kind": restriction["kind"], "reason": restriction["reason"]}
                blocking.append(item)
        adjacent: list[dict[str, Any]] = []
        for other in conn.execute("SELECT * FROM flight_plans WHERE id!=? AND status IN ('submitted','approved') AND starts_at<? AND ends_at>?", (plan["id"], iso(end), iso(start))):
            if boxes_overlap(bbox, route_bbox(self._route(other)), 0.002):
                adjacent.append({"plan_id": other["id"], "callsign": other["callsign"], "operator_id": other["operator_id"], "status": other["status"], "starts_at": other["starts_at"], "ends_at": other["ends_at"]})
        if adjacent: blocking.append({"code": "adjacent_traffic", "plans": adjacent, "message": "相邻航路与有效计划重叠"})
        return {"plan_id": plan["id"], "revision": plan["revision"], "hard_violations": hard, "blocking_conflicts": blocking, "approvable": not hard and not blocking}

    def endurance_check(self, plan_id: int, role: str, operator: str, body: dict[str, Any]) -> dict[str, Any]:
        with self.repo.tx() as conn:
            plan = self._plan_row(conn, plan_id)
            if role == "operator" and plan["operator_id"] != operator: raise ApiError(403, "plan_forbidden", "不能查看其他运营方计划")
            if role not in {"operator", "airspace_reviewer", "commander", "auditor", "viewer"}: raise ApiError(403, "check_forbidden", "无权检查续航")
            wind_raw = body.get("wind_factor", WIND_FACTOR_MIN)
            try: wind = float(wind_raw)
            except (TypeError, ValueError): raise ApiError(400, "wind_factor_range", f"风阻系数必须是 {WIND_FACTOR_MIN} 到 {WIND_FACTOR_MAX} 的数字") from None
            if not wind_factor_valid(wind): raise ApiError(400, "wind_factor_range", f"风阻系数必须在 {WIND_FACTOR_MIN} 到 {WIND_FACTOR_MAX} 之间")
            return self._endurance_report(conn, plan, wind)

    def _endurance_report(self, conn: sqlite3.Connection, plan: sqlite3.Row, wind: float) -> dict[str, Any]:
        if plan["aircraft_type_id"] is None or plan["alternate_lon"] is None:
            raise ApiError(409, "aircraft_params_missing", "计划缺少机型或备用降落点参数，无法进行续航放行检查")
        aircraft = conn.execute("SELECT * FROM aircraft_types WHERE id=?", (plan["aircraft_type_id"],)).fetchone()
        if not aircraft: raise ApiError(409, "aircraft_type_missing", "计划关联的机型资料不存在")
        report = assess_endurance(self._route(plan), aircraft["cruise_speed_kmh"], aircraft["endurance_minutes"], aircraft["return_minutes"], wind)
        lon, lat = plan["alternate_lon"], plan["alternate_lat"]
        start, end = parse_time(plan["starts_at"]), parse_time(plan["ends_at"])
        conflicts: list[dict[str, Any]] = []
        for restriction in conn.execute("SELECT * FROM restrictions WHERE status='active'"):
            if not times_overlap(start, end, parse_time(restriction["starts_at"]), parse_time(restriction["ends_at"])): continue
            if restriction["min_lon"] <= lon <= restriction["max_lon"] and restriction["min_lat"] <= lat <= restriction["max_lat"]:
                conflicts.append({"restriction_id": restriction["id"], "name": restriction["name"], "kind": restriction["kind"], "reason": restriction["reason"]})
        messages: list[str] = []
        if not report["energy_ok"]:
            messages.append(f"续航不足：按风阻系数 {wind} 折算全程需 {report['required_minutes']} 分钟（巡航折算 {report['adjusted_minutes']} + 安全返航 {report['return_minutes']}），满电时长 {report['endurance_minutes']} 分钟，还差 {report['shortfall_minutes']} 分钟")
        for item in conflicts:
            messages.append(f"备用降落点落入生效限制「{item['name']}」（{item['kind']}：{item['reason']}）")
        if not messages: messages.append("续航放行通过")
        report.update({
            "plan_id": plan["id"], "revision": plan["revision"], "callsign": plan["callsign"],
            "aircraft_type": aircraft["code"], "alternate_point": [lon, lat],
            "alternate_conflicts": conflicts, "released": report["energy_ok"] and not conflicts, "messages": messages,
        })
        return report

    def get_plan(self, plan_id: int, role: str, operator: str = "") -> dict[str, Any]:
        conn = self.repo.conn; row = self._plan_row(conn, plan_id)
        if role == "operator" and row["operator_id"] != operator: raise ApiError(403, "plan_forbidden", "不能查看其他运营方计划")
        result = dict(row); result["route"] = json.loads(result.pop("route_json")); result["route_bbox"] = route_bbox(result["route"])
        result["alternate_point"] = [result["alternate_lon"], result["alternate_lat"]] if result["alternate_lon"] is not None else None
        aircraft = conn.execute("SELECT * FROM aircraft_types WHERE id=?", (result["aircraft_type_id"],)).fetchone() if result["aircraft_type_id"] else None
        result["aircraft_type"] = aircraft["code"] if aircraft else None
        result["aircraft_type_info"] = dict(aircraft) if aircraft else None
        if role == "viewer":
            result = {key: result[key] for key in ("id", "callsign", "starts_at", "ends_at", "max_altitude", "region", "status", "valid_until" if "valid_until" in result else "updated_at")}
        if role in {"airspace_reviewer", "commander", "auditor"}: result["approvals"] = [dict(r) for r in conn.execute("SELECT * FROM approvals WHERE plan_id=? ORDER BY id", (plan_id,))]
        return result

    def submit(self, plan_id: int, actor: str, role: str, operator: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "operator": raise ApiError(403, "submit_forbidden", "只有运营方可以提交计划")
        with self.repo.tx() as conn:
            plan = self._plan_row(conn, plan_id)
            if plan["operator_id"] != operator: raise ApiError(403, "plan_forbidden", "不能提交其他运营方计划")
            if plan["status"] == "submitted": return {"plan": self.get_plan(plan_id, role, operator), "idempotent": True}
            if plan["status"] not in {"draft", "rejected"}: raise ApiError(409, "invalid_transition", "当前状态不能提交")
            if plan["aircraft_type_id"] is None or plan["alternate_lon"] is None:
                raise ApiError(409, "aircraft_params_missing", "旧计划缺少机型或备用降落点参数，不能再次提交，请先通过变更补齐")
            if parse_time(plan["starts_at"]) <= utcnow(): raise ApiError(409, "plan_expired", "计划起飞时间已过")
            conn.execute("UPDATE flight_plans SET status='submitted',updated_at=? WHERE id=?", (iso(), plan_id))
            Repository.audit(conn, plan_id, actor, role, "plan_submitted", {"revision": plan["revision"]})
            return {"plan": self.get_plan(plan_id, role, operator), "idempotent": False}

    def approve(self, plan_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"airspace_reviewer", "commander"}: raise ApiError(403, "review_forbidden", "只有空域审核员或指挥官可以批准")
        expected, offline_id = body.get("expected_revision"), str(body.get("offline_id", "")).strip()
        reason, override = str(body.get("reason", "")).strip(), str(body.get("override_reason", "")).strip()
        if not isinstance(expected, int) or not offline_id or not reason: raise ApiError(400, "review_details_required", "expected_revision、offline_id 和 reason 必填")
        with self.repo.tx() as conn:
            prior = conn.execute("SELECT * FROM approvals WHERE offline_id=?", (offline_id,)).fetchone()
            if prior:
                if prior["plan_id"] == plan_id and prior["plan_revision"] == expected and prior["decision"] == "approved":
                    return {"plan": self.get_plan(plan_id, role, ""), "idempotent": True, "approval_id": prior["id"]}
                raise ApiError(409, "offline_id_conflict", "该离线审核编号已经用于其他决定")
            plan = self._plan_row(conn, plan_id)
            if plan["status"] == "approved": return {"plan": self.get_plan(plan_id, role, ""), "idempotent": True}
            if plan["status"] != "submitted": raise ApiError(409, "invalid_transition", "只有已提交计划可以批准")
            if plan["revision"] != expected: raise ApiError(409, "revision_conflict", "计划版本已变化，审核决定不能套用")
            wind_raw = body.get("wind_factor")
            if wind_raw is None: raise ApiError(400, "wind_factor_required", "批准时必须填写 1.0 到 1.5 的风阻系数")
            try: wind = float(wind_raw)
            except (TypeError, ValueError): raise ApiError(400, "wind_factor_range", f"风阻系数必须是 {WIND_FACTOR_MIN} 到 {WIND_FACTOR_MAX} 的数字") from None
            if not wind_factor_valid(wind): raise ApiError(400, "wind_factor_range", f"风阻系数必须在 {WIND_FACTOR_MIN} 到 {WIND_FACTOR_MAX} 之间")
            report = self._conflict_report(conn, plan)
            if report["hard_violations"]: raise ApiError(409, "hard_constraint_violation", "计划违反不可覆盖的安全约束", report)
            endurance = self._endurance_report(conn, plan, wind)
            if not endurance["released"]: raise ApiError(409, "endurance_release_failed", "；".join(endurance["messages"]), endurance)
            if report["blocking_conflicts"] and not (role == "commander" and override):
                raise ApiError(409, "airspace_conflict", "计划存在空域或相邻交通冲突", report)
            override_kind = "emergency_authority" if report["blocking_conflicts"] else None
            cur = conn.execute("""INSERT INTO approvals(plan_id,plan_revision,reviewer,decision,reason,offline_id,override_kind,wind_factor,created_at)
                                  VALUES(?,?,?,?,?,?,?,?,?)""", (plan_id, expected, actor, "approved", reason, offline_id, override_kind, wind, iso()))
            conn.execute("UPDATE flight_plans SET status='approved',updated_at=? WHERE id=?", (iso(), plan_id))
            if override_kind: Repository.audit(conn, plan_id, actor, role, "emergency_override_used", {"override_reason": override, "conflicts": report["blocking_conflicts"]})
            Repository.audit(conn, plan_id, actor, role, "plan_approved", {"revision": expected, "offline_id": offline_id, "wind_factor": wind, "required_minutes": endurance["required_minutes"], "margin_minutes": endurance["margin_minutes"]})
            Repository.notify(conn, plan_id, "approved", f"飞行计划 {plan['callsign']} 已批准")
            return {"plan": self.get_plan(plan_id, role, ""), "idempotent": False, "approval_id": cur.lastrowid, "override_kind": override_kind, "endurance": endurance}

    def reject(self, plan_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"airspace_reviewer", "commander"}: raise ApiError(403, "review_forbidden", "当前角色不能拒绝计划")
        expected, offline_id, reason = body.get("expected_revision"), str(body.get("offline_id", "")).strip(), str(body.get("reason", "")).strip()
        if not isinstance(expected, int) or not offline_id or not reason: raise ApiError(400, "review_details_required", "expected_revision、offline_id 和 reason 必填")
        with self.repo.tx() as conn:
            prior = conn.execute("SELECT * FROM approvals WHERE offline_id=?", (offline_id,)).fetchone()
            if prior:
                if prior["plan_id"] == plan_id and prior["plan_revision"] == expected and prior["decision"] == "rejected": return {"plan": self.get_plan(plan_id, role, ""), "idempotent": True}
                raise ApiError(409, "offline_id_conflict", "该离线审核编号已经被使用")
            plan = self._plan_row(conn, plan_id)
            if plan["status"] != "submitted" or plan["revision"] != expected: raise ApiError(409, "revision_conflict", "计划状态或版本不匹配")
            conn.execute("INSERT INTO approvals(plan_id,plan_revision,reviewer,decision,reason,offline_id,created_at) VALUES(?,?,?,?,?,?,?)", (plan_id, expected, actor, "rejected", reason, offline_id, iso()))
            conn.execute("UPDATE flight_plans SET status='rejected',updated_at=? WHERE id=?", (iso(), plan_id))
            Repository.audit(conn, plan_id, actor, role, "plan_rejected", {"reason": reason, "offline_id": offline_id})
            Repository.notify(conn, plan_id, "rejected", f"飞行计划 {plan['callsign']} 被拒绝：{reason}")
            return {"plan": self.get_plan(plan_id, role, ""), "idempotent": False}

    def change(self, plan_id: int, actor: str, role: str, operator: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "operator": raise ApiError(403, "change_forbidden", "只有运营方可以变更计划")
        expected = body.get("expected_revision")
        if not isinstance(expected, int): raise ApiError(400, "revision_required", "expected_revision 必填")
        with self.repo.tx() as conn:
            plan = self._plan_row(conn, plan_id)
            if plan["operator_id"] != operator: raise ApiError(403, "plan_forbidden", "不能修改其他运营方计划")
            if plan["status"] in {"canceled", "expired"}: raise ApiError(409, "plan_closed", "已取消或过期计划不能修改")
            if plan["revision"] != expected: raise ApiError(409, "revision_conflict", "计划版本已变化")
            route = validate_route(body.get("route", self._route(plan)))
            start = parse_time(body.get("starts_at", plan["starts_at"])); end = parse_time(body.get("ends_at", plan["ends_at"]))
            if end <= start or start <= utcnow(): raise ApiError(400, "invalid_time", "新飞行时间无效")
            payload = float(body.get("payload_kg", plan["payload_kg"])); altitude = float(body.get("max_altitude", plan["max_altitude"]))
            risk = body.get("population_risk", plan["population_risk"])
            if not 0 <= payload <= 25 or altitude <= 0 or not isinstance(risk, int) or not 0 <= risk <= 5: raise ApiError(400, "invalid_plan", "变更后的载荷、高度或风险无效")
            if "aircraft_type" in body or "aircraft_type_id" in body:
                aircraft_type_id = self._aircraft_type(conn, body)["id"]
            else: aircraft_type_id = plan["aircraft_type_id"]
            if "alternate_point" in body: alt_lon, alt_lat = validate_point(body["alternate_point"])
            else: alt_lon, alt_lat = plan["alternate_lon"], plan["alternate_lat"]
            revision = expected + 1
            conn.execute("""UPDATE flight_plans SET route_json=?,starts_at=?,ends_at=?,payload_kg=?,max_altitude=?,population_risk=?,emergency_plan=?,region=?,aircraft_type_id=?,alternate_lon=?,alternate_lat=?,status='draft',revision=?,updated_at=? WHERE id=?""",
                         (json.dumps(route), iso(start), iso(end), payload, altitude, risk, body.get("emergency_plan", plan["emergency_plan"]), body.get("region", plan["region"]), aircraft_type_id, alt_lon, alt_lat, revision, iso(), plan_id))
            watched = [name for name, keys in (("route", ("route",)), ("aircraft_type", ("aircraft_type", "aircraft_type_id")), ("alternate_point", ("alternate_point",))) if any(key in body for key in keys)]
            Repository.audit(conn, plan_id, actor, role, "plan_changed", {"from_revision": expected, "to_revision": revision, "previous_status": plan["status"], "fields": watched})
            if plan["status"] == "approved": Repository.notify(conn, plan_id, "approval_invalidated", f"飞行计划 {plan['callsign']} 已修改，原批准自动失效")
            else: Repository.notify(conn, plan_id, "changed", f"飞行计划 {plan['callsign']} 已更新，需重新提交审核")
            return self.get_plan(plan_id, role, operator)

    def cancel(self, plan_id: int, actor: str, role: str, operator: str, body: dict[str, Any]) -> dict[str, Any]:
        reason = str(body.get("reason", "")).strip()
        if not reason: raise ApiError(400, "reason_required", "取消原因必填")
        with self.repo.tx() as conn:
            plan = self._plan_row(conn, plan_id)
            if role == "operator" and plan["operator_id"] != operator: raise ApiError(403, "plan_forbidden", "不能取消其他运营方计划")
            if role not in {"operator", "airspace_reviewer", "commander"}: raise ApiError(403, "cancel_forbidden", "当前角色不能取消计划")
            if plan["status"] == "canceled": return {"plan": self.get_plan(plan_id, role, operator), "idempotent": True}
            if plan["status"] == "expired": raise ApiError(409, "plan_expired", "已过期计划不能取消")
            conn.execute("UPDATE flight_plans SET status='canceled',updated_at=? WHERE id=?", (iso(), plan_id))
            Repository.audit(conn, plan_id, actor, role, "plan_canceled", {"reason": reason})
            Repository.notify(conn, plan_id, "canceled", f"飞行计划 {plan['callsign']} 已取消：{reason}")
            return {"plan": self.get_plan(plan_id, role, operator), "idempotent": False}

    def notifications(self, actor: str, role: str, operator: str) -> dict[str, Any]:
        if role == "operator":
            rows = self.repo.conn.execute("""SELECT n.* FROM notifications n JOIN flight_plans p ON p.id=n.plan_id WHERE p.operator_id=? ORDER BY n.id DESC""", (operator,))
        elif role in {"airspace_reviewer", "commander", "auditor"}: rows = self.repo.conn.execute("SELECT * FROM notifications ORDER BY id DESC")
        else: raise ApiError(403, "notifications_forbidden", "当前角色不能读取通知")
        return {"notifications": [dict(r) for r in rows]}

    def expire_plans(self, actor: str, role: str) -> dict[str, Any]:
        if role not in {"airspace_reviewer", "commander"}: raise ApiError(403, "expire_forbidden", "当前角色不能执行到期处理")
        now = iso()
        with self.repo.tx() as conn:
            rows = list(conn.execute("SELECT * FROM flight_plans WHERE status='approved' AND ends_at<=?", (now,)))
            for row in rows:
                conn.execute("UPDATE flight_plans SET status='expired',updated_at=? WHERE id=?", (now, row["id"]))
                Repository.audit(conn, row["id"], actor, role, "plan_expired", {})
                Repository.notify(conn, row["id"], "expired", f"飞行计划 {row['callsign']} 已过期")
        return {"expired": len(rows)}

    def state(self, role: str, operator: str) -> dict[str, Any]:
        conn = self.repo.conn
        if role == "operator": rows = conn.execute("SELECT * FROM flight_plans WHERE operator_id=? ORDER BY id DESC", (operator,))
        elif role in {"airspace_reviewer", "commander", "auditor"}: rows = conn.execute("SELECT * FROM flight_plans ORDER BY id DESC")
        else: rows = conn.execute("SELECT * FROM flight_plans WHERE status='approved' ORDER BY id DESC")
        plans = []
        for row in rows:
            item = self.get_plan(row["id"], role, operator); plans.append(item)
        restrictions = [dict(r) for r in conn.execute("SELECT * FROM restrictions WHERE status='active' ORDER BY id DESC")] if role in {"airspace_reviewer", "commander", "auditor"} else []
        aircraft_types = [dict(r) for r in conn.execute("SELECT * FROM aircraft_types ORDER BY code")]
        return {"plans": plans, "restrictions": restrictions, "aircraft_types": aircraft_types, "server_time": iso()}
