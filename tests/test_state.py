import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import renpho_garmin_sync as s  # noqa: E402


class StateSaveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.json"
        self.state = s.State(self.path)
        self.state.processed["x"] = {"status": "uploaded", "epoch": 1}

    def tearDown(self):
        self.tmp.cleanup()

    def test_retries_while_onedrive_locks_the_file(self):
        real_replace = s.os.replace
        calls = {"n": 0}

        def flaky(src, dst):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise PermissionError(5, "Access is denied")
            return real_replace(src, dst)

        with mock.patch.object(s.os, "replace", flaky), mock.patch.object(s.time, "sleep"):
            self.assertTrue(self.state.save())
        self.assertEqual(calls["n"], 3)
        self.assertIn("x", json.loads(self.path.read_text())["processed"])

    def test_falls_back_to_direct_write(self):
        with mock.patch.object(s.os, "replace", side_effect=PermissionError(5, "denied")), \
                mock.patch.object(s.time, "sleep"):
            self.assertTrue(self.state.save())
        self.assertIn("x", json.loads(self.path.read_text())["processed"])

    def test_never_raises_when_everything_fails(self):
        with mock.patch.object(s.os, "replace", side_effect=PermissionError(5, "denied")), \
                mock.patch.object(Path, "write_text", side_effect=PermissionError(5, "denied")), \
                mock.patch.object(s.time, "sleep"):
            self.assertFalse(self.state.save())


class UploadLoopTests(unittest.TestCase):
    def test_many_uploads_with_locked_state_file(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import test_csv as tc

        header = tc.EN.splitlines()[0]
        rows = [f"2026/09/{d:02d},07:30:00,80.{d % 10},24.7,21.1,63.2,18.7,9,54.4,49.2,59.7,3.11,17.7,1709,41,"
                for d in range(1, 26)]
        real_replace = s.os.replace
        n = {"c": 0}

        def flaky(src, dst):
            n["c"] += 1
            if n["c"] % 2 == 0:
                raise PermissionError(5, "Access is denied")
            return real_replace(src, dst)

        with tempfile.TemporaryDirectory() as tmp:
            imp = Path(tmp) / "import"
            imp.mkdir()
            (imp / "e.csv").write_text(header + "\n" + "\n".join(rows) + "\n", encoding="utf-8")
            case = tc.CsvEndToEndTests()
            case.tmp = mock.Mock(name=tmp)
            case.tmp.name = tmp
            with mock.patch.object(s.os, "replace", flaky):
                self.assertEqual(case.run_main([]), 0)
            self.assertEqual(len(tc.FakeGarmin.instances[-1].uploads), 25)
            state = json.loads((Path(tmp) / "state.json").read_text())
            self.assertEqual(sum(v["status"] == "uploaded" for v in state["processed"].values()), 25)


if __name__ == "__main__":
    unittest.main()
