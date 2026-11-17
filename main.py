#!/usr/bin/env python3
"""GTA6FORECAST — hour-by-hour Twitch viewer forecast for a game launch day.

Reads one case file per run (makefun/twitch-ccu-case/v1) from the input
directory and writes, for each case, a forecast of the category's hourly
viewer counts for hours 2..24 of the release day as five quantiles each.

Method: a launch-shape library. Every comparable past launch in the case
carries its own release-day hourly curve. For each hour we take the
log-ratio of that hour's viewers to the launch's hour-1 viewers, and we
pool those log-ratios across launches into weighted empirical quantiles.
A launch counts more when its hour-1 print is close in size to this
case's hour-1 print (a Gaussian kernel on the log scale), and console-only
launches count extra, because the target game is a console-only release
and launches of very different size ramp very differently. The median
ratio gives the shape; the spread of the ratios gives the uncertainty,
so the quantiles are honest about how much launches vary. The resulting
curves are lightly smoothed across neighbouring hours.

The program is deterministic, offline and standard-library only.
"""

import json
import math
import os
import sys
from datetime import datetime, timedelta, timezone

LEVELS = (0.05, 0.25, 0.5, 0.75, 0.95)
# Standard normal quantiles at the five levels, used by the fallback row.
Z_VALUES = (-1.6448536269514722, -0.6744897501960817, 0.0,
            0.6744897501960817, 1.6448536269514722)

SIZE_KERNEL_BW = 1.0       # log-scale bandwidth for launch-size similarity
CONSOLE_ONLY_BOOST = 3.0   # extra weight for console-only launches
MIN_KERNEL_WEIGHT = 0.02   # ignore launches more dissimilar than this
SMOOTH_HALF_WINDOW = 1     # hours either side in the smoothing pass
MIN_POOL_SIZE = 5        # below this, top the weighted pool up with the plain one
FALLBACK_SPREAD = 0.15     # log spread growth of the persistence fallback
VIEWER_FLOOR = 1.0
VIEWER_CAP = 1_000_000_000.0


def parse_instant(text):
    """Parse an ISO-8601 UTC instant such as 2026-11-19T01:00:00Z."""
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc)


def release_day_viewers(comparable):
    """Hourly viewers of a comparable's own release day, keyed by hour 1..24.

    A comparable carries hourly points from 14 days before its release day
    to 3 days after it; only the 24 hours starting at its day_start count.
    Hours the source never sampled are simply absent.
    """
    day_start = parse_instant(comparable["day_start"])
    day_end = day_start + timedelta(hours=24)
    hours = {}
    for row in comparable.get("hourly") or []:
        try:
            when = parse_instant(row["from"])
            viewers = row["viewers"]
        except (KeyError, TypeError, ValueError):
            continue
        if not (day_start <= when < day_end):
            continue
        if viewers is None or viewers <= 0:
            continue
        hour = (when - day_start).seconds // 3600 + 1
        hours[hour] = viewers
    return hours


def is_console_only(platforms):
    """True when a launch's platforms name a console but no PC."""
    text = platforms or ""
    has_console = any(name in text for name in ("PS5", "PS4", "Xbox", "Switch"))
    return has_console and "PC" not in text


def weighted_quantile(points, tau):
    """Smallest value whose cumulative weight reaches tau of the total.

    Points are (value, weight) pairs; the walk is deterministic because the
    list is sorted by value first and ties keep their input order.
    """
    ordered = sorted(points, key=lambda pair: pair[0])
    total = sum(weight for _, weight in ordered)
    running = 0.0
    for value, weight in ordered:
        running += weight
        if running >= tau * total:
            return value
    return ordered[-1][0]


def persistence_row(first_print, hour):
    """Fallback row: every hour stays at the first hour's print.

    The spread widens with the square root of the hours elapsed, which is
    the honest shape of a forecast that knows nothing but the anchor.
    """
    anchor = max(float(first_print), VIEWER_FLOOR)
    spread = FALLBACK_SPREAD * math.sqrt(hour - 1)
    row = []
    for z in Z_VALUES:
        value = anchor * math.exp(z * spread)
        row.append(min(max(round(value, 2), VIEWER_FLOOR), VIEWER_CAP))
    return row


