import json
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import renpho_garmin_sync as s  # noqa: E402

CFG = {"bone_unit": "percent", "muscle_unit": "percent", "muscle_source": "muscle"}


def ts(y, mo, d, h, mi=0):
    """Epoch (s) of a Madrid local time."""
    return int(datetime(y, mo, d, h, mi, tzinfo=s.LOCAL_TZ).timestamp())


def renpho(rid, epoch, weight=80.0):
    return {"id": rid, "timeStamp": epoch, "weight": weight, "bodyfat": 20.5, "water": 55.1,
            "muscle": 40.0, "bone": 4.0, "visfat": 9, "bmr": 1700, "bodyage": 40, "bmi": 24.7}


def garmin_payload(entries):
    """entries: [(day, epoch_s)] -> response shaped like get_weigh_ins."""
    by_day = {}
    for d, e in entries:
        by_day.setdefault(d, []).append({"calendarDate": d, "timestampGMT": e * 1000, "weight": 80000})
    return {"dailyWeightSummaries": [
        {"summaryDate": d, "numOfWeightEntries": len(v), "allWeightMetrics": v} for d, v in by_day.items()
    ]}


class NormalizeTests(unittest.TestCase):
    def test_units_and_local_time(self):
        m = s.normalize(renpho(1, ts(2026, 7, 1, 7, 30)), CFG)
        self.assertEqual(m.local_dt.strftime("%Y-%m-%d %H:%M"), "2026-07-01 07:30")
        self.assertEqual(m.bone_mass, 3.2)      # 4 % of 80 kg
        self.assertEqual(m.muscle_mass, 32.0)   # 40 % of 80 kg
        self.assertEqual(m.percent_fat, 20.5)

    def test_milliseconds_and_kg_units(self):
        raw = renpho(2, ts(2026, 1, 5, 23, 50) * 1000)
        m = s.normalize(raw, {"bone_unit": "kg", "muscle_unit": "kg", "muscle_source": "muscle"})
        self.assertEqual(m.local_day.isoformat(), "2026-01-05")
        self.assertEqual(m.bone_mass, 4.0)

    def test_invalid_records_dropped(self):
        self.assertIsNone(s.normalize({"id": 3, "weight": 0, "timeStamp": 1}, CFG))
        self.assertIsNone(s.normalize({"id": 4, "weight": 80}, CFG))

    def test_utc_midnight_maps_to_madrid_day(self):
        # 23:30 UTC on 30 Jun = 01:30 on 1 Jul in Madrid (summer time)
        epoch = int(datetime(2026, 6, 30, 23, 30, tzinfo=timezone.utc).timestamp())
        self.assertEqual(s.normalize(renpho(5, epoch), CFG).local_day.isoformat(), "2026-07-01")


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = s.State(Path(self.tmp.name) / "state.json")

    def tearDown(self):
        self.tmp.cleanup()

    def ms(self, *raws):
        return [s.normalize(r, CFG) for r in raws]

    def test_manual_entry_blocks_whole_day(self):
        meas = self.ms(renpho(1, ts(2026, 3, 1, 8)), renpho(2, ts(2026, 3, 1, 21)), renpho(3, ts(2026, 3, 2, 8)))
        garmin = s.parse_weigh_ins(garmin_payload([("2026-03-01", ts(2026, 3, 1, 12))]))
        plan = s.build_plan(meas, self.state, garmin)
        self.assertEqual([m.renpho_id for m in plan.upload], ["3"])
        self.assertEqual({m.renpho_id for m in plan.skip_day_in_garmin}, {"1", "2"})

    def test_two_renpho_same_free_day_both_upload(self):
        meas = self.ms(renpho(1, ts(2026, 3, 3, 8)), renpho(2, ts(2026, 3, 3, 21)))
        plan = s.build_plan(meas, self.state, {})
        self.assertEqual(len(plan.upload), 2)

    def test_own_upload_does_not_block_later_measurement_same_day(self):
        first = self.ms(renpho(1, ts(2026, 3, 4, 8)))[0]
        self.state.mark(first, "uploaded")
        later = self.ms(renpho(2, ts(2026, 3, 4, 21)))
        garmin = s.parse_weigh_ins(garmin_payload([("2026-03-04", first.epoch)]))
        plan = s.build_plan(later, self.state, garmin)
        self.assertEqual([m.renpho_id for m in plan.upload], ["2"])

    def test_exact_duplicate_detected_even_with_stale_state(self):
        m = self.ms(renpho(1, ts(2026, 3, 5, 8)))
        garmin = s.parse_weigh_ins(garmin_payload([("2026-03-05", ts(2026, 3, 5, 8) + 2)]))
        plan = s.build_plan(m, self.state, garmin)
        self.assertEqual(len(plan.skip_already_there), 1)
        self.assertEqual(plan.upload, [])

    def test_already_processed_ignored(self):
        m = self.ms(renpho(1, ts(2026, 3, 6, 8)))
        self.state.mark(m[0], "skipped_day_in_garmin")
        self.assertEqual(s.build_plan(m, self.state, {}).upload, [])

    def test_summary_without_entries_list_still_blocks(self):
        garmin = s.parse_weigh_ins({"dailyWeightSummaries": [{"summaryDate": "2026-03-07", "numOfWeightEntries": 1}]})
        plan = s.build_plan(self.ms(renpho(1, ts(2026, 3, 7, 8))), self.state, garmin)
        self.assertEqual(len(plan.skip_day_in_garmin), 1)


