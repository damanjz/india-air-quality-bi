"""Generate the Power BI project (PBIP): TMDL semantic model + PBIR report.

The model imports the CSVs that src/pipeline.py exports to data/model. Column types come from the
warehouse (data/aq.duckdb), so re-running the pipeline and this script keeps the two in sync.
Colours that follow data members (cities, seasons, AQI categories) and the heatmap geometry are
read from the warehouse at build time; nothing in a sentence on the report is typed in by hand.

Layout C, "Question rail": a left rail of five questions; each page opens with its one-sentence answer.
  1 How bad    days by AQI category per city; share of days over the NAAQS, city x pollutant
  2 When       mean AQI city x month; by season; typical year
  3 Spikes     short PM2.5 spikes found from the data: city x start month, the largest; October-November against
               September and December
  4 Hours      hour x weekday; hourly profile by season; weekday against the week
  5 Trust      completeness city x year; issue log with the action taken

    python powerbi/build_pbip.py                 build from data/aq.duckdb and data/model
    python powerbi/build_pbip.py --db X --data Y  build from another warehouse / CSV folder
"""
import argparse
import hashlib
import json
import math
import shutil
import uuid
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "powerbi"
NAME = "AirQuality"
SM = OUT / f"{NAME}.SemanticModel"
RP = OUT / f"{NAME}.Report"
DB = ROOT / "data" / "aq.duckdb"
DATA_FOLDER = str(ROOT / "data" / "model") + "\\"

SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition"

# ---------------------------------------------------------------- palette (Editorial ledger, as Finance BI, Healthcare Ops, SaaS)
INK, OCHRE, TEAL, RUST, PLUM = "#2f55b0", "#c27a1a", "#1f8f7a", "#c0533a", "#8456b0"
PAPER, RAIL, RULE = "#fbfaf7", "#f2efe7", "#e3ded2"
TEXT, MUTED, SLATE = "#1d2330", "#5b6170", "#5e6676"
SERIF, SANS = "Georgia", "Segoe UI"
RAMP = ["#f2efe7", "#dfe2ea", "#c6cee2", "#a6b5d8", "#8299cb", "#5d7cbe", "#3f62b4", "#2a4a96"]
LOSS_RAMP = ["#f2efe7", "#f0e0d6", "#ebcbbb", "#e3b09a", "#d99478", "#cd7558", "#c0533a", "#9c3f2a"]
# CPCB AQI categories in order (Good .. Severe): CPCB's green-to-maroon sequence, in the ledger's own tones
AQI_SCALE = ["#2a8f7b", "#9cc5b5", "#e6d3a3", "#d39a4a", "#c0533a", "#7d2e22"]
CITY_SCALE = [RUST, INK, PLUM, TEAL, OCHRE, SLATE]           # in dim_city order
SEASON_SCALE = [INK, OCHRE, TEAL, RUST]                      # in IMD season order
PERIOD_NAMES = ["September", "Oct-Nov", "December"]

# Rules the report applies on top of the marts (stated on the report where they matter)
MIN_MONTH_DAYS = 15        # a city-month is coloured only with 15+ days of city AQI (as docs/findings.md)
MIN_SEP_DAYS = 15          # Oct-Nov vs September: years with 15+ September days ...
MIN_OCTNOV_DAYS = 40       # ... and 40+ October-November days (as docs/findings.md)

# ---------------------------------------------------------------- model
TABLES = {  # model table -> (export name, visible columns)
    "City": ("dim_city", ["city"]),
    "Date": ("dim_date", ["date", "year", "month_name", "season"]),
    "Pollutant": ("dim_pollutant", ["pollutant_name", "unit"]),
    "City AQI": ("mart_city_daily_aqi", ["category"]),
    "Exceedance": ("mart_exceedance_city_year", []),
    "Spikes": ("mart_spike_episodes", []),
    "Oct-Nov": ("mart_octnov_season", []),
    "Profile": ("mart_profile_hourly", ["weekday_name", "hour"]),
    "Completeness": ("mart_station_completeness", []),
    "Issues": ("mart_dq_issues", ["issue", "action"]),
}
SORT_BY = {("City", "city"): "city_order", ("Date", "month_name"): "month", ("Date", "month_initial"): "month",
           ("Pollutant", "pollutant_name"): "pollutant_order", ("City AQI", "category"): "category_order",
           ("Profile", "weekday_name"): "weekday_no", ("Season", "season"): "season_order",
           ("Oct-Nov periods", "period"): "period_order",
           ("Issues", "issue"): "issue_rank"}
RELATIONSHIPS = [
    ("Date", "year", "Year", "year"), ("Date", "season", "Season", "season"),
    ("City AQI", "city", "City", "city"), ("City AQI", "date", "Date", "date"),
    ("Exceedance", "city", "City", "city"), ("Exceedance", "year", "Year", "year"),
    ("Exceedance", "pollutant", "Pollutant", "pollutant"),
    ("Spikes", "city", "City", "city"), ("Spikes", "start_date", "Date", "date"),
    ("Oct-Nov", "city", "City", "city"), ("Oct-Nov", "year", "Year", "year"),
    ("Oct-Nov periods", "city", "City", "city"), ("Oct-Nov periods", "year", "Year", "year"),
    ("Profile", "city", "City", "city"), ("Profile", "pollutant", "Pollutant", "pollutant"),
    ("Profile", "season", "Season", "season"),
    ("Completeness", "city", "City", "city"), ("Completeness", "pollutant", "Pollutant", "pollutant"),
    ("Completeness", "month_start", "Date", "date"),
    # Issues has rows without a pollutant (missing days, station facts): it is not related to Pollutant,
    # so the Pollutant slicer shows no "(Blank)"; measures apply the pollutant filter with TREATAS.
    ("Issues", "city", "City", "city"),
]

PCT = "0%"
RATIO = '0.0"x"'


def ramp_dax(value, lo, hi, colors=RAMP):
    """DAX: pick a colour by where value sits between lo and hi (lo and hi may be DAX expressions)."""
    cases = ", ".join(f'i = {i}, "{c}"' for i, c in enumerate(colors))
    return f"""
        VAR i = IF ( ISBLANK ( {value} ), BLANK (), INT ( MIN ( {len(colors) - 1}, MAX ( 0, DIVIDE ( {value} - {lo}, {hi} - {lo} ) * {len(colors)} ) ) ) )
        RETURN SWITCH ( TRUE (), ISBLANK ( i ), "{PAPER}", {cases} )"""


def text_on(value, lo, hi, colors):
    """DAX: light text on the two darkest ramp steps, so every number stays readable."""
    cut = len(colors) - 2
    return f"""
        VAR i = IF ( ISBLANK ( {value} ), BLANK (), INT ( MIN ( {len(colors) - 1}, MAX ( 0, DIVIDE ( {value} - {lo}, {hi} - {lo} ) * {len(colors)} ) ) ) )
        RETURN IF ( NOT ISBLANK ( i ) && i >= {cut}, "{PAPER}", "{TEXT}" )"""


def aqi_colour(value):
    """DAX: the CPCB category colour of an AQI value (bands as in sql/03_marts.sql aqi_category)."""
    c = AQI_SCALE
    return f"""
        VAR a = {value}
        RETURN SWITCH ( TRUE (), ISBLANK ( a ), "{PAPER}", a <= 50, "{c[0]}", a <= 100, "{c[1]}", a <= 200, "{c[2]}",
                        a <= 300, "{c[3]}", a <= 400, "{c[4]}", "{c[5]}" )"""


HOUR_CELLS = """CALCULATETABLE (
                ADDCOLUMNS ( SUMMARIZE ( Profile, Profile[hour], Profile[weekday_no] ), "@v", [Mean concentration] ),
                ALLSELECTED ( Profile ) )"""

