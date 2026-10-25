"""Import Renpho weight and Google Fit blood pressure into Garmin Connect.

Duplicate rule: if Garmin Connect already has a weigh-in on a given day (one
this tool did not upload), nothing from Renpho is uploaded for that day.

Usage:
    python garmin_health_import.py --dry-run
    python garmin_health_import.py
    python garmin_health_import.py --since 2024-01-01
    python garmin_health_import.py --blood-pressure-only --google-fit takeout-fit.zip --dry-run
"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from renpho_csv import detected_columns, find_csv_files, read_renpho_csv
from google_fit_blood_pressure import (
    BloodPressure,
    build_blood_pressure_plan,
    fetch_garmin_blood_pressure,
    load_google_fit,
)

# Stable storage/credential identifier: keep existing OneDrive history and keyring entries.
APP_NAME = "RenphoGarminSync"
FALLBACK_TZ = "Europe/Madrid"
LOCAL_TZ = ZoneInfo(FALLBACK_TZ)  # replaced at start-up by the configured time zone
GARMIN_TOKENSTORE = Path.home() / ".garminconnect"
UPLOAD_DELAY_SECONDS = 1.5
SAVE_EVERY = 10  # save the state file every N uploads
SAVE_RETRIES = 8
SAVE_RETRY_DELAY_S = 0.5
WEIGH_IN_CHUNK_DAYS = 90
SAME_INSTANT_TOLERANCE_S = 60

log = logging.getLogger("garmin_health_import")


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------
@dataclass
class Measurement:
    """A Renpho measurement already converted to Garmin's units."""

    renpho_id: str
    ts_utc: datetime
    weight: float
    bmi: float | None = None
    percent_fat: float | None = None
    percent_hydration: float | None = None
    bone_mass: float | None = None  # kg
    muscle_mass: float | None = None  # kg
    visceral_fat_rating: float | None = None
    basal_met: float | None = None
    metabolic_age: float | None = None
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def local_dt(self) -> datetime:
        return self.ts_utc.astimezone(LOCAL_TZ)

    @property
    def local_day(self) -> date:
        return self.local_dt.date()

    @property
    def epoch(self) -> int:
        return int(self.ts_utc.timestamp())


def _num(value) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def _parse_ts(raw: dict) -> datetime | None:
    for key in ("timeStamp", "timestamp", "time_stamp", "createdStamp"):
        v = raw.get(key)
        if v in (None, "", 0):
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        if v > 1e12:  # milliseconds
            v /= 1000
        return datetime.fromtimestamp(v, tz=timezone.utc)
    for key in ("localCreatedAt", "createdAt", "created_at"):
        v = raw.get(key)
        if isinstance(v, str) and v:
            try:
                dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
            except ValueError:
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=LOCAL_TZ)
            return dt.astimezone(timezone.utc)
    return None


def _to_kg(value: float | None, weight: float, unit: str) -> float | None:
    if value is None:
        return None
    if unit == "percent":
        return round(weight * value / 100, 2)
    return round(value, 2)


def normalize(raw: dict, mapping: dict) -> Measurement | None:
    """Convert a renpho-py record into a Measurement. None if it is not usable."""
    weight = _num(raw.get("weight"))
    ts = _parse_ts(raw)
    if weight is None or ts is None:
        return None
    rid = raw.get("id")
    renpho_id = str(rid) if rid not in (None, "") else f"ts-{int(ts.timestamp())}"

    muscle_kg = None
    if mapping.get("muscle_source") == "sinew_kg":
        muscle_kg = _num(raw.get("sinew"))
    if muscle_kg is None:
        muscle_kg = _to_kg(_num(raw.get("muscle")), weight, mapping.get("muscle_unit", "percent"))

    return Measurement(
        renpho_id=renpho_id,
        ts_utc=ts,
        weight=round(weight, 2),
        bmi=_num(raw.get("bmi")),
        percent_fat=_num(raw.get("bodyfat")),
        percent_hydration=_num(raw.get("water")),
        bone_mass=_to_kg(_num(raw.get("bone")), weight, mapping.get("bone_unit", "percent")),
        muscle_mass=muscle_kg,
        visceral_fat_rating=_num(raw.get("visfat")),
        basal_met=_num(raw.get("bmr")),
        metabolic_age=_num(raw.get("bodyage")),
        raw=raw,
    )


