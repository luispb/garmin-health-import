"""Reader for the CSV file exported by the RENPHO Health app.

The export layout (column names, language, delimiter, decimal separator, date
format, units) changes with the app version and language, so everything is
detected from the header row. Both English and Spanish exports are recognised.
"""

from __future__ import annotations

import csv
import io
import re
import unicodedata
from datetime import datetime
from pathlib import Path

LB_TO_KG = 0.45359237

# (field, keywords that identify the column). Order matters: first match wins.
# Spanish keywords are kept so Spanish-language exports keep working.
RULES = [
    ("ignore", ("fat-free", "fat free", "libre de grasa", "lean", "magra", "subcut", "protein",
                "optimal", "ideal", "objetivo", "target", "remark", "nota", "observ", "heart",
                "cardi", "frecuencia", "body shape", "tipo de cuerpo", "user", "usuario")),
    ("visceral", ("visceral",)),
    ("skeletal_pct", ("skeletal", "esquelet")),
    ("muscle", ("muscle", "muscul")),
    ("bone", ("bone", "osea", "oseo", "hueso")),
    ("water", ("water", "agua", "hidrat")),
    ("bmr", ("bmr", "basal", "tmb", "metabolismo")),
    ("age", ("metabolic age", "body age", "edad")),
    ("bmi", ("bmi", "imc")),
    ("fat", ("body fat", "grasa corporal", "fat", "grasa")),
    ("weight", ("weight", "peso")),
    ("datetime", ("measure time", "time of measurement", "fecha y hora", "date time", "datetime")),
    ("date", ("date", "fecha", "dia")),
    ("time", ("time", "hora")),
]

DATE_FORMATS_DMY = ["%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y"]
DATE_FORMATS_MDY = ["%m/%d/%Y", "%m-%d-%Y", "%m/%d/%y"]
DATE_FORMATS_ISO = ["%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%b %d, %Y", "%d %b %Y", "%B %d, %Y"]
TIME_FORMATS = ["%H:%M:%S", "%H:%M", "%I:%M:%S %p", "%I:%M %p", "%I:%M:%S%p", "%I:%M%p"]


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", text.strip().lower())


def classify_header(header: str) -> tuple[str | None, str | None]:
    """Return (field, unit) for a header. Unit is kg, lb, % or None."""
    h = _norm(header)
    unit = None
    m = re.search(r"\(([^)]*)\)|\[([^\]]*)\]", h)
    if m:
        u = (m.group(1) or m.group(2) or "").strip()
        unit = "kg" if u == "kg" else "lb" if u in ("lb", "lbs") else "%" if u == "%" else None
    for field, words in RULES:
        if any(w in h for w in words):
            return (None if field == "ignore" else field), unit
    return None, unit


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def _rows(text: str) -> list[list[str]]:
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        delim = dialect.delimiter
    except csv.Error:
        delim = ";" if sample.count(";") > sample.count(",") else ","
    return [r for r in csv.reader(io.StringIO(text), delimiter=delim) if any(c.strip() for c in r)]


def parse_number(value: str, *, thousands_ok: bool = False) -> tuple[float | None, str | None]:
    """'75,3 kg' -> (75.3, 'kg'); '--' -> (None, None)."""
    if value is None:
        return None, None
    s = _norm(str(value))
    if not s or s in ("-", "--", "---", "n/a", "na", "null", "none"):
        return None, None
    unit = "kg" if "kg" in s else "lb" if re.search(r"\blbs?\b|\dlbs?", s) else "%" if "%" in s else None
    s = re.sub(r"[^0-9,.\-]", "", s)
    if not s or s in ("-", ".", ","):
        return None, None
    if thousands_ok and re.fullmatch(r"\d{1,3}([.,]\d{3})+", s):
        s = s.replace(".", "").replace(",", "")
    elif "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    else:
        s = s.replace(",", ".")
    try:
        return float(s), unit
    except ValueError:
        return None, None


def _split_dt(text: str) -> tuple[str, str]:
    t = text.strip().replace("T", " ")
    m = re.match(r"^(.*?\d)[ ,]+(\d{1,2}:\d{2}.*)$", t)
    return (m.group(1).strip(" ,"), m.group(2).strip()) if m else (t, "")


def _infer_day_first(dates: list[str]) -> bool:
    """True if numeric a/b/yyyy dates are day/month (the default when ambiguous)."""
    for d in dates:
        m = re.match(r"^(\d{1,2})[/\-.](\d{1,2})[/\-.]\d{2,4}$", d.strip())
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a > 12:
                return True
            if b > 12:
                return False
    return True


