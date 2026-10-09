"""Prove the Power BI numbers: every measure evaluates, and every number the report shows matches DuckDB.

Needs the report open in its own Power BI window: powerbi/open_pbip.ps1 -Keep (close it with close_pbip.ps1).
Part 1 asks the engine for any measure with an error, then evaluates every measure.
Part 2 reads each figure on the five pages as Power BI shows it (default slicers: all cities, all years, PM2.5 on
page 4, all pollutants on page 5) and recomputes it with independent SQL on the marts in data/aq.duckdb:
KPI cards, every number inside the answer lines (the whole sentence is rebuilt from SQL and compared), heatmap
cells that print a number, the weekday labels and the issue log. Exit code 1 on any error or mismatch.

    python powerbi/check_report.py [--db data/aq.duckdb]
"""
import argparse
import json
import re
import subprocess
import sys
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import duckdb

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import build_pbip as B  # noqa: E402

con = None


# ---------------------------------------------------------------- engines
def dax_rows(query):
    r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(HERE / "query_desktop.ps1"),
                        "-Dax", query], capture_output=True, text=True, encoding="utf-8")
    out = r.stdout.strip()
    if not out or "Exception" in r.stderr:
        raise RuntimeError((r.stderr or "no output").strip().splitlines()[0])
    rows = json.loads(out)
    rows = rows if isinstance(rows, list) else [rows]
    return [{re.sub(r"^.*\[(.*)\]$", r"\1", k): v for k, v in row.items()} for row in rows]


def dax(expr):
    return dax_rows(f'EVALUATE ROW("v", {expr})')[0]["v"]


def sql(q):
    return con.sql(q).fetchall()


def one(q):
    return con.sql(q).fetchone()[0]


# ---------------------------------------------------------------- DAX FORMAT, reproduced
def rnd(x, places=0):
    q = Decimal(1).scaleb(-places)
    return Decimal(repr(float(x))).quantize(q, rounding=ROUND_HALF_UP)


def f0(x):
    return f"{rnd(x):f}"


def f1(x):
    return f"{rnd(x, 1):f}"


def pct(x):
    return f"{rnd(float(x) * 100):f}%"


def thousands(x):
    return f"{int(rnd(x)):,}"


def signed_pct1(x):
    v = rnd(float(x) * 100, 1)
    return ("+" if v > 0 else "-" if v < 0 else "") + f"{abs(v):f}%"


def month_abbr(m):
    return ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][m - 1]


# ---------------------------------------------------------------- report figures vs SQL
def same(a, b):
    if a is None or b is None:
        return a is None and b is None
    if isinstance(b, str) or isinstance(a, str):
        return str(a) == str(b)
    return abs(float(a) - float(b)) <= 1e-9 * max(1.0, abs(float(b)))


def page1():
    out = []
    n, poor = sql("SELECT count(*), count(*) FILTER (aqi > 200) FROM mart_city_daily_aqi")[0]
    by_city = sql("SELECT city, avg(aqi) FROM mart_city_daily_aqi GROUP BY city ORDER BY avg(aqi) DESC, city")
    worst, worst_aqi = by_city[0]
    clean = sorted(by_city, key=lambda r: (r[1], r[0]))[0][0]
    pm = one("""SELECT sum(days_over_limit) / sum(days_measured) FROM mart_exceedance_city_year WHERE pollutant = 'pm25'""")
    out += [("1 KPI worst city", "[Worst city]", worst), ("1 KPI cleanest city", "[Cleanest city]", clean),
            ("1 KPI city-days Poor or worse", "[Poor or worse share]", poor / n),
            ("1 KPI city-days over the PM2.5 limit", "[PM2.5 over limit share]", pm),
            ("1 answer line", "[Answer: how bad]",
             f"{pct(poor / n)} of city-days were Poor or worse (AQI above 200); the worst city is {worst}, "
             f"at a mean AQI of {f0(worst_aqi)}.")]
    cells = {(c, p): v for c, p, v in sql("""
        SELECT e.city, d.pollutant_name, sum(days_over_limit) / sum(days_measured)
        FROM mart_exceedance_city_year e JOIN dim_pollutant d USING (pollutant) GROUP BY ALL""")}
    pbi = {(r["city"], r["pollutant_name"]): r["v"] for r in dax_rows(
        'EVALUATE SUMMARIZECOLUMNS(City[city], Pollutant[pollutant_name], "v", [Over NAAQS share])')}
    out += [(f"1 heatmap {c} {p}", pbi.get((c, p)), v) for (c, p), v in sorted(cells.items())]
    return out