MEASURES = [
    # ---------------- 1 How bad
    ("City-days", "COUNTROWS ( 'City AQI' )", "#,0", "City-days with a CPCB AQI."),
    ("Mean AQI", "AVERAGE ( 'City AQI'[aqi] )", "0", "Mean of the daily city AQI."),
    ("Poor or worse days", "CALCULATE ( COUNTROWS ( 'City AQI' ), 'City AQI'[aqi] > 200 )", "#,0",
     "City-days with AQI above 200 (Poor, Very Poor or Severe)."),
    ("Poor or worse share", "DIVIDE ( [Poor or worse days], [City-days] )", PCT,
     "Share of city-days with AQI above 200."),
    ("Worst city", """
        VAR t = FILTER ( ADDCOLUMNS ( VALUES ( City[city] ), "@a", [Mean AQI] ), NOT ISBLANK ( [@a] ) )
        RETURN MAXX ( TOPN ( 1, t, [@a], DESC, City[city], ASC ), City[city] )""", None,
     "City with the highest mean AQI in the selection."),
    ("Worst city AQI", """
        VAR t = FILTER ( ADDCOLUMNS ( VALUES ( City[city] ), "@a", [Mean AQI] ), NOT ISBLANK ( [@a] ) )
        RETURN MAXX ( t, [@a] )""", "0", "Mean AQI of the worst city."),
    ("Cleanest city", """
        VAR t = FILTER ( ADDCOLUMNS ( VALUES ( City[city] ), "@a", [Mean AQI] ), NOT ISBLANK ( [@a] ) )
        RETURN MAXX ( TOPN ( 1, t, [@a], ASC, City[city], ASC ), City[city] )""", None,
     "City with the lowest mean AQI in the selection."),
    ("Days by category", "[City-days]", "#,0", "City-days in each AQI category (chart label)."),
    ("PM2.5 over limit share", """
        DIVIDE (
            CALCULATE ( SUM ( Exceedance[days_over_limit] ), Exceedance[pollutant] = "pm25" ),
            CALCULATE ( SUM ( Exceedance[days_measured] ), Exceedance[pollutant] = "pm25" )
        )""", PCT, "Share of city-days with mean PM2.5 above the 24-hour NAAQS (60 µg/m³)."),
    ("Over NAAQS share", "DIVIDE ( SUM ( Exceedance[days_over_limit] ), SUM ( Exceedance[days_measured] ) )", PCT,
     "Share of measured city-days over the pollutant's NAAQS (24-hour; 8-hour for O3 and CO)."),
    ("Over NAAQS colour", ramp_dax("[Over NAAQS share]", 0, 0.8, LOSS_RAMP), None, "Heatmap colour: darker = more days over."),
    ("Over NAAQS text", text_on("[Over NAAQS share]", 0, 0.8, LOSS_RAMP), None, "Light text on the darkest cells."),
    ("Answer: how bad", """
        VAR n = [City-days]
        VAR share = FORMAT ( [Poor or worse share], "0%" )
        RETURN
            SWITCH (
                TRUE (),
                ISBLANK ( n ), "No city-day in this selection has an AQI.",
                HASONEVALUE ( City[city] ),
                    share & " of days in " & VALUES ( City[city] ) & " were Poor or worse (AQI above 200); its mean AQI is "
                        & FORMAT ( [Mean AQI], "0" ) & ".",
                share & " of city-days were Poor or worse (AQI above 200); the worst city is " & [Worst city]
                    & ", at a mean AQI of " & FORMAT ( [Worst city AQI], "0" ) & "."
            )""", None, "One-sentence answer of page 1."),

    # ---------------- 2 When
    ("Month AQI", f"IF ( [City-days] >= {MIN_MONTH_DAYS}, [Mean AQI] )", "0",
     f"Mean AQI of a city-month with {MIN_MONTH_DAYS} or more days of AQI."),
    ("Month AQI colour", aqi_colour("[Month AQI]"), None, "Heatmap colour: the CPCB category of the month's mean AQI."),
    ("Answer: when", """
        VAR t = FILTER ( ADDCOLUMNS ( VALUES ( Season[season] ), "@a", [Mean AQI] ), NOT ISBLANK ( [@a] ) )
        VAR hi = TOPN ( 1, t, [@a], DESC, Season[season], ASC )
        VAR lo = TOPN ( 1, t, [@a], ASC, Season[season], ASC )
        VAR hiName = MAXX ( hi, Season[season] )
        VAR loName = MAXX ( lo, Season[season] )
        VAR hiMonths = CALCULATE ( FORMAT ( DATE ( 2000, MIN ( 'Date'[month] ), 1 ), "mmm" ) & "-" & FORMAT ( DATE ( 2000, MAX ( 'Date'[month] ), 1 ), "mmm" ), REMOVEFILTERS ( 'Date' ), 'Date'[season] = hiName )
        VAR loMonths = CALCULATE ( FORMAT ( DATE ( 2000, MIN ( 'Date'[month] ), 1 ), "mmm" ) & "-" & FORMAT ( DATE ( 2000, MAX ( 'Date'[month] ), 1 ), "mmm" ), REMOVEFILTERS ( 'Date' ), 'Date'[season] = loName )
        RETURN
            IF (
                COUNTROWS ( t ) < 2, "Too few seasons with data in this selection to compare.",
                hiName & " (" & hiMonths & ") is the worst season, at a mean AQI of " & FORMAT ( MAXX ( hi, [@a] ), "0" )
                    & ", against " & FORMAT ( MAXX ( lo, [@a] ), "0" ) & " in the " & LOWER ( loName ) & " (" & loMonths & ")."
            )""", None, "One-sentence answer of page 2."),

    # ---------------- 3 Spikes
    ("Spike episodes", "COUNTROWS ( Spikes )", "#,0", "Short PM2.5 spikes (episodes) in the selection."),
    ("Spike count", "[Spike episodes] + 0", "#,0", "Short PM2.5 spikes in the selection, zero when none."),
    ("Spike cell", 'VAR n = [Spike episodes] RETURN IF ( ISBLANK ( n ), "", FORMAT ( n, "0" ) )', None,
     "Spikes starting in the city-month as the grid shows them; empty when none."),
    ("Spike colour", "SPIKE_COLOUR", None, "Grid colour: darker = more spikes."),
    ("Spike text", "SPIKE_TEXT", None, "Light text on the darkest cells."),
    ("Median spike ratio", "MEDIAN ( Spikes[peak_ratio] )", RATIO,
     "Median of the spikes' peak PM2.5 over the median of their surrounding days."),
    ("Spike rank", """
        VAR ep = SELECTEDVALUE ( Spikes[episode_id] )
        VAR r = SELECTEDVALUE ( Spikes[peak_ratio] )
        RETURN
            IF ( NOT ISBLANK ( ep ),
                 COUNTROWS ( FILTER ( ALLSELECTED ( Spikes ),
                     Spikes[peak_ratio] > r || ( Spikes[peak_ratio] = r && Spikes[episode_id] < ep ) ) ) + 1 )""", "0",
     "Rank of a spike by its peak against the surroundings, biggest first, within the selection."),
    ("Top spike days", "IF ( [Spike rank] <= TOP_SPIKES, SUM ( Spikes[days] ) )", "0", "Days in the spike (largest spikes only)."),
    ("Top spike peak", "IF ( [Spike rank] <= TOP_SPIKES, SUM ( Spikes[peak_pm25] ) )", "0",
     "Highest city daily PM2.5 in the spike, µg/m³ (largest spikes only)."),
    ("Top spike ratio", "IF ( [Spike rank] <= TOP_SPIKES, SUM ( Spikes[peak_ratio] ) )", RATIO,
     "Peak over the median of the surrounding days (largest spikes only)."),
    ("Top spike stations", """
        IF ( [Spike rank] <= TOP_SPIKES,
             FORMAT ( SUM ( Spikes[stations_at_ratio] ), "0" ) & " of " & FORMAT ( SUM ( Spikes[stations_on_peak_day] ), "0" ) )""",
     None, "Stations at the spike ratio or more on the peak day, of the stations measured (largest spikes only)."),
    ("Oct-Nov vs Sep", "AVERAGEX ( FILTER ( 'Oct-Nov', 'Oct-Nov'[is_comparable] ), 'Oct-Nov'[octnov_vs_sep] )", RATIO,
     f"October-November PM2.5 over September PM2.5, averaged over city-years with {MIN_SEP_DAYS}+ September and "
     f"{MIN_OCTNOV_DAYS}+ Oct-Nov days."),
    ("Oct-Nov comparable years", "COUNTROWS ( FILTER ( 'Oct-Nov', 'Oct-Nov'[is_comparable] ) ) + 0", "0",
     "City-years in the Oct-Nov vs September comparison."),
    ("Oct-Nov Very Poor share", """
        DIVIDE ( SUM ( 'Oct-Nov'[octnov_days_very_poor_or_worse] ), SUM ( 'Oct-Nov'[octnov_days_with_aqi] ) )""", PCT,
     "Share of October-November city-days with AQI above 300 (Very Poor or Severe)."),
    ("Period PM2.5", "AVERAGEX ( FILTER ( 'Oct-Nov periods', 'Oct-Nov periods'[is_comparable] ), 'Oct-Nov periods'[pm25] )",
     "0", "Mean PM2.5 of the period, averaged over the comparable city-years."),
    ("Answer: spikes", """
        VAR n = [Spike episodes]
        VAR t = FILTER ( ADDCOLUMNS ( VALUES ( 'Date'[month] ), "@n", [Spike episodes] ), NOT ISBLANK ( [@n] ) )
        VAR best = TOPN ( 1, t, [@n], DESC, 'Date'[month], ASC )
        VAR m = MAXX ( best, 'Date'[month] )
        VAR k = MAXX ( best, [@n] )
        VAR s = [Oct-Nov vs Sep]
        VAR first =
            IF ( ISBLANK ( n ), "No short spike in this selection",
                 FORMAT ( n, "#,0" ) & IF ( n = 1, " short spike", " short spikes" ) & "; the most start in "
                     & FORMAT ( DATE ( 2000, m, 1 ), "mmmm" ) & " (" & FORMAT ( k, "0" ) & ")" )
        RETURN
            first & IF ( ISBLANK ( s ), ".", "; Oct-Nov ran " & FORMAT ( s, "0.0" ) & "x September." )""",
     None, "One-sentence answer of page 3."),

    # ---------------- 4 Hours
    ("Mean concentration", "DIVIDE ( SUMX ( Profile, Profile[mean_value] * Profile[n_hours] ), SUM ( Profile[n_hours] ) )",
     "0", "Mean concentration over the hours in context (weighted by hours measured)."),
    ("Hour cell", """
        VAR v = [Mean concentration]
        RETURN IF ( NOT ISBLANK ( v ), FORMAT ( v, IF ( v < 10, "0.00", "0" ) ) )""", None,
     "Mean concentration as the heatmap shows it."),
    ("Hour colour", f"""
        VAR grid = {HOUR_CELLS}
        VAR lo = MINX ( grid, [@v] )
        VAR hi = MAXX ( grid, [@v] ) + 0.000001
        {ramp_dax("[Mean concentration]", "lo", "hi").strip()}""", None,
     "Heatmap colour, light = cleanest hour in the selection, dark = dirtiest."),
    ("Hour text", f"""
        VAR grid = {HOUR_CELLS}
        VAR lo = MINX ( grid, [@v] )
        VAR hi = MAXX ( grid, [@v] ) + 0.000001
        {text_on("[Mean concentration]", "lo", "hi", RAMP).strip()}""", None, "Light text on the darkest cells."),
    ("Weekday vs week", """
        DIVIDE ( [Mean concentration], CALCULATE ( [Mean concentration], REMOVEFILTERS ( Profile[weekday_name], Profile[weekday_no] ) ) ) - 1""",
     "+0.0%;-0.0%;0.0%", "Weekday mean against the mean of the whole week."),
    ("Peak hour", """
        VAR t = FILTER ( ADDCOLUMNS ( VALUES ( Profile[hour] ), "@v", [Mean concentration] ), NOT ISBLANK ( [@v] ) )
        RETURN MAXX ( TOPN ( 1, t, [@v], DESC, Profile[hour], ASC ), Profile[hour] )""", "0", "Hour of day (IST) with the highest mean."),
    ("Cleanest hour", """
        VAR t = FILTER ( ADDCOLUMNS ( VALUES ( Profile[hour] ), "@v", [Mean concentration] ), NOT ISBLANK ( [@v] ) )
        RETURN MAXX ( TOPN ( 1, t, [@v], ASC, Profile[hour], ASC ), Profile[hour] )""", "0", "Hour of day (IST) with the lowest mean."),
    ("Peak vs cleanest hour", """
        VAR t = FILTER ( ADDCOLUMNS ( VALUES ( Profile[hour] ), "@v", [Mean concentration] ), NOT ISBLANK ( [@v] ) )
        RETURN DIVIDE ( MAXX ( t, [@v] ), MINX ( t, [@v] ) )""", RATIO, "Peak hour mean over the cleanest hour mean."),
    ("Answer: hours", """
        VAR p = SELECTEDVALUE ( Pollutant[pollutant_name] )
        VAR u = SELECTEDVALUE ( Pollutant[unit] )
        VAR t = FILTER ( ADDCOLUMNS ( VALUES ( Profile[hour] ), "@v", [Mean concentration] ), NOT ISBLANK ( [@v] ) )
        VAR hi = MAXX ( t, [@v] )
        VAR fmt = IF ( hi < 10, "0.00", "0" )
        RETURN
            SWITCH (
                TRUE (),
                ISBLANK ( p ), "Pick one pollutant to compare the hours of the day.",
                ISBLANK ( hi ), "No hourly " & p & " readings in this selection.",
                p & " peaks at " & FORMAT ( [Peak hour], "00" ) & ":00 IST (" & FORMAT ( hi, fmt ) & " " & u & "), "
                    & FORMAT ( [Peak vs cleanest hour], "0.0" ) & " times the cleanest hour, " & FORMAT ( [Cleanest hour], "00" ) & ":00."
            )""", None, "One-sentence answer of page 4."),

    # ---------------- 5 Trust
    ("Stations", "DISTINCTCOUNT ( Completeness[station_id] )", "0", "Monitoring stations in the selection."),
    ("Valid hours", "SUM ( Completeness[valid_hours] )", "#,0", "Station-pollutant hours with at least one valid reading."),
    ("Possible hours", "SUM ( Completeness[hours_in_month] )", "#,0", "Station-pollutant hours in the study window."),
    ("Completeness", "DIVIDE ( [Valid hours], [Possible hours] )", PCT, "Valid hours over possible hours."),
    ("Completeness cell", 'FORMAT ( [Completeness], "0%" )', None, "Completeness as the heatmap shows it."),
    ("Completeness colour", ramp_dax("[Completeness]", 0, 1.0001), None, "Heatmap colour: paper = no data, ink = complete."),
    ("Completeness text", text_on("[Completeness]", 0, 1.0001, RAMP), None, "Light text on the darkest cells."),
    ("Issue count", """
        IF (
            ISFILTERED ( Pollutant ),
            CALCULATE ( SUM ( Issues[n] ), KEEPFILTERS ( TREATAS ( VALUES ( Pollutant[pollutant] ), Issues[pollutant] ) ) )
                + CALCULATE ( SUM ( Issues[n] ), Issues[pollutant] = BLANK () ),
            SUM ( Issues[n] )
        )""", "#,0", "Count for each issue; with a pollutant picked, its issues plus those that concern every pollutant."),
    ("Readings set aside", 'CALCULATE ( [Issue count], Issues[level] = "reading", Issues[action] = "set aside" ) + 0', "#,0",
     "Archive readings set aside, each with a named reason."),
    ("Days missing", 'CALCULATE ( SUM ( Issues[n] ), Issues[level] = "station-day" ) + 0', "#,0",
     "Station-days in the window with no archive file at all."),
    ("Valid hours text", 'FORMAT ( [Valid hours], "#,0" )', None, "Valid hours, grouped in thousands."),
    ("Readings set aside text", 'FORMAT ( [Readings set aside], "#,0" )', None, "Readings set aside, grouped in thousands."),
    ("Days missing text", 'FORMAT ( [Days missing], "#,0" )', None, "Station-days missing, grouped in thousands."),
    ("Issue count text", 'VAR c = [Issue count] RETURN IF ( NOT ISBLANK ( c ), FORMAT ( c, "#,0" ) )', None,
     "Issue count, grouped in thousands."),
    ("Answer: trust", """
        VAR c = [Completeness]
        RETURN
            IF ( ISBLANK ( c ), "No station in this selection.",
                 FORMAT ( c, "0%" ) & " of possible station-hours hold a valid reading; " & FORMAT ( [Readings set aside], "#,0" )
                     & " readings were set aside, each under a named reason." )""", None,
     "One-sentence answer of page 5."),
]

