-- Each check returns the rows that break a rule. A clean build returns zero rows for every check.
-- src/mutation_test.py breaks the data once per check and confirms the check catches it.

-- name: every raw row of an included station is staged or out of scope, once
SELECT r.n_raw, s.n_staged, o.n_out_of_scope
FROM (SELECT sum(n_rows) AS n_raw FROM raw_file_rows r JOIN ref_stations s
      ON s.location_id = r.location_id AND s.included) r,
     (SELECT count(*) AS n_staged FROM stg_readings) s,
     (SELECT sum(n_rows) AS n_out_of_scope FROM raw_file_rows r JOIN ref_stations s
      ON s.location_id = r.location_id AND s.included
      WHERE r.parameter NOT IN ('pm25', 'pm10', 'no2', 'so2', 'o3', 'co')) o
WHERE r.n_raw <> s.n_staged + o.n_out_of_scope;

-- name: every staged reading has exactly one known disposition
SELECT disposition, count(*) FROM stg_readings
WHERE disposition IS NULL OR disposition NOT IN ('valid', 'unparseable timestamp', 'unparseable value', 'unknown unit',
      'outside study window', 'negative value', 'zero particulate reading', 'instrument fault code 985', 'above plausible maximum',
      'conflicting duplicate', 'duplicate across station ids', 'exact duplicate', 'stuck value')
GROUP BY ALL;

-- name: valid readings are on IST, in the window, in range and unique per station, pollutant and time
SELECT station_id, pollutant, ts_ist, count(*) FROM stg_readings
WHERE disposition = 'valid'
GROUP BY ALL
HAVING count(*) > 1 OR min(value) < 0 OR max(value) > any_value(max_value)
    OR CAST(min(hour_ist) AS DATE) < DATE '2019-01-01' OR CAST(max(hour_ist) AS DATE) > DATE '2026-09-30'
    OR bool_or(ts_ist <> local_ts - to_minutes(offset_min) + INTERVAL 330 MINUTE)
    OR bool_or(hour_ist <> date_trunc('hour', ts_ist - INTERVAL 1 SECOND));

-- name: "ppb" gas labels are CPCB units, not ppb (CO median under 20, NO2 level matches the same stations before)
WITH co AS (
    SELECT median(value_num) AS m FROM stg_readings WHERE pollutant = 'co' AND units_label = 'ppb' AND disposition = 'valid'
), no2 AS (
    SELECT median(value) FILTER (units_label = 'ppb') AS after_m, median(value) FILTER (units_label = 'µg/m³') AS before_m
    FROM stg_readings
    WHERE pollutant = 'no2' AND disposition = 'valid' AND month(hour_ist) IN (3, 4, 5, 6, 7, 8, 9)
      AND station_id IN (SELECT station_id FROM stg_readings WHERE units_label = 'ppb'
                         INTERSECT SELECT station_id FROM stg_readings WHERE units_label = 'µg/m³')
)
SELECT co.m, no2.after_m, no2.before_m FROM co, no2
WHERE co.m >= 20 OR no2.after_m / no2.before_m NOT BETWEEN 0.67 AND 1.5;

-- name: hourly values equal the mean of their valid readings, with the reading count
SELECT h.station_id, h.pollutant, h.hour_ist
FROM fact_measurement_hourly h
FULL JOIN (SELECT station_id, pollutant, hour_ist, CAST(round(avg(value), 4) AS DECIMAL(18, 4)) AS v, count(*) AS n
           FROM stg_readings WHERE disposition = 'valid' GROUP BY ALL) r
USING (station_id, pollutant, hour_ist)
WHERE h.value IS DISTINCT FROM r.v OR h.n_readings IS DISTINCT FROM r.n;

-- name: no hour or day is invented: every fact row has valid readings behind it
SELECT 'hour', h.station_id, h.pollutant, CAST(h.hour_ist AS VARCHAR) FROM fact_measurement_hourly h
WHERE h.n_readings < 1 OR NOT EXISTS (SELECT 1 FROM stg_readings r WHERE r.disposition = 'valid'
      AND r.station_id = h.station_id AND r.pollutant = h.pollutant AND r.hour_ist = h.hour_ist)