class FakeGarmin:
    instances = []

    def __init__(self, email=None, password=None, prompt_mfa=None):
        self.uploads = []
        self.ranges = []
        FakeGarmin.instances.append(self)

    def login(self, tokenstore=None):
        pass

    def get_weigh_ins(self, start, end):
        self.ranges.append((start, end))
        return garmin_payload([("2026-03-01", ts(2026, 3, 1, 12))])

    def add_body_composition(self, timestamp, **kw):
        self.uploads.append((timestamp, kw))


class EndToEndTests(unittest.TestCase):
    def run_main(self, argv, records):
        renpho_mod = types.ModuleType("renpho")

        class Client:
            def __init__(self, e, p):
                pass

            def login(self):
                pass

            def get_all_measurements(self):
                return records

        renpho_mod.RenphoClient = Client
        renpho_mod.RenphoAPIError = RuntimeError
        gc = types.ModuleType("garminconnect")
        gc.Garmin = FakeGarmin
        gc.GarminConnectAuthenticationError = RuntimeError
        gc.GarminConnectTooManyRequestsError = OSError
        FakeGarmin.instances.clear()
        with mock.patch.dict(sys.modules, {"renpho": renpho_mod, "garminconnect": gc}), \
                mock.patch.object(s, "_keyring", return_value=None), \
                mock.patch.object(s, "detect_timezone", return_value="Europe/Madrid"), \
                mock.patch("builtins.input", return_value="someone@example.com"), \
                mock.patch.object(s, "renpho_login", lambda c, a: c.login()), \
                mock.patch.object(s.getpass, "getpass", return_value="x"), \
                mock.patch.object(s, "GARMIN_TOKENSTORE", Path(self.tmp.name) / "tok"), \
                mock.patch.object(s.time, "sleep"):
            return s.main(argv + ["--state-dir", self.tmp.name])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_sync_then_idempotent(self):
        records = [renpho(1, ts(2026, 3, 1, 8)), renpho(2, ts(2026, 3, 2, 8)), renpho(3, ts(2026, 3, 3, 8))]
        self.assertEqual(self.run_main(["--dry-run"], records), 0)
        self.assertEqual(FakeGarmin.instances[-1].uploads, [])
        self.assertEqual(json.loads((Path(self.tmp.name) / "state.json").read_text())["processed"], {})

        self.assertEqual(self.run_main([], records), 0)
        api = FakeGarmin.instances[-1]
        self.assertEqual([u[0] for u in api.uploads], ["2026-03-02T08:00:00+01:00", "2026-03-03T08:00:00+01:00"])
        self.assertEqual(api.uploads[0][1]["bone_mass"], 3.2)
        state = json.loads((Path(self.tmp.name) / "state.json").read_text())
        self.assertEqual(state["processed"]["1"]["status"], "skipped_day_in_garmin")
        self.assertEqual(state["processed"]["2"]["status"], "uploaded")

        # Second run: nothing new, does not even log in to Garmin
        self.assertEqual(self.run_main([], records), 0)
        self.assertEqual(FakeGarmin.instances, [])

    def test_since_filter(self):
        records = [renpho(1, ts(2025, 1, 1, 8)), renpho(2, ts(2026, 3, 2, 8))]
        self.run_main(["--since", "2026-01-01"], records)
        self.assertEqual(len(FakeGarmin.instances[-1].uploads), 1)

    def test_weigh_in_range_chunked(self):
        records = [renpho(1, ts(2025, 1, 1, 8)), renpho(2, ts(2026, 3, 2, 8))]
        self.run_main(["--dry-run"], records)
        ranges = FakeGarmin.instances[-1].ranges
        self.assertGreater(len(ranges), 4)
        self.assertEqual(ranges[0][0], "2024-12-31")
        self.assertEqual(ranges[-1][1], "2026-03-03")