CALC_TABLES = {"Metrics": ("{ 1 }", [("Value", "int64", "[Value]", True, None)])}

TYPE_MAP = {"DATE": ("dateTime", "type date", "d mmm yyyy"), "TIMESTAMP": ("dateTime", "type datetime", "General Date"),
            "VARCHAR": ("string", "type text", None), "BIGINT": ("int64", "Int64.Type", "0"),
            "INTEGER": ("int64", "Int64.Type", "0"), "HUGEINT": ("int64", "Int64.Type", "0"),
            "DOUBLE": ("double", "type number", "#,0.00"), "FLOAT": ("double", "type number", "#,0.00"),
            "BOOLEAN": ("boolean", "type logical", None)}
COLUMN_FORMATS = {"n": "#,0", "valid_hours": "#,0", "hours_in_month": "#,0", "month_start": "mmm yyyy"}

# set from the warehouse in main()
CITIES: list = []
SEASONS: list = []
CATEGORIES: list = []
MONTH_COLUMNS = 0            # city-months that get a heatmap cell, as columns
YEAR_COLUMNS = 0
ISSUE_ROWS = 0
SPIKE_MAX_CELL = 1           # most spikes in one city-month (top of the grid's colour ramp)
SPIKE_RULE: dict = {}        # ref_spike_rule, as the pipeline set it


def fmt_value(fmt):
    """TMDL needs values containing quotes wrapped in quotes, with inner quotes doubled."""
    return '"' + fmt.replace('"', '""') + '"' if '"' in fmt else fmt


def q(name):
    return f"'{name}'" if any(c in name for c in " .=:'-()") else name


def tmdl_type(duck_type):
    if duck_type.startswith("DECIMAL"):
        return TYPE_MAP["DOUBLE"]
    return TYPE_MAP[duck_type]


