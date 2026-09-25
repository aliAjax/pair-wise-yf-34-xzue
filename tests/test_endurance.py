import sys, tempfile, unittest
from datetime import timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, DroneAirspaceService, iso, utcnow
from endurance import route_distance_km


class EnduranceReleaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.svc = DroneAirspaceService(Path(self.tmp.name) / "test.db"); self.start = utcnow() + timedelta(hours=2)
        self.svc.create_model("reviewer", "airspace_reviewer", {"model_code": "M400", "cruise_speed_kmh": 60, "endurance_minutes": 60, "return_home_minutes": 10})

    def tearDown(self): self.tmp.cleanup()

    def plan(self, callsign="E100", route=None, **extra):
        body = {"callsign": callsign, "drone_model": "四旋翼", "payload_kg": 5, "route": route or [[116.0, 39.0], [116.05, 39.0]],
                "starts_at": iso(self.start), "ends_at": iso(self.start + timedelta(hours=1)), "max_altitude": 100,
                "population_risk": 1, "emergency_plan": "返回起降点", "region": "BJ", "model_code": "M400", "alternate_point": [115.9, 39.0]}
        body.update(extra)
        return self.svc.create_plan("op-user", "operator", "OP1", body)

    def submit(self, plan):
        return self.svc.submit(plan["id"], "op-user", "operator", "OP1", {})

    def approve(self, plan, offline_id, **extra):
        body = {"expected_revision": plan["revision"], "offline_id": offline_id, "reason": "续航与空域核算通过"}
        body.update(extra)
        return self.svc.approve(plan["id"], "reviewer", "airspace_reviewer", body)

    def test_model_registry_rules(self):
        with self.assertRaises(ApiError) as ctx:
            self.svc.create_model("op-user", "operator", {"model_code": "X1", "cruise_speed_kmh": 40, "endurance_minutes": 30, "return_home_minutes": 5})
        self.assertEqual(ctx.exception.code, "model_forbidden")
        with self.assertRaises(ApiError) as ctx:
            self.svc.create_model("reviewer", "airspace_reviewer", {"model_code": "M400", "cruise_speed_kmh": 60, "endurance_minutes": 60, "return_home_minutes": 10})
        self.assertEqual(ctx.exception.code, "model_duplicate")
        with self.assertRaises(ApiError) as ctx:
            self.svc.create_model("reviewer", "airspace_reviewer", {"model_code": "BAD", "cruise_speed_kmh": 0, "endurance_minutes": 30, "return_home_minutes": 5})
        self.assertEqual(ctx.exception.code, "invalid_model")
        self.assertEqual([m["model_code"] for m in self.svc.list_models()["models"]], ["M400"])

    def test_unknown_model_rejected_on_create_and_change(self):
        with self.assertRaises(ApiError) as ctx:
            self.plan("E140", model_code="NOPE")
        self.assertEqual(ctx.exception.code, "unknown_model")
        plan = self.plan("E141")
        with self.assertRaises(ApiError) as ctx:
            self.svc.change(plan["id"], "op-user", "operator", "OP1", {"expected_revision": 1, "model_code": "NOPE"})
        self.assertEqual(ctx.exception.code, "unknown_model")

    def test_legacy_plan_without_model_params_cannot_submit(self):
        plan = self.plan("E120", model_code=None, alternate_point=None)
        with self.assertRaises(ApiError) as ctx:
            self.submit(plan)
        self.assertEqual(ctx.exception.code, "model_params_missing")
        changed = self.svc.change(plan["id"], "op-user", "operator", "OP1", {"expected_revision": 1, "model_code": "M400", "alternate_point": [115.9, 39.0]})
        self.assertEqual(changed["model_code"], "M400")
        self.assertEqual(self.submit(changed)["plan"]["status"], "submitted")

    def test_endurance_shortfall_reports_missing_minutes(self):
        self.svc.create_model("reviewer", "airspace_reviewer", {"model_code": "TIGHT", "cruise_speed_kmh": 60, "endurance_minutes": 10, "return_home_minutes": 5})
        route = [[116.0, 39.0], [116.2, 39.0]]
        plan = self.plan("E110", route=route, model_code="TIGHT"); self.submit(plan)
        check = self.svc.check_conflicts(plan["id"], "airspace_reviewer", "")
        self.assertFalse(check["approvable"]); self.assertFalse(check["endurance"]["ok"])
        violation = next(v for v in check["hard_violations"] if v["code"] == "endurance_insufficient")
        expected_shortfall = round(route_distance_km(route) + 5 - 10, 2)
        self.assertAlmostEqual(violation["shortfall_minutes"], expected_shortfall, places=2)
        self.assertIn(f"还差 {expected_shortfall} 分钟", violation["message"])
        with self.assertRaises(ApiError) as ctx:
            self.approve(plan, "off-t1")
        self.assertEqual(ctx.exception.code, "hard_constraint_violation")
        self.assertIn("还差", ctx.exception.message)
        with self.assertRaises(ApiError) as ctx:  # 续航不足属于硬约束，紧急授权也不能覆盖
            self.svc.approve(plan["id"], "cmd", "commander", {"expected_revision": 1, "offline_id": "off-t2", "reason": "紧急", "override_reason": "救援"})
        self.assertEqual(ctx.exception.code, "hard_constraint_violation")

    def test_wind_factor_range_and_effect(self):
        self.svc.create_model("reviewer", "airspace_reviewer", {"model_code": "WINDY", "cruise_speed_kmh": 60, "endurance_minutes": 25, "return_home_minutes": 5})
        route = [[116.0, 39.0], [116.2, 39.0]]  # 约 17.3 公里：静风可放，1.5 倍风阻超时
        plan = self.plan("E111", route=route, model_code="WINDY"); self.submit(plan)
        self.assertTrue(self.svc.check_conflicts(plan["id"], "airspace_reviewer", "", 1.0)["endurance"]["ok"])
        self.assertFalse(self.svc.check_conflicts(plan["id"], "airspace_reviewer", "", 1.5)["endurance"]["ok"])
        with self.assertRaises(ApiError) as ctx:
            self.svc.check_conflicts(plan["id"], "airspace_reviewer", "", 1.6)
        self.assertEqual(ctx.exception.code, "invalid_wind_factor")
        with self.assertRaises(ApiError) as ctx:
            self.approve(plan, "off-w1", wind_factor=0.5)
        self.assertEqual(ctx.exception.code, "invalid_wind_factor")
        with self.assertRaises(ApiError) as ctx:
            self.approve(plan, "off-w2", wind_factor=1.5)
        self.assertEqual(ctx.exception.code, "hard_constraint_violation")
        approved = self.approve(plan, "off-w3", wind_factor=1.0)
        self.assertEqual(approved["plan"]["status"], "approved")

    def test_alternate_point_in_active_restriction(self):
        self.svc.create_restriction("reviewer", "airspace_reviewer", {"name": "地面活动", "kind": "temporary_limit", "min_lon": 115.8, "min_lat": 38.9, "max_lon": 116.0, "max_lat": 39.1,
                                                                    "min_altitude": 0, "max_altitude": 120, "starts_at": iso(self.start - timedelta(minutes=30)), "ends_at": iso(self.start + timedelta(hours=2)), "reason": "活动"})
        plan = self.plan("E121", route=[[116.5, 39.5], [116.6, 39.6]], alternate_point=[115.9, 39.0]); self.submit(plan)
        check = self.svc.check_conflicts(plan["id"], "airspace_reviewer", "")
        self.assertIn("alternate_in_restriction", [c["code"] for c in check["blocking_conflicts"]])
        with self.assertRaises(ApiError) as ctx:
            self.approve(plan, "off-a1")
        self.assertEqual(ctx.exception.code, "airspace_conflict")
        override = self.svc.approve(plan["id"], "cmd", "commander", {"expected_revision": 1, "offline_id": "off-a2", "reason": "紧急任务", "override_reason": "救援"})
        self.assertEqual(override["plan"]["status"], "approved")
        # 只覆盖空中的限制不影响地面备用降落点
        self.svc.create_restriction("reviewer", "airspace_reviewer", {"name": "空中限制", "kind": "temporary_limit", "min_lon": 114.0, "min_lat": 38.0, "max_lon": 114.5, "max_lat": 38.5,
                                                                    "min_altitude": 50, "max_altitude": 120, "starts_at": iso(self.start - timedelta(minutes=30)), "ends_at": iso(self.start + timedelta(hours=2)), "reason": "跳伞"})
        high = self.plan("E122", route=[[114.1, 38.1], [114.2, 38.2]], alternate_point=[114.2, 38.2], max_altitude=40)
        self.submit(high)
        self.assertTrue(self.svc.check_conflicts(high["id"], "airspace_reviewer", "")["approvable"])

    def test_route_model_or_alternate_change_invalidates_approval(self):
        self.svc.create_model("reviewer", "airspace_reviewer", {"model_code": "M401", "cruise_speed_kmh": 80, "endurance_minutes": 45, "return_home_minutes": 8})
        plan = self.plan("E130"); self.submit(plan)
        self.assertEqual(self.approve(plan, "off-c1")["plan"]["status"], "approved")
        changed = self.svc.change(plan["id"], "op-user", "operator", "OP1", {"expected_revision": 1, "alternate_point": [115.8, 39.1]})
        self.assertEqual(changed["status"], "draft")
        self.assertEqual(self.svc.notifications("op-user", "operator", "OP1")["notifications"][0]["kind"], "approval_invalidated")
        self.submit(changed)
        self.assertEqual(self.approve(changed, "off-c2")["plan"]["status"], "approved")
        changed = self.svc.change(plan["id"], "op-user", "operator", "OP1", {"expected_revision": 2, "model_code": "M401"})
        self.assertEqual(changed["status"], "draft")
        self.assertEqual(self.svc.notifications("op-user", "operator", "OP1")["notifications"][0]["kind"], "approval_invalidated")


if __name__ == "__main__": unittest.main()
