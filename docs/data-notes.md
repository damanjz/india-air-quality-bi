# Data notes

How the data was obtained, what is wrong with it, and what the pipeline does about each problem.
Numbers in this file come from the build of 2026-10-09; `docs/findings.md` and the workbook are regenerated on every build.

## Source

| | |
|---|---|
| Archive | OpenAQ data archive on AWS Open Data, `s3://openaq-data-archive`, read over anonymous HTTPS (`https://openaq-data-archive.s3.amazonaws.com/records/csv.gz/locationid={id}/year={yyyy}/month={mm}/location-{id}-{yyyymmdd}.csv.gz`). No account, no API key. |
| Underlying data | Continuous Ambient Air Quality Monitoring Stations (CAAQMS) of the Central Pollution Control Board, state boards (DPCC, MPCB, KSPCB, TSPCB, TNPCB, WBPCB), IMD and IITM, as published on CPCB's CCR portal and collected by OpenAQ. |
| Downloaded | 2026-10-09 IST (2026-10-08 UTC). Every file's URL, size, ETag (MD5, verified), SHA-256 and UTC download time: `data/manifest.csv` (rebuilt by `src/download.py`). Per-location file counts and a digest over all file SHA-256s: `reference/manifest_summary.csv` (committed). |
| Window | 2019-01-01 to 2026-09-30 (last full month at download). |
| Attribution and licence | Credit CPCB and the state boards as the data owners, and OpenAQ. OpenAQ passes on each provider's own licence; its archive listing gives the licence as varying by provider, and the per-location licence is only exposed through the keyed API, which this project does not use. The report footer says the same: CPCB and state board stations via OpenAQ, licence per provider. |

### Finding the stations

The archive has no station list, and OpenAQ's API needs a key, which this project does not use. `src/discover.py` reads
the stations from the archive itself: it lists all 55,965 location ids and reads the first row of one file per id
(name, latitude, longitude) with a 2 KB range request, checkpointing every probe so an interrupted run resumes.
1,494 locations fall inside India's bounding box (which also covers parts of neighbouring countries); 170 of them are
government stations in the six cities. Result: `reference/openaq_locations_india_bbox.csv`.

Station names follow `"<site>, <city> - <agency>-<number>"`. Locations in the six cities are grouped into stations by
city, site name and agency (`reference/stations.csv`, with the decision and reason for every location).

Selection rules:
- at least 365 days of archive files in the window per station (`MIN_DAYS_IN_WINDOW` in `src/config.py`): 107 stations. The first download used 600 days to stay within a 300 MB budget (88 stations, 289.2 MB); the 19 stations that rule left out were added on 2026-10-09: five in Hyderabad that start in 2022, thirteen that start in February 2025 (nine in Mumbai, three in Bengaluru, one in Kolkata) and Bandra (Mumbai, 2020-2021);
- one monitor per site: where two operators publish the same site name in the same city less than 1 km apart, the longer record is kept;
- Navi Mumbai and other satellite cities are separate cities in the source and are not included;
- US diplomatic post monitors are not included (in the archive they stop in 2016 for five of the six cities).

Download budget: 300 MB, agreed before the first download; raised to 350 MB on Daman's approval on 2026-10-09 to add
the 19 later stations (337.4 MB in total). `src/download.py` refuses a plan above the budget.

Station sets grow over time. In Mumbai 9 of 28 stations first report in 2025 (and 10 more in 2020); in Hyderabad 8 of
14 first report in 2022; in Bengaluru 3 of 13 and in Kolkata 1 of 7 first report in 2025 (first valid hour per station,
`dim_station`). A city's average after those dates draws on more, and different, sites than before. City-level
figures across years are therefore not like-for-like; the station-level tables (`fact_measurement_daily`,
`mart_exceedance_station_year`, `mart_station_completeness`) allow comparisons on a fixed set of stations.

## Problems in the data and what is done

