import sys, tempfile, unittest
from datetime import timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, DroneAirspaceService, iso, utcnow
from endurance import assess_endurance, route_distance_km

ROUTE = [[116.1, 39.8], [116.3, 39.9]]


class EnduranceRulesTest(unittest.TestCase):
    def test_route_distance_accumulates_segments(self):
        self.assertAlmostEqual(route_distance_km(ROUTE), 20.4, delta=0.2)
        self.assertAlmostEqual(route_distance_km([ROUTE[0], ROUTE[1], ROUTE[0]]), 40.8, delta=0.4)

    def test_assess_endurance_math(self):
        report = assess_endurance(ROUTE, 60, 120, 15, 1.5)
        self.assertTrue(report["energy_ok"])
        self.assertAlmostEqual(report["required_minutes"], report["adjusted_minutes"] + 15, places=1)
        short = assess_endurance(ROUTE, 40, 30, 10, 1.0)
        self.assertFalse(short["energy_ok"])
        self.assertAlmostEqual(short["shortfall_minutes"], short["required_minutes"] - 30, places=1)


class EnduranceReleaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.svc = DroneAirspaceService(Path(self.tmp.name) / "test.db"); self.start = utcnow() + timedelta(hours=2)
        self.svc.create_aircraft_type("reviewer", "airspace_reviewer", {"code": "M400", "name": "四旋翼", "cruise_speed_kmh": 60, "endurance_minutes": 120, "return_minutes": 15})
        self.svc.create_aircraft_type("reviewer", "airspace_reviewer", {"code": "S100", "name": "轻型机", "cruise_speed_kmh": 40, "endurance_minutes": 30, "return_minutes": 10})

    def tearDown(self): self.tmp.cleanup()

    def plan(self, callsign="E100", aircraft="M400", route=None, alternate=None):
        return self.svc.create_plan("op-user", "operator", "OP1", {"callsign": callsign, "drone_model": aircraft, "aircraft_type": aircraft, "alternate_point": alternate or [116.4, 40.1], "payload_kg": 5, "route": route or ROUTE, "starts_at": iso(self.start), "ends_at": iso(self.start + timedelta(hours=1)), "max_altitude": 100, "population_risk": 1, "emergency_plan": "返回起降点", "region": "BJ"})

    def test_aircraft_type_registration_rules(self):
        with self.assertRaises(ApiError) as ctx:
            self.svc.create_aircraft_type("op-user", "operator", {"code": "X1", "cruise_speed_kmh": 50, "endurance_minutes": 60, "return_minutes": 10})
        self.assertEqual(ctx.exception.code, "aircraft_type_forbidden")
        with self.assertRaises(ApiError) as ctx:
            self.svc.create_aircraft_type("reviewer", "airspace_reviewer", {"code": "M400", "cruise_speed_kmh": 50, "endurance_minutes": 60, "return_minutes": 10})
        self.assertEqual(ctx.exception.code, "aircraft_type_exists")
        with self.assertRaises(ApiError) as ctx:
            self.svc.create_aircraft_type("reviewer", "airspace_reviewer", {"code": "X2", "cruise_speed_kmh": 50, "endurance_minutes": 10, "return_minutes": 10})
        self.assertEqual(ctx.exception.code, "invalid_aircraft_type")
        self.assertEqual(len(self.svc.list_aircraft_types("viewer")["aircraft_types"]), 2)

    def test_plan_requires_aircraft_and_alternate(self):
        body = {"callsign": "E200", "drone_model": "M400", "payload_kg": 5, "route": ROUTE, "starts_at": iso(self.start), "ends_at": iso(self.start + timedelta(hours=1)), "max_altitude": 100, "population_risk": 1, "emergency_plan": "返回", "region": "BJ"}
        with self.assertRaises(ApiError) as ctx:
            self.svc.create_plan("op-user", "operator", "OP1", dict(body, alternate_point=[116.4, 40.1]))
        self.assertEqual(ctx.exception.code, "aircraft_type_required")
        with self.assertRaises(ApiError) as ctx:
            self.svc.create_plan("op-user", "operator", "OP1", dict(body, aircraft_type="M400"))
        self.assertEqual(ctx.exception.code, "missing_fields")
        with self.assertRaises(ApiError) as ctx:
            self.svc.create_plan("op-user", "operator", "OP1", dict(body, aircraft_type="UNKNOWN", alternate_point=[116.4, 40.1]))
        self.assertEqual(ctx.exception.code, "aircraft_type_not_found")

    def test_endurance_check_and_release_with_wind_factor(self):
        plan = self.plan(); self.svc.submit(plan["id"], "op-user", "operator", "OP1", {})
        report = self.svc.endurance_check(plan["id"], "airspace_reviewer", "", {"wind_factor": 1.2})
        self.assertTrue(report["released"]); self.assertEqual(report["wind_factor"], 1.2)
        self.assertAlmostEqual(report["route_distance_km"], 20.4, delta=0.2)
        approved = self.svc.approve(plan["id"], "reviewer", "airspace_reviewer", {"expected_revision": 1, "offline_id": "off-e1", "reason": "续航充足", "wind_factor": 1.2})
        self.assertEqual(approved["plan"]["status"], "approved")
        self.assertTrue(approved["endurance"]["released"])
        full = self.svc.get_plan(plan["id"], "airspace_reviewer")
        self.assertEqual(full["approvals"][0]["wind_factor"], 1.2)

    def test_endurance_shortfall_blocks_approval_and_states_minutes(self):
        plan = self.plan("E101", aircraft="S100"); self.svc.submit(plan["id"], "op-user", "operator", "OP1", {})
        report = self.svc.endurance_check(plan["id"], "airspace_reviewer", "", {"wind_factor": 1.0})
        self.assertFalse(report["released"]); self.assertGreater(report["shortfall_minutes"], 0)
        with self.assertRaises(ApiError) as ctx:
            self.svc.approve(plan["id"], "reviewer", "airspace_reviewer", {"expected_revision": 1, "offline_id": "off-e2", "reason": "放行", "wind_factor": 1.0})
        self.assertEqual(ctx.exception.code, "endurance_release_failed")
        self.assertIn("还差", ctx.exception.message)
        self.assertEqual(ctx.exception.details["shortfall_minutes"], report["shortfall_minutes"])
        with self.assertRaises(ApiError) as ctx:
            self.svc.approve(plan["id"], "commander", "commander", {"expected_revision": 1, "offline_id": "off-e3", "reason": "紧急任务", "override_reason": "应急救援授权", "wind_factor": 1.0})
        self.assertEqual(ctx.exception.code, "endurance_release_failed")

    def test_wind_factor_validation(self):
        plan = self.plan("E102"); self.svc.submit(plan["id"], "op-user", "operator", "OP1", {})
        with self.assertRaises(ApiError) as ctx:
            self.svc.endurance_check(plan["id"], "airspace_reviewer", "", {"wind_factor": 1.6})
        self.assertEqual(ctx.exception.code, "wind_factor_range")
        with self.assertRaises(ApiError) as ctx:
            self.svc.approve(plan["id"], "reviewer", "airspace_reviewer", {"expected_revision": 1, "offline_id": "off-e4", "reason": "缺系数"})
        self.assertEqual(ctx.exception.code, "wind_factor_required")
        with self.assertRaises(ApiError) as ctx:
            self.svc.approve(plan["id"], "reviewer", "airspace_reviewer", {"expected_revision": 1, "offline_id": "off-e5", "reason": "系数超界", "wind_factor": 0.9})
        self.assertEqual(ctx.exception.code, "wind_factor_range")

    def test_alternate_point_in_active_restriction_blocks_release(self):
        self.svc.create_restriction("reviewer", "airspace_reviewer", {"name": "备降场施工", "kind": "temporary_limit", "min_lon": 116.3, "min_lat": 40.0, "max_lon": 116.5, "max_lat": 40.2, "min_altitude": 0, "max_altitude": 50, "starts_at": iso(self.start - timedelta(minutes=10)), "ends_at": iso(self.start + timedelta(hours=2)), "reason": "施工"})
        plan = self.plan("E103"); self.svc.submit(plan["id"], "op-user", "operator", "OP1", {})
        report = self.svc.endurance_check(plan["id"], "commander", "", {"wind_factor": 1.0})
        self.assertTrue(report["energy_ok"]); self.assertFalse(report["released"])
        self.assertEqual(report["alternate_conflicts"][0]["name"], "备降场施工")
        with self.assertRaises(ApiError) as ctx:
            self.svc.approve(plan["id"], "reviewer", "airspace_reviewer", {"expected_revision": 1, "offline_id": "off-e6", "reason": "放行", "wind_factor": 1.0})
        self.assertEqual(ctx.exception.code, "endurance_release_failed")
        self.assertIn("备用降落点", ctx.exception.message)

    def test_legacy_plan_without_aircraft_params_cannot_resubmit(self):
        plan = self.plan("E104")
        self.svc.repo.conn.execute("UPDATE flight_plans SET aircraft_type_id=NULL, alternate_lon=NULL, alternate_lat=NULL WHERE id=?", (plan["id"],))
        with self.assertRaises(ApiError) as ctx:
            self.svc.submit(plan["id"], "op-user", "operator", "OP1", {})
        self.assertEqual(ctx.exception.code, "aircraft_params_missing")
        changed = self.svc.change(plan["id"], "op-user", "operator", "OP1", {"expected_revision": 1, "aircraft_type": "M400", "alternate_point": [116.4, 40.1]})
        self.assertEqual(changed["aircraft_type"], "M400"); self.assertEqual(changed["alternate_point"], [116.4, 40.1])
        submitted = self.svc.submit(plan["id"], "op-user", "operator", "OP1", {})
        self.assertEqual(submitted["plan"]["status"], "submitted")

    def test_aircraft_or_alternate_change_invalidates_approval(self):
        plan = self.plan("E105"); self.svc.submit(plan["id"], "op-user", "operator", "OP1", {})
        self.svc.approve(plan["id"], "reviewer", "airspace_reviewer", {"expected_revision": 1, "offline_id": "off-e7", "reason": "放行", "wind_factor": 1.0})
        changed = self.svc.change(plan["id"], "op-user", "operator", "OP1", {"expected_revision": 1, "alternate_point": [116.45, 40.15]})
        self.assertEqual(changed["status"], "draft")
        self.assertEqual(self.svc.notifications("op-user", "operator", "OP1")["notifications"][0]["kind"], "approval_invalidated")
        self.svc.submit(plan["id"], "op-user", "operator", "OP1", {})
        self.svc.approve(plan["id"], "reviewer", "airspace_reviewer", {"expected_revision": 2, "offline_id": "off-e8", "reason": "复核放行", "wind_factor": 1.0})
        changed = self.svc.change(plan["id"], "op-user", "operator", "OP1", {"expected_revision": 2, "aircraft_type": "S100"})
        self.assertEqual(changed["status"], "draft"); self.assertEqual(changed["aircraft_type"], "S100")
        self.assertEqual(self.svc.notifications("op-user", "operator", "OP1")["notifications"][0]["kind"], "approval_invalidated")


if __name__ == "__main__": unittest.main()
