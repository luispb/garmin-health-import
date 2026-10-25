"""Import individual Google Fit blood-pressure readings and compare with Garmin.

Accepts Google Takeout's granular JSON ("All Data") and saved Fit REST datasets.
Daily summaries and averages are deliberately excluded: they are not readings.
"""

from __future__ import annotations

import json
import logging
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from zoneinfo import ZoneInfo

DATA_TYPE = "com.google.blood_pressure"
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class BloodPressure:
    ts_utc: datetime
    systolic: int
    diastolic: int

    @property
    def epoch(self) -> int:
        return int(self.ts_utc.timestamp())

    @property
    def reading_id(self) -> str:
        # Independent of file, source stream and export date, unlike Google IDs.
        return f"google-fit-bp-{self.epoch}-{self.systolic}-{self.diastolic}"


def _pressure(value, name: str, low: int, high: int) -> int:
    try:
        number = Decimal(str(value))
    except InvalidOperation as e:
        raise ValueError(f"Invalid {name}") from e
    if not number.is_finite() or not low <= number <= high:
        raise ValueError(f"{name} must be within Garmin's supported range [{low}, {high}] mmHg")
    return int(number.to_integral_value(rounding=ROUND_HALF_UP))


def _data_type(value) -> str | None:
    if isinstance(value, str):
        match = re.search(r"com\.google\.[a-z_.]+", value)
        return match.group() if match else None
    if isinstance(value, dict):
        for key in ("dataTypeName", "dataType", "name", "dataStreamId", "dataSourceId"):
            found = _data_type(value.get(key))
            if found:
                return found
    return None


def parse_google_fit(payload, filename: str = "") -> list[BloodPressure]:
    """Read raw points only; reject invalid BP points before any Garmin writes."""
    if not isinstance(payload, dict):
        if DATA_TYPE in filename:
            raise ValueError("Expected a Google Fit JSON object")
        return []
    source_type = (
        _data_type(payload.get("Data Source"))
        or _data_type(payload.get("dataSource"))
        or _data_type(payload)
        or (DATA_TYPE + ".summary" if DATA_TYPE + ".summary" in filename
            else DATA_TYPE if DATA_TYPE in filename else None)
    )
    if source_type and source_type != DATA_TYPE:
        return []
    points = payload.get("Data Points", payload.get("dataPoints", payload.get("point")))
    if points is None:
        if source_type == DATA_TYPE:
            raise ValueError("No individual data points; export Google Fit's All Data, not daily summaries")
        return []
    if not isinstance(points, list):
        raise ValueError("Google Fit data points must be a list")
    readings = []
    for index, point in enumerate(points, 1):
        if not isinstance(point, dict):
            raise ValueError(f"Invalid data point {index}")
        if (_data_type(point) or source_type) != DATA_TYPE:
            continue
        try:
            # Use integer arithmetic: Takeout can contain 19-digit JSON integers.
            nanos = int(point["startTimeNanos"])
            if nanos <= 0:
                raise ValueError("Timestamp must be positive")
            seconds, remainder = divmod(nanos, 1_000_000_000)
            ts = datetime.fromtimestamp(seconds, timezone.utc) + timedelta(microseconds=remainder // 1000)
            values = point.get("fitValue", point.get("value"))
            if not isinstance(values, list) or len(values) < 2:
                raise ValueError("Missing systolic or diastolic value")
            fields = [v.get("value", v) for v in values[:2]]
            readings.append(BloodPressure(
                ts_utc=ts,
                systolic=_pressure(fields[0]["fpVal"], "systolic", 70, 260),
                diastolic=_pressure(fields[1]["fpVal"], "diastolic", 40, 150),
            ))
            # Fit fields 3/4 describe body position/arm, NOT pulse.
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError, OSError) as e:
            raise ValueError(f"Invalid blood-pressure point {index}: {e}") from e
    return readings


