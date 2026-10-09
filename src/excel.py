"""Excel layer: one workbook built from the warehouse marts.

Sheets
  Summary        source, coverage, findings (recomputed from the warehouse), data-quality totals
  AQI matrix     month x city mean AQI, coloured by CPCB AQI category; blank where there is no data
  PM2.5 matrix   month x city mean PM2.5, colour scale; NAAQS 24-hour limit shown in the header
  Data sheets    Excel Tables (named, typed, filterable) ready for PivotTables or Power Query

Native PivotTables: XlsxWriter cannot write them and openpyxl cannot create them (it only keeps existing ones),
and there is no Excel on this machine to drive through COM. The data sheets are formatted Excel Tables so a
PivotTable is two clicks away (Insert > PivotTable > table name).

The workbook is reproducible: fixed document dates and sorted rows give byte-identical files on rebuild.
"""
import datetime as dt
import sys
from pathlib import Path

import duckdb
import xlsxwriter

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
import findings  # noqa: E402

DB = config.ROOT / "data" / "aq.duckdb"
OUT = config.ROOT / "excel" / "india-air-quality.xlsx"
FIXED_DATE = dt.datetime(2026, 10, 9)

# CPCB's published AQI category colours (National Air Quality Index, 2014).
CATEGORY = [("Good", 0, 50, "#00B050"), ("Satisfactory", 51, 100, "#92D050"), ("Moderate", 101, 200, "#FFFF00"),
            ("Poor", 201, 300, "#FF9900"), ("Very Poor", 301, 400, "#FF0000"), ("Severe", 401, 500, "#C00000")]

TABLES = [
    ("City daily", "CityDaily",
     """SELECT a.city, a.date, d.year, d.month, d.month_name, d.season, d.weekday_name, a.aqi, a.category,
               a.prominent_pollutant, a.n_stations,
               p25.value AS pm25, p10.value AS pm10, no2.value AS no2, o3.value AS o3_8h
        FROM mart_city_daily_aqi a JOIN dim_date d USING (date)
        LEFT JOIN mart_city_daily_pollutant p25 ON p25.city = a.city AND p25.date = a.date AND p25.pollutant = 'pm25'
        LEFT JOIN mart_city_daily_pollutant p10 ON p10.city = a.city AND p10.date = a.date AND p10.pollutant = 'pm10'
        LEFT JOIN mart_city_daily_pollutant no2 ON no2.city = a.city AND no2.date = a.date AND no2.pollutant = 'no2'
        LEFT JOIN mart_city_daily_pollutant o3 ON o3.city = a.city AND o3.date = a.date AND o3.pollutant = 'o3'
        ORDER BY a.city, a.date"""),
    ("City month", "CityMonth", "SELECT * FROM mart_city_month ORDER BY city, month_start"),
    ("Exceedance", "Exceedance", "SELECT * FROM mart_exceedance_city_year ORDER BY city, year, pollutant"),
    ("Spike episodes", "SpikeEpisodes", "SELECT * FROM mart_spike_episodes ORDER BY city, start_date"),
    ("Oct-Nov season", "OctNovSeason", "SELECT * FROM mart_octnov_season ORDER BY city, year"),
    ("Hour profile", "HourProfile",
     "SELECT * FROM mart_profile_hourly ORDER BY city, pollutant, season_order, weekday_no, hour"),
    ("Stations", "Stations", "SELECT * FROM dim_station ORDER BY city, station_id"),
    ("Completeness", "Completeness",
     "SELECT * FROM mart_station_completeness ORDER BY city, station_id, pollutant, month_start"),
    ("Data quality", "DataQuality",
     "SELECT * FROM mart_dq_issues ORDER BY level, issue, city, station_id, pollutant NULLS FIRST"),
]


def rows(con, sql):
    rel = con.sql(sql)
    return rel.columns, rel.fetchall()