class FakeKeyring:
    def __init__(self):
        self.store = {}

    def get_password(self, svc, user):
        return self.store.get((svc, user))

    def set_password(self, svc, user, pw):
        self.store[(svc, user)] = pw

    def delete_password(self, svc, user):
        self.store.pop((svc, user))


class CredentialTests(unittest.TestCase):
    def renpho_module(self, good_pw):
        mod = types.ModuleType("renpho")

        class Err(Exception):
            pass

        class Client:
            def __init__(self, e, p):
                self.p = p

            def login(self):
                if self.p != good_pw:
                    raise Err("Login failed: code=-114, msg=")

            def get_all_measurements(self):
                return []

        mod.RenphoClient, mod.RenphoAPIError = Client, Err
        return mod

    def test_wrong_password_not_kept_and_reasked(self):
        kr = FakeKeyring()
        key = (f"{s.APP_NAME}:Renpho", "a@b.c")
        with mock.patch.dict(sys.modules, {"renpho": self.renpho_module("buena")}), \
                mock.patch.object(s, "renpho_login", lambda c, a: c.login()), \
                mock.patch.object(s, "_keyring", return_value=kr):
            with mock.patch.object(s.getpass, "getpass", return_value="mala"):
                with self.assertRaises(SystemExit):
                    s.fetch_renpho("a@b.c", False)
            self.assertNotIn(key, kr.store)
            with mock.patch.object(s.getpass, "getpass", return_value="buena") as gp:
                s.fetch_renpho("a@b.c", False)
                gp.assert_called_once()
            self.assertEqual(kr.store[key], "buena")
            with mock.patch.object(s.getpass, "getpass") as gp:
                s.fetch_renpho("a@b.c", False)
                gp.assert_not_called()

    def test_stale_stored_password_is_deleted(self):
        kr = FakeKeyring()
        key = (f"{s.APP_NAME}:Renpho", "a@b.c")
        kr.store[key] = "vieja"
        with mock.patch.dict(sys.modules, {"renpho": self.renpho_module("buena")}), \
                mock.patch.object(s, "renpho_login", lambda c, a: c.login()), \
                mock.patch.object(s, "_keyring", return_value=kr):
            with self.assertRaises(SystemExit):
                s.fetch_renpho("a@b.c", False)
        self.assertNotIn(key, kr.store)

    def test_area_code_fallback_is_remembered(self):
        mod = self.renpho_module("buena")
        tried = []

        def fake_login(client, area):
            tried.append(area)
            if area != "US":
                raise mod.RenphoAPIError("Login failed: code=-114, msg=")

        cfg = {"renpho_area_codes": ["ES", "US"]}
        with mock.patch.dict(sys.modules, {"renpho": mod}), \
                mock.patch.object(s, "renpho_login", fake_login), \
                mock.patch.object(s, "_keyring", return_value=FakeKeyring()), \
                mock.patch.object(s.getpass, "getpass", return_value="buena"):
            s.fetch_renpho("a@b.c", False, cfg)
        self.assertEqual(tried, ["ES", "US"])
        self.assertEqual(cfg["renpho_area_codes"], ["US", "ES"])


