"""Headline findings, recomputed from the warehouse on every build. Used by the workbook and docs/findings.md.

    python src/findings.py      prints the findings and rewrites docs/findings.md
"""
import sys
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402

DB = config.ROOT / "data" / "aq.duckdb"


def coverage(con):
    s = con.sql("""SELECT count(*) AS stations, count(DISTINCT city) AS cities, sum(n_location_ids) AS ids,
                          min(first_hour), max(last_hour) FROM dim_station""").fetchone()
    r = con.sql("""SELECT (SELECT sum(n_rows) FROM raw_file_rows), (SELECT count(*) FROM stg_readings),
                          (SELECT count(*) FROM stg_readings WHERE disposition = 'valid'),
                          (SELECT count(*) FROM fact_measurement_hourly),
                          (SELECT count(*) FILTER (is_complete) FROM fact_measurement_daily),
                          (SELECT count(*) FROM mart_city_daily_aqi)""").fetchone()
    return [
        ("Source", "OpenAQ archive on AWS Open Data (s3://openaq-data-archive), anonymous access; the data are "
                   "CPCB / state pollution control board continuous monitoring stations (CAAQMS)."),
        ("Window", f"{config.WINDOW_START} to {config.WINDOW_END}; first valid hour {s[3]:%Y-%m-%d %H:%M}, "
                   f"last {s[4]:%Y-%m-%d %H:%M} IST"),
        ("Coverage", f"{s[0]} stations ({s[2]} OpenAQ location ids) in {s[1]} cities"),
        ("Rows", f"{r[0]:,} raw rows; {r[1]:,} AQI-pollutant readings; {r[2]:,} valid; {r[3]:,} station-hours; "
                 f"{r[4]:,} complete station-days; {r[5]:,} city-days with an AQI"),
        ("Known gap", "The archive holds no Indian station data from November 2022 to mid-February 2025, and many days "
                      "are missing in January-June 2019 and in 2022. Those days are blank, never estimated."),
    ]