def build():
    con = duckdb.connect(str(DB), read_only=True)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    wb = xlsxwriter.Workbook(str(OUT), {"default_date_format": "yyyy-mm-dd", "remove_timezone": True})
    wb.set_properties({"title": "India air quality, 2019-2026", "author": "damanjz", "created": FIXED_DATE,
                       "comments": "Built from the OpenAQ archive (CPCB stations) by src/excel.py"})
    wb.set_calc_mode("auto")
    f = {
        "title": wb.add_format({"bold": True, "font_size": 16}),
        "h": wb.add_format({"bold": True, "font_size": 12}),
        "bold": wb.add_format({"bold": True}),
        "wrap": wb.add_format({"text_wrap": True, "valign": "top"}),
        "muted": wb.add_format({"font_color": "#595959", "text_wrap": True, "valign": "top"}),
        "head": wb.add_format({"bold": True, "bottom": 1, "text_wrap": True, "valign": "bottom"}),
        "month": wb.add_format({"num_format": "mmm yyyy"}),
        "int": wb.add_format({"num_format": "0"}),
        "num1": wb.add_format({"num_format": "0.0"}),
        "thousands": wb.add_format({"num_format": "#,##0"}),
    }
    summary(wb, con, f)
    matrix(wb, con, f, "AQI matrix", "mean_aqi", "days_with_aqi",
           "Mean daily AQI by month (CPCB National AQI, city mean of station AQIs)", category=True)
    matrix(wb, con, f, "PM2.5 matrix", "mean_pm25", "days_pm25",
           "Mean daily PM2.5 by month, µg/m³ (NAAQS 24-hour limit: 60; WHO 2021 guideline: 15)", category=False)
    for sheet, name, sql in TABLES:
        table_sheet(wb, con, f, sheet, name, sql)
    wb.close()
    con.close()
    print(f"workbook: {OUT}")


