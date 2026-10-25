import sys
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import renpho_csv as rc  # noqa: E402
import garmin_health_import as s  # noqa: E402

EN = """Date,Time,Weight(kg),BMI,Body Fat(%),Fat-free Body Weight(kg),Subcutaneous Fat(%),Visceral Fat,Body Water(%),Skeletal Muscle(%),Muscle Mass(kg),Bone Mass(kg),Protein(%),BMR(kcal),Metabolic Age,Remarks
2026/10/18,07:31:12,80.4,24.8,21.3,63.3,18.9,9,54.2,49.1,59.8,3.12,17.6,1712,41,
2026/10/19,07:28:40,80.1,24.7,21.1,63.2,18.7,9,54.4,49.2,59.7,3.11,17.7,1709,41,
"""

ES = """Fecha;Hora;Peso(kg);IMC;Grasa corporal(%);Peso libre de grasa(kg);Grasa subcutánea(%);Grasa visceral;Agua corporal(%);Músculo esquelético(%);Masa muscular(kg);Masa ósea(kg);Proteína(%);TMB(kcal);Edad metabólica
18/10/2026;07:31;80,4;24,8;21,3;63,3;18,9;9;54,2;49,1;59,8;3,12;17,6;1.712;41
13/10/2026;21:05;81,0;25,0;--;--;--;--;--;--;--;--;--;--;--
"""

US = """Time of Measurement,Weight(lb),BMI,Body Fat(%),Skeletal Muscle(%),Bone Mass(lb),BMR(kcal),Metabolic Age
"10/19/2026, 7:28:40 AM",176.6lb,24.7,21.1%,49.2%,6.9lb,1709,41
"""


def write(tmp, name, text, encoding="utf-8-sig"):
    p = Path(tmp) / name
    p.write_text(text, encoding=encoding)
    return p


class CsvParseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_english_export(self):
        rows = rc.read_renpho_csv(write(self.tmp.name, "en.csv", EN))
        self.assertEqual(len(rows), 2)
        r = rows[1]
        self.assertEqual(r["local_dt"], datetime(2026, 10, 19, 7, 28, 40))
        self.assertEqual(r["weight"], 80.1)
        self.assertEqual(r["percent_fat"], 21.1)
        self.assertEqual(r["percent_hydration"], 54.4)
        self.assertEqual(r["muscle_mass"], 39.41)      # default: weight x skeletal muscle %
        self.assertEqual(rc.read_renpho_csv(write(self.tmp.name, "en2.csv", EN), muscle="mass")[1]["muscle_mass"], 59.7)
        self.assertEqual(r["bone_mass"], 3.11)
        self.assertEqual(r["visceral_fat_rating"], 9)
        self.assertEqual(r["basal_met"], 1709)
        self.assertEqual(r["metabolic_age"], 41)
        self.assertEqual(r["bmi"], 24.7)

    def test_spanish_export_semicolon_decimal_comma(self):
        rows = rc.read_renpho_csv(write(self.tmp.name, "es.csv", ES, "cp1252"))
        self.assertEqual(rows[0]["local_dt"], datetime(2026, 10, 18, 7, 31))
        self.assertEqual(rows[0]["weight"], 80.4)
        self.assertEqual(rows[0]["bone_mass"], 3.12)
        self.assertEqual(rows[0]["basal_met"], 1712)   # "1.712" with a thousands separator
        self.assertEqual(rows[0]["muscle_mass"], 39.48)
        # weight-only measurement (no impedance)
        self.assertEqual(rows[1]["local_dt"], datetime(2026, 10, 13, 21, 5))
        self.assertIsNone(rows[1]["percent_fat"])
        self.assertIsNone(rows[1]["bone_mass"])

    def test_pounds_single_datetime_column_month_first(self):
        rows = rc.read_renpho_csv(write(self.tmp.name, "us.csv", US))
        r = rows[0]
        self.assertEqual(r["local_dt"], datetime(2026, 10, 19, 7, 28, 40))
        self.assertAlmostEqual(r["weight"], 80.1, places=1)
        self.assertAlmostEqual(r["bone_mass"], 3.13, places=2)
        self.assertAlmostEqual(r["muscle_mass"], round(80.1 * 0.492, 2), places=1)  # skeletal muscle %

    def test_day_first_inference(self):
        self.assertTrue(rc._infer_day_first(["03/04/2026", "25/04/2026"]))
        self.assertFalse(rc._infer_day_first(["03/04/2026", "04/25/2026"]))
        self.assertTrue(rc._infer_day_first(["03/04/2026"]))  # ambiguous -> day first

    def test_header_classification(self):
        self.assertEqual(rc.classify_header("Fat-free Body Weight(kg)")[0], None)
        self.assertEqual(rc.classify_header("Grasa visceral")[0], "visceral")
        self.assertEqual(rc.classify_header("Weight(lb)"), ("weight", "lb"))
        self.assertEqual(rc.classify_header("Músculo esquelético(%)"), ("skeletal_pct", "%"))

    def test_missing_header_raises(self):
        with self.assertRaises(ValueError):
            rc.read_renpho_csv(write(self.tmp.name, "bad.csv", "a,b\n1,2\n"))