def collect_ratios(case, first_print):
    """Per-hour pooled log-ratios of comparables, weighted by similarity.

    Returns a dict hour -> list of (log-ratio, weight). Launches whose
    hour-1 print is far from this case's are down-weighted to near zero;
    console-only launches are boosted. A second, unweighted pool is kept
    as a fallback for hours the weighted pool leaves empty.
    """
    anchor_log = math.log(max(float(first_print), VIEWER_FLOOR))
    weighted = {hour: [] for hour in range(2, 25)}
    plain = {hour: [] for hour in range(2, 25)}
    for comparable in case.get("comparables") or []:
        day = release_day_viewers(comparable)
        first = day.get(1)
        if not first or first <= 0:
            continue
        size_gap = (math.log(first) - anchor_log) / SIZE_KERNEL_BW
        weight = math.exp(-0.5 * size_gap * size_gap)
        if is_console_only(comparable.get("platforms")):
            weight *= CONSOLE_ONLY_BOOST
        for hour in range(2, 25):
            viewers = day.get(hour)
            if not viewers:
                continue
            ratio_log = math.log(viewers / first)
            plain[hour].append((ratio_log, 1.0))
            if weight >= MIN_KERNEL_WEIGHT:
                weighted[hour].append((ratio_log, weight))
    return weighted, plain


def smooth_quantiles(rows):
    """Light centred smoothing of each quantile level across the hours.

    Each level is averaged in log space over its neighbouring hours, then
    every row is re-checked so its five quantiles stay non-decreasing.
    """
    hours = sorted(rows)
    levels = len(LEVELS)
    smoothed = {}
    for level in range(levels):
        series = [math.log(rows[hour][level]) for hour in hours]
        for index, hour in enumerate(hours):
            low = max(0, index - SMOOTH_HALF_WINDOW)
            high = min(len(series), index + SMOOTH_HALF_WINDOW + 1)
            window = series[low:high]
            smoothed.setdefault(hour, [0.0] * levels)[level] = math.exp(
                sum(window) / len(window))
    for hour in hours:
        row = smoothed[hour]
        for level in range(1, levels):
            if row[level] < row[level - 1]:
                row[level] = row[level - 1]
        rows[hour] = [min(max(round(v, 2), VIEWER_FLOOR), VIEWER_CAP)
                      for v in row]
    return rows


def forecast_case(case):
    """Forecast hours 2..24 of one case as five quantiles each."""
    first_print = case["known"][0]["viewers"]
    anchor = max(float(first_print), VIEWER_FLOOR)
    weighted, plain = collect_ratios(case, first_print)
    rows = {}
    for hour in range(2, 25):
        pool = weighted[hour]
        if len(pool) < MIN_POOL_SIZE:
            # A thin weighted pool (an extreme hour-1 print) would rest the
            # quantiles on one or two launches; top it up with the plain
            # pool so the estimate stays stable. Match on the ratio value
            # so a launch already in the pool is not counted twice.
            seen = {ratio for ratio, _ in pool}
            pool = pool + [point for point in plain[hour]
                           if point[0] not in seen]
        if not pool:
            rows[hour] = persistence_row(first_print, hour)
            continue
        quantiles = []
        for tau in LEVELS:
            log_ratio = weighted_quantile(pool, tau)
            quantiles.append(anchor * math.exp(log_ratio))
        rows[hour] = quantiles
    return smooth_quantiles(rows)


def run_case(input_path, output_path):
    """Read one case file, forecast it, write its output file."""
    with open(input_path, "r", encoding="utf-8") as handle:
        case = json.load(handle)
    rows = forecast_case(case)
    payload = {"hours": [{"hour": hour, "q": rows[hour]}
                         for hour in range(2, 25)]}
    with open(output_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, separators=(",", ":"))
        handle.write("\n")


def main(argv):
    input_dir = os.environ.get("MAKEFUN_INPUT_DIR", "/input")
    output_dir = os.environ.get("MAKEFUN_OUTPUT_DIR", "/output")
    os.makedirs(output_dir, exist_ok=True)
    case_files = sorted(name for name in os.listdir(input_dir)
                        if name.endswith(".in"))
    if not case_files:
        print("no case files in %s" % input_dir, file=sys.stderr)
        return 1
    for name in case_files:
        case_id = name[:-3]
        try:
            run_case(os.path.join(input_dir, name),
                     os.path.join(output_dir, case_id + ".out"))
        except Exception as exc:  # keep every case valid: fall back flat
            print("case %s failed (%s); writing persistence fallback"
                  % (case_id, exc), file=sys.stderr)
            rows = {}
            try:
                with open(os.path.join(input_dir, name),
                          encoding="utf-8") as handle:
                    first_print = json.load(handle)["known"][0]["viewers"]
            except Exception:
                first_print = 1.0
            for hour in range(2, 25):
                rows[hour] = persistence_row(first_print, hour)
            payload = {"hours": [{"hour": hour, "q": rows[hour]}
                                 for hour in range(2, 25)]}
            with open(os.path.join(output_dir, case_id + ".out"),
                      "w", encoding="utf-8") as handle:
                json.dump(payload, handle, separators=(",", ":"))
                handle.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