def load_csv_measurements(files: list[Path], muscle: str = "skeletal") -> list[Measurement]:
    """Read the CSV files exported by the app, removing duplicates across files."""
    by_epoch: dict[int, Measurement] = {}
    for f in files:
        try:
            records = read_renpho_csv(f, muscle=muscle)
            cols = detected_columns(f)
        except ValueError as e:
            raise SystemExit(f"Cannot read {f.name}: {e}") from e
        log.info("CSV %s: %d measurements.", f.name, len(records))
        log.info("  Columns used: %s", "; ".join(f"{k}={v}" for k, v in cols.items()))
        if muscle == "skeletal" and "skeletal_pct" not in cols:
            log.warning("  The CSV has no skeletal muscle %% column; using Renpho's muscle mass instead.")
        for r in records:
            ts = r.pop("local_dt").replace(tzinfo=LOCAL_TZ).astimezone(timezone.utc)
            m = Measurement(renpho_id=f"csv-{int(ts.timestamp())}", ts_utc=ts, raw=dict(r), **r)
            by_epoch[m.epoch] = m
    return list(by_epoch.values())


# ---------------------------------------------------------------------------
# Shared state (OneDrive)
# ---------------------------------------------------------------------------
def default_state_dir() -> Path:
    """OneDrive folder: personal OneDrive if present, otherwise work OneDrive."""
    for var in ("OneDriveConsumer", "OneDrive", "OneDriveCommercial"):
        base = os.environ.get(var)
        if base and Path(base).is_dir():
            return Path(base) / "Apps" / APP_NAME
    return Path.home() / "OneDrive" / "Apps" / APP_NAME


def detect_timezone() -> str:
    """IANA name of this computer's time zone (Windows names are mapped by tzlocal)."""
    try:
        from tzlocal import get_localzone_name

        name = get_localzone_name()
        ZoneInfo(name)
        return name
    except Exception:
        return FALLBACK_TZ


