#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
Pull GPS-tracked exercises from the Google Health API and drop them as TCX
files into Dreeve's watch folder for automatic import.

Non-GPS exercise types listed in NON_GPS_TYPES (default: PADEL) are exported
too. Google's export-tcx returns an empty shell for those, so a summary TCX
(duration, distance, calories, average heart rate) is generated from the
exercise record instead.

Wraps the `ghealth` CLI (https://github.com/rudrankriyam/Google-Health-CLI)
for the actual REST calls; this script handles token refresh, date
windowing (the API caps a single query at 90 days), state tracking to
avoid re-exporting the same activity, and writing into the shared volume.

Configuration is entirely via environment variables -- see README.md.
"""
import bisect
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.sax.saxutils import escape

GHEALTH_BIN = os.environ.get("GHEALTH_BIN") or shutil.which("ghealth") or "/usr/local/bin/ghealth"
GHEALTH_HOME = Path(os.environ.get("GHEALTH_HOME", "/tmp/ghealth-home"))
TOKEN_DIR = Path(os.environ.get("TOKEN_DIR", "/data"))
WATCH_DIR = Path(os.environ.get("WATCH_DIR", "/watch"))

CLIENT_ID = os.environ.get("GHEALTH_CLIENT_ID")
CLIENT_SECRET = os.environ.get("GHEALTH_CLIENT_SECRET")
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "30"))
FROM_DATE = os.environ.get("FROM_DATE")  # optional explicit backfill window
TO_DATE = os.environ.get("TO_DATE")
# Google Health exerciseType values to export even without GPS, comma-separated.
NON_GPS_TYPES = {
    t.strip().upper()
    for t in os.environ.get("NON_GPS_TYPES", "PADEL").split(",")
    if t.strip()
}

MAX_QUERY_DAYS = 90  # API limit for the "exercise" data type
TOKEN_URL = "https://oauth2.googleapis.com/token"

# Google Health exerciseType -> TCX Sport attribute. Dreeve resolves these
# names to its own sport types (see its SportType enum); anything it doesn't
# recognise is imported as a generic "Workout".
SPORT_NAMES = {
    "PADEL": "Padel",
    "TENNIS": "Tennis",
    "SQUASH": "Squash",
    "BADMINTON": "Badminton",
    "TABLE_TENNIS": "TableTennis",
    "PICKLEBALL": "Pickleball",
    "PICKELBALL": "Pickleball",  # sic -- that's how the API spells it
    "RACQUETBALL": "Racquetball",
    "WEIGHTLIFTING": "WeightTraining",
    "STRENGTH_TRAINING": "WeightTraining",
    "HIIT": "HIIT",
    "YOGA": "Yoga",
}
# Dreeve treats gaps > 60s between trackpoints as pauses, so keep them closer.
SYNTHETIC_TRACKPOINT_INTERVAL_S = 30
HR_MATCH_TOLERANCE_S = 30


def _avg_hr(point):
    avg = point.get("exercise", {}).get("metricsSummary", {}).get("averageHeartRateBeatsPerMinute")
    return int(round(float(avg))) if avg else None


def list_heart_rate(start, end):
    """Fetch heart-rate samples in [start, end] as sorted (timestamp_s, bpm) pairs.

    Requires the health_metrics_and_measurements.readonly scope; returns []
    if the token lacks that scope or the query fails."""
    since = start.strftime("%Y-%m-%dT%H:%M:%SZ")
    until = end.strftime("%Y-%m-%dT%H:%M:%SZ")
    samples = []
    page_token = None
    try:
        while True:
            args = ["data", "list", "heart-rate", "--from", since, "--to", until, "--limit", "1000"]
            if page_token:
                args += ["--page-token", page_token]
            data = json.loads(run_ghealth(*args))
            for p in data.get("dataPoints", []):
                hr = p.get("heartRate", {})
                ts = hr.get("sampleTime", {}).get("physicalTime")
                bpm = hr.get("beatsPerMinute")
                if ts and bpm:
                    samples.append((_parse_time(ts).timestamp(), int(round(float(bpm)))))
            page_token = data.get("nextPageToken")
            if not page_token:
                break
    except RuntimeError as e:
        logging.warning(f"heart-rate query failed (falling back to summary HR): {e}")
        return []
    samples.sort()
    return samples


def _nearest_hr(ts, times, bpms, default=None):
    if not times:
        return default
    idx = bisect.bisect_left(times, ts)
    candidates = []
    if idx < len(times):
        candidates.append((abs(times[idx] - ts), bpms[idx]))
    if idx > 0:
        candidates.append((abs(times[idx - 1] - ts), bpms[idx - 1]))
    dist, bpm = min(candidates)
    return bpm if dist <= HR_MATCH_TOLERANCE_S else default


def inject_heart_rate_tcx(tcx, point):
    """Ensure a GPS TCX contains <HeartRateBpm> data.

    Google's exportExerciseTcx includes heart rate when the OAuth token has
    the health_metrics_and_measurements.readonly scope. If it is still missing,
    splice in per-sample heart rate from the heart-rate endpoint (or fall back
    to the exercise's summary averageHeartRateBeatsPerMinute)."""
    if "<HeartRateBpm>" in tcx:
        return tcx

    interval = point.get("exercise", {}).get("interval", {})
    avg_hr = _avg_hr(point)
    samples = []
    if "startTime" in interval and "endTime" in interval:
        start = _parse_time(interval["startTime"]) - timedelta(seconds=HR_MATCH_TOLERANCE_S)
        end = _parse_time(interval["endTime"]) + timedelta(seconds=HR_MATCH_TOLERANCE_S)
        samples = list_heart_rate(start, end)

    if not samples and not avg_hr:
        return tcx

    times = [s[0] for s in samples]
    bpms = [s[1] for s in samples]
    matched_bpms = []

    def add_tp_hr(match):
        block = match.group(0)
        t_match = re.search(r"<Time>([^<]+)</Time>", block)
        bpm = _nearest_hr(_parse_time(t_match.group(1)).timestamp(), times, bpms, avg_hr) if t_match else avg_hr
        if not bpm:
            return block
        matched_bpms.append(bpm)
        indent_m = re.search(r"\n([ \t]*)</Trackpoint>", block)
        child_indent = (indent_m.group(1) + "    ") if indent_m else "                        "
        hr_xml = f"\n{child_indent}<HeartRateBpm><Value>{bpm}</Value></HeartRateBpm>"
        return re.sub(r"(\n[ \t]*</Trackpoint>)", hr_xml + r"\1", block, count=1)

    tcx = re.sub(r"<Trackpoint>.*?</Trackpoint>", add_tp_hr, tcx, flags=re.DOTALL)

    lap_avg = avg_hr or (int(round(sum(matched_bpms) / len(matched_bpms))) if matched_bpms else None)
    lap_max = max(matched_bpms) if matched_bpms else lap_avg
    if lap_avg and "<AverageHeartRateBpm>" not in tcx:
        lap_hr = f"\n                <AverageHeartRateBpm><Value>{lap_avg}</Value></AverageHeartRateBpm>"
        if lap_max:
            lap_hr += f"\n                <MaximumHeartRateBpm><Value>{lap_max}</Value></MaximumHeartRateBpm>"
        tcx = re.sub(r"(<Calories>\d+</Calories>)", r"\1" + lap_hr, tcx, count=1)

    return tcx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

STATE_FILE = TOKEN_DIR / "exported_ids.json"
TOKEN_FILE = TOKEN_DIR / "token.json"


def require_config():
    missing = [name for name, val in [
        ("GHEALTH_CLIENT_ID", CLIENT_ID),
        ("GHEALTH_CLIENT_SECRET", CLIENT_SECRET),
    ] if not val]
    if missing:
        sys.exit(f"missing required env var(s): {', '.join(missing)}")
    if not TOKEN_FILE.exists():
        sys.exit(
            f"{TOKEN_FILE} not found. Run bootstrap_login.py locally on a machine "
            "with a browser and mount the resulting token.json into TOKEN_DIR."
        )


def refresh_access_token():
    token = json.loads(TOKEN_FILE.read_text())

    data = urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": token["refresh_token"],
        "grant_type": "refresh_token",
    }).encode()

    req = urllib.request.Request(TOKEN_URL, data=data, method="POST")
    with urllib.request.urlopen(req) as resp:
        new_token = json.loads(resp.read())

    token["access_token"] = new_token["access_token"]
    token["expires_in"] = new_token["expires_in"]
    token["expiry"] = (datetime.now(timezone.utc) + timedelta(seconds=new_token["expires_in"])).isoformat()
    if "refresh_token" in new_token:  # Google rotates it occasionally
        token["refresh_token"] = new_token["refresh_token"]

    TOKEN_FILE.write_text(json.dumps(token, indent=2))
    return token


