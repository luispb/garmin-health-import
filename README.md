# garmin-health-import

Import health measurements from Renpho and Google Fit into Garmin Connect, with
duplicate detection.

Copies your RENPHO Health weigh-ins (weight, body fat, water, muscle, bone, visceral fat,
BMR, metabolic age, BMI) to Garmin Connect without creating duplicates.
Also imports individual **blood-pressure readings from Google Fit exports** into
Garmin Connect, including checking for readings previously entered by hand.

Windows only (it uses Windows Credential Manager and `.bat` launchers).

## Weight duplicate rule

- If Garmin already has a weigh-in on a day (entered by hand or from another source),
  **nothing from Renpho is uploaded for that day**.
- Weigh-ins uploaded by this tool do not block the day: if you weigh yourself twice, the
  second measurement is also uploaded on the next run.
- If a measurement is already in Garmin at the same time (±60 s), it is not uploaded again.
- Repeated weigh-ins less than 10 minutes apart are merged into one (the most complete).
- Weight-only readings (no body fat, water or muscle, e.g. weighing with socks on) are not
  uploaded by default.

## Installation (once per computer)

1. Install Python 3.12 or later from python.org and tick "Add python.exe to PATH".
2. Download or clone [garmin-health-import](https://github.com/luispb/garmin-health-import)
   to a local folder, e.g. `C:\Tools\garmin-health-import` (better outside OneDrive).
   When copying individual files, include `garmin_health_import.py`, `renpho_csv.py`, `requirements.txt`, `install.bat`,
   `google_fit_blood_pressure.py`, `sync.bat`, `THIRD_PARTY_NOTICES.md` and, optionally,
   `README.md` and `tests\`.
   Do **not** copy `.venv` or `__pycache__`.
3. Double-click `install.bat`.

## Getting the data out of Renpho

By default the tool reads the **CSV file exported by the RENPHO Health app**. It can also
try the unofficial Renpho API, but that login fails for some accounts (`code=-114`).

1. In the app, open your **Weight** details, find the history (clock icon) → **Select data**
   → "…" → **Select all data** → **share** icon. Menu names vary between app versions.
2. Save the file to OneDrive, in `Apps\GarminHealthImport\import` (the tool creates this
   folder the first time it runs).
3. Run `sync.bat --dry-run`, then `sync.bat`.

You can leave old CSV files in the folder: all of them are read, duplicates are removed and
anything already processed is not uploaded again. If the `import` folder is empty, the tool
tries the Renpho API. Options: `--source csv|api|auto` and `--csv PATH` (file or folder).

English and Spanish exports are recognised, in kg or lb, with comma or semicolon delimiters.

## Google Fit blood pressure

This integration imports files exported from Google Fit. **It does not download readings
automatically from the Google Fit API.** Google stopped accepting new Fit API developers
on May 1, 2024 and [supports the legacy APIs only until the end of 2026](
https://developer.android.com/health-and-fitness/health-connect/migration/fit).
An existing Google Fit app account alone does not provide API access. This file-based
import does not require a Google Cloud project, OAuth credentials or a Google password.

1. Open [Google Takeout](https://takeout.google.com/), deselect other products, select
   **Fit** and include **All Data** (individual measurements in JSON). Labels may vary
   by language. Create the export and download its ZIP.
2. Keep the ZIP outside the source repository, for example in
   `<OneDrive>\Apps\GarminHealthImport\google-fit`. You can also use extracted JSON files.
3. Open a terminal in this script's folder and preview the import:

   ```bat
   sync.bat --blood-pressure-only --google-fit "C:\Exports\takeout-fit.zip" --dry-run
   ```

4. Check the dates, times and pressures, then repeat without `--dry-run`:

   ```bat
   sync.bat --blood-pressure-only --google-fit "C:\Exports\takeout-fit.zip"
   ```

`--google-fit` accepts a JSON file, a ZIP, or a folder containing JSON/ZIP exports (including
split Takeout archives). ZIP members are read without extracting them. When the exports
are in the default `google-fit` folder next to `state.json`, use:

```bat
sync.bat --blood-pressure-only --dry-run
sync.bat --blood-pressure-only
```

To import both Renpho weight and Google Fit blood pressure in one run:

```bat
sync.bat --google-fit "C:\Exports\takeout-fit.zip" --dry-run
sync.bat --google-fit "C:\Exports\takeout-fit.zip"
```

Existing commands without either of these options continue to sync Renpho weight only.
`--since YYYY-MM-DD` filters both types of measurement using the configured time zone.
On an existing installation, rerun `install.bat` to install **garminconnect >= 0.3.14**,
which supports blood pressure without a pulse value.

### Blood-pressure duplicate rule

- A reading is already in Garmin if **systolic and diastolic values match** and its
  timestamp is within **±60 seconds**. This includes entries created manually. Pulse,
  notes and source names do not need to match.
- Other readings on that day remain eligible. The weight rule that blocks an entire
  day does not apply to blood pressure, nor does the 10-minute weight repeat merge.
- Exact duplicates across files/source streams are removed. Progress is saved after
  every upload in a separate `blood_pressure_processed` section of `state.json`.
- If progress is lost after an upload, the next run checks Garmin again before writing.
  If Garmin cannot be read or its response cannot be understood, the import stops.
- Adjust `blood_pressure_tolerance_seconds` in the state file (0–3600; default 60) if
  the times you entered manually differ. Matching values at another time are treated
  as a separate reading. Identical readings inside the tolerance may be indistinguishable.

Only raw `com.google.blood_pressure` points are imported. Daily/aggregated CSV averages
are **not** individual readings and are never uploaded. If the export contains no raw
blood-pressure points, the tool stops with an explanation. Readings only stored on the
phone in Health Connect may not appear in a Google Fit export; exporting those requires
a separate source integration. There is no Health Connect or Google Health API client here.

Values are in **mmHg**, rounded to the nearest integer (half up) for Garmin. Values outside
the Garmin library's supported ranges (systolic 70–260, diastolic 40–150) stop the import
for review; they are not silently discarded. Google Fit's blood-pressure type has body
position and measurement-location fields but no pulse, so no pulse value is invented.
Uploads retain the measurement's time and use the note `Imported from Google Fit`.

The importer has automated offline tests using synthetic exports and Garmin responses.
Validate a real Takeout export with `--dry-run` before the first upload; live-account
compatibility has not been tested by the development test suite.

## Usage

Open a terminal in the folder:

```
sync.bat --dry-run      # show what would be uploaded, write nothing
sync.bat                # sync
sync.bat --since 2025-01-01
sync.bat --verbose      # also list the skipped days
sync.bat --reset-credentials
```

The first run asks for your Garmin Connect email, your Garmin password and your MFA code if
you have MFA turned on (and your Renpho email and password only if the Renpho API is used).
Passwords are saved in Windows Credential Manager under `GarminHealthImport:Garmin`
and `GarminHealthImport:Renpho` (when the Renpho API is used), and Garmin session tokens in
`%USERPROFILE%\.garminconnect`. Neither goes to OneDrive, so this first login has to be done
on each computer.

If Garmin rate-limits the upload half-way, run `sync.bat` again later: it carries on where
it stopped and does not upload anything twice.

## State file

`<OneDrive>\Apps\GarminHealthImport\state.json` stores which weight and blood-pressure
measurements have already been processed, plus the settings. The personal OneDrive is used if it is installed,
otherwise the work OneDrive. Use `--state-dir` to choose another folder. Do not run the tool
on two computers at the same time, and do not share your state file with another person.

The repository's `.gitignore` excludes local CSV exports, sync state, Garmin session
files, `.env` files and Python environments. Do not commit personal health data or
credentials, even when using a custom state folder.

### Settings (the `config` section of `state.json`)

| Key | Values | Default |
|---|---|---|
| `garmin_email` | Garmin Connect account email | asked on first run |
| `renpho_email` | Renpho account email (Renpho API only) | asked when needed |
| `timezone` | IANA time zone used for the CSV times, e.g. `Europe/London`, `America/New_York` | this computer's time zone, detected on first run |
| `csv_muscle` | `skeletal` (weight × skeletal muscle %) / `mass` (Renpho's muscle mass) | `skeletal` |
| `skip_weight_only` | `true` skips weigh-ins without body composition / `false` uploads them | `true` |
| `bone_unit` | Renpho API only: `percent` / `kg` | `percent` |
| `muscle_unit` | Renpho API only: `percent` / `kg` | `percent` |
| `muscle_source` | Renpho API only: `muscle` / `sinew_kg` (lean mass in kg) | `muscle` |
| `renpho_area_codes` | Renpho API only: account regions to try, in order | `["ES", "US"]` |
| `blood_pressure_tolerance_seconds` | Maximum time difference for identical systolic/diastolic readings already in Garmin (0–3600 seconds) | `60` |

Check the values in your first `--dry-run`: muscle and bone should be in kg, and the times
should match what the Renpho app shows. If the times are off by whole hours, set `timezone`.

## Caveats

- It uses unofficial Renpho and Garmin APIs, which can change without notice.
- Renpho allows only one active session, so the phone app may ask you to sign in again after
  the tool uses the Renpho API.
- If you sign in to Renpho with Google, create a password first: sign out of the app and use
  "Forgot password".
- Garmin's "muscle mass" is skeletal muscle mass, which is why `csv_muscle` defaults to
  `skeletal` (Renpho's total muscle mass would look far too high in Garmin).

## Acknowledgements

This project relies on the following open-source libraries:

| Project | Contribution | License |
|---|---|---|
| [renpho-py](https://github.com/ChocoTonic/renpho-py) | Unofficial Renpho API client and the basis for the adapted Renpho login implementation | MIT |
| [python-garminconnect](https://github.com/cyberjunky/python-garminconnect) | Garmin Connect authentication, weight/body-composition and blood-pressure queries/uploads | MIT |
| [curl_cffi](https://github.com/lexiforest/curl_cffi) | HTTP/TLS support, included as a dependency | MIT |
| [keyring](https://github.com/jaraco/keyring) | Password storage through Windows Credential Manager | MIT |
| [tzdata](https://github.com/python/tzdata) | IANA time-zone data for Python, including on Windows | Apache-2.0 |
| [tzlocal](https://github.com/regebro/tzlocal) | Detection of the computer's local time zone | MIT |

Special thanks to **ChocoTonic** for `renpho-py` and **danvaneijck** for the original
`renpho-api` on which it builds. The `renpho_login` function in
[`garmin_health_import.py`](./garmin_health_import.py) adapts the login implementation from
`renpho-py` 1.2.0 to allow a configurable account region.

The applicable upstream copyright and full MIT license notice for that adapted code
are preserved in [Third-party notices](./THIRD_PARTY_NOTICES.md). Keep that notice with
copies or distributions of the adapted code. Each dependency remains subject to its
own license.