def indent(text, tabs):
    lines = text.strip("\n").splitlines()
    pad = min(len(l) - len(l.lstrip()) for l in lines if l.strip())
    return "\n".join("\t" * tabs + l[pad:] for l in lines)


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def write_json(path, obj):
    write(path, json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


def csv_source(source, types):
    """M steps that read one exported CSV with declared column types."""
    return f"""Csv.Document(File.Contents(DataFolder & "{source}.csv"), [Delimiter = ",", Encoding = 65001, QuoteStyle = QuoteStyle.Csv]),
\t\t\t\t    #"Promoted headers" = Table.PromoteHeaders(Source, [PromoteAllScalars = true]),
\t\t\t\t    #"Typed columns" = Table.TransformColumnTypes(
\t\t\t\t        #"Promoted headers",
\t\t\t\t        {{
{types}
\t\t\t\t        }},
\t\t\t\t        "en-US"
\t\t\t\t    )"""


# Extra steps per table (Power Query): derived label columns and the Oct-Nov comparability flag.
# Each entry: (steps after "Typed columns", final step name, [(column, duck type)])
ZW = "Character.FromNumber(8203)"   # zero-width space: keeps J / J / J (Jan, Jun, Jul) distinct labels
EXTRA_STEPS = {
    "Oct-Nov": (f"""
\t\t\t\t    #"Comparable" = Table.AddColumn(#"Typed columns", "is_comparable", each [sep_days] >= {MIN_SEP_DAYS} and [octnov_days] >= {MIN_OCTNOV_DAYS}, type logical)""",
                '#"Comparable"', [("is_comparable", "BOOLEAN")]),
    "Issues": ("""
				    Totals = Table.Group(#"Typed columns", {"issue"}, {{"total", each List.Sum([n]), type number}}),
				    Ranked = Table.AddIndexColumn(Table.Sort(Totals, {{"total", Order.Descending}, {"issue", Order.Ascending}}), "issue_rank", 1, 1, Int64.Type),
				    Joined = Table.NestedJoin(#"Typed columns", {"issue"}, Table.SelectColumns(Ranked, {"issue", "issue_rank"}), {"issue"}, "r", JoinKind.LeftOuter),
				    #"With rank" = Table.ExpandTableColumn(Joined, "r", {"issue_rank"})""",
               '#"With rank"', [("issue_rank", "BIGINT")]),
}


def month_initial_m():
    """M: first letter of the month name, made unique with zero-width spaces (J Jan, J+1 Jun, J+2 Jul, M Mar, M+1 May,
    A Apr, A+1 Aug) so the label sorts by month number one-to-one."""
    return ('each Text.Start([month_name], 1) & Text.Repeat(' + ZW +
            ', if List.Contains({6, 5, 8}, [month]) then 1 else if [month] = 7 then 2 else 0)')


EXTRA_STEPS["Date"] = ("""
\t\t\t\t    #"Month initial" = Table.AddColumn(#"Typed columns", "month_initial", """ + month_initial_m() + """, type text)""",
                       '#"Month initial"', [("month_initial", "VARCHAR")])


def table_tmdl(table, cols, partition_m, visible):
    out = [f"table {q(table)}", ""]
    for col, ctype in cols:
        dtype, _, fmt = tmdl_type(ctype)
        fmt = COLUMN_FORMATS.get(col, fmt)
        out.append(f"\tcolumn {q(col)}")
        out.append(f"\t\tdataType: {dtype}")
        if fmt:
            out.append(f"\t\tformatString: {fmt_value(fmt)}")
        if col not in visible:
            out.append("\t\tisHidden")
        if (table, col) in SORT_BY:
            out.append(f"\t\tsortByColumn: {SORT_BY[(table, col)]}")
        out.append("\t\tsummarizeBy: none")
        out.append(f"\t\tsourceColumn: {col}")
        out.append("")
    out.append(f"\tpartition {q(table)} = m")
    out.append("\t\tmode: import")
    out.append("\t\tsource =")
    out.append(partition_m)
    return "\n".join(out)


def data_bound(expr):
    """Fill the placeholders that depend on the warehouse or the page geometry (known only at build time)."""
    spike = LOSS_RAMP[1:]               # one spike already takes the first visible step, so a 1 never reads as empty
    return (expr.replace("TOP_SPIKES", str(TOP_SPIKES))
            .replace("SPIKE_COLOUR", ramp_dax("[Spike episodes]", 0, SPIKE_MAX_CELL + 1, spike).strip("\n"))
            .replace("SPIKE_TEXT", text_on("[Spike episodes]", 0, SPIKE_MAX_CELL + 1, spike).strip("\n")))


def build_model(con):
    d = SM / "definition"
    write_json(SM / "definition.pbism", {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/semanticModel/definitionProperties/1.0.0/schema.json",
        "version": "4.2", "settings": {}})
    write_json(SM / ".platform", platform("SemanticModel"))
    write(d / "database.tmdl", "database\n\tcompatibilityLevel: 1601\n")
    all_tables = list(TABLES) + ["Year", "Season", "Oct-Nov periods"] + list(CALC_TABLES)
    refs = "\n".join(f"ref table {q(t)}" for t in all_tables)
    write(d / "model.tmdl", f"""model Model
\tculture: en-US
\tdefaultPowerBIDataSourceVersion: powerBI_V3
\tdiscourageImplicitMeasures
\tsourceQueryCulture: en-US
\tdataAccessOptions
\t\tlegacyRedirects
\t\treturnErrorValuesAsNull

annotation __PBI_TimeIntelligenceEnabled = 0

annotation PBI_ProTooling = ["TMDL-Extension"]

{refs}
""")
    write(d / "expressions.tmdl",
          f'expression DataFolder = "{DATA_FOLDER}" meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]\n')

    def typed(cols):
        return ",\n".join(f'\t\t\t\t\t\t{{"{c}", {tmdl_type(t)[1]}}}' for c, t in cols)

    for table, (source, visible) in TABLES.items():
        cols = [(c, t) for c, t, *_ in con.sql(f"DESCRIBE {source}").fetchall()]
        steps, last, extra = EXTRA_STEPS.get(table, ("", '#"Typed columns"', []))
        m = f"""\t\t\t\tlet
\t\t\t\t    Source = {csv_source(source, typed(cols))}{"," if steps else ""}{steps}
\t\t\t\tin
\t\t\t\t    {last}
"""
        vis = visible + ([c for c, _ in extra if c == "month_initial"])
        write(d / "tables" / f"{table}.tmdl", table_tmdl(table, cols + extra, m, vis))

    # Year: the calendar years, flagged where the city AQI table has data (the Year slicers show only those)
    date_cols = [(c, t) for c, t, *_ in con.sql("DESCRIBE dim_date").fetchall()]
    aqi_cols = [(c, t) for c, t, *_ in con.sql("DESCRIBE mart_city_daily_aqi").fetchall()]
    year_m = f"""\t\t\t\tlet
\t\t\t\t    Source = {csv_source("dim_date", typed(date_cols))},
\t\t\t\t    Years = Table.Distinct(Table.SelectColumns(#"Typed columns", {{"year"}})),
\t\t\t\t    AqiSource = Csv.Document(File.Contents(DataFolder & "mart_city_daily_aqi.csv"), [Delimiter = ",", Encoding = 65001, QuoteStyle = QuoteStyle.Csv]),
\t\t\t\t    AqiTyped = Table.TransformColumnTypes(Table.PromoteHeaders(AqiSource, [PromoteAllScalars = true]), {{{{"date", type date}}}}, "en-US"),
\t\t\t\t    AqiYears = List.Distinct(List.Transform(AqiTyped[date], each Date.Year(_))),
\t\t\t\t    Flagged = Table.AddColumn(Years, "has_aqi_data", each List.Contains(AqiYears, [year]), type logical),
\t\t\t\t    Sorted = Table.Sort(Flagged, {{{{"year", Order.Ascending}}}})
\t\t\t\tin
\t\t\t\t    Sorted
"""
    write(d / "tables" / "Year.tmdl", table_tmdl("Year", [("year", "BIGINT"), ("has_aqi_data", "BOOLEAN")], year_m, ["year"]))

    # Season: IMD seasons as defined in dim_date
    season_m = f"""\t\t\t\tlet
\t\t\t\t    Source = {csv_source("dim_date", typed(date_cols))},
\t\t\t\t    Seasons = Table.Sort(Table.Distinct(Table.SelectColumns(#"Typed columns", {{"season", "season_order"}})), {{{{"season_order", Order.Ascending}}}})
\t\t\t\tin
\t\t\t\t    Seasons
"""
    write(d / "tables" / "Season.tmdl",
          table_tmdl("Season", [("season", "VARCHAR"), ("season_order", "INTEGER")], season_m, ["season"]))

    # Oct-Nov periods: mart_octnov_season unpivoted to one row per city, year and period (Sep, Oct-Nov, Dec),
    # for the slope chart; null periods drop out
    stub_cols = [(c, t) for c, t, *_ in con.sql("DESCRIBE mart_octnov_season").fetchall()]
    names = ", ".join(f'{{"{src}", "{name}", {i + 1}}}' for i, (src, name) in
                      enumerate(zip(["sep_pm25", "octnov_pm25", "dec_pm25"], PERIOD_NAMES)))
    periods_m = f"""\t\t\t\tlet
\t\t\t\t    Source = {csv_source("mart_octnov_season", typed(stub_cols))},
\t\t\t\t    Comparable = Table.AddColumn(#"Typed columns", "is_comparable", each [sep_days] >= {MIN_SEP_DAYS} and [octnov_days] >= {MIN_OCTNOV_DAYS}, type logical),
\t\t\t\t    Kept = Table.SelectColumns(Comparable, {{"city", "year", "is_comparable", "sep_pm25", "octnov_pm25", "dec_pm25"}}),
\t\t\t\t    Long = Table.UnpivotOtherColumns(Kept, {{"city", "year", "is_comparable"}}, "source_column", "pm25"),
\t\t\t\t    Names = {{{names}}},
\t\t\t\t    Named = Table.AddColumn(Long, "period", each List.First(List.Select(Names, (n) => n{{0}} = [source_column])){{1}}, type text),
\t\t\t\t    Ordered = Table.AddColumn(Named, "period_order", each List.First(List.Select(Names, (n) => n{{0}} = [source_column])){{2}}, Int64.Type),
\t\t\t\t    Typed = Table.TransformColumnTypes(Table.RemoveColumns(Ordered, {{"source_column"}}), {{{{"pm25", type number}}}}, "en-US")
\t\t\t\tin
\t\t\t\t    Typed
"""
    write(d / "tables" / "Oct-Nov periods.tmdl", table_tmdl(
        "Oct-Nov periods", [("city", "VARCHAR"), ("year", "BIGINT"), ("is_comparable", "BOOLEAN"), ("pm25", "DOUBLE"),
                            ("period", "VARCHAR"), ("period_order", "BIGINT")], periods_m, ["period"]))

    for table, (dax, cols) in CALC_TABLES.items():
        out = [f"table {q(table)}", ""]
        for name, expr, fmt, desc in MEASURES:
            expr = data_bound(expr)
            out.append(f"\t/// {desc}")
            if "\n" in expr.strip():
                out.append(f"\tmeasure {q(name)} = ```")
                out.append(indent(expr, 3))
                out.append("\t\t\t```")
            else:
                out.append(f"\tmeasure {q(name)} = {expr.strip()}")
            if fmt:
                out.append(f"\t\tformatString: {fmt_value(fmt)}")
            out.append("")
        for col, dtype, src, hidden, fmt in cols:
            out.append(f"\tcolumn {q(col)}")
            out.append(f"\t\tdataType: {dtype}")
            if hidden:
                out.append("\t\tisHidden")
            out.append("\t\tsummarizeBy: none")
            out.append(f"\t\tsourceColumn: {src}")
            out.append("")
        out.append(f"\tpartition {q(table)} = calculated")
        out.append("\t\tmode: import")
        out.append(f"\t\tsource = {dax}")
        out.append("")
        write(d / "tables" / f"{table}.tmdl", "\n".join(out))

    rels = []
    for ft, fc, tt, tc in RELATIONSHIPS:
        rid = uuid.uuid5(uuid.NAMESPACE_URL, f"{NAME}:{ft}.{fc}->{tt}.{tc}")
        rels.append(f"relationship {rid}\n\tfromColumn: {q(ft)}.{q(fc)}\n\ttoColumn: {q(tt)}.{q(tc)}\n")
    write(d / "relationships.tmdl", "\n".join(rels))


# ---------------------------------------------------------------- report helpers
def uid(*parts):
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:20]


