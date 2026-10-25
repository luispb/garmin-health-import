import json
import sys
import tempfile
import types
import unittest
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import google_fit_blood_pressure as bp
import garmin_health_import as sync

TZ = ZoneInfo("Europe/Madrid")
INSTANT = datetime(2026, 10, 24, 6, 30, tzinfo=timezone.utc)


def point(when=INSTANT, systolic=120, diastolic=80, takeout=True):
    values = [{"fpVal": systolic}, {"fpVal": diastolic}, {"intVal": 2}, {"intVal": 3}]
    return {
        "dataTypeName": bp.DATA_TYPE,
        "startTimeNanos": int(when.timestamp()) * 1_000_000_000,
        "endTimeNanos": int(when.timestamp()) * 1_000_000_000,
        "fitValue" if takeout else "value": [{"value": v} for v in values] if takeout else values,
    }


def export(*points):
    return {"Data Source": f"raw:{bp.DATA_TYPE}:com.example:", "Data Points": list(points)}


def garmin_payload(*readings):
    return {"measurementSummaries": [{"measurements": [
        {"measurementTimestampGMT": r.ts_utc.replace(tzinfo=None).isoformat(),
         "measurementTimestampLocal": r.ts_utc.astimezone(TZ).replace(tzinfo=None).isoformat(),
         "systolic": r.systolic, "diastolic": r.diastolic, "pulse": 68, "sourceType": "MANUAL"}
        for r in readings
    ]}]}


