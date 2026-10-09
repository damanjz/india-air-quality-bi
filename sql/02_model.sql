-- Star schema: dimensions for city, station, pollutant and date; hourly and daily measurement facts.
-- Only readings with disposition 'valid' reach the facts. No hour or day is filled in.

CREATE OR REPLACE TABLE dim_city AS
SELECT city, state, region, sort_order AS city_order FROM ref_cities;

CREATE OR REPLACE TABLE dim_pollutant AS
SELECT pollutant, name AS pollutant_name, unit, aqi_averaging, naaqs_24h_or_8h AS naaqs_limit, naaqs_averaging,
       naaqs_annual, sort_order AS pollutant_order
FROM ref_pollutants;

-- One row per monitoring station. A station can span several OpenAQ location ids (ids re-issued over the
-- years); its coordinates are the most recent published, and moved_m is the largest distance between any
-- two published coordinates of the station.
CREATE OR REPLACE TABLE dim_station AS
WITH coords AS (
    SELECT s.station_id, c.lat, c.lon, c.first_day, c.last_day
    FROM stg_location_coords c JOIN ref_stations s ON s.location_id = c.location_id AND s.included
    WHERE c.lat IS NOT NULL AND c.lon IS NOT NULL
), spread AS (
    SELECT a.station_id,
           max(6371000 * sqrt(pow(radians(a.lat - b.lat), 2)
               + pow(radians(a.lon - b.lon) * cos(radians((a.lat + b.lat) / 2)), 2))) AS moved_m,
           count(DISTINCT (a.lat, a.lon)) AS n_coordinates
    FROM coords a JOIN coords b USING (station_id) GROUP BY ALL
), latest AS (
    SELECT station_id, arg_max(lat, (last_day, lat, lon)) AS lat, arg_max(lon, (last_day, lat, lon)) AS lon
    FROM coords GROUP BY ALL
), hours AS (
    SELECT station_id, min(hour_ist) AS first_hour, max(hour_ist) AS last_hour
    FROM stg_readings WHERE disposition = 'valid' GROUP BY ALL
)
SELECT s.station_id, any_value(s.city) AS city, any_value(s.site) AS station_name, any_value(s.agency) AS agency,
       string_agg(CAST(s.location_id AS VARCHAR), ' ' ORDER BY s.location_id) AS openaq_location_ids,
       count(*) AS n_location_ids,
       any_value(l.lat) AS lat, any_value(l.lon) AS lon,
       round(any_value(sp.moved_m), 0) AS moved_m, any_value(sp.n_coordinates) AS n_coordinates,
       any_value(h.first_hour) AS first_hour, any_value(h.last_hour) AS last_hour
FROM ref_stations s
LEFT JOIN latest l USING (station_id)
LEFT JOIN spread sp USING (station_id)
LEFT JOIN hours h USING (station_id)
WHERE s.included
GROUP BY s.station_id;

-- Calendar for the study window. Seasons follow the India Meteorological Department:
-- winter Jan-Feb, pre-monsoon Mar-May, monsoon Jun-Sep, post-monsoon Oct-Dec.
CREATE OR REPLACE TABLE dim_date AS
WITH d AS (SELECT CAST(range AS DATE) AS date FROM range(TIMESTAMP '2019-01-01', TIMESTAMP '2026-10-01', INTERVAL 1 DAY))
SELECT d.date, year(d.date) AS year, month(d.date) AS month, strftime(d.date, '%b') AS month_name,
       CAST(date_trunc('month', d.date) AS DATE) AS month_start, quarter(d.date) AS quarter,
       isodow(d.date) AS weekday_no, strftime(d.date, '%a') AS weekday_name, isodow(d.date) >= 6 AS is_weekend,
       CASE WHEN month(d.date) IN (1, 2) THEN 'Winter' WHEN month(d.date) IN (3, 4, 5) THEN 'Pre-monsoon'
            WHEN month(d.date) IN (6, 7, 8, 9) THEN 'Monsoon' ELSE 'Post-monsoon' END AS season,
       CASE WHEN month(d.date) IN (1, 2) THEN 1 WHEN month(d.date) IN (3, 4, 5) THEN 2
            WHEN month(d.date) IN (6, 7, 8, 9) THEN 3 ELSE 4 END AS season_order,
       month(d.date) IN (10, 11) AS is_oct_nov
FROM d;

-- Hourly fact: the mean of the valid readings in each station-pollutant-hour, with the reading count.
CREATE OR REPLACE TABLE fact_measurement_hourly AS
SELECT station_id, pollutant, hour_ist, CAST(hour_ist AS DATE) AS date, hour(hour_ist) AS hour,
       CAST(round(avg(value), 4) AS DECIMAL(18, 4)) AS value, count(*) AS n_readings
FROM stg_readings WHERE disposition = 'valid'
GROUP BY ALL;

-- Daily fact. A day counts as complete with 16 or more valid hours (CPCB's minimum for a 24-hour value).
-- PM2.5, PM10, NO2 and SO2: mean of the valid hours. O3 and CO: the highest 8-hour rolling mean whose
-- window holds at least 6 valid hours, as CPCB uses 8-hour values for these two.
CREATE OR REPLACE TABLE fact_measurement_daily AS
WITH rolling AS (
    SELECT h.station_id, h.pollutant, h.date,
           avg(h.value) OVER w AS mean_8h, count(*) OVER w AS hours_8h
    FROM fact_measurement_hourly h
    WHERE h.pollutant IN ('o3', 'co')
    WINDOW w AS (PARTITION BY h.station_id, h.pollutant ORDER BY h.hour_ist
                 RANGE BETWEEN INTERVAL 7 HOUR PRECEDING AND CURRENT ROW)
), max8 AS (
    SELECT station_id, pollutant, date, max(mean_8h) AS max_8h
    FROM rolling WHERE hours_8h >= 6 GROUP BY ALL
), day AS (
    SELECT station_id, pollutant, date, avg(value) AS mean_24h, count(*) AS valid_hours,
           sum(n_readings) AS n_readings, max(value) AS max_hour
    FROM fact_measurement_hourly GROUP BY ALL
)
SELECT d.station_id, s.city, d.pollutant, d.date, d.valid_hours, d.n_readings,
       d.valid_hours >= 16 AS is_complete,
       CAST(round(d.mean_24h, 3) AS DECIMAL(18, 3)) AS mean_24h, CAST(round(m.max_8h, 3) AS DECIMAL(18, 3)) AS max_8h,
       d.max_hour,
       CAST(round(CASE WHEN d.pollutant IN ('o3', 'co') THEN m.max_8h ELSE d.mean_24h END, 3) AS DECIMAL(18, 3)) AS aqi_value
FROM day d
JOIN dim_station s USING (station_id)
LEFT JOIN max8 m USING (station_id, pollutant, date);
