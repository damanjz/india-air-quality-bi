-- Data-quality mart: one row per issue, city, station and pollutant, with what was done about it.
-- level: reading (one archive row), hour, day, station-day (whole days missing from the archive), station.

CREATE OR REPLACE TABLE mart_dq_issues AS
WITH reading AS (
    SELECT 'reading' AS level, disposition AS issue,
           CASE WHEN disposition = 'outside study window' THEN 'excluded (time zone edge of the window)'
                WHEN disposition IN ('exact duplicate', 'duplicate across station ids') THEN 'one copy kept'
                ELSE 'set aside' END AS action,
           city, station_id, pollutant, count(*) AS n
    FROM stg_readings WHERE disposition <> 'valid' GROUP BY ALL
), relabelled AS (
    SELECT 'reading', 'gas unit labelled ppb (value is CPCB µg/m³, CO mg/m³)', 'kept, label corrected',
           city, station_id, pollutant, count(*)
    FROM stg_readings WHERE units_label = 'ppb' AND disposition = 'valid' GROUP BY ALL
), ceiling AS (
    SELECT 'reading', 'particulate at the analyser ceiling (999.99-1000 µg/m³)', 'kept, reported (true value may be higher)',
           city, station_id, pollutant, count(*)
    FROM stg_readings WHERE disposition = 'valid' AND pollutant IN ('pm25', 'pm10') AND value BETWEEN 999.99 AND 1000
    GROUP BY ALL
), pm_ratio AS (
    SELECT 'hour', 'PM2.5 above PM10 in the same hour', 'kept, reported',
           s.city, a.station_id, 'pm25', count(*)
    FROM fact_measurement_hourly a
    JOIN fact_measurement_hourly b ON b.station_id = a.station_id AND b.hour_ist = a.hour_ist AND b.pollutant = 'pm10'
    JOIN dim_station s ON s.station_id = a.station_id
    WHERE a.pollutant = 'pm25' AND a.value > b.value
    GROUP BY ALL
), spike AS (
    -- one hour over 3x both neighbouring hours and above 150 µg/m³: possibly a fault, possibly a real short local
    -- event; there is no basis to tell, so the hour is kept and counted
    SELECT 'hour', 'isolated one-hour particulate spike', 'kept, reported', s.city, h.station_id, h.pollutant, count(*)
    FROM (SELECT *, lag(value) OVER w AS prev_v, lead(value) OVER w AS next_v,
                 lag(hour_ist) OVER w AS prev_h, lead(hour_ist) OVER w AS next_h
          FROM fact_measurement_hourly WHERE pollutant IN ('pm25', 'pm10')
          WINDOW w AS (PARTITION BY station_id, pollutant ORDER BY hour_ist)) h
    JOIN dim_station s ON s.station_id = h.station_id
    WHERE h.value > 150 AND h.value > 3 * h.prev_v AND h.value > 3 * h.next_v
      AND h.prev_h = h.hour_ist - INTERVAL 1 HOUR AND h.next_h = h.hour_ist + INTERVAL 1 HOUR
    GROUP BY ALL
), short_day AS (
    SELECT 'day', 'incomplete day (under 16 valid hours)', 'no daily value or AQI', city, station_id, pollutant, count(*)
    FROM fact_measurement_daily WHERE NOT is_complete GROUP BY ALL
), missing_day AS (
    -- station-days in the window with no archive file at all, for any pollutant
    SELECT 'station-day', 'day missing from the archive', 'no data', s.city, s.station_id, NULL, count(*)
    FROM dim_station s CROSS JOIN dim_date d
    WHERE NOT EXISTS (SELECT 1 FROM stg_station_days x WHERE x.station_id = s.station_id AND x.date = d.date)
    GROUP BY ALL
), moved AS (
    SELECT 'station', 'published coordinates changed by over 100 m', 'kept, one station; latest coordinates used',
           city, station_id, NULL, 1
    FROM dim_station WHERE moved_m > 100
), multi_id AS (
    SELECT 'station', 'station published under several OpenAQ ids', 'ids merged into one station',
           city, station_id, NULL, n_location_ids - 1
    FROM dim_station WHERE n_location_ids > 1
)
SELECT * FROM reading UNION ALL SELECT * FROM relabelled UNION ALL SELECT * FROM ceiling UNION ALL SELECT * FROM pm_ratio UNION ALL SELECT * FROM spike
UNION ALL SELECT * FROM short_day UNION ALL SELECT * FROM missing_day UNION ALL SELECT * FROM moved
UNION ALL SELECT * FROM multi_id;

-- Overview: total per issue.
CREATE OR REPLACE TABLE mart_dq_summary AS
SELECT level, issue, action, sum(n) AS n, count(DISTINCT station_id) AS stations
FROM mart_dq_issues GROUP BY ALL;
