"""Find the Indian government monitoring stations in the OpenAQ archive and choose the study stations.

The archive holds only measurement files (no station list), and the OpenAQ API needs a key, which this
project does not use. So stations are found from the archive itself:
  1. list every location id in the archive;
  2. read the first row of one file (name, latitude, longitude) for every id: one listing request and a
     2 KB range request per id, 96 at a time;
  3. keep the locations inside India's bounding box.
Every probe is appended to data/discovery_probes.jsonl as it finishes, so an interrupted run resumes where
it stopped; ids that could not be read are retried on the next run.

    python src/discover.py                 full discovery (~56,000 ids; resumes from data/discovery_probes.jsonl)
    python src/discover.py --select-only   re-apply the selection rules to the committed location list

Outputs (committed):
  reference/openaq_locations_india_bbox.csv  every location found inside the bounding box
  reference/stations.csv                     candidate locations in the six cities, grouped into stations,
                                              with coverage and the inclusion decision and its reason
"""
import csv
import json
import math
import re
import sys
import zlib
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
import config  # noqa: E402
from openaq import BUCKET_URL, PREFIX, fetch, list_objects  # noqa: E402

BBOX = (6.0, 37.5, 68.0, 97.5)  # lat min, lat max, lon min, lon max (includes parts of neighbouring countries)
WORKERS = 96
PROBES = config.ROOT / "data" / "discovery_probes.jsonl"
NAME_RE = re.compile(r"^(?P<site>.*?),\s*(?P<city>%s)\s+-\s+(?P<agency>[A-Za-z]+)\s*-\s*\d+$"
                     % "|".join(["New Delhi"] + config.CITIES))
AGENCY_ALIASES = {"WBSPCB": "WBPCB"}


def all_location_ids():
    found = (re.search(r"locationid=(\d+)/", p) for p in list_objects(PREFIX, delimiter="/") if isinstance(p, str))
    return sorted(int(m.group(1)) for m in found if m)


def probe(location_id):
    """Name, latitude and longitude from the first row of the location's earliest file (first 2 KB only)."""
    try:
        key = next(list_objects(f"{PREFIX}locationid={location_id}/", max_keys=1))[0]
        raw = fetch(BUCKET_URL + key, headers={"Range": "bytes=0-2047"})
        text = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(raw).decode("utf-8", "replace")
        row = next(csv.reader(text.splitlines()[1:2]))
        lat, lon = (float(v) if v else math.nan for v in row[4:6])
        return {"location_id": location_id, "location": row[2], "lat": lat, "lon": lon,
                "first_file": re.search(r"-(\d{8})\.csv", key).group(1)}
    except Exception as e:  # noqa: BLE001 - a location we cannot read is reported, not fatal
        return {"location_id": location_id, "error": repr(e)[:120]}


def in_bbox(r):
    return "lat" in r and BBOX[0] <= r["lat"] <= BBOX[1] and BBOX[2] <= r["lon"] <= BBOX[3]


def find_india(ids):
    seen = {}
    if PROBES.exists():
        for line in PROBES.read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            if "error" not in r:
                seen[r["location_id"]] = r
    todo = [i for i in ids if i not in seen]
    print(f"  {len(seen)} ids already probed, {len(todo)} to go", flush=True)
    PROBES.parent.mkdir(parents=True, exist_ok=True)
    with open(PROBES, "a", encoding="utf-8") as log, ThreadPoolExecutor(WORKERS) as ex:
        for n, r in enumerate(ex.map(probe, todo), 1):
            log.write(json.dumps(r) + "\n")
            if "error" not in r:
                seen[r["location_id"]] = r
            if n % 2000 == 0:
                log.flush()
                print(f"  probed {n}/{len(todo)}", flush=True)
    missing = [i for i in ids if i not in seen]
    print(f"  {len(missing)} ids could not be read (retried on the next run): {missing[:20]}", flush=True)
    for r in seen.values():  # NaN coordinates from the cache come back as None
        for k in ("lat", "lon"):
            if r.get(k) is None:
                r[k] = math.nan
    return sorted((r for r in seen.values() if in_bbox(r)), key=lambda r: r["location_id"])