class ImportTests(unittest.TestCase):
    def test_takeout_unquoted_nanoseconds_and_position_fields(self):
        reading, = bp.parse_google_fit(export(point()))
        self.assertEqual(reading, bp.BloodPressure(INSTANT, 120, 80))
        self.assertFalse(hasattr(reading, "pulse"))

    def test_rest_dataset_and_string_nanoseconds(self):
        entry = point(takeout=False)
        entry["startTimeNanos"] = str(entry["startTimeNanos"] + 123_456_789)
        reading, = bp.parse_google_fit({"point": [entry]})
        self.assertEqual(reading.ts_utc.microsecond, 123456)

    def test_metadata_or_filename_identifies_untyped_points(self):
        entry = point()
        del entry["dataTypeName"]
        metadata = {"dataType": {"name": bp.DATA_TYPE}}
        for payload, filename in [
            ({"Data Source": metadata, "Data Points": [entry]}, ""),
            ({"dataSource": metadata, "dataPoints": [entry]}, ""),
            ({"Data Points": [entry]}, "raw_com.google.blood_pressure_com.example.json"),
        ]:
            with self.subTest(filename=filename):
                self.assertEqual(len(bp.parse_google_fit(payload, filename)), 1)

    def test_summaries_and_other_metrics_are_never_readings(self):
        for kind in (bp.DATA_TYPE + ".summary", "com.google.heart_rate.bpm"):
            entry = point()
            entry["dataTypeName"] = kind
            payload = {"Data Source": f"derived:{kind}:aggregate", "Data Points": [entry]}
            self.assertEqual(bp.parse_google_fit(payload), [])
        self.assertEqual(bp.parse_google_fit({"bucket": [{"dataset": []}]}), [])

    def test_float_values_are_rounded_half_up_for_garmin(self):
        reading, = bp.parse_google_fit(export(point(systolic=120.5, diastolic=80.4)))
        self.assertEqual((reading.systolic, reading.diastolic), (121, 80))

    def test_invalid_bp_point_rejects_the_import(self):
        bad_points = [point(systolic=v) for v in (0, 69, 261, float("nan"), float("inf"), "bad")]
        bad_points += [point(diastolic=v) for v in (39, 151)]
        missing_time = point()
        del missing_time["startTimeNanos"]
        missing_values = point()
        missing_values["fitValue"] = []
        bad_points += [missing_time, missing_values, dict(point(), startTimeNanos=-1)]
        for bad in bad_points:
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "Invalid blood-pressure point"):
                bp.parse_google_fit(export(point(), bad))

    def test_folder_and_zip_deduplicate_overlapping_exports(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "a.json").write_text(json.dumps(export(point())), encoding="utf-8")
            (folder / "b.json").write_text(json.dumps(export(point())), encoding="utf-8")
            (folder / "profile.json").write_text("[]", encoding="utf-8")
            self.assertEqual(len(bp.load_google_fit(folder)), 1)
            zipped = folder / "takeout.zip"
            with zipfile.ZipFile(zipped, "w") as archive:
                for file in folder.glob("*.json"):
                    archive.write(file, "Takeout/Fit/All Data/" + file.name)
            self.assertEqual(bp.load_google_fit(zipped), bp.load_google_fit(folder))

    def test_empty_or_summary_export_fails_with_actionable_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "daily.json"
            path.write_text('{"bucket": []}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "All Data"):
                bp.load_google_fit(path)
            with self.assertRaisesRegex(ValueError, "not found"):
                bp.load_google_fit(path / "missing")

    def test_folder_reads_split_zip_exports_and_uppercase_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            for index, when in enumerate((INSTANT, INSTANT + timedelta(days=1))):
                with zipfile.ZipFile(folder / f"takeout-{index}.zip", "w") as archive:
                    archive.writestr("Takeout/Fit/All Data/bp.JSON", json.dumps(export(point(when))))
            self.assertEqual(len(bp.load_google_fit(folder)), 2)

    def test_invalid_zip_has_an_actionable_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.zip"
            path.write_text("not a ZIP", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Invalid Takeout ZIP"):
                bp.load_google_fit(path)

    def test_content_identity_survives_another_export(self):
        original, = bp.parse_google_fit(export(point()))
        other = export(point())
        other["Data Source"] = f"derived:{bp.DATA_TYPE}:com.example:merged"
        copied, = bp.parse_google_fit(other)
        self.assertEqual(original.reading_id, copied.reading_id)


class ComparisonTests(unittest.TestCase):
    def test_garmin_utc_takes_priority_over_local_wall_time(self):
        reading = bp.BloodPressure(INSTANT, 120, 80)
        self.assertEqual(bp.parse_garmin_blood_pressure(garmin_payload(reading), TZ), [reading])

    def test_local_fallback_explicit_offset_and_numeric_utc(self):
        for timestamp in ("2026-10-24T08:30:00", "2026-10-24T08:30:00+02:00"):
            payload = garmin_payload(bp.BloodPressure(INSTANT, 120, 80))
            entry = payload["measurementSummaries"][0]["measurements"][0]
            del entry["measurementTimestampGMT"]
            entry["measurementTimestampLocal"] = timestamp
            self.assertEqual(bp.parse_garmin_blood_pressure(payload, TZ)[0].ts_utc, INSTANT)
        payload["measurementSummaries"][0]["measurements"][0]["measurementTimestampGMT"] = int(INSTANT.timestamp() * 1000)
        self.assertEqual(bp.parse_garmin_blood_pressure(payload, TZ)[0].ts_utc, INSTANT)

    def test_unknown_garmin_response_cannot_be_treated_as_empty(self):
        for payload in (None, {}, [], {"measurementSummaries": [{}]},
                        {"measurementSummaries": [{"measurements": [{"systolic": 120, "diastolic": 80}]}]}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                bp.parse_garmin_blood_pressure(payload, TZ)

    def test_manual_duplicate_uses_values_and_time_without_blocking_day(self):
        original = bp.BloodPressure(INSTANT, 120, 80)
        manual = bp.BloodPressure(INSTANT + timedelta(seconds=60), 120, 80)
        different_values = bp.BloodPressure(INSTANT, 122, 81)
        later = bp.BloodPressure(INSTANT + timedelta(seconds=121), 120, 80)
        plan = bp.build_blood_pressure_plan([original, different_values, later], {}, [manual])
        self.assertEqual(plan.already_in_garmin, [original])
        self.assertEqual(plan.upload, [different_values, later])

    def test_close_repeated_readings_are_not_merged_like_weigh_ins(self):
        first = bp.BloodPressure(INSTANT, 120, 80)
        second = bp.BloodPressure(INSTANT + timedelta(seconds=30), 120, 80)
        self.assertEqual(bp.build_blood_pressure_plan([first, second], {}, []).upload, [first, second])

    def test_processed_readings_and_exact_source_duplicates(self):
        reading = bp.BloodPressure(INSTANT, 120, 80)
        self.assertEqual(bp.build_blood_pressure_plan([reading, reading], {}, []).upload, [reading])
        self.assertEqual(bp.build_blood_pressure_plan([reading], {reading.reading_id: {}}, []).upload, [])

    def test_configurable_tolerance_and_different_days(self):
        reading = bp.BloodPressure(INSTANT, 120, 80)
        manual = bp.BloodPressure(INSTANT + timedelta(minutes=5), 120, 80)
        self.assertEqual(bp.build_blood_pressure_plan([reading], {}, [manual]).upload, [reading])
        self.assertEqual(bp.build_blood_pressure_plan([reading], {}, [manual], 300).already_in_garmin, [reading])
        next_day = bp.BloodPressure(INSTANT + timedelta(days=1), 120, 80)
        self.assertEqual(bp.build_blood_pressure_plan([reading], {}, [next_day]).upload, [reading])

    def test_garmin_ranges_are_chunked(self):
        api = mock.Mock()
        api.get_blood_pressure.return_value = {"measurementSummaries": []}
        bp.fetch_garmin_blood_pressure(api, date(2026, 1, 1), date(2026, 3, 1), TZ)
        self.assertEqual(api.get_blood_pressure.call_args_list, [
            mock.call("2026-01-01", "2026-01-28"), mock.call("2026-01-29", "2026-02-25"),
            mock.call("2026-02-26", "2026-03-01"),
        ])

    def test_utc_distinguishes_repeated_wall_time_at_dst_change(self):
        first = bp.BloodPressure(datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc), 120, 80)
        second = bp.BloodPressure(datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc), 120, 80)
        self.assertEqual(first.ts_utc.astimezone(TZ).hour, second.ts_utc.astimezone(TZ).hour)
        parsed = bp.parse_garmin_blood_pressure(garmin_payload(first), TZ)
        self.assertEqual(bp.build_blood_pressure_plan([second], {}, parsed).upload, [second])

    def test_v1_state_migrates_without_mixing_weight_and_bp(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text(json.dumps({"version": 1, "processed": {"weight": {"status": "uploaded", "epoch": 1}},
                                        "config": {"garmin_email": "saved@example.com"}}), encoding="utf-8")
            state = sync.State(path)
            self.assertEqual(state.blood_pressure_processed, {})
            reading = bp.BloodPressure(INSTANT, 120, 80)
            state.mark_blood_pressure(reading, "uploaded")
            state.save()
            reloaded = sync.State(path)
            self.assertEqual(reloaded.processed, state.processed)
            self.assertEqual(reloaded.config["garmin_email"], "saved@example.com")
            self.assertEqual(reloaded.uploaded_epochs(), {1})
            self.assertIn(reading.reading_id, reloaded.blood_pressure_processed)


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.file = self.folder / "fit.json"
        self.file.write_text(json.dumps(export(point(), point(INSTANT + timedelta(hours=12), 125, 82))), encoding="utf-8")
        self.api = mock.Mock()
        self.api.get_blood_pressure.return_value = {"measurementSummaries": []}
        self.api.set_blood_pressure.return_value = {"version": 1}
        gc = types.ModuleType("garminconnect")

        class RateLimit(Exception):
            pass

        gc.GarminConnectTooManyRequestsError = RateLimit
        self.rate_limit = RateLimit
        for patch in (
            mock.patch.dict(sys.modules, {"garminconnect": gc}),
            mock.patch.object(sync, "garmin_login", return_value=self.api),
            mock.patch.object(sync, "fetch_renpho", side_effect=AssertionError("Unexpected Renpho access")),
            mock.patch.object(sync, "detect_timezone", return_value="Europe/Madrid"),
            mock.patch.object(sync, "LOCAL_TZ", TZ),
            mock.patch("builtins.input", return_value="someone@example.com"),
            mock.patch.object(sync.time, "sleep"),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def run_main(self, *extra, only=True):
        args = ["--state-dir", str(self.folder), "--google-fit", str(self.file), *extra]
        if only:
            args.append("--blood-pressure-only")
        return sync.main(args)

    def state(self):
        return sync.State(self.folder / "state.json")

    def test_dry_run_then_sync_then_repeat(self):
        self.assertEqual(self.run_main("--dry-run"), 0)
        self.api.set_blood_pressure.assert_not_called()
        self.assertEqual(self.state().blood_pressure_processed, {})
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.api.set_blood_pressure.call_count, 2)
        first = self.api.set_blood_pressure.call_args_list[0].kwargs
        self.assertEqual(first["timestamp"], "2026-10-24T08:30:00+02:00")
        self.assertEqual((first["systolic"], first["diastolic"]), (120, 80))
        self.assertNotIn("pulse", first)
        self.api.reset_mock()
        sync.garmin_login.reset_mock()
        self.assertEqual(self.run_main(), 0)
        sync.garmin_login.assert_not_called()

    def test_lost_state_still_detects_manually_copied_readings(self):
        manual = bp.BloodPressure(INSTANT + timedelta(seconds=20), 120, 80)
        self.api.get_blood_pressure.return_value = garmin_payload(manual)
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.api.set_blood_pressure.call_count, 1)
        statuses = {v["status"] for v in self.state().blood_pressure_processed.values()}
        self.assertEqual(statuses, {"uploaded", "skipped_already_in_garmin"})

    def test_partial_failure_persists_progress_and_resumes(self):
        self.api.set_blood_pressure.side_effect = [{"version": 1}, RuntimeError("connection failed")]
        self.assertEqual(self.run_main(), 1)
        self.assertEqual(len(self.state().blood_pressure_processed), 1)
        self.api.set_blood_pressure.reset_mock(side_effect=True)
        self.assertEqual(self.run_main(), 0)
        self.api.set_blood_pressure.assert_called_once()
        self.assertEqual(len(self.state().blood_pressure_processed), 2)

    def test_rate_limit_and_failed_duplicate_check(self):
        self.api.set_blood_pressure.side_effect = self.rate_limit()
        self.assertEqual(self.run_main(), 2)
        self.assertEqual(self.state().blood_pressure_processed, {})
        self.api.set_blood_pressure.reset_mock()
        self.api.get_blood_pressure.side_effect = RuntimeError("server unavailable")
        self.assertEqual(self.run_main(), 1)
        self.api.set_blood_pressure.assert_not_called()

    def test_unrecognised_response_aborts_without_uploads_or_marks(self):
        self.api.get_blood_pressure.return_value = {"newResponseShape": []}
        self.assertEqual(self.run_main(), 1)
        self.api.set_blood_pressure.assert_not_called()
        self.assertEqual(self.state().blood_pressure_processed, {})

    def test_since_uses_local_date_at_utc_midnight(self):
        when = datetime(2026, 10, 23, 23, 30, tzinfo=timezone.utc)
        self.file.write_text(json.dumps(export(point(when))), encoding="utf-8")
        self.assertEqual(self.run_main("--since", "2026-10-24"), 0)
        self.api.set_blood_pressure.assert_called_once()

    def test_no_new_weight_does_not_bypass_pressure_sync(self):
        weight = sync.Measurement("weight-id", INSTANT, 80)
        state = self.state()
        state.mark(weight, "uploaded")
        state.save()
        with mock.patch.object(sync, "find_csv_files", return_value=[Path("unused.csv")]), \
                mock.patch.object(sync, "load_csv_measurements", return_value=[weight]):
            self.assertEqual(self.run_main(only=False), 0)
        self.assertEqual(self.api.set_blood_pressure.call_count, 2)

    def test_invalid_export_stops_before_any_garmin_login(self):
        self.file.write_text(json.dumps(export(point(systolic=0))), encoding="utf-8")
        with self.assertRaises(SystemExit):
            self.run_main()
        sync.garmin_login.assert_not_called()

    def test_invalid_duplicate_tolerance_stops_before_login(self):
        state = self.state()
        state.config["blood_pressure_tolerance_seconds"] = -1
        state.save()
        with self.assertRaisesRegex(SystemExit, "blood_pressure_tolerance_seconds"):
            self.run_main()
        sync.garmin_login.assert_not_called()

    def test_default_blood_pressure_folder(self):
        folder = self.folder / "google-fit"
        folder.mkdir()
        (folder / "bp.json").write_bytes(self.file.read_bytes())
        self.assertEqual(sync.main(["--state-dir", str(self.folder), "--blood-pressure-only"]), 0)
        self.assertEqual(self.api.set_blood_pressure.call_count, 2)


if __name__ == "__main__":
    unittest.main()
