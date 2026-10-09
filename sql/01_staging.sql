-- Staging: read every downloaded archive file as text, type it, put it on Indian time and canonical units,
-- and give every reading exactly one disposition. Nothing is filled or estimated: a reading is either used
-- as published or set aside with a named reason, and every reason is counted in mart_dq_issues.

-- Reference tables (committed CSVs in reference/)
CREATE OR REPLACE TABLE ref_stations AS
SELECT station_id, city, site, agency, CAST(location_id AS INTEGER) AS location_id, location,
       CAST(lat AS DOUBLE) AS lat, CAST(lon AS DOUBLE) AS lon,
       CAST(days_in_window AS INTEGER) AS days_in_window, included = '1' AS included, reason
FROM read_csv('reference/stations.csv', all_varchar = true);

CREATE OR REPLACE TABLE ref_pollutants AS
SELECT * FROM read_csv('reference/pollutants.csv', header = true);

CREATE OR REPLACE TABLE ref_breakpoints AS
-- Exact decimals, so a concentration exactly halfway to the next AQI point rounds the same way every time.
SELECT pollutant, category, CAST(aqi_lo AS DECIMAL(10, 3)) AS aqi_lo, CAST(aqi_hi AS DECIMAL(10, 3)) AS aqi_hi,
       CAST(conc_lo AS DECIMAL(10, 3)) AS conc_lo, CAST(conc_hi AS DECIMAL(10, 3)) AS conc_hi
FROM read_csv('reference/cpcb_aqi_breakpoints.csv', all_varchar = true);

CREATE OR REPLACE TABLE ref_cities AS SELECT * FROM read_csv('reference/cities.csv', header = true);

-- Unit rules. The archive labels gases in µg/m³ up to 2022 and in "ppb" from 2025. The "ppb" values are
-- not ppb: CO labelled ppb sits around 1 (ambient CO is hundreds of ppb), and NO2/SO2 labelled ppb match
-- the same stations' µg/m³ levels before the gap. They are CPCB's own units (µg/m³; CO in mg/m³) under a
-- wrong label. sql/checks.sql tests this evidence on every build ("ppb" labels are CPCB units).
CREATE OR REPLACE TABLE ref_unit_rules (pollutant VARCHAR, units_label VARCHAR, factor DOUBLE, basis VARCHAR);
INSERT INTO ref_unit_rules VALUES
    ('pm25', 'µg/m³', 1, 'as published'),
    ('pm10', 'µg/m³', 1, 'as published'),
    ('no2', 'µg/m³', 1, 'as published'),
    ('so2', 'µg/m³', 1, 'as published'),
    ('o3', 'µg/m³', 1, 'as published'),
    ('co', 'µg/m³', 0.001, 'µg/m³ to mg/m³'),
    ('no2', 'ppb', 1, 'label is wrong: value is CPCB µg/m³'),
    ('so2', 'ppb', 1, 'label is wrong: value is CPCB µg/m³'),
    ('co', 'ppb', 1, 'label is wrong: value is CPCB mg/m³');

-- Plausibility ceilings: above these a reading is an instrument fault, not air (analysers top out near 1000 µg/m³).
CREATE OR REPLACE TABLE ref_plausible_max (pollutant VARCHAR, max_value DOUBLE);
INSERT INTO ref_plausible_max VALUES ('pm25', 1000), ('pm10', 2000), ('no2', 1000), ('so2', 2000), ('o3', 1000), ('co', 50);

CREATE OR REPLACE MACRO window_start() AS DATE '2019-01-01';
CREATE OR REPLACE MACRO window_end() AS DATE '2026-09-30';