class State:
    """Shared settings and separate histories for weight and blood pressure."""

    VERSION = 2

    def __init__(self, path: Path):
        self.path = path
        self.processed: dict[str, dict] = {}  # renpho_id -> {status, epoch, day, at}
        self.blood_pressure_processed: dict[str, dict] = {}
        self.config: dict = {
            "garmin_email": "",  # asked on first run
            "renpho_email": "",  # asked on first run (only needed for the Renpho API)
            "timezone": "",  # IANA name, e.g. Europe/London; detected on first run
            "csv_muscle": "skeletal",  # skeletal | mass
            "skip_weight_only": True,  # do not upload weigh-ins without body composition
            "bone_unit": "percent",  # Renpho API only: percent | kg
            "muscle_unit": "percent",  # Renpho API only: percent | kg
            "muscle_source": "muscle",  # Renpho API only: muscle | sinew_kg
            "renpho_area_codes": ["ES", "US"],  # Renpho API only: tried in this order
            "blood_pressure_tolerance_seconds": 60,  # identical values within this time count as a duplicate
        }
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.processed = data.get("processed", {})
        self.blood_pressure_processed = data.get("blood_pressure_processed", {})
        self.config.update(data.get("config", {}))

    def save(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        payload = {
            "version": self.VERSION,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "config": self.config,
            "processed": self.processed,
            "blood_pressure_processed": self.blood_pressure_processed,
        }
        text = json.dumps(payload, indent=2, ensure_ascii=False)
        # OneDrive locks the file for a moment while it syncs it: retry.
        last_error: OSError | None = None
        for attempt in range(SAVE_RETRIES):
            try:
                tmp.write_text(text, encoding="utf-8")
                os.replace(tmp, self.path)
                return True
            except PermissionError as e:
                last_error = e
                time.sleep(SAVE_RETRY_DELAY_S * (attempt + 1))
        try:  # last resort: overwrite the file in place
            self.path.write_text(text, encoding="utf-8")
            return True
        except OSError as e:
            last_error = e
        log.warning("Could not save %s (%s); will try again later.", self.path.name, last_error)
        return False

    def mark(self, m: Measurement, status: str) -> None:
        self.processed[m.renpho_id] = {
            "status": status,
            "epoch": m.epoch,
            "day": m.local_day.isoformat(),
            "weight": m.weight,
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    def uploaded_epochs(self) -> set[int]:
        return {v["epoch"] for v in self.processed.values() if v.get("status") == "uploaded"}

    def mark_blood_pressure(self, reading: BloodPressure, status: str) -> None:
        self.blood_pressure_processed[reading.reading_id] = {
            "status": status,
            "epoch": reading.epoch,
            "systolic": reading.systolic,
            "diastolic": reading.diastolic,
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }


# ---------------------------------------------------------------------------
# Garmin: existing weigh-ins
# ---------------------------------------------------------------------------
def _entry_epoch(entry: dict) -> int | None:
    for key in ("timestampGMT", "date", "timestamp"):
        v = entry.get(key)
        if isinstance(v, (int, float)) and v > 0:
            return int(v / 1000) if v > 1e12 else int(v)
    return None


def _entry_day(entry: dict, fallback: str | None) -> str | None:
    for key in ("calendarDate", "summaryDate"):
        v = entry.get(key)
        if isinstance(v, str) and len(v) >= 10:
            return v[:10]
    return fallback


def parse_weigh_ins(payload) -> dict[str, list[int | None]]:
    """Return {ISO day: [epoch of each weigh-in]} from get_weigh_ins()."""
    days: dict[str, list[int | None]] = {}
    summaries = []
    if isinstance(payload, dict):
        summaries = payload.get("dailyWeightSummaries") or []
    elif isinstance(payload, list):
        summaries = payload
    for s in summaries:
        if not isinstance(s, dict):
            continue
        day = s.get("summaryDate")
        metrics = s.get("allWeightMetrics")
        if metrics is None and s.get("latestWeight"):
            metrics = [s["latestWeight"]]
        for e in metrics or []:
            if not isinstance(e, dict):
                continue
            d = _entry_day(e, day)
            if d:
                days.setdefault(d, []).append(_entry_epoch(e))
        if not metrics and day and (s.get("numOfWeightEntries") or 0) > 0:
            days.setdefault(day, []).append(None)
    return days


def fetch_garmin_weigh_ins(api, start: date, end: date) -> dict[str, list[int | None]]:
    result: dict[str, list[int | None]] = {}
    cur = start
    while cur <= end:
        chunk_end = min(cur + timedelta(days=WEIGH_IN_CHUNK_DAYS - 1), end)
        payload = api.get_weigh_ins(cur.isoformat(), chunk_end.isoformat())
        for d, epochs in parse_weigh_ins(payload).items():
            result.setdefault(d, []).extend(epochs)
        cur = chunk_end + timedelta(days=1)
    return result


def blocked_days(garmin_days: dict[str, list[int | None]], own_epochs: set[int]) -> set[str]:
    """Days that have a Garmin weigh-in this tool did NOT upload."""
    blocked = set()
    for d, epochs in garmin_days.items():
        for e in epochs:
            if e is None or not any(abs(e - o) <= SAME_INSTANT_TOLERANCE_S for o in own_epochs):
                blocked.add(d)
                break
    return blocked


def same_instant_exists(m: Measurement, garmin_days: dict[str, list[int | None]]) -> bool:
    for e in garmin_days.get(m.local_day.isoformat(), []):
        if e is not None and abs(e - m.epoch) <= SAME_INSTANT_TOLERANCE_S:
            return True
    return False


REPEAT_WINDOW_S = 600


def _completeness(m: Measurement) -> int:
    return sum(v is not None for v in (m.percent_fat, m.percent_hydration, m.bone_mass, m.muscle_mass,
                                       m.visceral_fat_rating, m.basal_met, m.metabolic_age, m.bmi))


def collapse_repeats(measurements: list[Measurement], window_s: int = REPEAT_WINDOW_S) -> tuple[list[Measurement], int]:
    """Merge weigh-ins less than window_s apart, keeping the most complete one (or the latest)."""
    kept: list[Measurement] = []
    for m in sorted(measurements, key=lambda x: x.ts_utc):
        if kept and m.epoch - kept[-1].epoch < window_s:
            if _completeness(m) >= _completeness(kept[-1]):
                kept[-1] = m
            continue
        kept.append(m)
    return kept, len(measurements) - len(kept)


def is_weight_only(m: Measurement) -> bool:
    return m.percent_fat is None and m.percent_hydration is None and m.muscle_mass is None


# ---------------------------------------------------------------------------
# Sync plan (pure logic, testable)
# ---------------------------------------------------------------------------
@dataclass
class Plan:
    upload: list[Measurement] = field(default_factory=list)
    skip_day_in_garmin: list[Measurement] = field(default_factory=list)
    skip_already_there: list[Measurement] = field(default_factory=list)
    skip_weight_only: list[Measurement] = field(default_factory=list)


def build_plan(
    measurements: list[Measurement],
    state: State,
    garmin_days: dict[str, list[int | None]],
    skip_weight_only: bool = False,
) -> Plan:
    plan = Plan()
    blocked = blocked_days(garmin_days, state.uploaded_epochs())
    for m in sorted(measurements, key=lambda x: x.ts_utc):
        if m.renpho_id in state.processed:
            continue
        if skip_weight_only and is_weight_only(m):
            plan.skip_weight_only.append(m)
        elif same_instant_exists(m, garmin_days):
            plan.skip_already_there.append(m)
        elif m.local_day.isoformat() in blocked:
            plan.skip_day_in_garmin.append(m)
        else:
            plan.upload.append(m)
    return plan


# ---------------------------------------------------------------------------
# Credentials and clients
# ---------------------------------------------------------------------------
def _keyring():
    try:
        import keyring

        return keyring
    except ImportError:
        return None


def get_password(service: str, email: str, reset: bool) -> tuple[str, bool]:
    """Return (password, came_from_store). Stores nothing: that happens after a successful login."""
    kr = _keyring()
    if kr and not reset:
        pw = kr.get_password(f"{APP_NAME}:{service}", email)
        if pw:
            return pw, True
    pw = getpass.getpass(f"{service} password for {email}: ")
    if not pw:
        raise SystemExit(f"No {service} password entered.")
    return pw, False


def store_password(service: str, email: str, pw: str) -> None:
    kr = _keyring()
    if kr:
        kr.set_password(f"{APP_NAME}:{service}", email, pw)
        log.info("%s password saved in Windows Credential Manager.", service)


def forget_password(service: str, email: str) -> None:
    kr = _keyring()
    if not kr:
        return
    try:
        kr.delete_password(f"{APP_NAME}:{service}", email)
        log.info("Deleted the saved %s password; you will be asked for it next time.", service)
    except Exception:
        pass


def ask_email(cfg: dict, key: str, service: str) -> str:
    """Return the account email from the settings, asking for it the first time."""
    email = (cfg.get(key) or "").strip()
    while not email:
        email = input(f"{service} account email: ").strip()
    cfg[key] = email
    return email


def renpho_login(client, area_code: str) -> None:
    """Same login as renpho-py 1.2.0, but with a configurable areaCode (the library hardcodes "US")."""
    from renpho.constants import APP_VERSION, BODY_WEIGHT_SCALES, ENDPOINTS, PLATFORM
    from renpho.crypto import decrypt_response, encrypt_request
    from renpho.exceptions import RenphoAPIError, check_response

    payload = {
        "questionnaire": {},
        "login": {
            "password": client.password,
            "areaCode": area_code,
            "appRevision": APP_VERSION,
            "cellphoneType": "PythonScript",
            "systemType": "11",
            "email": client.email,
            "platform": PLATFORM,
        },
        "bindingList": {"deviceTypes": BODY_WEIGHT_SCALES},
    }
    result = client._post(ENDPOINTS["login"], encrypt_request(payload), auth=False)
    check_response(result, "Login")
    info = decrypt_response(result["data"]).get("login", {})
    client.token, client.user_id, client.user_info = info.get("token"), info.get("id"), info
    if not client.token:
        raise RenphoAPIError("Login", None, "No token in login response")


def fetch_renpho(email: str, reset: bool, cfg: dict | None = None) -> list[dict]:
    from renpho import RenphoAPIError, RenphoClient

    cfg = cfg if cfg is not None else {}
    areas = list(dict.fromkeys(cfg.get("renpho_area_codes") or ["ES", "US"]))
    password, from_store = get_password("Renpho", email, reset)
    client = RenphoClient(email, password)
    errors = []
    for area in areas:
        try:
            renpho_login(client, area)
        except RenphoAPIError as e:
            log.info("Renpho login with region %s: %s", area, e)
            errors.append(f"{area}: {e}")
            continue
        log.info("Renpho login OK (region %s).", area)
        if areas[0] != area:
            cfg["renpho_area_codes"] = [area] + [a for a in areas if a != area]
        break
    else:
        forget_password("Renpho", email)
        raise SystemExit(
            "Renpho error: " + "; ".join(errors) + "\n"
            "Check the password (if you sign in to Renpho with Google, create one with 'Forgot password')."
        )
    if not from_store:
        store_password("Renpho", email, password)
    try:
        return list(client.get_all_measurements())
    except RenphoAPIError as e:
        raise SystemExit(f"Renpho error: {e}") from e


def garmin_login(email: str, reset: bool):
    from garminconnect import Garmin, GarminConnectAuthenticationError

    tokenstore = str(GARMIN_TOKENSTORE)
    if not reset and (GARMIN_TOKENSTORE / "garmin_tokens.json").exists():
        try:
            api = Garmin()
            api.login(tokenstore)
            return api
        except Exception as e:  # expired tokens: full login
            log.info("Saved Garmin tokens are no longer valid (%s); asking for the password.", e)
    password, from_store = get_password("Garmin", email, reset)
    api = Garmin(email, password, prompt_mfa=lambda: input("Garmin MFA code: ").strip())
    try:
        api.login(tokenstore)
    except GarminConnectAuthenticationError as e:
        forget_password("Garmin", email)
        raise SystemExit(f"Garmin authentication error: {e}") from e
    if not from_store:
        store_password("Garmin", email, password)
    return api


def upload(api, m: Measurement) -> None:
    api.add_body_composition(
        m.local_dt.isoformat(timespec="seconds"),
        weight=m.weight,
        percent_fat=m.percent_fat,
        percent_hydration=m.percent_hydration,
        bone_mass=m.bone_mass,
        muscle_mass=m.muscle_mass,
        basal_met=m.basal_met,
        metabolic_age=m.metabolic_age,
        visceral_fat_rating=m.visceral_fat_rating,
        bmi=m.bmi,
    )


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def fmt(m: Measurement) -> str:
    def f(v, unit=""):
        return "-" if v is None else f"{v:g}{unit}"

    return (
        f"{m.local_dt:%Y-%m-%d %H:%M}  weight {f(m.weight, ' kg')}  fat {f(m.percent_fat, '%')}  "
        f"water {f(m.percent_hydration, '%')}  muscle {f(m.muscle_mass, ' kg')}  "
        f"bone {f(m.bone_mass, ' kg')}  visceral {f(m.visceral_fat_rating)}  "
        f"BMR {f(m.basal_met)}  age {f(m.metabolic_age)}  BMI {f(m.bmi)}"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def parse_args(argv=None):
    p = argparse.ArgumentParser(description="garmin-health-import: Renpho weight and Google Fit blood pressure -> Garmin Connect")
    p.add_argument("--dry-run", action="store_true", help="show what would be uploaded without writing anything")
    p.add_argument("--since", type=date.fromisoformat, help="only measurements from YYYY-MM-DD onwards")
    p.add_argument("--state-dir", type=Path, help="folder for the state file (default: OneDrive)")
    p.add_argument("--reset-credentials", action="store_true", help="ask for the passwords again")
    p.add_argument("--source", choices=["auto", "csv", "api"], default="auto",
                   help="Renpho source: csv exported from the app, api, or auto (csv if there are files)")
    p.add_argument("--csv", type=Path, help="CSV file or folder (default: <state folder>\\import)")
    p.add_argument("--google-fit", type=Path,
                   help="Google Fit All Data JSON, Takeout ZIP or extracted folder; also sync blood pressure")
    p.add_argument("--blood-pressure-only", action="store_true",
                   help="skip Renpho; import --google-fit or <state folder>\\google-fit")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(message)s")

    global LOCAL_TZ
    state_path = (args.state_dir or default_state_dir()) / "state.json"
    state = State(state_path)
    cfg = state.config
    if not cfg.get("timezone"):
        cfg["timezone"] = detect_timezone()
    try:
        LOCAL_TZ = ZoneInfo(cfg["timezone"])
    except Exception as e:
        raise SystemExit(f"Unknown time zone '{cfg['timezone']}' in {state_path}: {e}") from e
    state.save()  # creates the file with the default (editable) settings
    log.info("State file: %s (%d measurements already processed)", state_path, len(state.processed))
    log.info("Time zone: %s", cfg["timezone"])

    # Validate every requested export before starting any uploads.
    blood_pressure = None
    if args.google_fit is not None or args.blood_pressure_only:
        try:
            tolerance = cfg.get("blood_pressure_tolerance_seconds", 60)
            if type(tolerance) is not int or not 0 <= tolerance <= 3600:
                raise ValueError("blood_pressure_tolerance_seconds must be an integer from 0 to 3600")
            blood_pressure = load_google_fit(args.google_fit or state_path.parent / "google-fit")
        except (ValueError, OSError) as e:
            raise SystemExit(str(e)) from e
        if args.since:
            blood_pressure = [r for r in blood_pressure if r.ts_utc.astimezone(LOCAL_TZ).date() >= args.since]

    if not args.blood_pressure_only:
        result = sync_weight(args, state)
        if result:
            return result
    if blood_pressure is not None:
        return sync_blood_pressure(args, state, blood_pressure)
    return 0


def sync_weight(args, state: State) -> int:
    cfg = state.config
    state_path = state.path
    # 1. Renpho
    import_dir = state_path.parent / "import"
    import_dir.mkdir(parents=True, exist_ok=True)
    csv_files = find_csv_files(args.csv or import_dir)
    source = args.source if args.source != "auto" else ("csv" if csv_files else "api")
    if source == "csv":
        if not csv_files:
            raise SystemExit(f"No CSV files in {args.csv or import_dir}. Export your data from the app first.")
        measurements = load_csv_measurements(csv_files, cfg.get("csv_muscle", "skeletal"))
    else:
        log.info("No CSV files in %s; trying the Renpho API.", import_dir)
        renpho_email = ask_email(cfg, "renpho_email", "Renpho")
        raw = fetch_renpho(renpho_email, args.reset_credentials, cfg)
        state.save()  # remembers the email and the region that worked
        measurements = [m for m in (normalize(r, cfg) for r in raw) if m]
        log.info("Renpho (API): %d records.", len(raw))
    measurements, merged = collapse_repeats(measurements)
    if merged:
        log.info("Merged %d repeated weigh-ins (less than %d min apart).", merged, REPEAT_WINDOW_S // 60)
    if args.since:
        measurements = [m for m in measurements if m.local_day >= args.since]
    log.info("Renpho: %d valid measurements.", len(measurements))
    pending = [m for m in measurements if m.renpho_id not in state.processed]
    if not pending:
        log.info("No new Renpho weigh-ins to sync.")
        return 0

    # 2. Garmin: existing weigh-ins in the affected date range
    garmin_email = ask_email(cfg, "garmin_email", "Garmin Connect")
    state.save()
    api = garmin_login(garmin_email, args.reset_credentials)
    start = min(m.local_day for m in pending) - timedelta(days=1)
    end = max(m.local_day for m in pending) + timedelta(days=1)
    log.info("Reading Garmin weigh-ins from %s to %s...", start, end)
    garmin_days = fetch_garmin_weigh_ins(api, start, end)

    # 3. Plan
    plan = build_plan(measurements, state, garmin_days, bool(cfg.get("skip_weight_only", True)))
    log.info(
        "Plan: %d to upload, %d skipped (day already has a Garmin weigh-in), %d skipped (already in Garmin), "
        "%d skipped (weight only).",
        len(plan.upload), len(plan.skip_day_in_garmin), len(plan.skip_already_there), len(plan.skip_weight_only),
    )
    for m in plan.skip_weight_only:
        log.debug("  SKIP (weight only) %s", fmt(m))
    for m in plan.skip_day_in_garmin:
        log.debug("  SKIP (day taken)   %s", fmt(m))
    for m in plan.upload:
        log.info("  %s %s", "WOULD UPLOAD" if args.dry_run else "UPLOAD", fmt(m))

    if args.dry_run:
        log.info("--dry-run: nothing was written to Garmin and no measurements were marked as processed.")
        return 0

    # 4. Run (saving progress along the way)
    for m in plan.skip_day_in_garmin:
        state.mark(m, "skipped_day_in_garmin")
    for m in plan.skip_already_there:
        state.mark(m, "skipped_already_in_garmin")
    for m in plan.skip_weight_only:
        state.mark(m, "skipped_weight_only")
    state.save()

    from garminconnect import GarminConnectTooManyRequestsError

    ok = 0
    total = len(plan.upload)
    try:
        for i, m in enumerate(plan.upload):
            try:
                upload(api, m)
            except GarminConnectTooManyRequestsError:
                log.error("Garmin is rate-limiting requests. Uploaded %d of %d; run again later.", ok, total)
                return 2
            except Exception as e:
                log.error("Error uploading %s: %s", fmt(m), e)
                return 1
            state.mark(m, "uploaded")
            ok += 1
            if ok % SAVE_EVERY == 0:
                state.save()
                log.info("  Uploaded %d of %d...", ok, total)
            if i < total - 1:
                time.sleep(UPLOAD_DELAY_SECONDS)
    finally:
        if not state.save():
            log.error("Progress was not saved. No harm done: uploaded weigh-ins will be found in Garmin next time.")
    log.info("Done: %d measurements uploaded to Garmin Connect.", ok)
    return 0


def sync_blood_pressure(args, state: State, readings: list[BloodPressure]) -> int:
    pending = [r for r in readings if r.reading_id not in state.blood_pressure_processed]
    if not pending:
        log.info("No new Google Fit blood-pressure readings to sync.")
        return 0

    garmin_email = ask_email(state.config, "garmin_email", "Garmin Connect")
    state.save()
    api = garmin_login(garmin_email, args.reset_credentials)
    start = min(r.ts_utc.astimezone(LOCAL_TZ).date() for r in pending) - timedelta(days=1)
    end = max(r.ts_utc.astimezone(LOCAL_TZ).date() for r in pending) + timedelta(days=1)
    log.info("Reading Garmin blood pressure from %s to %s...", start, end)
    try:
        existing = fetch_garmin_blood_pressure(api, start, end, LOCAL_TZ)
    except Exception as e:
        log.error("Cannot check Garmin blood pressure for duplicates: %s", e)
        return 1
    plan = build_blood_pressure_plan(
        pending, state.blood_pressure_processed, existing,
        tolerance_s=state.config.get("blood_pressure_tolerance_seconds", 60),
    )
    log.info("Blood pressure: %d to upload, %d skipped (already in Garmin).",
             len(plan.upload), len(plan.already_in_garmin))

    def fmt_bp(reading):
        local = reading.ts_utc.astimezone(LOCAL_TZ)
        return f"{local:%Y-%m-%d %H:%M:%S}  {reading.systolic}/{reading.diastolic} mmHg"

    for reading in plan.already_in_garmin:
        log.debug("  SKIP (already in Garmin) %s", fmt_bp(reading))
    for reading in plan.upload:
        log.info("  %s %s", "WOULD UPLOAD" if args.dry_run else "UPLOAD", fmt_bp(reading))
    if args.dry_run:
        log.info("--dry-run: no blood-pressure readings uploaded or marked as processed.")
        return 0

    from garminconnect import GarminConnectTooManyRequestsError

    for reading in plan.already_in_garmin:
        state.mark_blood_pressure(reading, "skipped_already_in_garmin")
    state.save()
    ok = 0
    try:
        for index, reading in enumerate(plan.upload):
            try:
                api.set_blood_pressure(
                    systolic=reading.systolic,
                    diastolic=reading.diastolic,
                    timestamp=reading.ts_utc.astimezone(LOCAL_TZ).isoformat(timespec="seconds"),
                    notes="Imported from Google Fit",
                )
            except GarminConnectTooManyRequestsError:
                log.error("Garmin is rate-limiting requests. Uploaded %d of %d BP readings; run again later.",
                          ok, len(plan.upload))
                return 2
            except Exception as e:
                log.error("Error uploading blood pressure %s: %s", fmt_bp(reading), e)
                return 1
            state.mark_blood_pressure(reading, "uploaded")
            ok += 1
            state.save()
            if index < len(plan.upload) - 1:
                time.sleep(UPLOAD_DELAY_SECONDS)
    finally:
        if not state.save():
            log.error("Progress not saved. Existing Garmin blood-pressure readings will be checked next run.")
    log.info("Done: %d blood-pressure readings uploaded to Garmin Connect.", ok)
    return 0


if __name__ == "__main__":
    sys.exit(main())