Nothing is filled, interpolated or estimated. Each reading of the six AQI pollutants (PM2.5, PM10, NO2, SO2, O3, CO)
gets exactly one disposition; only `valid` readings reach the facts. Counts per issue, city, station and pollutant are in
`mart_dq_issues` (summary: `mart_dq_summary`, and the workbook's Summary sheet).

| Issue | Rule | Action |
|---|---|---|
| Archive gaps | Days with no file for a station | Left empty. The archive holds no Indian station data from November 2022 to mid-February 2025, and many days are missing in January-June 2019 and in 2022 (2022 has no July or August). |
| Unparseable timestamp or value | `TRY_CAST` fails | Set aside |
| Time zones | Timestamps carry a UTC offset; converted to IST arithmetically (IST has no daylight saving) | Readings are period-ending: a 17:00 stamp belongs to the 16:00-17:00 hour |
| Units | Up to 2022 gases are labelled µg/m³ (CO in µg/m³, converted to mg/m³). From 2025 NO2, SO2 and CO are labelled "ppb", but the values are CPCB's µg/m³ (CO mg/m³): CO "ppb" sits near 1 (ambient CO is hundreds of ppb) and NO2 "ppb" matches the same stations' µg/m³ level before the gap | Label corrected, counted; `checks.sql` re-tests the evidence on every build |
| Negative values | value < 0 | Set aside |
| Zero particulates | PM2.5 or PM10 exactly 0 | Set aside (physically implausible for an Indian city) |
| Fault code 985 | PM2.5 or PM10 exactly 985: in the staged readings exactly 985 occurs 22,707 times against 15 and 20 times for 984 and 986, and after zero it is the most common frozen particulate value (6,265 hours in runs of 8+ hours, the longest 283 hours at Colaba) | Set aside |
| Analyser ceiling | PM2.5 or PM10 at 999.99-1000 µg/m³ (the analyser's upper limit) | Kept and counted: real, but the true value may be higher |
| Sensor faults | Above a plausibility ceiling (PM2.5 1000, PM10 2000, NO2 1000, SO2 2000, O3 1000 µg/m³, CO 50 mg/m³) | Set aside |
| Stuck values | Identical value for 8+ consecutive hours (e.g. PM10 frozen at 985 for 92 hours at Worli) | Set aside |
| Exact duplicates | Same station, pollutant, timestamp and value | One copy kept |
| Duplicates across station ids | The same reading published under two OpenAQ ids of one station (ids were re-issued) | One copy kept |
| Conflicting duplicates | Same station, pollutant and timestamp, different values. Happens at two stations from February 2025, where an old and a new OpenAQ id both publish: Pusa (Delhi, IMD; same coordinates) and Worli (Mumbai, MPCB; the old id moved 730 m). The two ids rarely publish the same PM2.5 value at the same timestamp (31 of 18,368 shared timestamps at Pusa, 3 of 29,630 at Worli; correlation 0.76 and 0.19), so they are not the same feed | All copies set aside: there is no basis to choose |
| Unknown unit | O3 labelled ppm at one Delhi station (North Campus, IMD) | Set aside, not converted |
| Station moves | Published coordinates of one station differ by more than 100 m (across ids or over time). Shifts of a few hundred metres to 2 km look like re-surveys; shifts of 30-360 km (Manali Village, ICRISAT Patancheru, Borivali East) are geocoding errors (one point is in the sea or another district) | Kept as one station; latest coordinates used; counted |
| Incomplete days | Under 16 valid hours | No daily value and no AQI for that day |
| PM2.5 above PM10 | Same station and hour | Kept and counted (separate instruments; not physically impossible within error) |

## AQI method

CPCB National Air Quality Index: Central Pollution Control Board, *National Air Quality Index*, Control of Urban
Pollution Series CUPS/82/2014-15, October 2014 (breakpoint table, sub-index formula and minimum-data rules).
Breakpoints: `reference/cpcb_aqi_breakpoints.csv`.

- Sub-index: linear interpolation within the band, `I = I_lo + (I_hi - I_lo) / (B_hi - B_lo) * (C - B_lo)`. Bands are treated as continuous (the published bands are integer ranges such as 31-60; real-valued concentrations fall between them).
- The Severe band is open-ended in CPCB's table. It is closed here at the lower bound plus the width of the Very Poor band (PM2.5 380, PM10 510, NO2 520, O3 1288, SO2 2400 µg/m³, CO 51 mg/m³); concentrations at or above that score 500.
- Averaging: 24-hour mean for PM2.5, PM10, NO2, SO2; highest 8-hour rolling mean (window with 6+ valid hours) for O3 and CO.
- A day needs 16 valid hours. A station AQI needs three pollutants with complete days, one of them PM2.5 or PM10; it is the highest sub-index, and that pollutant is the prominent pollutant. NH3 and Pb are not in the archive.
- City AQI: mean of the station AQIs that day (as in CPCB's daily bulletin). The station set changes over time (Mumbai and Hyderabad gain stations in later years, see above), which shifts city averages; station-level tables are provided for like-for-like comparisons.

## Standards and seasons

- NAAQS: Central Pollution Control Board, National Ambient Air Quality Standards, Gazette of India notification of 18 November 2009. 24-hour limits: PM2.5 60, PM10 100, NO2 80, SO2 80 µg/m³; 8-hour: O3 100 µg/m³, CO 2 mg/m³ (`reference/pollutants.csv`).
- Seasons: India Meteorological Department (winter Jan-Feb, pre-monsoon Mar-May, monsoon Jun-Sep, post-monsoon Oct-Dec).
- October-November (`mart_octnov_season`): compared with September and December. The marts describe levels only; the dataset has no emissions or weather data, so no cause is attributed.

## Short spikes

Found from the data alone (`mart_spike_days`, `mart_spike_runs`, `mart_spike_episodes`); there is no event calendar,
no label and no cause. The rule is in `src/config.py` and travels with the data as `ref_spike_rule`.

- Reference: for each city-day, the median city PM2.5 of the days 4 to 14 days before and after it. The 3 days either
  side are left out so that an episode of up to 3 days never sets its own reference. A day is evaluated when at least
  6 of the 11 reference days are measured on each side.
- Spike day: city PM2.5 at least 2.0 times the reference. Double the surrounding level is a plain, round threshold; it
  marks 2.5% of evaluated city-days (144 of 5,724).
- Short spike: a run of 1 to 3 consecutive spike days whose neighbouring days are measured and evaluated (so its length
  is known). Longer runs and runs next to a gap are kept in `mart_spike_runs` with the reason and are not counted.
- Each episode records its start and end, length, peak day, peak PM2.5, the reference on the peak day and the ratio,
  and how many of the stations measured on the peak day were themselves at 2.0 times the city reference or more. A
  city value is a mean of stations, so a spike carried by one monitor is shown as such, not hidden.
