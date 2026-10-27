# dreeve-google-health-sync

Automatically pull GPS-tracked workouts from the **Google Health API** (the
API that replaced the legacy Fitbit Web API in 2026) and drop them as TCX
files into [Dreeve](https://dreeve.app)'s watch folder, so activities
recorded on a Pixel Watch / Fitbit / any Health Connect source show up in
Dreeve automatically.

## Why this exists

If you track workouts with a Pixel Watch or Fitbit, the obvious paths don't
work for automatic sync:

- **Health Connect** (Android's on-device data store) cannot be read by a
  background app for exercise *routes* recorded by another app -- it's a
  hard OS-level restriction, not a permissions issue. Apps like
  SparkyFitness hit this wall too.
- **Strava's API** now requires a paid Strava subscription to even create a
  developer app.
- **Garmin Connect** only has full GPS data for real Garmin hardware.

The Google Health API is different: it's a genuine cloud API (Google's
servers, not your phone), so it isn't subject to the Health Connect
background-read restriction, and it includes a `exportExerciseTcx` endpoint
that returns exactly the file format Dreeve imports.

## How it works

- A small Python script (run via [`uv`](https://docs.astral.sh/uv/), no
  dependencies) calls the Google Health API for exercises in a given window,
  and for anything with `hasGps: true`, exports it as TCX.
- Non-GPS workouts whose type is listed in `NON_GPS_TYPES` (default:
  `PADEL`) are exported too, as a summary TCX built from the exercise record
  (see [Non-GPS workouts](#non-gps-workouts-eg-padel)).
- It wraps [`ghealth`](https://github.com/rudrankriyam/Google-Health-CLI),
  an open-source CLI for the Google Health API, for the actual REST/OAuth
  plumbing.
- Files are written straight into Dreeve's `./watch` folder (a shared Docker
  volume) -- Dreeve's own daemon already watches that folder and imports
  from it on its own cron, so this tool doesn't touch Dreeve directly at
  all.
- Runs as its own container, on a loop, alongside your existing Dreeve
  `docker-compose.yml`.

## One hard limitation, by Google's design

Google's OAuth **device-code flow** (the "visit this URL on your phone and
type a code" method that needs no browser on the server) explicitly does
not support Health API scopes -- Google restricts it to
email/profile/Drive/YouTube only. So the *first-ever* authorization has to
happen through a real browser, once. After that, this tool runs unattended
forever using the refresh token -- no browser, no server-side listener, no
recurring action.

## Setup

### 1. Register a Google Cloud OAuth app

1. Create a project at [console.cloud.google.com](https://console.cloud.google.com)
   and enable the **Google Health API**.
2. Go to **APIs & Services -> Google Auth Platform -> Audience**: set User
   type to **External**, keep publishing status as **Testing**, and add your
   own Google account as a **Test user**.
3. Go to **Data Access -> Add or Remove Scopes** and add:
   - `https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly`
   - `https://www.googleapis.com/auth/googlehealth.location.readonly`
4. Go to **Clients -> Create Client**, type **Desktop app**, and note the
   Client ID and Client Secret.

Testing-mode apps don't need Google's verification review -- that's only
required for public/production apps.

### 2. Bootstrap the token (once, on any machine with a browser)

```bash
export GHEALTH_CLIENT_ID=...
export GHEALTH_CLIENT_SECRET=...
uv run bootstrap_login.py
```

This opens your browser, you approve consent, and it writes `token.json` to
the current directory. This does **not** need to run on your server --
run it on your laptop and copy the file over.

### 3. Add the service to your Dreeve stack

Copy the block from [`docker-compose.example.yml`](./docker-compose.example.yml)
into the same `docker-compose.yml` your Dreeve `app`/`daemon` services are
defined in, then:

```bash
mkdir -p google-health-token
cp /path/to/token.json google-health-token/token.json
```

Set `GHEALTH_CLIENT_ID` / `GHEALTH_CLIENT_SECRET` in your `.env` (see
[`.env.example`](./.env.example)), then:

```bash
docker compose up -d --build google-health-sync
```

## Environment variables

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `GHEALTH_CLIENT_ID` | yes | -- | OAuth client ID from Google Cloud |
| `GHEALTH_CLIENT_SECRET` | yes | -- | OAuth client secret |
| `LOOKBACK_DAYS` | no | `30` | Rolling window checked on every sync |
| `SYNC_INTERVAL_SECONDS` | no | `3600` | Delay between sync runs |
| `FROM_DATE` / `TO_DATE` | no | -- | One-off backfill window (`YYYY-MM-DD`), overrides `LOOKBACK_DAYS` when set |
| `NON_GPS_TYPES` | no | `PADEL` | Comma-separated Google Health `exerciseType`s to export even without GPS (e.g. `PADEL,TENNIS`). Set to `NONE` to disable |

For a one-off historical backfill, run it manually instead of waiting for
the loop:

```bash
docker compose run --rm -e FROM_DATE=2025-03-01 -e TO_DATE=2025-07-01 google-health-sync \
    uv run --script /app/sync.py
```

## Non-GPS workouts (e.g. Padel)

Google's `exportExerciseTcx` returns an empty shell (no laps, no
trackpoints) for exercises without GPS, which Dreeve can't import. For types
listed in `NON_GPS_TYPES`, the script instead builds a TCX from the
exercise's own summary:

- `Sport` is set to Dreeve's sport name (`PADEL` -> `Padel`, `TENNIS` ->
  `Tennis`, ...), so it shows up with the right sport type in Dreeve.
- One lap with the active duration, total distance, calories and average
  heart rate.
- Synthetic trackpoints every 30s spanning the session, with linearly
  interpolated distance and the *average* heart rate -- so the heart-rate
  chart in Dreeve is flat and max HR equals average HR. Per-second heart
  rate would need the extra `health_metrics_and_measurements` scope.

Activities exported by previous versions of this tool are tracked in a
separate `legacy` list in `exported_ids.json` (older versions also recorded
*skipped* non-GPS activities there), so Padel sessions that were skipped in
the past are still picked up by the rolling window or a `FROM_DATE`
backfill.

## Notes and limitations

- Only exercises with `hasGps: true`, or whose type is in `NON_GPS_TYPES`,
  are exported -- passive/auto-detected activity (e.g. background
  step-counted walks) has no route data at all, by Health Connect's own
  design, and is skipped.
- Query windows are automatically chunked at 90 days, the API's per-request
  limit for the `exercise` data type.
- Exported activity IDs are tracked in `TOKEN_DIR/exported_ids.json` so
  re-running never creates duplicates; Dreeve's own importer also dedupes
  independently by activity start time as a second safety net.
- **Your refresh token will expire every 7 days.** Google enforces a 7-day
  refresh token lifetime for apps in "Testing" publishing status *when the
  app requests sensitive or restricted scopes* -- and `activity_and_fitness`
  / `location` are both classified as Restricted. (Thanks to a Dreeve
  Discord member for flagging this -- confirmed, not theoretical.) When it
  expires, redo step 2 and replace `token.json`.

## License

MIT