UNION ALL
SELECT 'day', d.station_id, d.pollutant, CAST(d.date AS VARCHAR) FROM fact_measurement_daily d
WHERE NOT EXISTS (SELECT 1 FROM fact_measurement_hourly h
                  WHERE h.station_id = d.station_id AND h.pollutant = d.pollutant AND h.date = d.date)
UNION ALL
SELECT 'city day', c.city, NULL, CAST(c.date AS VARCHAR) FROM mart_city_daily_aqi c
WHERE NOT EXISTS (SELECT 1 FROM mart_station_daily_aqi a WHERE a.city = c.city AND a.date = c.date);

-- name: daily 24-hour means and valid-hour counts recompute from the hourly fact
SELECT d.station_id, d.pollutant, d.date
FROM fact_measurement_daily d
FULL JOIN (SELECT station_id, pollutant, date, CAST(round(avg(value), 3) AS DECIMAL(18, 3)) AS m, count(*) AS n
           FROM fact_measurement_hourly GROUP BY ALL) h
USING (station_id, pollutant, date)
WHERE d.mean_24h IS DISTINCT FROM h.m OR d.valid_hours IS DISTINCT FROM h.n;

-- name: a day is complete exactly when it has 16 or more valid hours, and only complete days get a sub-index
SELECT d.station_id, d.pollutant, d.date FROM fact_measurement_daily d
WHERE d.is_complete <> (d.valid_hours >= 16)
UNION ALL
SELECT s.station_id, s.pollutant, s.date FROM mart_station_daily_subindex s
JOIN fact_measurement_daily d USING (station_id, pollutant, date) WHERE NOT d.is_complete;

-- name: PM2.5 and PM10 sub-indices match an independent implementation of the CPCB breakpoints
SELECT station_id, pollutant, date, aqi_value, sub_index FROM mart_station_daily_subindex
WHERE pollutant IN ('pm25', 'pm10') AND sub_index <> CAST(CASE
    WHEN pollutant = 'pm25' THEN CASE
        WHEN aqi_value < 30 THEN round(aqi_value * 50 / 30)
        WHEN aqi_value < 60 THEN round(50 + (aqi_value - 30) * 50 / 30)
        WHEN aqi_value < 90 THEN round(100 + (aqi_value - 60) * 100 / 30)
        WHEN aqi_value < 120 THEN round(200 + (aqi_value - 90) * 100 / 30)
        WHEN aqi_value < 250 THEN round(300 + (aqi_value - 120) * 100 / 130)
        WHEN aqi_value < 380 THEN round(400 + (aqi_value - 250) * 100 / 130)
        ELSE 500 END
    ELSE CASE
        WHEN aqi_value < 100 THEN round(aqi_value)
        WHEN aqi_value < 250 THEN round(100 + (aqi_value - 100) * 100 / 150)
        WHEN aqi_value < 350 THEN round(200 + (aqi_value - 250))
        WHEN aqi_value < 430 THEN round(300 + (aqi_value - 350) * 100 / 80)
        WHEN aqi_value < 510 THEN round(400 + (aqi_value - 430) * 100 / 80)
        ELSE 500 END
    END AS INTEGER);