def compute(con):
    out = []

    worst = con.sql("""SELECT city, strftime(month_start, '%b %Y'), mean_aqi, days_with_aqi, days_severe + days_very_poor
                       FROM mart_city_month WHERE days_with_aqi >= 15 ORDER BY mean_aqi DESC LIMIT 3""").fetchall()
    best_non_delhi = con.sql("""SELECT city, strftime(month_start, '%b %Y'), mean_aqi FROM mart_city_month
                                WHERE days_with_aqi >= 15 AND city <> 'Delhi' ORDER BY mean_aqi DESC LIMIT 1""").fetchone()
    out.append(("Worst city-months", "; ".join(
        f"{c} {m}: mean AQI {a:.0f} ({vp} of {d} days Very Poor or Severe)" for c, m, a, d, vp in worst)
        + f". Worst outside Delhi: {best_non_delhi[0]} {best_non_delhi[1]} at {best_non_delhi[2]:.0f}."
        + " (Months with 15+ days of city AQI.)"))

    rule = con.sql("SELECT min_ratio, max_days, guard_days, window_days FROM ref_spike_rule").fetchone()
    ep = con.sql("""SELECT city, count(*), median(peak_ratio), arg_max(month_name, (n_month, -month)), max(n_month)
                    FROM (SELECT *, count(*) OVER (PARTITION BY city, month) AS n_month FROM mart_spike_episodes)
                    GROUP BY city ORDER BY count(*) DESC, city""").fetchall()
    if ep:
        total, evaluated = con.sql("""SELECT (SELECT count(*) FROM mart_spike_episodes),
                                             (SELECT count(*) FILTER (is_evaluated) FROM mart_spike_days)""").fetchone()
        months = con.sql("""SELECT d.month_name, count(e.episode_id) AS n, count(DISTINCT (d.city, d.date)) AS days,
                                   100.0 * count(e.episode_id) / count(DISTINCT (d.city, d.date)) AS rate
                            FROM (SELECT s.city, s.date, strftime(s.date, '%b') AS month_name, month(s.date) AS month
                                  FROM mart_spike_days s WHERE s.is_evaluated) d
                            LEFT JOIN mart_spike_episodes e ON e.city = d.city AND e.start_date = d.date
                            GROUP BY d.month_name, d.month ORDER BY rate DESC, d.month""").fetchall()
        top = con.sql("""SELECT city, strftime(start_date, '%d %b %Y'), days, peak_pm25, peak_ratio FROM mart_spike_episodes
                         ORDER BY peak_ratio DESC, episode_id LIMIT 1""").fetchone()
        single = con.sql("""SELECT count(*) FILTER (stations_at_ratio = 1 AND stations_on_peak_day > 1),
                                   count(*) FILTER (stations_on_peak_day = 1)
                            FROM mart_spike_episodes""").fetchone()
        top_breadth = con.sql("""SELECT stations_at_ratio, stations_on_peak_day FROM mart_spike_episodes
                                 ORDER BY peak_ratio DESC, episode_id LIMIT 1""").fetchone()
        dropped = con.sql("""SELECT outcome, count(*) FROM mart_spike_runs WHERE outcome <> 'short spike'
                             GROUP BY ALL ORDER BY outcome""").fetchall()
        text = (f"Runs of 1-{rule[1]} days with city PM2.5 at least {rule[0]:.1f}x the median of the days {rule[2] + 1}-{rule[3]} "
                f"days either side, with evaluated ordinary days before and after: {total} episodes in {evaluated:,} "
                f"evaluated city-days. " + "; ".join(
                    f"{c} {n} (median peak {r:.2f}x; most often in {m}, {k})" for c, n, r, m, k in ep) + ". "
                f"Per 100 evaluated city-days they start most often in {months[0][0]} ({months[0][3]:.1f}; "
                f"{months[0][1]} of {months[0][2]:,} days) and {months[1][0]} ({months[1][3]:.1f}), least often in "
                f"{months[-1][0]} ({months[-1][3]:.1f}). Largest: {top[0]} from {top[1]}, {top[2]} day"
                f"{'' if top[2] == 1 else 's'}, peak {top[3]:.0f} µg/m³, {top[4]:.2f}x its surroundings, with "
                f"{top_breadth[0]} of {top_breadth[1]} stations at {rule[0]:.1f}x or more on the peak day. A city value is a "
                f"mean of stations: in {single[0]} episodes one station of several carries the peak day, and "
                f"{single[1]} rest on a day with a single station reporting. Not counted: "
                + ", ".join(f"{n} run{'' if n == 1 else 's'} {o}" for o, n in dropped) + ".")
        out.append(("Short PM2.5 spikes", text))

    hyd = con.sql("""SELECT d.season, round(avg(p.value), 1), count(*)
                     FROM mart_city_daily_pollutant p JOIN dim_date d USING (date)
                     WHERE p.city = 'Hyderabad' AND p.pollutant = 'pm25'
                     GROUP BY d.season, d.season_order ORDER BY d.season_order""").fetchall()
    if hyd:
        by = {s: v for s, v, _ in hyd}
        text = "Mean daily PM2.5: " + ", ".join(f"{s} {v:.1f} ({n:,} days)" for s, v, n in hyd) + " µg/m³."
        hi, lo = max(by, key=by.get), min(by, key=by.get)
        text += f" Worst season: {hi}, {by[hi] / by[lo]:.1f}x the cleanest ({lo})."
        out.append(("Hyderabad by season", text))

    hrs = con.sql("""WITH h AS (SELECT city, hour, avg(mean_value * n_hours) / avg(n_hours) AS v
                                FROM mart_profile_hourly WHERE pollutant = 'pm25' GROUP BY ALL)
                     SELECT city, arg_max(hour, v), round(max(v), 1), arg_min(hour, v), round(min(v), 1),
                            round(max(v) / min(v), 2)
                     FROM h GROUP BY city ORDER BY max(v) / min(v) DESC""").fetchall()
    out.append(("Worst hour of day", "Hour of the day with the highest mean PM2.5 (all seasons): " + "; ".join(
        f"{c} {hi:02d}:00 ({vhi:.0f}) vs {lo:02d}:00 ({vlo:.0f}), {r:.2f}x" for c, hi, vhi, lo, vlo, r in hrs)
        + ". Hours are IST hour-starting."))

    st = con.sql("""SELECT year, sep_pm25, octnov_pm25, octnov_vs_sep, octnov_days
                    FROM mart_octnov_season WHERE city = 'Delhi' AND sep_days >= 15 AND octnov_days >= 40
                    ORDER BY year""").fetchall()
    if st:
        out.append(("Delhi in October-November", "Delhi PM2.5, October-November against September (years with 15+ "
                    "September and 40+ Oct-Nov days): " + "; ".join(
                        f"{y}: {s:.0f} to {o:.0f} µg/m³ ({r:.1f}x)" for y, s, o, r, _ in st)
                    + ". Levels only: the dataset has no emissions or weather data, so no cause is attributed."))

    ex = con.sql("""SELECT city, sum(days_over_limit), sum(days_measured),
                           round(100.0 * sum(days_over_limit) / sum(days_measured), 1)
                    FROM mart_exceedance_city_year WHERE pollutant = 'pm25'
                    GROUP BY city ORDER BY 4 DESC""").fetchall()
    out.append(("Days over the PM2.5 standard", "City-days with mean PM2.5 above the 24-hour NAAQS (60 µg/m³): "
                + "; ".join(f"{c} {p:.1f}% ({o:,} of {n:,})" for c, o, n, p in ex) + "."))
    return out


def main():
    con = duckdb.connect(str(DB), read_only=True)
    cov, fnd = coverage(con), compute(con)
    con.close()
    lines = ["# Findings", "", "Generated by `src/findings.py` from `data/aq.duckdb`; every number is recomputed on "
             "each build.", "", "| | |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in cov]
    lines += ["", "| Finding | Numbers |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in fnd]
    (config.ROOT / "docs" / "findings.md").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    for k, v in fnd:
        print(f"{k}: {v}\n")


if __name__ == "__main__":
    main()