class RenphoLoginPayloadTests(unittest.TestCase):
    def test_payload_uses_given_area_code(self):
        import base64
        import json as _json
        consts = types.ModuleType("renpho.constants")
        consts.APP_VERSION, consts.BODY_WEIGHT_SCALES = "6.6.0", ["01"]
        consts.ENDPOINTS, consts.PLATFORM = {"login": "x/login"}, "android"
        crypto = types.ModuleType("renpho.crypto")
        crypto.encrypt_request = lambda o: {"plain": o}
        crypto.decrypt_response = lambda d: {"login": {"token": "t", "id": 7}}
        exc = types.ModuleType("renpho.exceptions")
        exc.RenphoAPIError = RuntimeError
        exc.check_response = lambda r, c: None
        sent = {}

        class C:
            email, password = "a@b.c", "pw"

            def _post(self, ep, body, auth=True):
                sent["ep"], sent["body"] = ep, body
                return {"code": 101, "data": "x"}

        c = C()
        with mock.patch.dict(sys.modules, {"renpho": types.ModuleType("renpho"), "renpho.constants": consts,
                                           "renpho.crypto": crypto, "renpho.exceptions": exc}):
            s.renpho_login(c, "ES")
        self.assertEqual(sent["body"]["plain"]["login"]["areaCode"], "ES")
        self.assertEqual((c.token, c.user_id), ("t", 7))


class SharingTests(unittest.TestCase):
    """The tool must work for someone else: no hardcoded email or time zone."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_new_user_is_asked_for_email_and_timezone_is_saved(self):
        state = s.State(Path(self.tmp.name) / "state.json")
        self.assertEqual(state.config["garmin_email"], "")
        cfg = {"garmin_email": ""}
        with mock.patch("builtins.input", side_effect=["", "  new@example.com "]):
            self.assertEqual(s.ask_email(cfg, "garmin_email", "Garmin Connect"), "new@example.com")
        self.assertEqual(cfg["garmin_email"], "new@example.com")

    def test_saved_email_is_not_asked_again(self):
        with mock.patch("builtins.input", side_effect=AssertionError("must not ask")):
            self.assertEqual(s.ask_email({"garmin_email": "a@b.c"}, "garmin_email", "Garmin"), "a@b.c")

    def test_existing_state_keeps_its_settings(self):
        path = Path(self.tmp.name) / "state.json"
        path.write_text(json.dumps({"config": {"garmin_email": "old@x.com", "renpho_email": "old@x.com"},
                                    "processed": {}}), encoding="utf-8")
        cfg = s.State(path).config
        self.assertEqual((cfg["garmin_email"], cfg["renpho_email"]), ("old@x.com", "old@x.com"))
        self.assertEqual(cfg["csv_muscle"], "skeletal")

    def test_timezone_from_settings_is_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "state.json").write_text(json.dumps({"config": {"timezone": "America/New_York"}}))
            with mock.patch.object(s, "find_csv_files", return_value=[]), \
                    mock.patch.object(s, "fetch_renpho", return_value=[]), \
                    mock.patch("builtins.input", return_value="x@y.z"):
                s.main(["--state-dir", tmp, "--dry-run"])
            self.assertEqual(str(s.LOCAL_TZ), "America/New_York")
            s.LOCAL_TZ = s.ZoneInfo(s.FALLBACK_TZ)

    def test_unknown_timezone_fails_clearly(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "state.json").write_text(json.dumps({"config": {"timezone": "Mars/Olympus"}}))
            with self.assertRaises(SystemExit) as ctx:
                s.main(["--state-dir", tmp, "--dry-run"])
            self.assertIn("Unknown time zone", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