def setup_ghealth_home(token):
    """Point $HOME at a scratch dir we control so we don't need extra CLI flags.
    Writes to both Linux (~/.config) and macOS (~/Library/Application Support) paths."""
    config = {
        "baseURL": "https://health.googleapis.com",
        "user": "users/me",
        "clientID": CLIENT_ID,
    }
    for rel_dir in (".config/ghealth", "Library/Application Support/ghealth"):
        config_dir = GHEALTH_HOME / rel_dir
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.json").write_text(json.dumps(config, indent=2))
        (config_dir / "token.json").write_text(json.dumps(token, indent=2))


def run_ghealth(*args):
    env = dict(os.environ, HOME=str(GHEALTH_HOME))
    result = subprocess.run([GHEALTH_BIN, *args], capture_output=True, text=True, env=env)
    if result.returncode != 0:
        raise RuntimeError(f"ghealth {' '.join(args)} failed: {result.stderr or result.stdout}")
    return result.stdout


def load_state():
    """Returns (processed_ids, legacy_ids).

    `legacy` holds IDs written by older versions, which marked skipped non-GPS
    activities as processed too. They're kept apart so activities of a newly
    enabled NON_GPS_TYPES type can still be exported (incl. via backfills)."""
    if not STATE_FILE.exists():
        return set(), set()
    data = json.loads(STATE_FILE.read_text())
    if isinstance(data, list):
        return set(), set(data)
    return set(data.get("processed", [])), set(data.get("legacy", []))