-- Every row of every file, as text. The layout is fixed and declared, so no file is sniffed. It is a temporary
-- table (48 million rows); raw_file_rows below keeps its row counts per file and parameter for the checks.
CREATE OR REPLACE TEMP TABLE raw_readings AS
SELECT regexp_extract(replace(filename, '\', '/'), 'locationid=[0-9]+/year=[0-9]+/month=[0-9]+/[^/]+$') AS file,
       location_id, sensors_id, location, datetime, lat, lon, parameter, units, value
FROM read_csv('data/raw/locationid=*/year=*/month=*/*.csv.gz', header = true, auto_detect = false,
              delim = ',', quote = '"', escape = '"', filename = true, hive_partitioning = false,
              columns = {'location_id': 'VARCHAR', 'sensors_id': 'VARCHAR', 'location': 'VARCHAR',
                         'datetime': 'VARCHAR', 'lat': 'VARCHAR', 'lon': 'VARCHAR', 'parameter': 'VARCHAR',
                         'units': 'VARCHAR', 'value': 'VARCHAR'});

CREATE OR REPLACE TABLE raw_file_rows AS
SELECT file, TRY_CAST(location_id AS INTEGER) AS location_id, parameter, count(*) AS n_rows
FROM raw_readings GROUP BY ALL;

-- Coordinates as published, per location and day (used to detect station moves).
CREATE OR REPLACE TABLE stg_location_coords AS
SELECT CAST(location_id AS INTEGER) AS location_id, CAST(lat AS DOUBLE) AS lat, CAST(lon AS DOUBLE) AS lon,
       min(CAST(left(datetime, 10) AS DATE)) AS first_day, max(CAST(left(datetime, 10) AS DATE)) AS last_day,
       count(*) AS n_rows
FROM raw_readings GROUP BY ALL;

-- Days on which the archive has a file for a station (any parameter).
CREATE OR REPLACE TABLE stg_station_days AS
SELECT DISTINCT s.station_id, strptime(regexp_extract(r.file, '-([0-9]{8})\.csv', 1), '%Y%m%d')::DATE AS date
FROM raw_readings r JOIN ref_stations s ON s.location_id = TRY_CAST(r.location_id AS INTEGER) AND s.included;

-- Typed readings of the six AQI pollutants. Values are stored as exact decimals (6 places) so every later
-- sum and mean is exact and independent of the order DuckDB's threads add them in.
-- Timestamps carry their UTC offset; they are converted to IST
-- arithmetically (IST has no daylight saving), so no time-zone database is involved. Archive timestamps
-- mark the END of the averaging period, so a reading stamped 17:00 covers 16:45-17:00 and belongs to the
-- 16:00 hour.
CREATE OR REPLACE TABLE stg_typed AS
WITH typed AS (
    SELECT r.file, s.station_id, s.city,
           TRY_CAST(r.location_id AS INTEGER) AS location_id,
           TRY_CAST(r.sensors_id AS BIGINT) AS sensor_id,
           r.parameter AS pollutant, r.units AS units_label, r.datetime AS datetime_raw, r.value AS value_raw,
           TRY_STRPTIME(left(r.datetime, 19), '%Y-%m-%dT%H:%M:%S') AS local_ts,
           CASE WHEN regexp_matches(r.datetime, '[+-][0-9]{2}:[0-9]{2}$')
                THEN (CASE WHEN substr(r.datetime, -6, 1) = '-' THEN -1 ELSE 1 END)
                     * (CAST(substr(r.datetime, -5, 2) AS INTEGER) * 60 + CAST(substr(r.datetime, -2, 2) AS INTEGER))
           END AS offset_min,
           TRY_CAST(r.value AS DOUBLE) AS value_num
    FROM raw_readings r
    JOIN ref_stations s ON s.location_id = TRY_CAST(r.location_id AS INTEGER) AND s.included
    WHERE r.parameter IN ('pm25', 'pm10', 'no2', 'so2', 'o3', 'co')
), ist AS (
    SELECT t.*, t.local_ts - to_minutes(t.offset_min) + INTERVAL 330 MINUTE AS ts_ist,
           u.factor, TRY_CAST(t.value_num * u.factor AS DECIMAL(18, 6)) AS value, p.max_value
    FROM typed t
    LEFT JOIN ref_unit_rules u ON u.pollutant = t.pollutant AND u.units_label = t.units_label
    LEFT JOIN ref_plausible_max p ON p.pollutant = t.pollutant
)
SELECT *, date_trunc('hour', ts_ist - INTERVAL 1 SECOND) AS hour_ist,
       -- first matching rule wins
       CASE WHEN local_ts IS NULL OR offset_min IS NULL THEN 'unparseable timestamp'
            WHEN value_num IS NULL THEN 'unparseable value'
            WHEN factor IS NULL THEN 'unknown unit'
            WHEN value IS NULL THEN 'unparseable value'
            WHEN CAST(date_trunc('hour', ts_ist - INTERVAL 1 SECOND) AS DATE) NOT BETWEEN window_start() AND window_end()
                THEN 'outside study window'
            WHEN value < 0 THEN 'negative value'
            WHEN value = 0 AND pollutant IN ('pm25', 'pm10') THEN 'zero particulate reading'
            -- 985 is an analyser status code, not a concentration: exactly 985 occurs over 700 times as often as
            -- 984 or 986 (see docs/data-notes.md)
            WHEN value = 985 AND pollutant IN ('pm25', 'pm10') THEN 'instrument fault code 985'
            WHEN value > max_value THEN 'above plausible maximum'
       END AS rule_issue
FROM ist;

-- Duplicates. The same reading can arrive twice: inside one location (resent rows) or through two OpenAQ
-- locations of one station (the archive re-created many Indian stations under new ids in February 2025).
-- Identical values: keep one, mark the rest. Different values for the same station, pollutant and timestamp:
-- there is no basis to choose, so all of them are set aside.
CREATE OR REPLACE TABLE stg_flagged AS
SELECT *,
       CASE WHEN rule_issue IS NOT NULL THEN rule_issue
            WHEN min(value) OVER k <> max(value) OVER k THEN 'conflicting duplicate'
            WHEN row_number() OVER (PARTITION BY rule_issue IS NULL, station_id, pollutant, ts_ist
                                    ORDER BY location_id, sensor_id, file) > 1
                 THEN CASE WHEN min(location_id) OVER k <> max(location_id) OVER k
                           THEN 'duplicate across station ids' ELSE 'exact duplicate' END
       END AS issue
FROM stg_typed
WINDOW k AS (PARTITION BY rule_issue IS NULL, station_id, pollutant, ts_ist);

-- Stuck sensors: the same value in every reading for 8 or more consecutive hours. A real atmosphere does
-- not hold a concentration to the decimal for eight hours; the feed has frozen.
CREATE OR REPLACE TABLE stg_stuck_hours AS
WITH hv AS (
    SELECT station_id, pollutant, hour_ist, min(value) AS vmin, max(value) AS vmax
    FROM stg_flagged WHERE issue IS NULL GROUP BY ALL
), h AS (
    SELECT *, vmin = vmax AS flat,
           lag(vmin) OVER w AS prev_v, lag(hour_ist) OVER w AS prev_h, lag(vmin = vmax) OVER w AS prev_flat
    FROM hv WINDOW w AS (PARTITION BY station_id, pollutant ORDER BY hour_ist)
), runs AS (
    SELECT *, sum(CASE WHEN flat AND prev_flat AND vmin = prev_v AND hour_ist = prev_h + INTERVAL 1 HOUR
                       THEN 0 ELSE 1 END) OVER (PARTITION BY station_id, pollutant ORDER BY hour_ist) AS run_id
    FROM h
)
SELECT station_id, pollutant, hour_ist, vmin AS value, run_id,
       count(*) OVER (PARTITION BY station_id, pollutant, run_id) AS run_hours
FROM runs WHERE flat
QUALIFY run_hours >= 8;

-- One disposition per reading.
CREATE OR REPLACE TABLE stg_readings AS
SELECT f.* EXCLUDE (issue),
       coalesce(f.issue, CASE WHEN s.hour_ist IS NOT NULL THEN 'stuck value' END, 'valid') AS disposition
FROM stg_flagged f
LEFT JOIN stg_stuck_hours s ON s.station_id = f.station_id AND s.pollutant = f.pollutant AND s.hour_ist = f.hour_ist;

DROP TABLE stg_typed;
DROP TABLE stg_flagged;