def page2():
    rows = sql("""SELECT d.season, avg(a.aqi) FROM mart_city_daily_aqi a JOIN dim_date d USING (date)
                  GROUP BY d.season""")
    hi = sorted(rows, key=lambda r: (-r[1], r[0]))[0]
    lo = sorted(rows, key=lambda r: (r[1], r[0]))[0]

    def months(season):
        a, b = sql(f"SELECT min(month), max(month) FROM dim_date WHERE season = '{season}'")[0]
        return f"{month_abbr(a)}-{month_abbr(b)}"
    text = (f"{hi[0]} ({months(hi[0])}) is the worst season, at a mean AQI of {f0(hi[1])}, against {f0(lo[1])} "
            f"in the {lo[0].lower()} ({months(lo[0])}).")
    return [("2 answer line", "[Answer: when]", text)]


def page3():
    out = []
    n, med = sql("SELECT count(*), median(peak_ratio) FROM mart_spike_episodes")[0]
    comp = f"sep_days >= {B.MIN_SEP_DAYS} AND octnov_days >= {B.MIN_OCTNOV_DAYS}"
    s = one(f"SELECT avg(octnov_vs_sep) FROM mart_octnov_season WHERE {comp}")
    vp = one("SELECT sum(octnov_days_very_poor_or_worse) / sum(octnov_days_with_aqi) FROM mart_octnov_season")
    out += [("3 KPI short spikes found", "[Spike count]", n),
            ("3 KPI median peak vs surroundings", "[Median spike ratio]", med),
            ("3 KPI Oct-Nov vs Sep", "[Oct-Nov vs Sep]", s),
            ("3 KPI Oct-Nov city-days Very Poor+", "[Oct-Nov Very Poor share]", vp)]
    months = sql("SELECT month, count(*) FROM mart_spike_episodes GROUP BY month")
    m, k = sorted(months, key=lambda r: (-r[1], r[0]))[0] if months else (None, None)
    month_name = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
                  "November", "December"]
    first = (f"{n:,} short spike{'' if n == 1 else 's'}; the most start in {month_name[m - 1]} ({k})" if n
             else "No short spike in this selection")
    text = first + ("." if s is None else f"; Oct-Nov ran {f1(s)}x September.")
    out.append(("3 answer line", "[Answer: spikes]", text))
    # grid: every city x month cell, empty where no spike starts
    counts = {(c, mn): str(v) for c, mn, v in sql("SELECT city, month_name, count(*) FROM mart_spike_episodes GROUP BY ALL")}
    cities = [r[0] for r in sql("SELECT city FROM dim_city")]
    names = [r[0] for r in sql("SELECT DISTINCT month_name FROM dim_date")]
    cells = {(c, mn): counts.get((c, mn), "") for c in cities for mn in names}
    pbi = {(r["city"], r["month_name"]): r["v"] for r in dax_rows(
        "EVALUATE SUMMARIZECOLUMNS(City[city], 'Date'[month_name], \"v\", [Spike cell])")}
    out += [(f"3 grid {c} {mn}", pbi.get((c, mn)), v) for (c, mn), v in sorted(cells.items())]
    out.append(("3 grid cell count", len(pbi), len(cells)))
    # the largest spikes: the same episodes, in the same order, with the same figures
    top = sql(f"""SELECT city, start_date, days, peak_pm25, peak_ratio,
                         stations_at_ratio || ' of ' || stations_on_peak_day FROM mart_spike_episodes
                  ORDER BY peak_ratio DESC, episode_id LIMIT {B.TOP_SPIKES}""")
    rows = dax_rows('EVALUATE ADDCOLUMNS(FILTER(SUMMARIZECOLUMNS(Spikes[city], Spikes[start_date], "rank", [Spike rank], '
                    '"d", [Top spike days], "p", [Top spike peak], "r", [Top spike ratio], "n", [Top spike stations]), '
                    'NOT ISBLANK([r])), '
                    '"s", FORMAT(Spikes[start_date], "yyyy-mm-dd"))')
    rows = sorted(rows, key=lambda r: r["rank"])
    out.append(("3 largest spikes row count", len(rows), len(top)))
    for i, (c, sd, d, pk, rt, st) in enumerate(top):
        got = rows[i] if i < len(rows) else {}
        label = f"3 largest #{i + 1} {c} {sd}"
        out += [(f"{label} city", got.get("city"), c),
                (f"{label} start", got.get("s"), str(sd)),
                (f"{label} days", got.get("d"), d), (f"{label} peak", got.get("p"), pk),
                (f"{label} ratio", got.get("r"), rt), (f"{label} stations", got.get("n"), st)]
    return out