def metres(a, b):
    lat = math.radians((a["lat"] + b["lat"]) / 2)
    return 6371000 * math.hypot(math.radians(a["lat"] - b["lat"]), math.radians(a["lon"] - b["lon"]) * math.cos(lat))


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def coverage(location_id):
    days = [re.search(r"-(\d{4})(\d{2})(\d{2})\.csv", k).groups() for k, _, _ in
            list_objects(f"{PREFIX}locationid={location_id}/")]
    days = ["-".join(d) for d in days]
    inside = [d for d in days if config.WINDOW_START <= d <= config.WINDOW_END]
    return location_id, (len(inside), min(inside, default=""), max(inside, default=""))


def choose_stations(found):
    cand = []
    for r in found:
        m = NAME_RE.match(r["location"])
        if not m:
            continue
        city = "Delhi" if m["city"] == "New Delhi" else m["city"]
        agency = AGENCY_ALIASES.get(m["agency"].upper(), m["agency"].upper())
        cand.append({**r, "site": m["site"].strip(), "city": city, "agency": agency})
    with ThreadPoolExecutor(16) as ex:
        cov = dict(ex.map(coverage, [c["location_id"] for c in cand]))
    for c in cand:
        c["days_in_window"], c["first_day"], c["last_day"] = cov[c["location_id"]]
        c["station_id"] = f"{c['city'][:3].upper()}-{slug(c['site'])}-{c['agency'].lower()}"

    stations = defaultdict(list)
    for c in cand:
        stations[c["station_id"]].append(c)
    days = {s: sum(c["days_in_window"] for c in cs) for s, cs in stations.items()}
    for c in cand:
        if c["days_in_window"] == 0:
            c["included"], c["reason"] = 0, "no files in the study window"
        elif days[c["station_id"]] < config.MIN_DAYS_IN_WINDOW:
            c["included"], c["reason"] = 0, f"station has under {config.MIN_DAYS_IN_WINDOW} days in the window"
        else:
            c["included"], c["reason"] = 1, ""
    # Same site name in the same city run by two operators within 1 km: one monitoring site, keep the longer record.
    live = [c for c in cand if c["included"]]
    for a in live:
        for b in live:
            if (a["city"] == b["city"] and slug(a["site"]) == slug(b["site"]) and a["agency"] != b["agency"]
                    and metres(a, b) < config.COLOCATED_METRES
                    and (days[a["station_id"]], b["station_id"]) < (days[b["station_id"]], a["station_id"])):
                for c in stations[a["station_id"]]:
                    c["included"], c["reason"] = 0, f"co-located with {b['station_id']}, which has the longer record"
    return sorted(cand, key=lambda c: (c["city"], c["station_id"], c["location_id"]))


def write(path, rows, cols):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def read_found():
    with open(config.REFERENCE / "openaq_locations_india_bbox.csv", encoding="utf-8") as f:
        return [{**r, "location_id": int(r["location_id"]), "lat": float(r["lat"]), "lon": float(r["lon"])}
                for r in csv.DictReader(f)]


def main():
    select_only = "--select-only" in sys.argv  # reuse the committed location list, redo only the selection
    if select_only:
        found = read_found()
    else:
        ids = all_location_ids()
        print(f"{len(ids)} location ids in the archive")
        found = find_india(ids)
        write(config.REFERENCE / "openaq_locations_india_bbox.csv", found,
              ["location_id", "location", "lat", "lon", "first_file"])
    print(f"{len(found)} locations inside the bounding box")
    cand = choose_stations(found)
    write(config.REFERENCE / "stations.csv", cand,
          ["station_id", "city", "site", "agency", "location_id", "location", "lat", "lon",
           "days_in_window", "first_day", "last_day", "included", "reason"])
    inc = [c for c in cand if c["included"]]
    print(f"{len(cand)} candidate locations in {len(config.CITIES)} cities; "
          f"{len(inc)} locations ({len({c['station_id'] for c in inc})} stations) included")


if __name__ == "__main__":
    main()