def summary(wb, con, f):
    ws = wb.add_worksheet("Summary")
    ws.hide_gridlines(2)
    ws.set_column(0, 0, 34)
    ws.set_column(1, 1, 64)
    ws.set_column(2, 3, 14)
    ws.write(0, 0, "India air quality, 2019 to September 2026", f["title"])
    facts = findings.coverage(con)
    r = 2
    for k, v in facts:
        ws.write(r, 0, k, f["bold"])
        ws.write(r, 1, v, f["wrap"])
        r += 1
    r += 1
    ws.write(r, 0, "Findings (recomputed from the warehouse on every build)", f["h"])
    r += 1
    for title, text in findings.compute(con):
        ws.write(r, 0, title, f["bold"])
        ws.write(r, 1, text, f["wrap"])
        ws.set_row(r, 15 * max(1, len(text) // 60 + 1))
        r += 1
    r += 1
    ws.write(r, 0, "Data quality (counts per issue)", f["h"])
    r += 1
    for c, h in enumerate(["Issue", "What was done", "Count", "Stations"]):
        ws.write(r, c, h, f["head"])
    r += 1
    for level, issue, action, n, stations in con.sql(
            "SELECT level, issue, action, n, stations FROM mart_dq_summary ORDER BY level, n DESC, issue").fetchall():
        ws.write(r, 0, f"{issue} ({level})", f["wrap"])
        ws.write(r, 1, action, f["wrap"])
        ws.write_number(r, 2, n, f["thousands"])
        ws.write_number(r, 3, stations, f["thousands"])
        r += 1
    r += 1
    ws.write(r, 0, "Method", f["h"])
    r += 1
    for k, v in [
        ("AQI", "CPCB National Air Quality Index (2014): sub-index by linear interpolation in the breakpoint band; "
                "station AQI is the highest sub-index when 3+ pollutants (one of them PM) have a complete day; "
                "city AQI is the mean of station AQIs."),
        ("Complete day", "16 or more valid hours. PM2.5, PM10, NO2, SO2: 24-hour mean. O3, CO: highest 8-hour mean."),
        ("Nothing filled", "Missing hours and days stay missing. Set-aside readings are counted above, never replaced."),
        ("Short spikes", f"Found from the data alone: 1 to {config.SPIKE_MAX_DAYS} days in a row with city PM2.5 at least "
                         f"{config.SPIKE_MIN_RATIO:.1f}x the median of the days {config.SPIKE_GUARD_DAYS + 1} to "
                         f"{config.SPIKE_WINDOW_DAYS} days either side, with evaluated ordinary days before and after. "
                         "No calendar, labels or causes."),
        ("Native PivotTables", "Not generated: XlsxWriter cannot write them and openpyxl cannot create them. Every data "
                               "sheet is an Excel Table, so Insert > PivotTable works on it directly."),
    ]:
        ws.write(r, 0, k, f["bold"])
        ws.write(r, 1, v, f["muted"])
        ws.set_row(r, 15 * max(1, len(v) // 60 + 1))
        r += 1


MIN_DAYS_PER_MONTH = 10  # a month needs this many days with a value to be shown in the matrix


def matrix(wb, con, f, sheet, measure, days, title, category):
    cities = [c for (c,) in con.sql("SELECT city FROM dim_city ORDER BY city_order").fetchall()]
    data = con.sql(f"""PIVOT (SELECT month_start, city,
                              CASE WHEN {days} >= {MIN_DAYS_PER_MONTH} THEN {measure} END AS v FROM mart_city_month)
                       ON city IN ({', '.join(repr(c) for c in cities)}) USING any_value(v)
                       ORDER BY month_start""").fetchall()
    ws = wb.add_worksheet(sheet)
    ws.write(0, 0, title, f["h"])
    ws.write(1, 0, f"Blank cell: fewer than {MIN_DAYS_PER_MONTH} days with a value in that month. The archive has no "
                   "Indian data from November 2022 to mid-February 2025, and many days are missing in January-June 2019 "
                   "and in 2022.",
             f["muted"])
    ws.set_column(0, 0, 12)
    ws.set_column(1, len(cities), 12)
    ws.write(3, 0, "Month", f["head"])
    for c, city in enumerate(cities, 1):
        ws.write(3, c, city, f["head"])
    fmt = f["int"] if category else f["num1"]
    for r, row in enumerate(data, 4):
        ws.write_datetime(r, 0, dt.datetime.combine(row[0], dt.time()), f["month"])
        for c, v in enumerate(row[1:], 1):
            if v is None:
                ws.write_blank(r, c, None)
            else:
                ws.write_number(r, c, v, fmt)
    last = 3 + len(data)
    ws.freeze_panes(4, 1)
    rng = (4, 1, last, len(cities))
    if category:
        for name, lo, hi, colour in CATEGORY:
            font = "#FFFFFF" if name in ("Very Poor", "Severe") else "#000000"
            ws.conditional_format(*rng, {"type": "cell", "criteria": "between", "minimum": lo - 0.5,
                                         "maximum": hi + 0.49, "format": wb.add_format({"bg_color": colour,
                                                                                        "font_color": font})})
        ws.write(1, len(cities) + 2, "CPCB category", f["bold"])
        for i, (name, lo, hi, colour) in enumerate(CATEGORY):
            ws.write(2 + i, len(cities) + 2, f"{name} ({lo}-{hi})",
                     wb.add_format({"bg_color": colour, "font_color": "#FFFFFF" if i >= 4 else "#000000"}))
        ws.set_column(len(cities) + 2, len(cities) + 2, 22)
    else:
        ws.conditional_format(*rng, {"type": "3_color_scale", "min_type": "num", "min_value": 15,
                                     "min_color": "#F2F2F2", "mid_type": "num", "mid_value": 60,
                                     "mid_color": "#FFC000", "max_type": "num", "max_value": 150,
                                     "max_color": "#C00000"})


def table_sheet(wb, con, f, sheet, name, sql):
    cols, data = rows(con, sql)
    ws = wb.add_worksheet(sheet)
    if not data:
        ws.write(0, 0, "No rows.")
        return
    columns = []
    for i, c in enumerate(cols):
        sample = next((r[i] for r in data if r[i] is not None), None)
        if isinstance(sample, (dt.date, dt.datetime)):
            fmt = wb.add_format({"num_format": "yyyy-mm-dd hh:mm" if isinstance(sample, dt.datetime) else "yyyy-mm-dd"})
            width = 16 if isinstance(sample, dt.datetime) else 11
        elif isinstance(sample, float):
            fmt, width = f["num1"], 10
        else:
            fmt, width = None, max(8, min(40, max(len(str(r[i] or "")) for r in data[:2000]) + 2, len(c) + 2))
        columns.append({"header": c, "format": fmt} if fmt else {"header": c})
        ws.set_column(i, i, width)
    clean = [[(dt.datetime.combine(v, dt.time()) if type(v) is dt.date else v) for v in r] for r in data]
    ws.add_table(0, 0, len(clean), len(cols) - 1,
                 {"name": name, "columns": columns, "data": clean, "style": "Table Style Light 1"})
    ws.freeze_panes(1, 0)


if __name__ == "__main__":
    build()