def page4():
    out = []
    ctx = 'Pollutant[pollutant_name] = "PM2.5"'
    unit = one("SELECT unit FROM dim_pollutant WHERE pollutant_name = 'PM2.5'")
    hours = sql("""SELECT hour, sum(mean_value * n_hours) / sum(n_hours) FROM mart_profile_hourly
                   WHERE pollutant = 'pm25' GROUP BY hour""")
    hi = sorted(hours, key=lambda r: (-r[1], r[0]))[0]
    lo = sorted(hours, key=lambda r: (r[1], r[0]))[0]
    fmt = (lambda v: f"{rnd(v, 2):f}") if hi[1] < 10 else f0
    text = (f"PM2.5 peaks at {hi[0]:02d}:00 IST ({fmt(hi[1])} {unit}), {f1(hi[1] / lo[1])} times the cleanest hour, "
            f"{lo[0]:02d}:00.")
    out.append(("4 answer line", f"CALCULATE([Answer: hours], {ctx})", text))
    cells = {(h, w): (f"{rnd(v, 2):f}" if v < 10 else f0(v)) for h, w, v in sql("""
        SELECT hour, weekday_name, sum(mean_value * n_hours) / sum(n_hours) FROM mart_profile_hourly
        WHERE pollutant = 'pm25' GROUP BY ALL""")}
    pbi = {(r["hour"], r["weekday_name"]): r["v"] for r in dax_rows(
        f'EVALUATE SUMMARIZECOLUMNS(Profile[hour], Profile[weekday_name], TREATAS({{"PM2.5"}}, Pollutant[pollutant_name]), '
        f'"v", [Hour cell])')}
    out += [(f"4 heatmap {h:02d}:00 {w}", pbi.get((h, w)), v) for (h, w), v in sorted(cells.items())]
    week = one("SELECT sum(mean_value * n_hours) / sum(n_hours) FROM mart_profile_hourly WHERE pollutant = 'pm25'")
    wd = {w: signed_pct1(v / week - 1) for w, v in sql("""
        SELECT weekday_name, sum(mean_value * n_hours) / sum(n_hours) FROM mart_profile_hourly
        WHERE pollutant = 'pm25' GROUP BY ALL""")}
    pbi = {r["weekday_name"]: r["v"] for r in dax_rows(
        'EVALUATE SUMMARIZECOLUMNS(Profile[weekday_name], TREATAS({"PM2.5"}, Pollutant[pollutant_name]), '
        '"v", FORMAT([Weekday vs week], "+0.0%;-0.0%;0.0%"))')}
    out += [(f"4 weekday label {w}", pbi.get(w), v) for w, v in sorted(wd.items())]
    return out