def parse_datetime(date_s: str, time_s: str, day_first: bool) -> datetime | None:
    date_s, time_s = date_s.strip(), time_s.strip().upper().replace(".", "")
    formats = DATE_FORMATS_ISO + (DATE_FORMATS_DMY + DATE_FORMATS_MDY if day_first else DATE_FORMATS_MDY + DATE_FORMATS_DMY)
    d = None
    for f in formats:
        try:
            d = datetime.strptime(date_s, f)
            break
        except ValueError:
            continue
    if d is None:
        return None
    if not time_s:
        return d
    for f in TIME_FORMATS:
        try:
            t = datetime.strptime(time_s, f)
            return d.replace(hour=t.hour, minute=t.minute, second=t.second)
        except ValueError:
            continue
    return None


def _find_header(rows: list[list[str]]) -> int:
    for i, r in enumerate(rows[:10]):
        fields = {classify_header(c)[0] for c in r}
        if "weight" in fields and fields & {"date", "time", "datetime"}:
            return i
    raise ValueError("header row not found (need at least a date/time column and a weight column)")


def _columns(rows: list[list[str]]) -> tuple[int, dict[str, tuple[int, str | None]]]:
    hi = _find_header(rows)
    cols: dict[str, tuple[int, str | None]] = {}
    for idx, h in enumerate(rows[hi]):
        field, unit = classify_header(h)
        if field and field not in cols:
            cols[field] = (idx, unit)
    return hi, cols


def detected_columns(path: Path) -> dict[str, str]:
    """{field: original header} for the columns that will be used."""
    rows = _rows(_read_text(path))
    hi, cols = _columns(rows)
    return {f: rows[hi][i] for f, (i, _) in cols.items()}


def read_renpho_csv(path: Path, muscle: str = "skeletal") -> list[dict]:
    """Return normalised records: {local_dt (naive), weight, bmi, percent_fat, ...}, masses in kg.

    muscle: "skeletal" = weight x skeletal muscle % (if that column exists),
            "mass" = Renpho's muscle mass in kg (if that column exists).
    """
    rows = _rows(_read_text(path))
    hi, cols = _columns(rows)

    def cell(r, field):
        if field not in cols or cols[field][0] >= len(r):
            return ""
        return r[cols[field][0]]

    data = rows[hi + 1:]
    raw_dates = []
    for r in data:
        if "datetime" in cols:
            raw_dates.append(_split_dt(cell(r, "datetime"))[0])
        elif "date" in cols:
            ds, _ = _split_dt(cell(r, "date"))
            raw_dates.append(ds)
    day_first = _infer_day_first(raw_dates)

    out = []
    for r in data:
        if "datetime" in cols:
            ds, ts = _split_dt(cell(r, "datetime"))
        else:
            ds, ts = _split_dt(cell(r, "date")) if "date" in cols else ("", "")
            if "time" in cols:
                tcell = cell(r, "time")
                d2, t2 = _split_dt(tcell)
                if t2:  # the "time" column holds date and time
                    ds, ts = d2, t2
                else:
                    ts = tcell
        dt = parse_datetime(ds, ts, day_first)
        weight, wunit = parse_number(cell(r, "weight"))
        if dt is None or weight is None or weight <= 0:
            continue
        lb = (cols["weight"][1] or wunit) == "lb"
        weight_kg = weight * LB_TO_KG if lb else weight

        def mass(field):
            v, u = parse_number(cell(r, field))
            if v is None or v <= 0:
                return None
            unit = cols[field][1] or u
            if unit == "%":
                return round(weight_kg * v / 100, 2)
            if unit == "lb" or (unit is None and lb):
                return round(v * LB_TO_KG, 2)
            return round(v, 2)

        def plain(field, **kw):
            v, _ = parse_number(cell(r, field), **kw)
            return v if v is not None and v > 0 else None

        total = mass("muscle") if "muscle" in cols else None
        skeletal = None
        if "skeletal_pct" in cols:
            pct = plain("skeletal_pct")
            skeletal = round(weight_kg * pct / 100, 2) if pct else None
        muscle_kg = (skeletal or total) if muscle == "skeletal" else (total or skeletal)

        out.append({
            "local_dt": dt,
            "weight": round(weight_kg, 2),
            "bmi": plain("bmi"),
            "percent_fat": plain("fat"),
            "percent_hydration": plain("water"),
            "bone_mass": mass("bone") if "bone" in cols else None,
            "muscle_mass": muscle_kg,
            "visceral_fat_rating": plain("visceral"),
            "basal_met": plain("bmr", thousands_ok=True),
            "metabolic_age": plain("age"),
        })
    return out


def find_csv_files(target: Path) -> list[Path]:
    if target.is_file():
        return [target]
    if not target.is_dir():
        return []
    files = [p for p in target.iterdir() if p.suffix.lower() in (".csv", ".txt") and p.is_file()]
    return sorted(files, key=lambda p: p.stat().st_mtime)