def save_state(processed, legacy):
    STATE_FILE.write_text(json.dumps({
        "version": 2,
        "processed": sorted(processed),
        "legacy": sorted(legacy - processed),
    }))


def list_exercises_window(since_dt, until_dt):
    since = since_dt.strftime("%Y-%m-%dT%H:%M:%S")
    until = until_dt.strftime("%Y-%m-%dT%H:%M:%S")
    points = []
    page_token = None
    while True:
        args = ["data", "list", "exercise", "--from", since, "--to", until, "--limit", "25"]
        if page_token:
            args += ["--page-token", page_token]
        data = json.loads(run_ghealth(*args))
        points.extend(data.get("dataPoints", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return points


def list_exercises_range(start, end):
    points = []
    window_start = start
    while window_start < end:
        window_end = min(window_start + timedelta(days=MAX_QUERY_DAYS), end)
        logging.info(f"querying {window_start.date()} .. {window_end.date()}")
        points.extend(list_exercises_window(window_start, window_end))
        window_start = window_end
    return points


def resolve_range():
    if FROM_DATE:
        start = datetime.fromisoformat(FROM_DATE).replace(tzinfo=timezone.utc)
        end = datetime.fromisoformat(TO_DATE).replace(tzinfo=timezone.utc) if TO_DATE else datetime.now(timezone.utc)
        return start, end
    now = datetime.now(timezone.utc)
    return now - timedelta(days=LOOKBACK_DAYS), now


def safe_filename(point):
    exercise = point.get("exercise", {})
    start = exercise.get("interval", {}).get("startTime", "unknown")
    start = re.sub(r"[^0-9A-Za-z]", "-", start)
    ex_type = exercise.get("exerciseType", "activity").lower()
    point_id = point["name"].rsplit("/", 1)[-1]
    return f"{start}_{ex_type}_{point_id}.tcx"


def export_tcx(point_id):
    return run_ghealth("data", "export-tcx", f"users/me/dataTypes/exercise/dataPoints/{point_id}")


def _parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _parse_duration(value):
    """Google duration strings look like "4800s" / "12.5s"."""
    return float(str(value).rstrip("s")) if value else None


def _tcx_time(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def build_summary_tcx(point):
    """Build a TCX for an exercise without GPS from its summary metrics
    (and per-sample heart rate when available).

    Produces a single lap with evenly spaced trackpoints carrying linearly
    interpolated distance and heart rate, which is the shape Dreeve's TCX
    parser needs to pick up duration, distance, calories and heart rate."""
    exercise = point.get("exercise", {})
    interval = exercise.get("interval", {})
    start = _parse_time(interval["startTime"])
    end = _parse_time(interval["endTime"])
    elapsed = max((end - start).total_seconds(), 1.0)
    active = _parse_duration(exercise.get("activeDuration")) or elapsed

    summary = exercise.get("metricsSummary", {})
    distance_m = (summary.get("distanceMillimeters") or 0) / 1000
    calories = int(round(float(summary.get("caloriesKcal") or 0)))
    avg_hr = _avg_hr(point)

    samples = list_heart_rate(
        start - timedelta(seconds=HR_MATCH_TOLERANCE_S),
        end + timedelta(seconds=HR_MATCH_TOLERANCE_S),
    )
    times = [s[0] for s in samples]
    bpms = [s[1] for s in samples]

    ex_type = exercise.get("exerciseType", "")
    sport = SPORT_NAMES.get(ex_type, ex_type.replace("_", " ").title().replace(" ", "") or "Other")
    device = point.get("dataSource", {}).get("device", {}).get("displayName", "Google Health")

    offsets = list(range(0, int(elapsed), SYNTHETIC_TRACKPOINT_INTERVAL_S)) + [int(elapsed)]
    trackpoints = []
    matched_bpms = []
    for offset in offsets:
        tp_dt = start + timedelta(seconds=offset)
        bpm = _nearest_hr(tp_dt.timestamp(), times, bpms, avg_hr)
        tp = [f"<Time>{_tcx_time(tp_dt)}</Time>"]
        tp.append(f"<DistanceMeters>{distance_m * offset / elapsed:.1f}</DistanceMeters>")
        if bpm:
            tp.append(f"<HeartRateBpm><Value>{bpm}</Value></HeartRateBpm>")
            matched_bpms.append(bpm)
        trackpoints.append("            <Trackpoint>" + "".join(tp) + "</Trackpoint>")

    lap_avg = avg_hr or (int(round(sum(matched_bpms) / len(matched_bpms))) if matched_bpms else None)
    lap_max = max(matched_bpms) if matched_bpms else lap_avg
    hr_lap = ""
    if lap_avg:
        hr_lap += f"\n        <AverageHeartRateBpm><Value>{lap_avg}</Value></AverageHeartRateBpm>"
    if lap_max:
        hr_lap += f"\n        <MaximumHeartRateBpm><Value>{lap_max}</Value></MaximumHeartRateBpm>"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<TrainingCenterDatabase xmlns="http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2">
  <Activities>
    <Activity Sport="{escape(sport)}">
      <Id>{_tcx_time(start)}</Id>
      <Lap StartTime="{_tcx_time(start)}">
        <TotalTimeSeconds>{active:.0f}</TotalTimeSeconds>
        <DistanceMeters>{distance_m:.1f}</DistanceMeters>
        <Calories>{calories}</Calories>{hr_lap}
        <Intensity>Active</Intensity>
        <TriggerMethod>Manual</TriggerMethod>
        <Track>
{chr(10).join(trackpoints)}
        </Track>
      </Lap>
      <Creator><Name>{escape(device)}</Name></Creator>
    </Activity>
  </Activities>
</TrainingCenterDatabase>
"""


def main():
    require_config()
    token = refresh_access_token()
    setup_ghealth_home(token)

    processed, legacy = load_state()
    start, end = resolve_range()
    points = list_exercises_range(start, end)

    new_count = 0
    for point in points:
        point_id = point["name"].rsplit("/", 1)[-1]
        exercise = point.get("exercise", {})
        has_gps = exercise.get("exerciseMetadata", {}).get("hasGps", False)
        is_non_gps_type = not has_gps and exercise.get("exerciseType", "").upper() in NON_GPS_TYPES

        if point_id in processed:
            continue
        if point_id in legacy and not is_non_gps_type:
            continue

        if not has_gps and not is_non_gps_type:
            # Not recorded in state: the decision is free to re-make, and this
            # lets activities be picked up later if NON_GPS_TYPES changes.
            continue

        if is_non_gps_type:
            try:
                tcx = build_summary_tcx(point)
            except (KeyError, ValueError) as e:
                logging.warning(f"skip {point_id}: cannot build summary TCX: {e}")
                continue
        else:
            try:
                tcx = export_tcx(point_id)
            except RuntimeError as e:
                logging.warning(f"skip {point_id}: {e}")
                continue

            if "<Trackpoint>" not in tcx:
                processed.add(point_id)
                save_state(processed, legacy)
                continue

            tcx = inject_heart_rate_tcx(tcx, point)

        filename = safe_filename(point)
        WATCH_DIR.mkdir(parents=True, exist_ok=True)
        (WATCH_DIR / filename).write_text(tcx)

        processed.add(point_id)
        save_state(processed, legacy)
        new_count += 1
        logging.info(f"exported {filename}")

    save_state(processed, legacy)  # also migrates the old list-only format
    logging.info(f"done: {new_count} new TCX file(s) written")


if __name__ == "__main__":
    main()