class FakeGarmin:
    instances = []

    def __init__(self, *a, **k):
        self.uploads = []
        FakeGarmin.instances.append(self)

    def login(self, tokenstore=None):
        pass

    def get_weigh_ins(self, start, end):
        # manual weigh-in on 18/10 -> that day is skipped
        epoch = int(datetime(2026, 10, 18, 12, 0, tzinfo=s.LOCAL_TZ).timestamp())
        return {"dailyWeightSummaries": [{"summaryDate": "2026-10-18", "allWeightMetrics": [
            {"calendarDate": "2026-10-18", "timestampGMT": epoch * 1000}]}]}

    def add_body_composition(self, timestamp, **kw):
        self.uploads.append((timestamp, kw))


class CsvEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def run_main(self, argv):
        gc = types.ModuleType("garminconnect")
        gc.Garmin = FakeGarmin
        gc.GarminConnectAuthenticationError = RuntimeError
        gc.GarminConnectTooManyRequestsError = OSError
        FakeGarmin.instances.clear()
        with mock.patch.dict(sys.modules, {"garminconnect": gc}), \
                mock.patch.object(s, "_keyring", return_value=None), \
                mock.patch.object(s, "detect_timezone", return_value="Europe/Madrid"), \
                mock.patch("builtins.input", return_value="someone@example.com"), \
                mock.patch.object(s.getpass, "getpass", return_value="x"), \
                mock.patch.object(s, "GARMIN_TOKENSTORE", Path(self.tmp.name) / "tok"), \
                mock.patch.object(s, "fetch_renpho", side_effect=AssertionError("no debe usar la API")), \
                mock.patch.object(s.time, "sleep"):
            return s.main(argv + ["--state-dir", self.tmp.name])

    def test_csv_in_import_folder_is_used_and_deduplicated(self):
        imp = Path(self.tmp.name) / "import"
        imp.mkdir()
        write(imp, "export1.csv", EN)
        write(imp, "export2.csv", EN)  # same content: no duplicates
        self.assertEqual(self.run_main([]), 0)
        api = FakeGarmin.instances[-1]
        self.assertEqual([u[0] for u in api.uploads], ["2026-10-19T07:28:40+02:00"])
        self.assertEqual(api.uploads[0][1]["muscle_mass"], 39.41)
        # second run: nothing new
        self.assertEqual(self.run_main([]), 0)
        self.assertEqual(FakeGarmin.instances, [])

    def test_weight_only_skipped_and_repeats_merged(self):
        text = EN + "2026/10/19,07:30:10,80.1,24.7,21.1,63.2,18.7,9,54.4,49.2,59.7,3.11,17.7,1709,41,\n" \
                    "2026/10/19,18:11:00,83.5,25.8,--,--,--,--,--,--,--,--,--,--,--,\n"
        imp = Path(self.tmp.name) / "import"
        imp.mkdir()
        write(imp, "e.csv", text)
        self.assertEqual(self.run_main([]), 0)
        api = FakeGarmin.instances[-1]
        self.assertEqual([u[0] for u in api.uploads], ["2026-10-19T07:30:10+02:00"])  # 07:28 and 07:30 merged
        state = __import__("json").loads((Path(self.tmp.name) / "state.json").read_text())
        statuses = sorted(v["status"] for v in state["processed"].values())
        self.assertIn("skipped_weight_only", statuses)

    def test_explicit_csv_path(self):
        p = write(self.tmp.name, "otro.csv", ES)
        self.assertEqual(self.run_main(["--csv", str(p), "--dry-run"]), 0)

    def test_source_csv_without_files_fails_clearly(self):
        with self.assertRaises(SystemExit):
            self.run_main(["--source", "csv"])


if __name__ == "__main__":
    unittest.main()