-- name: every sub-index is between 0 and 500 and rises with concentration
SELECT station_id, pollutant, date FROM (
    SELECT *, max(sub_index) OVER (PARTITION BY pollutant ORDER BY aqi_value, sub_index
                                   ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS prev_max
    FROM mart_station_daily_subindex
) WHERE sub_index IS NULL OR sub_index NOT BETWEEN 0 AND 500 OR sub_index < prev_max;

-- name: station AQI is the highest sub-index, from three or more pollutants including PM
SELECT a.station_id, a.date FROM mart_station_daily_aqi a
FULL JOIN (SELECT station_id, date, max(sub_index) AS m, count(*) AS n,
                  bool_or(pollutant IN ('pm25', 'pm10')) AS pm
           FROM mart_station_daily_subindex GROUP BY ALL HAVING count(*) >= 3 AND bool_or(pollutant IN ('pm25', 'pm10'))) s
USING (station_id, date)
WHERE a.aqi IS DISTINCT FROM s.m OR a.n_pollutants IS DISTINCT FROM s.n;

-- name: city AQI is the rounded mean of the station AQIs, with the right station count and category
SELECT c.city, c.date FROM mart_city_daily_aqi c
FULL JOIN (SELECT city, date, CAST(round(avg(aqi)) AS INTEGER) AS a, count(*) AS n FROM mart_station_daily_aqi GROUP BY ALL) s
USING (city, date)
WHERE c.aqi IS DISTINCT FROM s.a OR c.n_stations IS DISTINCT FROM s.n
   OR c.category <> CASE WHEN c.aqi <= 50 THEN 'Good' WHEN c.aqi <= 100 THEN 'Satisfactory' WHEN c.aqi <= 200 THEN 'Moderate'
                         WHEN c.aqi <= 300 THEN 'Poor' WHEN c.aqi <= 400 THEN 'Very Poor' ELSE 'Severe' END;

-- name: city-month figures recount from the city daily tables
SELECT m.city, m.month_start FROM mart_city_month m
LEFT JOIN (SELECT city, CAST(date_trunc('month', date) AS DATE) AS month_start, count(*) AS n, round(avg(aqi), 1) AS a,
                  count(*) FILTER (category = 'Severe') AS sev
           FROM mart_city_daily_aqi GROUP BY ALL) d USING (city, month_start)
LEFT JOIN (SELECT city, CAST(date_trunc('month', date) AS DATE) AS month_start,
                  count(*) FILTER (value > 60) AS over
           FROM mart_city_daily_pollutant WHERE pollutant = 'pm25' GROUP BY ALL) p USING (city, month_start)
WHERE m.days_with_aqi <> coalesce(d.n, 0) OR m.mean_aqi IS DISTINCT FROM d.a OR m.days_severe <> coalesce(d.sev, 0)
   OR m.days_pm25_over_naaqs <> coalesce(p.over, 0)
   OR m.days_good + m.days_satisfactory + m.days_moderate + m.days_poor + m.days_very_poor + m.days_severe <> m.days_with_aqi;

-- name: exceedance days recount from daily values against the NAAQS limits
SELECT e.city, e.year, e.pollutant FROM mart_exceedance_city_year e
FULL JOIN (SELECT p.city, year(p.date) AS year, p.pollutant, count(*) AS n,
                  count(*) FILTER (p.value > CASE p.pollutant WHEN 'pm25' THEN 60 WHEN 'pm10' THEN 100 WHEN 'no2' THEN 80
                                                 WHEN 'so2' THEN 80 WHEN 'o3' THEN 100 WHEN 'co' THEN 2 END) AS over
           FROM mart_city_daily_pollutant p GROUP BY ALL) r USING (city, year, pollutant)
WHERE e.days_measured IS DISTINCT FROM r.n OR e.days_over_limit IS DISTINCT FROM r.over;

-- name: spike days: the reference is the median of the surrounding measured days, and a spike day is at least the set ratio above it
WITH r AS (
    SELECT p.city, p.date, p.value AS pm25, k.min_ratio, k.min_days_each_side,
           (SELECT median(CAST(q.value AS DOUBLE)) FROM mart_city_daily_pollutant q
            WHERE q.pollutant = 'pm25' AND q.city = p.city
              AND (q.date BETWEEN p.date - k.window_days AND p.date - k.guard_days - 1
                   OR q.date BETWEEN p.date + k.guard_days + 1 AND p.date + k.window_days)) AS ref,
           (SELECT count(*) FROM mart_city_daily_pollutant q WHERE q.pollutant = 'pm25' AND q.city = p.city
              AND q.date BETWEEN p.date - k.window_days AND p.date - k.guard_days - 1) AS nb,
           (SELECT count(*) FROM mart_city_daily_pollutant q WHERE q.pollutant = 'pm25' AND q.city = p.city
              AND q.date BETWEEN p.date + k.guard_days + 1 AND p.date + k.window_days) AS na
    FROM mart_city_daily_pollutant p, ref_spike_rule k WHERE p.pollutant = 'pm25'
)
SELECT s.city, s.date FROM mart_spike_days s
FULL JOIN r USING (city, date)
WHERE s.pm25 IS DISTINCT FROM CAST(r.pm25 AS DOUBLE) OR s.reference_pm25 IS DISTINCT FROM round(r.ref, 2)
   OR s.ref_days_before IS DISTINCT FROM r.nb OR s.ref_days_after IS DISTINCT FROM r.na
   OR s.is_spike_day IS DISTINCT FROM coalesce(r.nb >= r.min_days_each_side AND r.na >= r.min_days_each_side
                                               AND r.pm25 >= r.min_ratio * r.ref, false)
   OR (SELECT count(*) FROM ref_spike_rule) <> 1;

-- name: spike episodes are whole runs of 1 to the set maximum of spike days, with evaluated ordinary days on both sides
WITH k AS (SELECT * FROM ref_spike_rule),
starts AS (   -- a run starts on a spike day whose previous calendar day is not a spike day
    SELECT d.city, d.date AS start_date FROM mart_spike_days d
    WHERE d.is_spike_day AND NOT EXISTS (SELECT 1 FROM mart_spike_days x
                                         WHERE x.city = d.city AND x.date = d.date - 1 AND x.is_spike_day)
), runs AS (
    SELECT s.city, s.start_date,
           (SELECT min(e.date) FROM mart_spike_days e
            WHERE e.city = s.city AND e.date >= s.start_date AND e.is_spike_day
              AND NOT EXISTS (SELECT 1 FROM mart_spike_days y WHERE y.city = e.city AND y.date = e.date + 1 AND y.is_spike_day)) AS end_date
    FROM starts s
), expected AS (
    SELECT r.city, r.start_date, r.end_date, date_diff('day', r.start_date, r.end_date) + 1 AS days
    FROM runs r, k
    WHERE date_diff('day', r.start_date, r.end_date) + 1 <= k.max_days
      AND EXISTS (SELECT 1 FROM mart_spike_days b WHERE b.city = r.city AND b.date = r.start_date - 1 AND b.is_evaluated)
      AND EXISTS (SELECT 1 FROM mart_spike_days a WHERE a.city = r.city AND a.date = r.end_date + 1 AND a.is_evaluated)
)
SELECT e.city, e.start_date FROM mart_spike_episodes e
FULL JOIN expected x USING (city, start_date)
WHERE e.end_date IS DISTINCT FROM x.end_date OR e.days IS DISTINCT FROM x.days
   OR e.days NOT BETWEEN 1 AND (SELECT max_days FROM k) OR e.month <> month(e.start_date);

-- name: spike episode peaks, ratios and station counts recompute from the spike days and stations
SELECT e.episode_id FROM mart_spike_episodes e
LEFT JOIN (SELECT e2.episode_id, max(d.pm25) AS peak FROM mart_spike_episodes e2
           JOIN mart_spike_days d ON d.city = e2.city AND d.date BETWEEN e2.start_date AND e2.end_date
           GROUP BY e2.episode_id) m USING (episode_id)
LEFT JOIN mart_spike_days p ON p.city = e.city AND p.date = e.peak_date
WHERE e.peak_pm25 IS DISTINCT FROM m.peak OR p.pm25 IS DISTINCT FROM m.peak
   OR e.peak_date NOT BETWEEN e.start_date AND e.end_date
   OR e.peak_ratio IS DISTINCT FROM round(p.pm25 / p.reference_pm25, 2)
   OR e.peak_ratio < (SELECT min_ratio FROM ref_spike_rule) - 0.01
   OR e.stations_on_peak_day IS DISTINCT FROM (SELECT p2.n_stations FROM mart_city_daily_pollutant p2
                                               WHERE p2.city = e.city AND p2.date = e.peak_date AND p2.pollutant = 'pm25')
   OR e.stations_at_ratio IS DISTINCT FROM (SELECT count(*) FROM fact_measurement_daily f
                                            WHERE f.city = e.city AND f.date = e.peak_date AND f.pollutant = 'pm25'
                                              AND f.is_complete AND f.aqi_value >= (SELECT min_ratio FROM ref_spike_rule) * e.reference_pm25)
   OR e.stations_at_ratio NOT BETWEEN 1 AND e.stations_on_peak_day;

-- name: October-November and September levels recount from the city daily PM2.5
SELECT o.city, o.year FROM mart_octnov_season o
FULL JOIN (SELECT city, year(date) AS year,
                  count(*) FILTER (month(date) = 9) AS sep_n, round(avg(value) FILTER (month(date) = 9), 1) AS sep,
                  count(*) FILTER (month(date) IN (10, 11)) AS on_n,
                  round(avg(value) FILTER (month(date) IN (10, 11)), 1) AS octnov
           FROM mart_city_daily_pollutant WHERE pollutant = 'pm25' GROUP BY ALL) r USING (city, year)
WHERE o.sep_days IS DISTINCT FROM r.sep_n OR o.sep_pm25 IS DISTINCT FROM r.sep
   OR o.octnov_days IS DISTINCT FROM r.on_n OR o.octnov_pm25 IS DISTINCT FROM r.octnov;

-- name: profile hours add up to the city hourly table
SELECT p.city, p.pollutant FROM (SELECT city, pollutant, sum(n_hours) AS n FROM mart_profile_hourly GROUP BY ALL) p
FULL JOIN (SELECT city, pollutant, count(*) AS n FROM mart_city_hourly GROUP BY ALL) h USING (city, pollutant)
WHERE p.n IS DISTINCT FROM h.n;

-- name: completeness counts valid hours correctly and stays between 0 and 100 percent
SELECT c.station_id, c.pollutant, c.month_start FROM mart_station_completeness c
LEFT JOIN (SELECT station_id, pollutant, CAST(date_trunc('month', date) AS DATE) AS month_start, count(*) AS n
           FROM fact_measurement_hourly GROUP BY ALL) h USING (station_id, pollutant, month_start)
WHERE c.valid_hours <> coalesce(h.n, 0) OR c.completeness_pct NOT BETWEEN 0 AND 100
   OR c.hours_in_month NOT IN (672, 696, 720, 744);

-- name: data-quality counts reconcile with the staged readings
SELECT q.issue, q.n, r.n FROM (SELECT issue, sum(n) AS n FROM mart_dq_issues WHERE level = 'reading' GROUP BY ALL) q
FULL JOIN (SELECT disposition AS issue, count(*) AS n FROM stg_readings WHERE disposition <> 'valid' GROUP BY ALL
           UNION ALL
           SELECT 'gas unit labelled ppb (value is CPCB µg/m³, CO mg/m³)', count(*) FROM stg_readings
           WHERE units_label = 'ppb' AND disposition = 'valid' HAVING count(*) > 0
           UNION ALL
           SELECT 'particulate at the analyser ceiling (999.99-1000 µg/m³)', count(*) FROM stg_readings
           WHERE disposition = 'valid' AND pollutant IN ('pm25', 'pm10') AND value BETWEEN 999.99 AND 1000
           HAVING count(*) > 0) r USING (issue)
WHERE q.n IS DISTINCT FROM r.n;

-- name: every included location belongs to one station and every station to one of the six cities
SELECT s.station_id, s.city FROM dim_station s LEFT JOIN dim_city c USING (city) WHERE c.city IS NULL
UNION ALL
SELECT CAST(location_id AS VARCHAR), NULL FROM ref_stations WHERE included GROUP BY location_id HAVING count(DISTINCT station_id) > 1
UNION ALL
SELECT station_id, NULL FROM ref_stations WHERE included GROUP BY station_id HAVING count(DISTINCT city) > 1;

-- name: the calendar is continuous over the window
SELECT count(*), min(date), max(date) FROM dim_date
HAVING count(*) <> date_diff('day', DATE '2019-01-01', DATE '2026-09-30') + 1 OR count(DISTINCT date) <> count(*)
    OR min(date) <> DATE '2019-01-01' OR max(date) <> DATE '2026-09-30';