def page5():
    out = []
    st, valid, possible = sql("""SELECT count(DISTINCT station_id), sum(valid_hours), sum(hours_in_month)
                                 FROM mart_station_completeness""")[0]
    aside = one("SELECT sum(n) FROM mart_dq_issues WHERE level = 'reading' AND action = 'set aside'")
    missing = one("SELECT sum(n) FROM mart_dq_issues WHERE level = 'station-day'")
    out += [("5 KPI stations", "[Stations]", st), ("5 KPI valid station-hours", "[Valid hours text]", thousands(valid)),
            ("5 KPI readings set aside", "[Readings set aside text]", thousands(aside)),
            ("5 KPI station-days missing", "[Days missing text]", thousands(missing)),
            ("5 KPI completeness", "[Completeness]", valid / possible),
            ("5 answer line", "[Answer: trust]",
             f"{pct(valid / possible)} of possible station-hours hold a valid reading; {thousands(aside)} readings were "
             f"set aside, each under a named reason.")]
    log = {(i, a): thousands(n) for i, a, n in sql("SELECT issue, action, sum(n) FROM mart_dq_issues GROUP BY ALL")}
    pbi = {(r["issue"], r["action"]): r["v"] for r in dax_rows(
        'EVALUATE SUMMARIZECOLUMNS(Issues[issue], Issues[action], "v", [Issue count text])')}
    out += [(f"5 issue log {i[:40]}", pbi.get((i, a)), v) for (i, a), v in sorted(log.items())]
    first = sql("SELECT issue FROM mart_dq_issues GROUP BY issue ORDER BY sum(n) DESC, issue LIMIT 1")[0][0]
    out.append(("5 issue log first row", dax_rows(
        'EVALUATE TOPN(1, VALUES(Issues[issue]), CALCULATE(MIN(Issues[issue_rank])), ASC)')[0]["issue"], first))
    cells = {(c, y): pct(v / p) for c, y, v, p in sql("""
        SELECT city, year(month_start), sum(valid_hours), sum(hours_in_month) FROM mart_station_completeness GROUP BY ALL""")}
    pbi = {(r["city"], r["year"]): r["v"] for r in dax_rows(
        'EVALUATE SUMMARIZECOLUMNS(City[city], \'Date\'[year], "v", [Completeness cell])')}
    out += [(f"5 completeness {c} {y}", pbi.get((c, y)), v) for (c, y), v in sorted(cells.items())]
    return out


def main():
    global con
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(B.DB))
    args = ap.parse_args()
    con = duckdb.connect(args.db, read_only=True)

    errors = [r for r in dax_rows('EVALUATE SELECTCOLUMNS(INFO.MEASURES(), "n", [Name], "e", [ErrorMessage])') if r["e"]]
    names = [m[0] for m in B.MEASURES]
    bad = len(errors)
    for r in errors:
        print(f"  ERROR  {r['n']}: {r['e'][:160]}")
    for m in names:
        try:
            dax(f"[{m}]")
        except Exception as e:  # noqa: BLE001
            bad += 1
            print(f"  ERROR  {m}: {e}")
    print(f"Part 1: {len(names) - bad} of {len(names)} measures evaluate without error")

    cases = page1() + page2() + page3() + page4() + page5()
    print(f"Part 2: {len(cases)} report figures vs DuckDB")
    diffs = 0
    for name, d, expected in cases:
        got = dax(d) if isinstance(d, str) and d.startswith(("[", "CALCULATE(")) else d
        ok = same(got, expected)
        diffs += not ok
        if not ok or name.split()[1] in ("KPI", "answer"):
            print(f"  {'ok  ' if ok else 'DIFF'}  {name:44s} Power BI {str(got)[:120]!s}")
            if not ok:
                print(f"        {'':44s} DuckDB   {str(expected)[:120]}")
    print(f"  {len(cases) - diffs} of {len(cases)} match")
    sys.exit(1 if bad or diffs else 0)


if __name__ == "__main__":
    main()
