# Renpho → Garmin Connect

Copies your RENPHO Health weigh-ins (weight, body fat, water, muscle, bone, visceral fat,
BMR, metabolic age, BMI) to Garmin Connect without creating duplicates.

Windows only (it uses Windows Credential Manager and `.bat` launchers).

## Duplicate rule

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
2. Copy these files to a local folder, e.g. `C:\Tools\renpho-garmin-sync` (better outside
   OneDrive): `renpho_garmin_sync.py`, `renpho_csv.py`, `requirements.txt`, `install.bat`,
   `sync.bat` and, optionally, `README.md` and `tests\`. Do **not** copy `.venv` or
   `__pycache__`.
3. Double-click `install.bat`.

## Getting the data out of Renpho

By default the tool reads the **CSV file exported by the RENPHO Health app**. It can also
try the unofficial Renpho API, but that login fails for some accounts (`code=-114`).

1. In the app, open your **Weight** details, find the history (clock icon) → **Select data**
   → "…" → **Select all data** → **share** icon. Menu names vary between app versions.
2. Save the file to OneDrive, in `Apps\RenphoGarminSync\import` (the tool creates this
   folder the first time it runs).
3. Run `sync.bat --dry-run`, then `sync.bat`.

You can leave old CSV files in the folder: all of them are read, duplicates are removed and
anything already processed is not uploaded again. If the `import` folder is empty, the tool
tries the Renpho API. Options: `--source csv|api|auto` and `--csv PATH` (file or folder).

English and Spanish exports are recognised, in kg or lb, with comma or semicolon delimiters.

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
Passwords are saved in Windows Credential Manager and Garmin session tokens in
`%USERPROFILE%\.garminconnect`. Neither goes to OneDrive, so this first login has to be done
on each computer.

If Garmin rate-limits the upload half-way, run `sync.bat` again later: it carries on where
it stopped and does not upload anything twice.

## State file

`<OneDrive>\Apps\RenphoGarminSync\state.json` stores which Renpho measurements have already
been processed, plus the settings. The personal OneDrive is used if it is installed,
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