def platform(kind):
    return {"$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",
            "metadata": {"type": kind, "displayName": NAME},
            "config": {"version": "2.0", "logicalId": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{NAME}-{kind}"))}}


def lit(v):
    return {"expr": {"Literal": {"Value": v}}}


def s(text):
    return lit("'" + text.replace("'", "''") + "'")


def num(v, suffix="D"):
    return lit(f"{v}{suffix}")


def color(hex_):
    return {"solid": {"color": s(hex_)}}


def measure(name, entity="Metrics"):
    return {"Measure": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": name}}


def column(entity, name):
    return {"Column": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": name}}


def measure_color(name):
    return {"solid": {"color": {"expr": measure(name)}}}


def proj(field, display=None):
    kind = "Measure" if "Measure" in field else "Column"
    ent = field[kind]["Expression"]["SourceRef"]["Entity"]
    prop = field[kind]["Property"]
    p = {"field": field, "queryRef": f"{ent}.{prop}", "nativeQueryRef": prop}
    if kind == "Column":
        p["active"] = True
    if display:
        p["displayName"] = display
    return p


def container(title=None, bg=None, pad=None):
    c = {"title": [{"properties": {"show": lit("true" if title else "false")}}],
         "background": [{"properties": {"show": lit("true" if bg else "false")}}],
         "border": [{"properties": {"show": lit("false")}}],
         "dropShadow": [{"properties": {"show": lit("false")}}],
         "visualHeader": [{"properties": {"show": lit("false")}}]}
    if title:
        c["title"][0]["properties"].update({"text": s(title), "fontFamily": s(SANS), "fontSize": num(TITLE_PT),
                                            "fontColor": color(MUTED), "bold": lit("false")})
    if bg:
        c["background"][0]["properties"].update({"color": color(bg), "transparency": num(0)})
    if pad is not None:
        c["padding"] = [{"properties": {k: num(pad) for k in ("top", "bottom", "left", "right")}}]
    return c


def eq(entity, col, value):
    return {"Comparison": {"ComparisonKind": 0, "Left": column(entity, col), "Right": {"Literal": {"Value": f"'{value}'"}}}}


def series_colors(entity, col, mapping):
    return [{"properties": {"fill": color(hex_)}, "selector": {"data": [{"scopeId": eq(entity, col, value)}]}}
            for value, hex_ in mapping]


def categorical_filter(entity, col, values, name):
    return {"name": name, "field": column(entity, col), "type": "Categorical",
            "filter": {"Version": 2, "From": [{"Name": "f", "Entity": entity, "Type": 0}],
                       "Where": [{"Condition": {"In": {
                           "Expressions": [{"Column": {"Expression": {"SourceRef": {"Source": "f"}}, "Property": col}}],
                           "Values": [[{"Literal": {"Value": v}}] for v in values]}}}]},
            "howCreated": "User"}


class Page:
    def __init__(self, key, display):
        self.key, self.display, self.visuals, self.interactions = key, display, [], []

    def add(self, vid, x, y, w, h, visual, filters=None, z=None):
        n = len(self.visuals) + 1
        v = {"$schema": f"{SCHEMA}/visualContainer/2.1.0/schema.json",
             "name": uid(self.key, vid),
             "position": {"x": x, "y": y, "z": z if z is not None else 1000 * n, "height": h, "width": w,
                          "tabOrder": 1000 * n},
             "visual": visual}
        if filters:
            v["filterConfig"] = {"filters": filters}
        v["_folder"] = vid
        self.visuals.append(v)

    def no_filter(self, source, target):
        self.interactions.append({"source": uid(self.key, source), "target": uid(self.key, target), "type": "NoFilter"})


def textbox(paragraphs, bg=None):
    return {"visualType": "textbox", "objects": {"general": [{"properties": {"paragraphs": paragraphs}}]},
            "visualContainerObjects": container(bg=bg), "drillFilterOtherVisuals": True}


def run(text, font=SANS, size=10, col=TEXT, bold=False):
    style = {"fontFamily": font, "fontSize": f"{size}pt", "color": col}
    if bold:
        style["fontWeight"] = "bold"
    return {"value": text, "textStyle": style}


def kpi_cards(fields):
    return {"visualType": "cardVisual",
            "query": {"queryState": {"Data": {"projections": [proj(measure(m), d) for m, d in fields]}}},
            "objects": {
                "layout": [{"properties": {"orientation": num(0), "columnCount": num(len(fields), "L"),
                                           "style": s("Cards"), "alignment": s("left")}}],
                "value": [{"properties": {"fontColor": color(TEXT), "fontSize": num(KPI_PT), "fontFamily": s(SERIF),
                                          "labelDisplayUnits": num(1)},
                           "selector": {"id": "default"}}],
                "label": [{"properties": {"fontColor": color(MUTED), "fontSize": num(10), "fontFamily": s(SANS),
                                          "position": s("aboveValue")}, "selector": {"id": "default"}}],
                "fillCustom": [{"properties": {"show": lit("false")}, "selector": {"id": "default"}}],
                "outline": [{"properties": {"show": lit("false")}, "selector": {"id": "default"}}],
                "accentBar": [{"properties": {"show": lit("true"), "position": s("Left"), "color": color(RULE),
                                              "width": num(2)}, "selector": {"id": "default"}}],
                "shadowCustom": [{"properties": {"show": lit("false")}, "selector": {"id": "default"}}],
                "padding": [{"properties": {"paddingSelection": s("Narrow")}, "selector": {"id": "default"}}]},
            "visualContainerObjects": container(), "drillFilterOtherVisuals": True}


def answer_card(measure_name):
    return {"visualType": "cardVisual",
            "query": {"queryState": {"Data": {"projections": [proj(measure(measure_name))]}}},
            "objects": {
                "layout": [{"properties": {"orientation": num(0), "columnCount": num(1, "L"), "alignment": s("left")}}],
                "value": [{"properties": {"fontColor": color(TEXT), "fontSize": num(ANSWER_PT), "fontFamily": s(SERIF),
                                          "horizontalAlignment": s("left")}, "selector": {"id": "default"}}],
                "label": [{"properties": {"show": lit("false")}, "selector": {"id": "default"}}],
                "fillCustom": [{"properties": {"show": lit("false")}, "selector": {"id": "default"}}],
                "outline": [{"properties": {"show": lit("false")}, "selector": {"id": "default"}}],
                "accentBar": [{"properties": {"show": lit("false")}, "selector": {"id": "default"}}],
                "shadowCustom": [{"properties": {"show": lit("false")}, "selector": {"id": "default"}}],
                "padding": [{"properties": {"paddingSelection": s("Narrow")}, "selector": {"id": "default"}}]},
            "visualContainerObjects": container(), "drillFilterOtherVisuals": True}


def axis_objects(legend=True):
    return {"categoryAxis": [{"properties": {"labelColor": color(MUTED), "fontSize": num(9), "showAxisTitle": lit("false"),
                                             "gridlineShow": lit("false")}}],
            "valueAxis": [{"properties": {"labelColor": color(MUTED), "fontSize": num(9), "showAxisTitle": lit("false"),
                                          "gridlineColor": color(RULE), "gridlineThickness": num(1, "L")}}],
            "legend": [{"properties": {"show": lit("true" if legend else "false"), "position": s("Top"),
                                       "labelColor": color(TEXT), "fontSize": num(9), "showTitle": lit("false")}}]}


def chart(vtype, category, values, title, series=None, extra=None, sort=None):
    qs = {"Category": {"projections": [proj(category)]},
          "Y": {"projections": [proj(measure(v)) for v in values]}}
    if series:
        qs["Series"] = {"projections": [proj(series)]}
    v = {"visualType": vtype, "query": {"queryState": qs}, "objects": axis_objects(),
         "visualContainerObjects": container(title), "drillFilterOtherVisuals": True}
    if sort:
        field, direction = sort
        v["query"]["sortDefinition"] = {"sort": [{"field": field, "direction": direction}], "isDefaultSort": False}
    if extra:
        v["objects"].update(extra)
    return v


def slicer(entity, col, title, mode="Dropdown", sync=None, default_text=None, single=False):
    v = {"visualType": "slicer",
         "query": {"queryState": {"Values": {"projections": [proj(column(entity, col))]}}},
         "objects": {"data": [{"properties": {"mode": s(mode)}}],
                     "header": [{"properties": {"show": lit("true"), "text": s(title), "fontColor": color(MUTED),
                                                "fontFamily": s(SANS), "textSize": num(9)}}],
                     "items": [{"properties": {"fontColor": color(TEXT), "fontFamily": s(SANS), "textSize": num(10)}}]},
         "visualContainerObjects": container(), "drillFilterOtherVisuals": True}
    if single:
        v["objects"]["selection"] = [{"properties": {"strictSingleSelect": lit("true")}}]
    if default_text is not None:
        # open with one value selected, the way Desktop saves a user's pick
        v["objects"]["general"] = [{"properties": {"filter": {"filter": {
            "Version": 2, "From": [{"Name": "p", "Entity": entity, "Type": 0}],
            "Where": [{"Condition": {"In": {
                "Expressions": [{"Column": {"Expression": {"SourceRef": {"Source": "p"}}, "Property": col}}],
                "Values": [[{"Literal": {"Value": "'" + default_text.replace("'", "''") + "'"}}]]}}}]}}}}]
    if sync:
        v["syncGroup"] = {"groupName": sync, "fieldChanges": True, "filterChanges": True}
    return v


def chips(entity, col):
    """A row of tiles (horizontal list slicer); no tile picked = all."""
    v = slicer(entity, col, "", mode="Basic")
    v["objects"]["header"] = [{"properties": {"show": lit("false")}}]
    v["objects"]["general"] = [{"properties": {"orientation": num(1), "outlineColor": color(RULE), "outlineWeight": num(1, "L")}}]
    v["objects"]["items"] = [{"properties": {"fontColor": color(TEXT), "fontFamily": s(SANS), "textSize": num(10),
                                             "background": color("#ffffff"), "outline": s("Frame"),
                                             "outlineColor": color(RULE)}}]
    v["visualContainerObjects"]["padding"] = [{"properties": {k: num(0) for k in ("top", "bottom", "left", "right")}}]
    return v


def matrix(rows, cols, value, back, title, cell_px, fore_measure=None, font=9, header_font=8, padding=2,
           hide_text=False, row_header_px=None):
    """A grid of coloured cells: rows x columns (one or two column levels, all expanded), colour from a measure."""
    cols = cols if isinstance(cols, list) else [cols]
    v = {"visualType": "pivotTable",
         "query": {"queryState": {"Rows": {"projections": [proj(rows, " ")]},
                                  "Columns": {"projections": [proj(c, " ") for c in cols]},
                                  "Values": {"projections": [proj(measure(value))]}}},
         "objects": {
             "values": [{"properties": {"fontColor": color(TEXT), "fontSize": num(font), "fontFamily": s(SANS),
                                        "wordWrap": lit("false"),
                                        "backColorPrimary": color(PAPER), "backColorSecondary": color(PAPER)}},
                        {"properties": {"backColor": measure_color(back),
                                        **({"fontColor": measure_color(back)} if hide_text else {}),
                                        **({"fontColor": measure_color(fore_measure)} if fore_measure else {})},
                         "selector": {"data": [{"dataViewWildcard": {"matchingOption": 1}}], "metadata": f"Metrics.{value}"}}],
             "columnHeaders": [{"properties": {"fontColor": color(MUTED), "fontSize": num(header_font), "fontFamily": s(SANS),
                                               "backColor": color(PAPER), "outline": s("None"), "wordWrap": lit("false"),
                                               "alignment": s("Center"), "autoSizeColumnWidth": lit("false")}}],
             "rowHeaders": [{"properties": {"fontColor": color(MUTED), "fontSize": num(9), "fontFamily": s(SANS),
                                            "backColor": color(PAPER), "outline": s("None"), "wordWrap": lit("false"),
                                            "showExpandCollapseButtons": lit("false")}}],
             "grid": [{"properties": {"gridVertical": lit("true"), "gridVerticalColor": color(PAPER), "gridVerticalWeight": num(1, "L"),
                                      "gridHorizontal": lit("true"), "gridHorizontalColor": color(PAPER), "gridHorizontalWeight": num(2, "L"),
                                      "outlineColor": color(PAPER), "rowPadding": num(padding, "L"), "textSize": num(font)}}],
             "subTotals": [{"properties": {"rowSubtotals": lit("false"), "columnSubtotals": lit("false")}}],
             "columnWidth": [{"properties": {"value": num(cell_px)}, "selector": {"metadata": f"Metrics.{value}"}}],
             "columnFormatting": [{"properties": {"alignment": s("Center")}, "selector": {"metadata": f"Metrics.{value}"}}]},
         "visualContainerObjects": container(title), "drillFilterOtherVisuals": True}
    if len(cols) > 1:
        v["expansionStates"] = [{"roles": ["Columns"],
                                 "levels": [{"queryRefs": [proj(c)["queryRef"]], "isPinned": True} for c in cols],
                                 "root": {"isToggled": True}}]
    return v


def table(columns, title, sort=None, widths=None, wrap=False, right=()):
    v = {"visualType": "tableEx",
         "query": {"queryState": {"Values": {"projections": [dict(proj(f, d), active=True) for f, d in columns]}}},
         "objects": {
             "values": [{"properties": {"fontColor": color(TEXT), "fontSize": num(9), "fontFamily": s(SANS),
                                        "backColorPrimary": color(PAPER), "backColorSecondary": color(PAPER),
                                        "wordWrap": lit("true" if wrap else "false")}}],
             "columnHeaders": [{"properties": {"fontColor": color(MUTED), "fontSize": num(9), "fontFamily": s(SANS),
                                               "backColor": color(PAPER), "outline": s("BottomOnly"),
                                               "autoSizeColumnWidth": lit("false"), "wordWrap": lit("false")}}],
             "grid": [{"properties": {"gridVertical": lit("false"), "gridHorizontal": lit("true"),
                                      "gridHorizontalColor": color(RULE), "outlineColor": color(RULE), "rowPadding": num(TABLE_PAD, "L")}}],
             "total": [{"properties": {"totals": lit("false")}}]},
         "visualContainerObjects": container(title), "drillFilterOtherVisuals": True}
    if widths:
        v["objects"]["columnWidth"] = [
            {"properties": {"value": num(w)}, "selector": {"metadata": proj(f)["queryRef"]}}
            for (f, _), w in zip(columns, widths)]
    if right:
        v["objects"]["columnFormatting"] = [{"properties": {"alignment": s("Right")}, "selector": {"metadata": proj(f)["queryRef"]}}
                                            for f, _ in columns if proj(f)["nativeQueryRef"] in right]
    if sort:
        field, direction = sort
        v["query"]["sortDefinition"] = {"sort": [{"field": field, "direction": direction}], "isDefaultSort": False}
    return v


def nav_button(target):
    """An invisible button over a rail question that goes to its page: no icon, fill, outline or text in any state."""
    states = [None, "default", "hover", "press", "disabled"]

    def each(props):
        return [{"properties": props, **({"selector": {"id": st}} if st else {})} for st in states]
    return {"visualType": "actionButton",
            "objects": {"icon": each({"shapeType": s("blank"), "show": lit("false")}),
                        "fill": each({"show": lit("false"), "transparency": num(100)}),
                        "outline": each({"show": lit("false"), "transparency": num(100), "weight": num(0)}),
                        "shadow": each({"show": lit("false")}),
                        "glow": each({"show": lit("false")}),
                        "text": each({"show": lit("false")})},
            "visualContainerObjects": {
                **container(),
                "visualLink": [{"properties": {"show": lit("true"), "type": s("PageNavigation"),
                                               "navigationSection": s(target)}}]},
            "drillFilterOtherVisuals": True}


# ---------------------------------------------------------------- report pages
TITLE_PT, KPI_PT, ANSWER_PT, HEAD_PT = 10, 20, 12, 22
RAIL_W = 232
X0, W = 256, 1000                 # content column: 256 .. 1256
GUT = 16                          # gutter between visuals
TOP_KPI, KPI_H = 112, 72          # KPI row
TOP = TOP_KPI + KPI_H + 16        # first visual row under the KPIs (200)
BOTTOM = 704                      # last visual row ends here (16 px margin)
HALF = (W - GUT) // 2             # two equal columns (492)
# Rail: five question entries, 96 px apart, from y = 96 (as the mockup)
Q_Y0, Q_STEP, Q_H = 96, 100, 88
PAGES = [("bad", "How bad", "How bad is each city?", "How bad is each city?"),       # key, name, heading, rail
         ("when", "When", "When in the year is it worst?", "When in the year is it worst?"),
         ("spikes", "Spikes", "When do short spikes hit?", "When do spikes hit?"),
         ("hours", "Hours", "Which hours and weekdays are worst?", "Which hours and weekdays are worst?"),
         ("trust", "Trust", "Can the data be trusted?", "Can the data be trusted?")]
# Page 3: method note at the bottom; above it a city x month grid (left, top), the largest-spikes table under it and
# the Oct-Nov chart (right)
NOTE_H = 46
SPIKE_BOTTOM = BOTTOM - NOTE_H - 8
SPIKE_GRID_H = 176                   # 7 rows (header + 6 cities) at 20 px under a 36 px title
SPIKE_LW = 560                                  # left column: grid and table; the Oct-Nov chart takes the rest
SPIKE_WIDTHS = [96, 104, 50, 92, 106, 96]       # sums to SPIKE_LW less 16 px; check_fit.py proves the cells fit
TABLE_HEAD_PX, TABLE_ROW_PX = 30 + 26, 16       # table title + header row; a 9 pt line (Desktop render)
TABLE_PAD = 2
# as many largest spikes as the table shows without a scrollbar
TOP_SPIKES = (SPIKE_BOTTOM - (TOP + SPIKE_GRID_H + GUT) - TABLE_HEAD_PX) // (1 + 2 * TABLE_PAD + TABLE_ROW_PX)
# Matrix geometry, calibrated against the Desktop render: a 9 pt row is 20 px with padding 2 (16 + 2 x padding)
TITLE_H, ROW_HEADER_PX = 36, 76


def padding_for(rows, height, font=9):
    """Row padding so that `rows` matrix rows (headers included) fill a visual of `height` under its title.
    A row is about font x 4/3 + 7 px before padding (measured on the Desktop render)."""
    base = int(font * 4 / 3 + 7.5)
    return max(0, round(((height - TITLE_H - 4) / rows - base) / 2))


def rail(page, active):
    page.add("railBg", 0, 0, RAIL_W, 720, textbox([{"textRuns": [run(" ")]}], bg=RAIL), z=0)
    page.add("railTitle", 16, 18, 200, 64, textbox([
        {"textRuns": [run("Air quality", SERIF, 18, TEXT)]},
        {"textRuns": [run("Six Indian cities, 2019 to 2026", SANS, 8, MUTED)]}]))
    for i, (key, _, _, question) in enumerate(PAGES):
        y = Q_Y0 + i * Q_STEP
        on = key == active
        if on:
            page.add(f"q{i}Bg", 16, y, 200, Q_H, textbox([{"textRuns": [run(" ")]}], bg=PAPER))
            page.add(f"q{i}Bar", 16, y, 3, Q_H, textbox([{"textRuns": [run(" ")]}], bg=INK))
        col = INK if on else MUTED
        page.add(f"q{i}Num", 20, y + 10, 40, 44, textbox([{"textRuns": [run(str(i + 1), SERIF, 20, col)]}]))
        page.add(f"q{i}Text", 54, y + 12, 158, Q_H - 16, textbox([{"textRuns": [run(question, SERIF, 11, col)]}]))
        if not on:
            page.add(f"q{i}Go", 16, y, 200, Q_H, nav_button(key))
    page.add("railNote", 16, 620, 200, 84, textbox([
        {"textRuns": [run("CPCB and state board stations via OpenAQ. Licence per provider.", SANS, 8, MUTED)]},
        {"textRuns": [run("Hours in IST. Gaps are left empty.", SANS, 8, MUTED)]}]))


def header(page, question, answer_measure, second=("Year", "year", "Year", "year")):
    page.add("heading", X0, 12, 620, 52, textbox([{"textRuns": [run(question, SERIF, HEAD_PT, TEXT)]}]))
    page.add("city", 900, 10, 170, 52, slicer("City", "city", "City", sync="city"))
    entity, col, title, sync = second
    if entity == "Year":
        page.add("year", 1086, 10, 170, 52, slicer("Year", "year", "Year", sync="year"),
                 filters=[categorical_filter("Year", "has_aqi_data", ["true"], "withData")])
    else:
        page.add(sync, 1086, 10, 170, 52, slicer(entity, col, title, single=(sync == "pollutantOne"),
                                                 default_text="PM2.5" if sync == "pollutantOne" else None))
    page.add("answerLabel", X0, 75, 84, 32, textbox([{"textRuns": [run("Answer", SERIF, ANSWER_PT, INK)]}]))
    page.add("answer", X0 + 70, 64, W - 70, 42, answer_card(answer_measure))


def build_pages():
    pages = []
    city = column("City", "city")
    city_colours = list(zip(CITIES, CITY_SCALE))

    # ------------------------------------------------ 1 How bad
    p = Page("bad", "How bad")
    rail(p, "bad")
    header(p, PAGES[0][2], "Answer: how bad")
    p.add("kpis", X0, TOP_KPI, W, KPI_H, kpi_cards([
        ("Worst city", "Worst city, by mean AQI"), ("Cleanest city", "Cleanest city, by mean AQI"),
        ("Poor or worse share", "City-days Poor or worse"), ("PM2.5 over limit share", "City-days over the PM2.5 limit")]))
    bar_w = 600
    p.add("categories", X0, TOP, bar_w, BOTTOM - TOP, chart(
        "hundredPercentStackedBarChart", city, ["Days by category"], "Days by CPCB AQI category, per city",
        series=column("City AQI", "category"),
        extra={"dataPoint": series_colors("City AQI", "category", CATEGORIES),
               "valueAxis": [{"properties": {"show": lit("true"), "labelColor": color(MUTED), "fontSize": num(9),
                                             "showAxisTitle": lit("false"), "gridlineColor": color(RULE)}}],
               "categoryAxis": [{"properties": {"labelColor": color(TEXT), "fontSize": num(10), "showAxisTitle": lit("false")}}],
               "labels": [{"properties": {"show": lit("false")}}]}))
    hx = X0 + bar_w + GUT
    hw = W - bar_w - GUT
    n_pol = 6
    p.add("naaqs", hx, TOP, hw, BOTTOM - TOP, matrix(
        city, column("Pollutant", "pollutant_name"), "Over NAAQS share", "Over NAAQS colour",
        "Share of days over the national standard", cell_px=(hw - 16 - ROW_HEADER_PX) // n_pol,
        fore_measure="Over NAAQS text", font=10, header_font=9, padding=padding_for(len(CITIES) + 1, BOTTOM - TOP, 10)))
    pages.append(p)

    # ------------------------------------------------ 2 When
    p = Page("when", "When")
    rail(p, "when")
    header(p, PAGES[1][2], "Answer: when")
    hm_h = 330
    p.add("monthly", X0, TOP_KPI, W, hm_h, matrix(
        city, [column("Date", "year"), column("Date", "month_initial")], "Month AQI", "Month AQI colour",
        f"Mean AQI by city and month, coloured by CPCB category (months with {MIN_MONTH_DAYS}+ days; "
        "the archive has no data Nov 2022 to Feb 2025)",
        cell_px=max(6, (W - 16 - ROW_HEADER_PX) // max(1, MONTH_COLUMNS)), hide_text=True, font=7, header_font=7,
        padding=padding_for(len(CITIES) + 2, hm_h, 7)))
    low_y = TOP_KPI + hm_h + GUT
    p.add("seasons", X0, low_y, HALF, BOTTOM - low_y, chart(
        "clusteredColumnChart", city, ["Mean AQI"], "Mean AQI by season, per city", series=column("Season", "season"),
        extra={"dataPoint": series_colors("Season", "season", list(zip(SEASONS, SEASON_SCALE))),
               "categoryAxis": [{"properties": {"labelColor": color(TEXT), "fontSize": num(9), "showAxisTitle": lit("false")}}]}))
    p.add("typicalYear", X0 + HALF + GUT, low_y, W - HALF - GUT, BOTTOM - low_y, chart(
        "lineChart", column("Date", "month_name"), ["Mean AQI"], "Typical year: mean AQI by calendar month",
        series=city, extra={"dataPoint": series_colors("City", "city", city_colours)}))
    pages.append(p)

    # ------------------------------------------------ 3 Spikes
    p = Page("spikes", "Spikes")
    rail(p, "spikes")
    header(p, PAGES[2][2], "Answer: spikes")
    p.add("kpis", X0, TOP_KPI, W, KPI_H, kpi_cards([
        ("Spike count", "Short spikes found"), ("Median spike ratio", "Median peak vs surroundings"),
        ("Oct-Nov vs Sep", "Oct-Nov vs Sep, PM2.5"), ("Oct-Nov Very Poor share", "Oct-Nov city-days Very Poor+")]))
    p.add("spikeGrid", X0, TOP, SPIKE_LW, SPIKE_GRID_H, matrix(
        city, column("Date", "month_name"), "Spike cell", "Spike colour",
        "Short spikes by city and start month (empty: none)",
        cell_px=(SPIKE_LW - 16 - ROW_HEADER_PX) // 12, fore_measure="Spike text", font=10, header_font=9,
        padding=padding_for(len(CITIES) + 1, SPIKE_GRID_H, 10)))
    ty = TOP + SPIKE_GRID_H + GUT
    p.add("topSpikes", X0, ty, SPIKE_LW, SPIKE_BOTTOM - ty, table(
        [(column("Spikes", "city"), "City"), (column("Spikes", "start_date"), "Start"), (measure("Top spike days"), "Days"),
         (measure("Top spike peak"), "Peak PM2.5"), (measure("Top spike ratio"), "x surroundings"),
         (measure("Top spike stations"), f"Stations {SPIKE_RULE['min_ratio']:g}x+")],
        f"The {TOP_SPIKES} largest, by peak against the surrounding days",
        sort=(measure("Top spike ratio"), "Descending"), widths=SPIKE_WIDTHS,
        right=("Top spike days", "Top spike peak", "Top spike ratio", "Top spike stations")))
    p.add("octnov", X0 + SPIKE_LW + GUT, TOP, W - SPIKE_LW - GUT, SPIKE_BOTTOM - TOP, chart(
        "lineChart", column("Oct-Nov periods", "period"), ["Period PM2.5"],
        "Mean PM2.5 by city: September, Oct-Nov, December",
        series=city, extra={"dataPoint": series_colors("City", "city", city_colours),
                            "lineStyles": [{"properties": {"showMarker": lit("true"), "markerSize": num(5, "L")}}]}))
    r = SPIKE_RULE
    p.add("method", X0, BOTTOM - NOTE_H, W, NOTE_H, textbox([{"textRuns": [run(
        f"A short spike: 1 to {r['max_days']} days in a row with city PM2.5 at least {r['min_ratio']:.1f}x the median of the "
        f"days {r['guard_days'] + 1} to {r['window_days']} days either side, with ordinary measured days before and after; "
        f"found from the data alone. Oct-Nov is compared with September in city-years with {MIN_SEP_DAYS}+ September and "
        f"{MIN_OCTNOV_DAYS}+ Oct-Nov days. Levels are described, not attributed to any cause.", SANS, 8, MUTED)]}]))
    pages.append(p)

    # ------------------------------------------------ 4 Hours
    p = Page("hours", "Hours")
    rail(p, "hours")
    header(p, PAGES[3][2], "Answer: hours", second=("Pollutant", "pollutant_name", "Pollutant", "pollutantOne"))
    # the tiles draw 16 px inside their container: widen it so the tiles line up with the content column
    p.add("season", X0 - CHIP_INSET, TOP_KPI + 4, W + 2 * CHIP_INSET, 36, chips("Season", "season"))
    hy = TOP_KPI + 4 + 36 + GUT
    hm_w = 620
    p.add("hourWeek", X0, hy, hm_w, BOTTOM - hy, matrix(
        column("Profile", "hour"), column("Profile", "weekday_name"), "Hour cell", "Hour colour",
        "Mean concentration by hour of day (IST, rows) and weekday", cell_px=(hm_w - 16 - 40) // 7,
        fore_measure="Hour text", font=8, header_font=9, padding=padding_for(25, BOTTOM - hy, 8)))
    sx = X0 + hm_w + GUT
    sw = W - hm_w - GUT
    sh = (BOTTOM - hy - GUT) // 2
    p.add("hourSeason", sx, hy, sw, sh, chart(
        "lineChart", column("Profile", "hour"), ["Mean concentration"], "Hourly profile by season",
        series=column("Season", "season"),
        extra={"dataPoint": series_colors("Season", "season", list(zip(SEASONS, SEASON_SCALE)))}))
    p.add("weekday", sx, hy + sh + GUT, sw, BOTTOM - hy - sh - GUT, chart(
        "columnChart", column("Profile", "weekday_name"), ["Weekday vs week"], "Weekday mean against the whole week",
        extra={"dataPoint": [{"properties": {"fill": color(INK)}}], "legend": [{"properties": {"show": lit("false")}}],
               "labels": [{"properties": {"show": lit("true"), "color": color(MUTED), "fontSize": num(8)}}]}))
    p.no_filter("season", "hourSeason")
    pages.append(p)

    # ------------------------------------------------ 5 Trust
    p = Page("trust", "Trust")
    rail(p, "trust")
    header(p, PAGES[4][2], "Answer: trust", second=("Pollutant", "pollutant_name", "Pollutant", "pollutant"))
    p.add("kpis", X0, TOP_KPI, W, KPI_H, kpi_cards([
        ("Stations", "Stations"), ("Valid hours text", "Valid station-hours"), ("Readings set aside text", "Readings set aside"),
        ("Days missing text", "Station-days missing"), ("Completeness", "Completeness")]))
    cw = 480
    p.add("completeness", X0, TOP, cw, BOTTOM - TOP, matrix(
        city, column("Date", "year"), "Completeness cell", "Completeness colour",
        "Completeness: valid station-hours out of possible, by city and year",
        cell_px=(cw - 16 - ROW_HEADER_PX) // max(1, YEAR_COLUMNS), fore_measure="Completeness text", font=10, header_font=9,
        padding=padding_for(len(CITIES) + 1, BOTTOM - TOP, 10)))
    lx = X0 + cw + GUT
    lw = W - cw - GUT
    p.add("issues", lx, TOP, lw, BOTTOM - TOP, table(
        [(column("Issues", "issue"), "Issue"), (column("Issues", "action"), "Action taken"),
         (measure("Issue count text"), "Count")],
        "Issue log with the action taken, biggest first",
        widths=ISSUE_WIDTHS, wrap=True, right=("Issue count text",)))
    pages.append(p)
    return pages


ISSUE_WIDTHS = [236, 170, 82]       # sums to the log's width less 16 px; check_fit.py proves the cells fit
CHIP_INSET = 16


def theme():
    return {
        "name": "Editorial ledger",
        "dataColors": [INK, OCHRE, TEAL, RUST, PLUM, SLATE, "#9aa6c8", "#c9b99a"],
        "background": PAPER, "foreground": TEXT, "tableAccent": INK,
        "good": TEAL, "neutral": OCHRE, "bad": RUST,
        "textClasses": {
            "callout": {"fontFace": SERIF, "fontSize": 22, "color": TEXT},
            "title": {"fontFace": SANS, "fontSize": 10, "color": MUTED},
            "header": {"fontFace": SANS, "fontSize": 11, "color": TEXT},
            "label": {"fontFace": SANS, "fontSize": 10, "color": MUTED}},
        "visualStyles": {
            "page": {"*": {"background": [{"color": {"solid": {"color": PAPER}}, "transparency": 0}],
                           "outspace": [{"color": {"solid": {"color": PAPER}}}]}},
            "*": {"*": {"background": [{"show": False}], "border": [{"show": False}],
                        "dropShadow": [{"show": False}]}}},
    }


def build_report():
    d = RP / "definition"
    write_json(RP / ".platform", platform("Report"))
    write_json(RP / "definition.pbir", {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definitionProperties/2.0.0/schema.json",
        "version": "4.0", "datasetReference": {"byPath": {"path": f"../{NAME}.SemanticModel"}}})
    write_json(d / "version.json", {"$schema": f"{SCHEMA}/versionMetadata/1.0.0/schema.json", "version": "2.0.0"})
    write_json(RP / "StaticResources" / "RegisteredResources" / "theme.json", theme())
    write_json(d / "report.json", {
        "$schema": f"{SCHEMA}/report/3.0.0/schema.json",
        "themeCollection": {
            "baseTheme": {"name": "CY24SU10", "reportVersionAtImport": {"visual": "1.8.97", "report": "2.0.97", "page": "1.3.97"},
                          "type": "SharedResources"},
            "customTheme": {"name": "theme.json", "reportVersionAtImport": {"visual": "1.8.100", "report": "2.0.100", "page": "1.3.100"},
                            "type": "RegisteredResources"}},
        "resourcePackages": [
            {"name": "SharedResources", "type": "SharedResources",
             "items": [{"name": "CY24SU10", "path": "BaseThemes/CY24SU10.json", "type": "BaseTheme"}]},
            {"name": "RegisteredResources", "type": "RegisteredResources",
             "items": [{"name": "theme.json", "path": "theme.json", "type": "CustomTheme"}]}],
        "settings": {"useStylableVisualContainerHeader": True, "defaultDrillFilterOtherVisuals": True,
                     "useEnhancedTooltips": True, "hideVisualContainerHeader": True}})
    pages = build_pages()
    write_json(d / "pages" / "pages.json", {
        "$schema": f"{SCHEMA}/pagesMetadata/1.0.0/schema.json",
        "pageOrder": [p.key for p in pages], "activePageName": pages[0].key})
    for p in pages:
        page = {"$schema": f"{SCHEMA}/page/1.4.0/schema.json", "name": p.key, "displayName": p.display,
                "displayOption": "FitToPage", "height": 720, "width": 1280}
        if p.interactions:
            page["visualInteractions"] = p.interactions
        write_json(d / "pages" / p.key / "page.json", page)
        for v in p.visuals:
            folder = v.pop("_folder")
            write_json(d / "pages" / p.key / "visuals" / folder / "visual.json", v)


def read_members(con):
    """Members and counts that set colours and heatmap geometry."""
    global CITIES, SEASONS, CATEGORIES, MONTH_COLUMNS, YEAR_COLUMNS, ISSUE_ROWS, SPIKE_MAX_CELL, SPIKE_RULE
    CITIES = [r[0] for r in con.sql("SELECT city FROM dim_city ORDER BY city_order").fetchall()]
    SEASONS = [r[0] for r in con.sql("SELECT DISTINCT season, season_order FROM dim_date ORDER BY season_order").fetchall()]
    # (category, colour) by CPCB order, so a category absent from the data never shifts the colours
    CATEGORIES = [(c, AQI_SCALE[o - 1]) for c, o in con.sql(
        "SELECT DISTINCT category, category_order FROM mart_city_daily_aqi ORDER BY category_order").fetchall()]
    MONTH_COLUMNS = con.sql(f"""SELECT count(DISTINCT m) FROM (
        SELECT city, date_trunc('month', date) AS m FROM mart_city_daily_aqi
        GROUP BY ALL HAVING count(*) >= {MIN_MONTH_DAYS})""").fetchone()[0]
    YEAR_COLUMNS = con.sql("SELECT count(DISTINCT year) FROM dim_date").fetchone()[0]
    SPIKE_MAX_CELL = con.sql("SELECT max(n) FROM (SELECT count(*) AS n FROM mart_spike_episodes "
                             "GROUP BY city, month)").fetchone()[0] or 1
    rel = con.sql("SELECT * FROM ref_spike_rule")
    SPIKE_RULE = dict(zip(rel.columns, rel.fetchone()))
    ISSUE_ROWS = con.sql("SELECT count(*) FROM (SELECT DISTINCT issue, action FROM mart_dq_issues)").fetchone()[0]


def main():
    global DATA_FOLDER
    parser = argparse.ArgumentParser(description="Generate the Power BI project.")
    parser.add_argument("--db", default=str(DB), help="warehouse to read column types and members from")
    parser.add_argument("--data", help="folder of exported CSVs the model imports (default data/model)")
    args = parser.parse_args()
    if args.data:
        DATA_FOLDER = str(Path(args.data).resolve()) + "\\"
    for path in (SM, RP):
        if path.exists():
            shutil.rmtree(path)
    con = duckdb.connect(args.db, read_only=True)
    read_members(con)
    build_model(con)
    con.close()
    build_report()
    write_json(OUT / f"{NAME}.pbip", {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/pbip/pbipProperties/1.0.0/schema.json",
        "version": "1.0", "artifacts": [{"report": {"path": f"{NAME}.Report"}}], "settings": {"enableAutoRecovery": True}})
    print(f"wrote {OUT / (NAME + '.pbip')}")


if __name__ == "__main__":
    main()