def load_google_fit(path: Path) -> list[BloodPressure]:
    """Read a JSON file, a Takeout ZIP or a folder of extracted JSON files."""
    if not path.exists():
        raise ValueError(f"Google Fit export not found: {path}")
    readings: dict[str, BloodPressure] = {}

    def read(stream, name):
        try:
            parsed = parse_google_fit(json.load(stream), name)
        except (ValueError, UnicodeError) as e:
            raise ValueError(f"Cannot read {name}: {e}") from e
        for reading in parsed:
            readings[reading.reading_id] = reading

    files = sorted(f for f in path.rglob("*") if f.suffix.lower() in (".json", ".zip") and f.is_file()) \
        if path.is_dir() else [path]
    for file in files:
        if file.suffix.lower() == ".zip":
            # Read members in place; no extraction or filesystem writes.
            try:
                with zipfile.ZipFile(file) as archive:
                    for member in archive.infolist():
                        if not member.is_dir() and member.filename.lower().endswith(".json"):
                            with archive.open(member) as stream:
                                read(stream, member.filename)
            except zipfile.BadZipFile as e:
                raise ValueError(f"Invalid Takeout ZIP: {file.name}") from e
        else:
            with file.open(encoding="utf-8-sig") as stream:
                read(stream, str(file))
    if not readings:
        raise ValueError(
            "No individual Google Fit blood-pressure readings found. Use Takeout's Fit > All Data JSON; "
            "daily/aggregated CSV summaries cannot be imported as measurements. "
            "Data only stored in Health Connect may not be included in Google Fit's export."
        )
    log.info("Google Fit: %d unique blood-pressure readings.", len(readings))
    return sorted(readings.values(), key=lambda r: r.ts_utc)


def _garmin_timestamp(entry: dict, local_tz: ZoneInfo) -> datetime:
    for key, tz in (("measurementTimestampGMT", timezone.utc), ("measurementTimestampLocal", local_tz)):
        value = entry.get(key)
        if value in (None, ""):
            continue
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value / 1000 if value > 1e12 else value, timezone.utc)
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return (dt if dt.tzinfo else dt.replace(tzinfo=tz)).astimezone(timezone.utc)
    raise ValueError("Garmin reading has no timestamp")


def parse_garmin_blood_pressure(payload, local_tz: ZoneInfo) -> list[BloodPressure]:
    """Fail if Garmin's response cannot be checked reliably for duplicates."""
    if not isinstance(payload, dict) or not isinstance(payload.get("measurementSummaries"), list):
        raise ValueError("Unrecognised Garmin blood-pressure response; duplicate check stopped")
    readings = []
    for summary in payload["measurementSummaries"]:
        if not isinstance(summary, dict) or not isinstance(summary.get("measurements"), list):
            raise ValueError("Garmin summary lacks individual measurements; duplicate check stopped")
        for entry in summary["measurements"]:
            try:
                readings.append(BloodPressure(
                    _garmin_timestamp(entry, local_tz),
                    _pressure(entry["systolic"], "systolic", 70, 260),
                    _pressure(entry["diastolic"], "diastolic", 40, 150),
                ))
            except (KeyError, TypeError, ValueError, AttributeError, OverflowError, OSError) as e:
                raise ValueError(f"Cannot compare an existing Garmin blood-pressure reading: {e}") from e
    return readings


def fetch_garmin_blood_pressure(api, start: date, end: date, local_tz: ZoneInfo) -> list[BloodPressure]:
    readings = []
    current = start
    while current <= end:
        chunk_end = min(current + timedelta(days=27), end)
        payload = api.get_blood_pressure(current.isoformat(), chunk_end.isoformat())
        readings.extend(parse_garmin_blood_pressure(payload, local_tz))
        current = chunk_end + timedelta(days=1)
    return readings


@dataclass
class BloodPressurePlan:
    upload: list[BloodPressure] = field(default_factory=list)
    already_in_garmin: list[BloodPressure] = field(default_factory=list)


def build_blood_pressure_plan(
    readings: list[BloodPressure],
    processed: dict,
    existing: list[BloodPressure],
    tolerance_s: int = 60,
) -> BloodPressurePlan:
    plan = BloodPressurePlan()
    # Pulse/notes do not participate: manual entries may omit them.
    by_values: dict[tuple[int, int], list[int]] = {}
    for reading in existing:
        by_values.setdefault((reading.systolic, reading.diastolic), []).append(reading.epoch)
    seen = set(processed)
    for reading in sorted(readings, key=lambda r: r.ts_utc):
        if reading.reading_id in seen:
            continue
        seen.add(reading.reading_id)
        epochs = by_values.get((reading.systolic, reading.diastolic), [])
        if any(abs(epoch - reading.epoch) <= tolerance_s for epoch in epochs):
            plan.already_in_garmin.append(reading)
        else:
            plan.upload.append(reading)
    return plan
