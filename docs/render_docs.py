"""Fill the case study, the reviewer's guide and the README with numbers computed from the warehouse and the build logs.

    python docs/render_docs.py
reads docs/case-study.template.html, docs/how-it-works.template.html and docs/readme.template.md, replaces every
{{token}} with a figure queried from data/aq.duckdb (read only), the download manifest, the build, check and mutation
logs, the report checks, src/config.py, the SQL and the generated Power BI project, and writes docs/case-study.html,
docs/how-it-works.html and README.md. Nothing is typed by hand, so a rebuild cannot leave a
stale number behind. Fails if any token is unfilled, or if the current exports no longer match the logged build.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import sys
import zipfile
from datetime import datetime
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
sys.path.insert(0, str(ROOT / "src"))
import config  # noqa: E402
import pipeline  # noqa: E402

# Same minimums as src/findings.py and the report: a city-month needs 15 days of AQI; Oct-Nov against September
# needs 15 September and 40 October-November days in that city-year.
MIN_MONTH_DAYS, MIN_SEP_DAYS, MIN_OCTNOV_DAYS = 15, 15, 40
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]


def n0(v):
    return f"{v:,.0f}"


def pct(v, digits=0):
    return f"{v * 100:.{digits}f}%"


def day(d):
    return f"{d.day} {d:%b %Y}"


def log_line(path: Path, pattern: str) -> re.Match:
    for line in reversed(path.read_text(encoding="utf-8-sig").splitlines()):
        m = re.search(pattern, line)
        if m:
            return m
    raise SystemExit(f"{path.name}: no line matching {pattern!r}")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def proof(t):
    """Checks, mutation test, report checks and determinism, read from the logs the build writes."""
    checks = (ROOT / "sql" / "checks.sql").read_text(encoding="utf-8").count("-- name:")
    log = (ROOT / "data" / "pipeline.log").read_text(encoding="utf-8").splitlines()
    passed = sum(1 for line in log if line.strip().startswith("pass "))
    failed = sum(1 for line in log if line.strip().startswith("FAIL"))
    if failed or passed != checks:
        raise SystemExit(f"pipeline.log: {passed} of {checks} checks pass")
    t["checks"] = f"{passed} of {checks}"
    t["build_minutes"] = f"{round(sum(int(s) for s in re.findall(r'built \S+ \((\d+)s\)', chr(10).join(log))) / 60)}"
    m = log_line(ROOT / "data" / "mutation.log", r"(\d+) of (\d+) mutations caught")
    t["mutations"] = f"{m.group(1)} of {m.group(2)}"
    caps = ROOT / "powerbi" / ".captures"
    m = log_line(caps / "check_report.txt", r"Part 1: (\d+) of (\d+) measures")
    t["measures_ok"] = f"{m.group(1)} of {m.group(2)}"
    m = log_line(caps / "check_report.txt", r"(\d+) of (\d+) match")
    t["report_match"] = f"{m.group(1)} of {m.group(2)}"
    fit = (caps / "check_fit.txt").read_text(encoding="utf-8-sig")
    t["fit_lines"] = "{} of {}".format(*re.search(r"(\d+) of (\d+) single lines fit", fit).groups())
    t["fit_wrapped"] = "{} of {}".format(*re.search(r"(\d+) of (\d+) wrapped texts fit", fit).groups())
    t["fit_grids"] = "{} of {}".format(*re.search(r"(\d+) of (\d+) grids fit", fit).groups())

    def hashes(p):
        return {line.split(" *")[1].strip(): line.split(" *")[0] for line in p.read_text().splitlines() if " *" in line}
    a, b = hashes(ROOT / "data" / "build_a.sha"), hashes(ROOT / "data" / "build_b.sha")
    same = sum(1 for k in a if b.get(k) == a[k])
    now = sum(1 for k in b if (ROOT / k).exists() and sha256(ROOT / k) == b[k])
    if same != len(a) or now != len(b):
        raise SystemExit(f"determinism: {same} of {len(a)} identical across builds, {now} of {len(b)} match the files now")
    t["identical"] = f"{same} of {len(a)}"

    sm = ROOT / "powerbi" / "AirQuality.SemanticModel" / "definition"
    tmdl = [p.read_text(encoding="utf-8") for p in (sm / "tables").glob("*.tmdl")]
    t["pbi_tables"] = f"{len(tmdl)}"
    t["pbi_measures"] = f"{sum(len(re.findall(r'^\s*measure ', s, re.M)) for s in tmdl)}"
    t["pbi_rels"] = f"{len(re.findall(r'^relationship ', (sm / 'relationships.tmdl').read_text(encoding='utf-8'), re.M))}"
    pages = json.loads((ROOT / "powerbi" / "AirQuality.Report" / "definition" / "pages" / "pages.json").read_text())
    t["pbi_pages"] = f"{len(pages['pageOrder'])}"
    t["pbi_pages_word"] = ["No", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine"][len(pages["pageOrder"])]
    with zipfile.ZipFile(ROOT / "excel" / "india-air-quality.xlsx") as z:
        names = z.namelist()
    t["xl_tables"] = f"{sum(1 for n in names if re.fullmatch(r'xl/tables/table\d+\.xml', n))}"
    t["xl_sheets"] = f"{sum(1 for n in names if re.fullmatch(r'xl/worksheets/sheet\d+\.xml', n))}"


def source(t, con):
    q = lambda s: con.sql(s).fetchone()
    with open(ROOT / "data" / "manifest.csv", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    t["files"] = n0(len(rows))
    t["download_mb"] = f"{sum(int(r['bytes']) for r in rows) / 1e6:.1f}"
    t["budget_mb"] = f"{config.MAX_DOWNLOAD_MB}"
    db = (ROOT / "data" / "aq.duckdb").stat().st_size
    t["warehouse_gb"] = f"{db / 2 ** 30:.1f}"
    model = sum(f.stat().st_size for f in (ROOT / "data" / "model").glob("*.csv"))
    # raw files, the warehouse, a copy of it during the mutation test, and the exported tables
    t["disk_gb"] = f"{math.ceil((sum(int(r['bytes']) for r in rows) + 2 * db + model) / 2 ** 30)}"
    with open(ROOT / "data" / "discovery_probes.jsonl", encoding="utf-8") as f:
        t["ids_probed"] = n0(len({json.loads(line)["location_id"] for line in f if line.strip()}))
    with open(ROOT / "reference" / "openaq_locations_india_bbox.csv", newline="", encoding="utf-8") as f:
        t["ids_bbox"] = n0(sum(1 for _ in csv.DictReader(f)))
    t["candidates"] = n0(q("SELECT count(*) FROM ref_stations")[0])
    t["min_days"] = f"{config.MIN_DAYS_IN_WINDOW}"
    st, ids, first, last = q("SELECT count(*), sum(n_location_ids), min(first_hour), max(last_hour) FROM dim_station")
    t["stations"], t["ids"] = f"{st}", f"{ids}"
    t["first_hour"], t["last_hour"] = f"{first:%Y-%m-%d %H:%M}", f"{last:%Y-%m-%d %H:%M}"
    by_city = con.sql("SELECT city, count(*) FROM dim_station GROUP BY city ORDER BY 2 DESC, city").fetchall()
    t["stations_by_city"] = ", ".join(f"{c} {n}" for c, n in by_city)
    t["window_start"], t["window_end"] = config.WINDOW_START, config.WINDOW_END
    raw, readings, valid, hours, days, citydays = q("""
        SELECT (SELECT sum(n_rows) FROM raw_file_rows), (SELECT count(*) FROM stg_readings),
               (SELECT count(*) FROM stg_readings WHERE disposition = 'valid'), (SELECT count(*) FROM fact_measurement_hourly),
               (SELECT count(*) FILTER (is_complete) FROM fact_measurement_daily), (SELECT count(*) FROM mart_city_daily_aqi)""")
    t["raw_rows"], t["readings"], t["valid"] = n0(raw), n0(readings), n0(valid)
    t["raw_m"], t["readings_m"], t["valid_m"] = f"{raw / 1e6:.1f}", f"{readings / 1e6:.1f}", f"{valid / 1e6:.1f}"
    t["station_hours"], t["station_days"], t["city_days"] = n0(hours), n0(days), n0(citydays)


def costs(t, con):
    """What real data costs: the gap, the station sets, and each named problem."""
    q = lambda s: con.sql(s).fetchone()
    before, after = q("""SELECT max(date) FILTER (date < DATE '2024-01-01'), min(date) FILTER (date >= DATE '2023-01-01')
                         FROM (SELECT DISTINCT date FROM fact_measurement_hourly)""")
    t["gap_last"], t["gap_first"] = day(before), day(after)
    t["gap_days"] = n0((after - before).days - 1)
    t["gap_months"] = f"{(after.year - before.year) * 12 + after.month - before.month - 1}"
    days_in_window = q("SELECT count(*) FROM dim_date")[0]
    t["gap_share"] = pct(((after - before).days - 1) / days_in_window)
    t["no_jul_aug_2022"] = "no" if q("""SELECT count(*) FROM fact_measurement_hourly
                                         WHERE date BETWEEN DATE '2022-07-01' AND DATE '2022-08-31'""")[0] == 0 else "some"
    v, p = q("SELECT sum(valid_hours), sum(hours_in_month) FROM mart_station_completeness")
    t["completeness"] = pct(v / p)
    v, p, y0, y1 = q(f"""SELECT sum(valid_hours), sum(hours_in_month), min(year(month_start)), max(year(month_start))
                         FROM mart_station_completeness WHERE month_start >= DATE '{after:%Y-%m-01}'""")
    t["completeness_after"], t["after_years"] = pct(v / p), f"{y0}-{y1}"
    grow = dict(((c, y), n) for c, y, n in con.sql("""
        SELECT city, year(first_hour), count(*) FROM dim_station GROUP BY ALL""").fetchall())
    total = dict(con.sql("SELECT city, count(*) FROM dim_station GROUP BY city").fetchall())
    t["mum_2025"] = f"{grow.get(('Mumbai', 2025), 0)} of {total['Mumbai']}"
    t["hyd_2022"] = f"{grow.get(('Hyderabad', 2022), 0)} of {total['Hyderabad']}"
    t["ben_2025"] = f"{grow.get(('Bengaluru', 2025), 0)} of {total['Bengaluru']}"
    t["kol_2025"] = f"{grow.get(('Kolkata', 2025), 0)} of {total['Kolkata']}"

    dq = con.sql("SELECT level, issue, action, n, stations FROM mart_dq_summary ORDER BY n DESC, issue").fetchall()
    t["dq_kinds"] = f"{len(dq)}"
    t["set_aside"] = n0(sum(n for _, _, a, n, _ in dq if a == "set aside"))
    t["set_aside_kinds"] = f"{sum(1 for _, _, a, _, _ in dq if a == 'set aside')}"
    dqd = {i: (n, s) for _, i, _, n, s in dq}
    t["missing_days"] = n0(dqd["day missing from the archive"][0])
    t["ppb_n"] = n0(dqd["gas unit labelled ppb (value is CPCB µg/m³, CO mg/m³)"][0])
    t["ppb_stations"] = f"{dqd['gas unit labelled ppb (value is CPCB µg/m³, CO mg/m³)'][1]}"
    co_ppb, co_ug = q("""SELECT median(value_num) FILTER (units_label = 'ppb'), median(value_num) FILTER (units_label = 'µg/m³')
                         FROM stg_readings WHERE pollutant = 'co' AND disposition = 'valid'""")
    t["co_ppb"], t["co_ug"], t["co_mg"] = f"{co_ppb:.2f}", n0(co_ug), f"{co_ug / 1000:.2f}"
    after_m, before_m = q("""
        SELECT median(value) FILTER (units_label = 'ppb'), median(value) FILTER (units_label = 'µg/m³') FROM stg_readings
        WHERE pollutant = 'no2' AND disposition = 'valid' AND month(hour_ist) IN (3, 4, 5, 6, 7, 8, 9)
          AND station_id IN (SELECT station_id FROM stg_readings WHERE units_label = 'ppb'
                             INTERSECT SELECT station_id FROM stg_readings WHERE units_label = 'µg/m³')""")
    t["no2_after"], t["no2_before"] = f"{after_m:.1f}", f"{before_m:.1f}"

    near = dict(con.sql("""SELECT CAST(value_num AS INTEGER), count(*) FROM stg_readings
                           WHERE pollutant IN ('pm25', 'pm10') AND value_num IN (984, 985, 986) GROUP BY 1""").fetchall())
    t["n985"], t["n984"], t["n986"] = n0(near.get(985, 0)), n0(near.get(984, 0)), n0(near.get(986, 0))
    t["n985_stations"] = f"{dqd['instrument fault code 985'][1]}"
    runs, run_hours = q("SELECT count(*), sum(run_hours) FROM (SELECT DISTINCT station_id, pollutant, run_id, run_hours FROM stg_stuck_hours)")
    t["stuck_runs"], t["stuck_hours"] = n0(runs), n0(run_hours)
    t["stuck_n"] = n0(dqd["stuck value"][0])
    t["stuck_min"] = f"{q('SELECT min(run_hours) FROM stg_stuck_hours')[0]}"
    t["day_hours"] = re.search(r"under (\d+) valid hours", next(i for i in dqd if i.startswith("incomplete day"))).group(1)
    longest = q("""SELECT s.station_name, s.city, p.pollutant_name, h.run_hours FROM stg_stuck_hours h
                   JOIN dim_station s USING (station_id) JOIN dim_pollutant p USING (pollutant)
                   ORDER BY h.run_hours DESC, h.station_id LIMIT 1""")
    t["stuck_longest"] = f"{longest[3]} hours of {longest[2]} at {longest[0]} ({longest[1]})"

    dup = con.sql("""
        WITH p AS (SELECT r.station_id, s.station_name, s.city, r.ts_ist, r.location_id, avg(r.value) AS v
                   FROM stg_readings r JOIN dim_station s USING (station_id)
                   WHERE r.station_id IN (SELECT DISTINCT station_id FROM stg_readings WHERE disposition = 'conflicting duplicate')
                     AND r.pollutant = 'pm25' AND r.rule_issue IS NULL GROUP BY ALL)
        SELECT a.station_name, a.city, count(*), count(*) FILTER (a.v = b.v), corr(a.v, b.v)
        FROM p a JOIN p b ON a.station_id = b.station_id AND a.ts_ist = b.ts_ist AND a.location_id < b.location_id
        GROUP BY ALL ORDER BY 1""").fetchall()
    t["dup_stations"] = f"{len(dup)}"
    t["dup_pairs"] = "; ".join(f"{s} ({c}): the same value at {n0(eq)} of {n0(n)} shared timestamps, correlation {r:.2f}"
                               for s, c, n, eq, r in dup)
    t["conflict_n"] = n0(dqd["conflicting duplicate"][0])
    t["dup_from"] = f"{q("SELECT min(hour_ist) FROM stg_readings WHERE disposition = 'conflicting duplicate'")[0]:%B %Y}"
    t["moves"] = f"{dqd['published coordinates changed by over 100 m'][0]}"
    t["max_move_km"] = f"{q('SELECT max(moved_m) FROM dim_station')[0] / 1000:.0f}"
    t["zero_n"], t["neg_n"], t["max_n"] = (n0(dqd["zero particulate reading"][0]), n0(dqd["negative value"][0]),
                                          n0(dqd["above plausible maximum"][0]))
    t["ceiling_n"] = n0(dqd["particulate at the analyser ceiling (999.99-1000 µg/m³)"][0])
    t["incomplete_n"] = n0(dqd["incomplete day (under 16 valid hours)"][0])


def findings(t, con):
    q = lambda s: con.sql(s).fetchone()
    n, poor = q("SELECT count(*), count(*) FILTER (aqi > 200) FROM mart_city_daily_aqi")
    t["poor_share"] = pct(poor / n)
    by_city = con.sql("SELECT city, avg(aqi) FROM mart_city_daily_aqi GROUP BY city ORDER BY 2 DESC, city").fetchall()
    t["worst_city"], t["worst_city_aqi"] = by_city[0][0], n0(by_city[0][1])
    t["clean_city"], t["clean_city_aqi"] = by_city[-1][0], n0(by_city[-1][1])
    t["min_month_days"], t["min_sep"], t["min_octnov"] = f"{MIN_MONTH_DAYS}", f"{MIN_SEP_DAYS}", f"{MIN_OCTNOV_DAYS}"
    t["n_cities"] = f"{len(config.CITIES)}"
    t["n_cities_word"] = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"][len(config.CITIES)]
    worst = con.sql(f"""SELECT city, strftime(month_start, '%b %Y'), mean_aqi, days_with_aqi, days_severe + days_very_poor
                       FROM mart_city_month WHERE days_with_aqi >= {MIN_MONTH_DAYS} ORDER BY mean_aqi DESC LIMIT 2""").fetchall()
    t["wm_city"], t["wm_month"], t["wm_aqi"] = worst[0][0], worst[0][1], n0(worst[0][2])
    t["wm_vp"], t["wm_days"] = f"{worst[0][4]}", f"{worst[0][3]}"
    t["wm2"] = f"{worst[1][0]} {worst[1][1]} ({n0(worst[1][2])}, {worst[1][4]} of {worst[1][3]} days Very Poor or Severe)"
    o = q(f"""SELECT city, strftime(month_start, '%b %Y'), mean_aqi FROM mart_city_month
             WHERE days_with_aqi >= {MIN_MONTH_DAYS} AND city <> 'Delhi' ORDER BY mean_aqi DESC LIMIT 1""")
    t["wm_outside"] = f"{o[0]} {o[1]} at {n0(o[2])}"
    ex = con.sql("""SELECT city, sum(days_over_limit), sum(days_measured) FROM mart_exceedance_city_year
                    WHERE pollutant = 'pm25' GROUP BY city ORDER BY sum(days_over_limit) / sum(days_measured) DESC""").fetchall()
    t["pm_limit"] = n0(q("SELECT naaqs_limit FROM dim_pollutant WHERE pollutant = 'pm25'")[0])
    t["ex_top"] = pct(ex[0][1] / ex[0][2], 1)
    t["ex_top_city"] = ex[0][0]
    t["ex_list"] = ", ".join(f"{c} {pct(o / m, 1)} ({n0(o)} of {n0(m)})" for c, o, m in ex[1:])
    t["ex_md"] = ", ".join(f"{c} {pct(o / m, 1)}" for c, o, m in ex)
    seasons = con.sql("""SELECT d.season, avg(a.aqi), min(d.month), max(d.month) FROM mart_city_daily_aqi a JOIN dim_date d USING (date)
                         GROUP BY d.season ORDER BY 2 DESC""").fetchall()
    ab = lambda m: MONTHS[m - 1][:3]
    t["season_hi"], t["season_hi_aqi"], t["season_hi_m"] = seasons[0][0], n0(seasons[0][1]), f"{ab(seasons[0][2])}-{ab(seasons[0][3])}"
    t["season_lo"], t["season_lo_aqi"], t["season_lo_m"] = seasons[-1][0].lower(), n0(seasons[-1][1]), f"{ab(seasons[-1][2])}-{ab(seasons[-1][3])}"

    st = con.sql(f"""SELECT year, sep_pm25, octnov_pm25, octnov_vs_sep FROM mart_octnov_season
                     WHERE city = 'Delhi' AND sep_days >= {MIN_SEP_DAYS} AND octnov_days >= {MIN_OCTNOV_DAYS} ORDER BY year""").fetchall()
    t["delhi_octnov"] = "; ".join(f"{y}: {s:.0f} to {o:.0f} µg/m³ ({r:.1f}x)" for y, s, o, r in st)
    t["delhi_octnov_lo"] = f"{min(r for *_, r in st):.1f}"
    t["delhi_octnov_hi"] = f"{max(r for *_, r in st):.1f}"
    t["octnov_all"] = f"{q(f'SELECT avg(octnov_vs_sep) FROM mart_octnov_season WHERE sep_days >= {MIN_SEP_DAYS} AND octnov_days >= {MIN_OCTNOV_DAYS}')[0]:.1f}"
    t["octnov_vp"] = pct(q("SELECT sum(octnov_days_very_poor_or_worse) / sum(octnov_days_with_aqi) FROM mart_octnov_season")[0])

    hours = con.sql("""SELECT hour, sum(mean_value * n_hours) / sum(n_hours) FROM mart_profile_hourly
                       WHERE pollutant = 'pm25' GROUP BY hour""").fetchall()
    hi = sorted(hours, key=lambda r: (-r[1], r[0]))[0]
    lo = sorted(hours, key=lambda r: (r[1], r[0]))[0]
    t["hour_hi"], t["hour_hi_v"], t["hour_lo"] = f"{hi[0]:02d}:00", n0(hi[1]), f"{lo[0]:02d}:00"
    t["hour_ratio"] = f"{hi[1] / lo[1]:.1f}"
    hrs = con.sql("""WITH h AS (SELECT city, hour, avg(mean_value * n_hours) / avg(n_hours) AS v
                                FROM mart_profile_hourly WHERE pollutant = 'pm25' GROUP BY ALL)
                     SELECT city, arg_max(hour, v), max(v), arg_min(hour, v), min(v), max(v) / min(v)
                     FROM h GROUP BY city ORDER BY 6 DESC""").fetchall()
    t["hour_list"] = "; ".join(f"{c} {h:02d}:00 ({vh:.0f}) against {l:02d}:00 ({vl:.0f}), {r:.2f}x"
                               for c, h, vh, l, vl, r in hrs)
    late = [c for c, h, *_ in hrs if h >= 18 or h == 0]
    t["hour_evening_n"] = f"{len(late)}"
    t["hour_morning"] = " and ".join(f"{c} ({h:02d}:00)" for c, h, *_ in hrs if 5 <= h <= 11)

    hyd = con.sql("""SELECT d.season, avg(p.value) FROM mart_city_daily_pollutant p JOIN dim_date d USING (date)
                     WHERE p.city = 'Hyderabad' AND p.pollutant = 'pm25' GROUP BY d.season ORDER BY 2 DESC""").fetchall()
    t["hyd_hi"], t["hyd_hi_v"] = hyd[0][0].lower(), f"{hyd[0][1]:.1f}"
    t["hyd_lo"], t["hyd_lo_v"] = hyd[-1][0].lower(), f"{hyd[-1][1]:.1f}"
    t["hyd_ratio"] = f"{hyd[0][1] / hyd[-1][1]:.1f}"


def spikes(t, con):
    q = lambda s: con.sql(s).fetchone()
    ratio, maxd, guard, window, side = q("SELECT min_ratio, max_days, guard_days, window_days, min_days_each_side FROM ref_spike_rule")
    t["sp_ratio"], t["sp_max"], t["sp_guard"], t["sp_window"] = f"{ratio:.1f}", f"{maxd}", f"{guard}", f"{window}"
    t["sp_ref_from"], t["sp_side"], t["sp_ref_days"] = f"{guard + 1}", f"{side}", f"{window - guard}"
    total, med = q("SELECT count(*), median(peak_ratio) FROM mart_spike_episodes")
    evaluated, spike_days = q("SELECT count(*) FILTER (is_evaluated), count(*) FILTER (is_spike_day) FROM mart_spike_days")
    t["sp_n"], t["sp_med"], t["sp_eval"] = f"{total}", f"{med:.1f}", n0(evaluated)
    t["sp_days"], t["sp_days_share"] = f"{spike_days}", pct(spike_days / evaluated, 1)
    by = con.sql("SELECT city, count(*) FROM mart_spike_episodes GROUP BY city ORDER BY 2 DESC, city").fetchall()
    t["sp_by_city"] = ", ".join(f"{c} {n}" for c, n in by)
    months = con.sql("""SELECT month(d.date) AS m, count(e.episode_id) AS n, count(*) AS nd
                        FROM mart_spike_days d LEFT JOIN mart_spike_episodes e ON e.city = d.city AND e.start_date = d.date
                        WHERE d.is_evaluated GROUP BY m ORDER BY n * 1.0 / nd DESC, m""").fetchall()
    t["sp_top_month"], t["sp_top_n"], t["sp_top_days"] = MONTHS[months[0][0] - 1], f"{months[0][1]}", n0(months[0][2])
    t["sp_top_rate"] = f"{100 * months[0][1] / months[0][2]:.1f}"
    t["sp_low_month"], t["sp_low_rate"] = MONTHS[months[-1][0] - 1], f"{100 * months[-1][1] / months[-1][2]:.1f}"
    single, alone = q("""SELECT count(*) FILTER (stations_at_ratio = 1 AND stations_on_peak_day > 1),
                                count(*) FILTER (stations_on_peak_day = 1) FROM mart_spike_episodes""")
    t["sp_single"], t["sp_alone"] = f"{single}", f"{alone}"
    dropped = dict(con.sql("SELECT outcome, count(*) FROM mart_spike_runs WHERE outcome <> 'short spike' GROUP BY 1").fetchall())
    t["sp_long"] = f"{dropped.get(f'longer than {maxd} days', 0)}"
    t["sp_edge"] = f"{dropped.get('next to a day without a reading or reference', 0)}"
    top = q("""SELECT city, start_date, days, peak_pm25, peak_ratio, stations_at_ratio, stations_on_peak_day
               FROM mart_spike_episodes ORDER BY peak_ratio DESC, episode_id LIMIT 1""")
    t["sp_big"] = (f"{top[0]}, {day(top[1])}: {top[2]} day{'' if top[2] == 1 else 's'}, peak {top[3]:.0f} µg/m³, "
                   f"{top[4]:.1f}x its surroundings, with {top[5]} of {top[6]} stations at {ratio:.1f}x")
    # the example drawn in docs/img/spike-method.svg (same rule as docs/charts.py)
    ex = q("""SELECT city, start_date, end_date, peak_date, peak_pm25, reference_pm25, peak_ratio, stations_at_ratio,
                     stations_on_peak_day, days FROM mart_spike_episodes
              WHERE stations_on_peak_day >= 3 AND 2 * stations_at_ratio > stations_on_peak_day
              ORDER BY peak_ratio DESC, episode_id LIMIT 1""")
    t["ex_city"], t["ex_peak"], t["ex_days"] = ex[0], day(ex[3]), f"{ex[9]}"
    t["ex_pm"], t["ex_ref"], t["ex_ratio"] = f"{ex[4]:.0f}", f"{ex[5]:.0f}", f"{ex[6]:.1f}"
    t["ex_st"] = f"{ex[7]} of {ex[8]}"


def num(v):
    """A breakpoint or limit as printed: thousands separator, no trailing zeros (30, 1.0 -> 1, 1288 -> 1,288)."""
    v = float(v)
    return f"{v:,.0f}" if v == int(v) else f"{v:,.1f}"


def minutes(s):
    return f"{s:.0f} s" if s < 90 else f"{s / 60:.1f} min" if s < 600 else f"{s / 60:.0f} min"


def guide(t, con):
    """Figures only the reviewer's guide uses: timings from the logs, method constants from the code and the warehouse."""
    q = lambda s: con.sql(s).fetchone()
    # build timings: each stage's seconds, as src/pipeline.py logged them in the two builds compared for determinism
    for tag, log in (("a", ROOT / "data" / "build_a.log"), ("b", ROOT / "data" / "build_b.log")):
        secs = dict(re.findall(r"built (\S+)\.sql \((\d+)s\)", log.read_text(encoding="utf-8-sig")))
        if len(secs) != 4:
            raise SystemExit(f"{log.name}: expected four timed stages, found {sorted(secs)}")
        for stage, s in secs.items():
            t[f"t{tag}_{stage[3:]}"] = minutes(int(s))
        t[f"t{tag}_total"] = minutes(sum(int(s) for s in secs.values()))
    m = log_line(ROOT / "data" / "mutation.log", r"mutations caught \((\d+)s\)")
    t["mut_time"] = minutes(int(m.group(1)))
    # download time: the manifest stamps every file; runs are separated by pauses of over 30 minutes
    with open(ROOT / "data" / "manifest.csv", newline="", encoding="utf-8") as f:
        stamps = sorted((r["downloaded_utc"], int(r["bytes"])) for r in csv.DictReader(f))
    ts = [(datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ"), b) for s, b in stamps]
    runs, cur = [], [ts[0]]
    for prev, nxt in zip(ts, ts[1:]):
        if (nxt[0] - prev[0]).total_seconds() > 1800:
            runs.append(cur)
            cur = []
        cur.append(nxt)
    runs.append(cur)
    t["dl_runs"] = "; ".join(f"{minutes((r[-1][0] - r[0][0]).total_seconds())} for {n0(len(r))} files "
                             f"({sum(b for _, b in r) / 1e6:.1f} MB)" for r in runs)

    csvs = sorted(p.stem for p in (ROOT / "data" / "model").glob("*.csv"))
    if csvs != sorted(pipeline.EXPORT):
        raise SystemExit("data/model does not hold exactly the tables src/pipeline.py exports")
    t["export_n"] = f"{len(pipeline.EXPORT)}"
    t["n_checks"] = t["checks"].split(" of ")[1]
    t["files_raw"] = f"{len(stamps)}"
    t["hashed_n"] = t["identical"].split(" of ")[1]
    t["fit_margin"] = re.search(r"\((\d+)% margin\)", (ROOT / "powerbi" / ".captures" / "check_fit.txt")
                                .read_text(encoding="utf-8-sig")).group(1)

    # the spike rule in the warehouse must be the one in src/config.py
    rule = q("SELECT min_ratio, max_days, guard_days, window_days, min_days_each_side FROM ref_spike_rule")
    cfg = (config.SPIKE_MIN_RATIO, config.SPIKE_MAX_DAYS, config.SPIKE_GUARD_DAYS, config.SPIKE_WINDOW_DAYS,
           config.SPIKE_MIN_DAYS_EACH_SIDE)
    if tuple(float(v) for v in rule) != tuple(float(v) for v in cfg):
        raise SystemExit(f"ref_spike_rule {rule} is not src/config.py {cfg}: rebuild the warehouse")
    t["sp_runs"] = f"{q('SELECT count(*) FROM mart_spike_runs')[0]}"

    # AQI method: minimum-data rules as written in the SQL; breakpoints and limits from the warehouse
    marts = (ROOT / "sql" / "03_marts.sql").read_text(encoding="utf-8")
    model = (ROOT / "sql" / "02_model.sql").read_text(encoding="utf-8")
    t["aqi_min_poll"] = re.search(r"HAVING count\(\*\) >= (\d+)", marts).group(1)
    t["min_8h_hours"] = re.search(r"hours_8h >= (\d+)", model).group(1)
    checks = (ROOT / "sql" / "checks.sql").read_text(encoding="utf-8")
    names = re.findall(r"-- name: ([^\n]+)\n", checks)
    caught = set(re.findall(r"^\s+caught\s+(.+?)\s*$", (ROOT / "data" / "mutation.log").read_text(encoding="utf-8-sig"), re.M))
    if set(names) - caught:
        raise SystemExit(f"mutation.log: not caught {sorted(set(names) - caught)}")
    esc = lambda x: x.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    t["check_rows"] = "\n".join(f'  <tr><td class="num">{i}</td><td>{esc(n)}</td><td>caught</td></tr>'
                                 for i, n in enumerate(names, 1))
    t["ppb_co_max"] = re.search(r"WHERE co\.m >= (\d+)", checks).group(1)
    t["ppb_lo"], t["ppb_hi"] = re.search(r"after_m / no2\.before_m NOT BETWEEN ([\d.]+) AND ([\d.]+)", checks).groups()
    pols = con.sql("SELECT pollutant, pollutant_name, unit, naaqs_limit, naaqs_averaging, aqi_averaging "
                   "FROM dim_pollutant ORDER BY pollutant_order").fetchall()
    bands = con.sql("SELECT pollutant, category, aqi_lo, aqi_hi, conc_lo, conc_hi FROM ref_breakpoints "
                    "ORDER BY pollutant, aqi_lo").fetchall()
    cats = [(c, num(lo), num(hi)) for p, c, lo, hi, *_ in bands if p == "pm25"]
    t["aqi_head"] = "".join(f'<th class="num">{c}<br>{lo}-{hi}</th>' for c, lo, hi in cats)
    rows = []
    for p, name, unit, *_, avg in pols:
        cells = [f"{num(lo)}-{num(hi)}" for pp, _, _, _, lo, hi in bands if pp == p]
        rows.append(f'<tr><td>{name} <span class="muted">{unit}, {avg}</span></td>'
                    + "".join(f'<td class="num">{c}</td>' for c in cells) + "</tr>")
    t["aqi_rows"] = "\n".join(rows)
    caps = dict(con.sql("SELECT pollutant, max_value FROM ref_plausible_max").fetchall())

    def grouped(items):
        """'PM2.5 60, PM10 100 µg/m³ (24-hour); CO 2 mg/m³ (8-hour)': one unit and label per group of like items."""
        groups: dict[tuple, list] = {}
        for name, value, key in items:
            groups.setdefault(key, []).append(f"{name} {num(value)}")
        return "; ".join(", ".join(v) + " " + " ".join(k).replace(" ()", "") for k, v in groups.items())
    t["naaqs_list"] = grouped((name, lim, (unit, f"({avg})")) for _, name, unit, lim, avg, _ in pols)
    t["plausible_list"] = grouped((name, caps[p], (unit, "()")) for p, name, unit, *_ in pols)


def main():
    con = duckdb.connect(str(ROOT / "data" / "aq.duckdb"), read_only=True)
    t: dict[str, str] = {}
    source(t, con)
    costs(t, con)
    findings(t, con)
    spikes(t, con)
    proof(t)
    guide(t, con)
    con.close()
    for tpl, out in ((DOCS / "case-study.template.html", DOCS / "case-study.html"),
                     (DOCS / "how-it-works.template.html", DOCS / "how-it-works.html"),
                     (DOCS / "readme.template.md", ROOT / "README.md")):
        text = tpl.read_text(encoding="utf-8")
        missing = sorted(set(re.findall(r"\{\{(\w+)\}\}", text)) - set(t))
        if missing:
            raise SystemExit(f"{tpl.name}: unfilled tokens {missing}")
        out.write_text(re.sub(r"\{\{(\w+)\}\}", lambda m: t[m.group(1)], text), encoding="utf-8", newline="\n")
        print(f"wrote {out.relative_to(ROOT)}")
    print(f"{len(t)} figures from the warehouse and the build logs")
    if "--show" in sys.argv:
        for k, v in t.items():
            if "\n" not in v:
                print(f"  {k:18s} {v}")


if __name__ == "__main__":
    main()
