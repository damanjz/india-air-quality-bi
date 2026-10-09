"""Download the archive files for the included stations: idempotent, resumable, with a manifest.

For each included OpenAQ location it lists the daily files inside the study window, totals the bytes and
refuses to start if the plan exceeds the agreed budget (config.MAX_DOWNLOAD_MB). Each file is written to a
.part file, checked against the archive's MD5 ETag and renamed into place, so an interrupted run resumes
where it stopped. Files already on disk with the right size and MD5 are skipped.

Manifest: data/manifest.csv, one row per file: source URL, bytes, ETag, SHA-256, UTC download time.
A committed summary (reference/manifest_summary.csv) records per-location file counts, bytes and a digest
over the per-file SHA-256s, so anyone can confirm they hold the same raw data.
"""
import argparse
import csv
import datetime as dt
import hashlib
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
from openaq import BUCKET_URL, PREFIX, fetch, list_objects  # noqa: E402

MANIFEST = config.ROOT / "data" / "manifest.csv"
SUMMARY = config.REFERENCE / "manifest_summary.csv"
COLS = ["location_id", "key", "url", "bytes", "etag", "sha256", "downloaded_utc"]


def plan():
    with open(config.REFERENCE / "stations.csv", encoding="utf-8") as f:
        ids = sorted(int(r["location_id"]) for r in csv.DictReader(f) if r["included"] == "1")

    def files(location_id):
        out = []
        for key, size, etag in list_objects(f"{PREFIX}locationid={location_id}/"):
            y, m, d = re.search(r"-(\d{4})(\d{2})(\d{2})\.csv\.gz$", key).groups()
            if config.WINDOW_START <= f"{y}-{m}-{d}" <= config.WINDOW_END:
                out.append({"location_id": location_id, "key": key, "bytes": size, "etag": etag})
        return out

    with ThreadPoolExecutor(16) as ex:
        return [f for fs in ex.map(files, ids) for f in fs]


def local_path(key):
    return config.RAW / key[len(PREFIX):]


def md5_ok(data, etag):
    """Single-part uploads carry the MD5 as ETag; multipart ETags ("...-N") cannot be checked this way."""
    return "-" in etag or hashlib.md5(data).hexdigest() == etag


def get(item, known):
    path = local_path(item["key"])
    if path.exists() and path.stat().st_size == item["bytes"] and md5_ok(path.read_bytes(), item["etag"]):
        prev = known.get(item["key"], {})
        return {**item, "url": BUCKET_URL + item["key"],
                "sha256": prev.get("sha256") or hashlib.sha256(path.read_bytes()).hexdigest(),
                # a file from an interrupted run has no manifest row yet: its write time is its download time
                "downloaded_utc": prev.get("downloaded_utc") or dt.datetime.fromtimestamp(
                    path.stat().st_mtime, dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "fresh": False}
    data = fetch(BUCKET_URL + item["key"])
    if len(data) != item["bytes"] or not md5_ok(data, item["etag"]):
        raise RuntimeError(f"checksum mismatch for {item['key']}")
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_suffix(path.suffix + ".part")
    part.write_bytes(data)
    part.replace(path)
    return {**item, "url": BUCKET_URL + item["key"], "sha256": hashlib.sha256(data).hexdigest(),
            "downloaded_utc": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "fresh": True}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan-only", action="store_true", help="list and size the files, download nothing")
    args = ap.parse_args()

    items = plan()
    total_mb = sum(i["bytes"] for i in items) / 1e6
    print(f"plan: {len(items)} files, {total_mb:.1f} MB from {len({i['location_id'] for i in items})} locations")
    if total_mb > config.MAX_DOWNLOAD_MB:
        sys.exit(f"plan exceeds the {config.MAX_DOWNLOAD_MB} MB budget; nothing downloaded")
    if args.plan_only:
        return

    known = {}
    if MANIFEST.exists():
        with open(MANIFEST, encoding="utf-8") as f:
            known = {r["key"]: r for r in csv.DictReader(f)}
    rows = []
    with ThreadPoolExecutor(96) as ex:
        for n, row in enumerate(ex.map(lambda i: get(i, known), items), 1):
            rows.append(row)
            if n % 5000 == 0:
                print(f"  {n}/{len(items)}")
    fresh = sum(r["fresh"] for r in rows)
    print(f"downloaded {fresh} files, {len(rows) - fresh} already present and verified")

    rows.sort(key=lambda r: r["key"])
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    with open(MANIFEST, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLS, extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        w.writerows(rows)

    by_loc = {}
    for r in rows:
        by_loc.setdefault(r["location_id"], []).append(r)
    with open(SUMMARY, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["location_id", "files", "bytes", "first_file", "last_file", "sha256_of_file_sha256s"])
        for loc, rs in sorted(by_loc.items()):
            days = [re.search(r"-(\d{8})\.csv", r["key"]).group(1) for r in rs]
            digest = hashlib.sha256("".join(r["sha256"] for r in rs).encode()).hexdigest()
            w.writerow([loc, len(rs), sum(r["bytes"] for r in rs), min(days), max(days), digest])
        allsha = hashlib.sha256("".join(r["sha256"] for r in rows).encode()).hexdigest()
        w.writerow(["ALL", len(rows), sum(r["bytes"] for r in rows), "", "", allsha])
    print(f"manifest: {MANIFEST.relative_to(config.ROOT)}; summary: {SUMMARY.relative_to(config.ROOT)}")


if __name__ == "__main__":
    main()
