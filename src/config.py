"""Fixed settings shared by the download and the pipeline. Changing any of these changes the dataset."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
REFERENCE = ROOT / "reference"

# Study window: first day of 2019 to the last full month at download time (downloaded 2026-10-09).
WINDOW_START = "2019-01-01"
WINDOW_END = "2026-09-30"

CITIES = ["Delhi", "Mumbai", "Kolkata", "Chennai", "Bengaluru", "Hyderabad"]

# Download budget: stop and report instead of exceeding it. Agreed at 300 MB before the first download; raised to
# 350 MB on Daman's approval on 2026-10-09 to take in the 19 stations that started in 2022 or 2025 (337 MB in total).
MAX_DOWNLOAD_MB = 350

# Station selection rules (see docs/data-notes.md)
# A station needs at least a year of archive files in the window: 107 stations, a 337 MB download.
MIN_DAYS_IN_WINDOW = 365
COLOCATED_METRES = 1000         # same site name, different operator, closer than this: keep the longer record

# Short PM2.5 spikes (sql/03_marts.sql: mart_spike_days, mart_spike_runs, mart_spike_episodes), found from the data
# alone. A spike day has city PM2.5 at least SPIKE_MIN_RATIO times the median of the days SPIKE_GUARD_DAYS + 1 to
# SPIKE_WINDOW_DAYS days before and after it; the days next to it are left out so an episode never sets its own
# reference. 2.0 (double the surrounding level) marks 2.5% of evaluated city-days in the 2026-10-09 build: rare
# enough to be a departure, not day-to-day noise. A short spike is a run of 1 to SPIKE_MAX_DAYS such days.
SPIKE_MIN_RATIO = 2.0
SPIKE_MAX_DAYS = 3
SPIKE_GUARD_DAYS = 3
SPIKE_WINDOW_DAYS = 14
SPIKE_MIN_DAYS_EACH_SIDE = 6    # measured days needed on each side of the reference (out of 11)
