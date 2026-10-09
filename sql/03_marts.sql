-- Report tables. Every number here is built from the facts in 02_model.sql; nothing is filled in.

-- CPCB National AQI sub-index for every complete station-day: linear interpolation inside the breakpoint
-- band (CPCB, National Air Quality Index, CUPS/82/2014-15). The Severe band is open-ended in CPCB's table;
-- concentrations at or above the top of our Severe band score 500.
CREATE OR REPLACE TABLE mart_station_daily_subindex AS
WITH top AS (SELECT pollutant, max(conc_hi) AS conc_top FROM ref_breakpoints GROUP BY ALL)
SELECT d.station_id, d.city, d.pollutant, d.date, d.aqi_value,
       CAST(CASE WHEN d.aqi_value >= t.conc_top THEN 500
                 ELSE round(b.aqi_lo + (b.aqi_hi - b.aqi_lo) * (d.aqi_value - b.conc_lo) / (b.conc_hi - b.conc_lo))
            END AS INTEGER) AS sub_index
FROM fact_measurement_daily d
JOIN top t USING (pollutant)
LEFT JOIN ref_breakpoints b
       ON b.pollutant = d.pollutant AND d.aqi_value >= b.conc_lo AND d.aqi_value < b.conc_hi
WHERE d.is_complete AND d.aqi_value IS NOT NULL;

CREATE OR REPLACE MACRO aqi_category(a) AS CASE
    WHEN a IS NULL THEN NULL WHEN a <= 50 THEN 'Good' WHEN a <= 100 THEN 'Satisfactory'
    WHEN a <= 200 THEN 'Moderate' WHEN a <= 300 THEN 'Poor' WHEN a <= 400 THEN 'Very Poor' ELSE 'Severe' END;

CREATE OR REPLACE MACRO aqi_category_order(a) AS CASE
    WHEN a IS NULL THEN NULL WHEN a <= 50 THEN 1 WHEN a <= 100 THEN 2
    WHEN a <= 200 THEN 3 WHEN a <= 300 THEN 4 WHEN a <= 400 THEN 5 ELSE 6 END;

-- Station AQI: the highest sub-index, published only when at least three pollutants have a complete day and
-- one of them is PM2.5 or PM10 (CPCB's minimum). The prominent pollutant is the one with that sub-index.
CREATE OR REPLACE TABLE mart_station_daily_aqi AS
SELECT s.station_id, any_value(s.city) AS city, s.date,
       count(*) AS n_pollutants, bool_or(s.pollutant IN ('pm25', 'pm10')) AS has_pm,
       max(s.sub_index) AS aqi,
       arg_max(s.pollutant, (s.sub_index, -p.pollutant_order)) AS prominent_pollutant
FROM mart_station_daily_subindex s JOIN dim_pollutant p USING (pollutant)
GROUP BY s.station_id, s.date
HAVING count(*) >= 3 AND bool_or(s.pollutant IN ('pm25', 'pm10'));

-- City AQI: the mean of the station AQIs that day (CPCB's daily bulletin averages station AQIs for a city).
CREATE OR REPLACE TABLE mart_city_daily_aqi AS
WITH prom AS (
    SELECT city, date, prominent_pollutant, count(*) AS n,
           row_number() OVER (PARTITION BY city, date ORDER BY count(*) DESC, any_value(p.pollutant_order)) AS rk
    FROM mart_station_daily_aqi a JOIN dim_pollutant p ON p.pollutant = a.prominent_pollutant
    GROUP BY city, date, prominent_pollutant
), c AS (
    SELECT city, date, CAST(round(avg(aqi)) AS INTEGER) AS aqi, count(*) AS n_stations,
           min(aqi) AS min_station_aqi, max(aqi) AS max_station_aqi
    FROM mart_station_daily_aqi GROUP BY ALL
)
SELECT c.*, aqi_category(c.aqi) AS category, aqi_category_order(c.aqi) AS category_order,
       p.prominent_pollutant
FROM c JOIN prom p ON p.city = c.city AND p.date = c.date AND p.rk = 1;

-- City daily concentration per pollutant: mean of the stations' complete-day values (same averaging as AQI).
CREATE OR REPLACE TABLE mart_city_daily_pollutant AS
SELECT d.city, d.pollutant, d.date, CAST(round(avg(d.aqi_value), 2) AS DECIMAL(18, 2)) AS value, count(*) AS n_stations,
       round(avg(d.aqi_value), 2) > p.naaqs_limit AS exceeds_naaqs
FROM fact_measurement_daily d JOIN dim_pollutant p USING (pollutant)
WHERE d.is_complete AND d.aqi_value IS NOT NULL
GROUP BY d.city, d.pollutant, d.date, p.naaqs_limit;

-- City hourly concentration: mean of the stations' valid hourly values.
CREATE OR REPLACE TABLE mart_city_hourly AS
SELECT s.city, h.pollutant, h.hour_ist, h.date, h.hour, CAST(round(avg(h.value), 2) AS DECIMAL(18, 2)) AS value,
       count(*) AS n_stations
FROM fact_measurement_hourly h JOIN dim_station s USING (station_id)
GROUP BY ALL;

-- Month by city.
CREATE OR REPLACE TABLE mart_city_month AS
WITH months AS (
    SELECT c.city, d.month_start, any_value(d.year) AS year, any_value(d.month) AS month, count(*) AS days_in_month
    FROM dim_city c CROSS JOIN dim_date d GROUP BY ALL
), aqi AS (
    SELECT city, CAST(date_trunc('month', date) AS DATE) AS month_start, count(*) AS days_with_aqi,
           round(avg(aqi), 1) AS mean_aqi, max(aqi) AS max_aqi, arg_max(date, (aqi, date)) AS worst_day,
           count(*) FILTER (category = 'Good') AS days_good,
           count(*) FILTER (category = 'Satisfactory') AS days_satisfactory,
           count(*) FILTER (category = 'Moderate') AS days_moderate,
           count(*) FILTER (category = 'Poor') AS days_poor,
           count(*) FILTER (category = 'Very Poor') AS days_very_poor,
           count(*) FILTER (category = 'Severe') AS days_severe
    FROM mart_city_daily_aqi GROUP BY ALL
), conc AS (
    SELECT city, CAST(date_trunc('month', date) AS DATE) AS month_start,
           round(avg(value) FILTER (pollutant = 'pm25'), 1) AS mean_pm25,
           count(*) FILTER (pollutant = 'pm25') AS days_pm25,
           count(*) FILTER (pollutant = 'pm25' AND exceeds_naaqs) AS days_pm25_over_naaqs,
           round(avg(value) FILTER (pollutant = 'pm10'), 1) AS mean_pm10,
           count(*) FILTER (pollutant = 'pm10') AS days_pm10,
           count(*) FILTER (pollutant = 'pm10' AND exceeds_naaqs) AS days_pm10_over_naaqs,
           round(avg(value) FILTER (pollutant = 'no2'), 1) AS mean_no2,
           round(avg(value) FILTER (pollutant = 'o3'), 1) AS mean_o3_8h
    FROM mart_city_daily_pollutant GROUP BY ALL
)
SELECT m.city, m.month_start, m.year, m.month, m.days_in_month,
       coalesce(a.days_with_aqi, 0) AS days_with_aqi,
       round(100.0 * coalesce(a.days_with_aqi, 0) / m.days_in_month, 1) AS aqi_coverage_pct,
       a.mean_aqi, aqi_category(a.mean_aqi) AS mean_aqi_category, a.max_aqi, a.worst_day,
       coalesce(a.days_good, 0) AS days_good, coalesce(a.days_satisfactory, 0) AS days_satisfactory,
       coalesce(a.days_moderate, 0) AS days_moderate, coalesce(a.days_poor, 0) AS days_poor,
       coalesce(a.days_very_poor, 0) AS days_very_poor, coalesce(a.days_severe, 0) AS days_severe,
       c.mean_pm25, coalesce(c.days_pm25, 0) AS days_pm25, coalesce(c.days_pm25_over_naaqs, 0) AS days_pm25_over_naaqs,
       c.mean_pm10, coalesce(c.days_pm10, 0) AS days_pm10, coalesce(c.days_pm10_over_naaqs, 0) AS days_pm10_over_naaqs,
       c.mean_no2, c.mean_o3_8h
FROM months m
LEFT JOIN aqi a USING (city, month_start)
LEFT JOIN conc c USING (city, month_start);

-- Season by city and year (IMD seasons).
CREATE OR REPLACE TABLE mart_city_season AS
SELECT a.city, d.year, d.season, d.season_order, count(*) AS days_with_aqi,
       round(avg(a.aqi), 1) AS mean_aqi,
       round(100.0 * count(*) FILTER (a.aqi > 100) / count(*), 1) AS pct_days_worse_than_satisfactory,
       (SELECT round(avg(p.value), 1) FROM mart_city_daily_pollutant p JOIN dim_date dd USING (date)
        WHERE p.city = a.city AND p.pollutant = 'pm25' AND dd.year = d.year AND dd.season = d.season) AS mean_pm25
FROM mart_city_daily_aqi a JOIN dim_date d USING (date)
GROUP BY a.city, d.year, d.season, d.season_order;

-- Days over the 24-hour (8-hour for O3 and CO) National Ambient Air Quality Standards, 2009.
-- City level uses the city daily mean; station level uses each station's own complete days.
CREATE OR REPLACE TABLE mart_exceedance_city_year AS
SELECT p.city, year(p.date) AS year, p.pollutant, any_value(dp.naaqs_limit) AS naaqs_limit,
       count(*) AS days_measured, count(*) FILTER (p.exceeds_naaqs) AS days_over_limit,
       round(100.0 * count(*) FILTER (p.exceeds_naaqs) / count(*), 1) AS pct_days_over_limit
FROM mart_city_daily_pollutant p JOIN dim_pollutant dp USING (pollutant)
GROUP BY ALL;

CREATE OR REPLACE TABLE mart_exceedance_station_year AS
SELECT d.station_id, d.city, year(d.date) AS year, d.pollutant, any_value(p.naaqs_limit) AS naaqs_limit,
       count(*) AS days_measured, count(*) FILTER (d.aqi_value > p.naaqs_limit) AS days_over_limit,
       round(100.0 * count(*) FILTER (d.aqi_value > p.naaqs_limit) / count(*), 1) AS pct_days_over_limit
FROM fact_measurement_daily d JOIN dim_pollutant p USING (pollutant)
WHERE d.is_complete AND d.aqi_value IS NOT NULL
GROUP BY ALL;

-- Short PM2.5 spikes, found from the data alone: no calendar, no labels, no causes. The rule is in ref_spike_rule
-- (set from src/config.py by src/pipeline.py).
-- Each city-day's reference is the median city PM2.5 of the days 4 to 14 days before and after it (the 3 days either
-- side are left out, so an episode of up to 3 days never sets its own reference). A day is evaluated when at least
-- min_days_each_side of those days are measured on each side; it is a spike day when its PM2.5 is at least min_ratio
-- times the reference.
CREATE OR REPLACE TABLE mart_spike_days AS
WITH pm AS (
    SELECT city, date, CAST(value AS DOUBLE) AS pm25 FROM mart_city_daily_pollutant WHERE pollutant = 'pm25'
), ref AS (
    SELECT p.city, p.date, p.pm25, median(q.pm25) AS reference_pm25,
           count(q.date) FILTER (q.date < p.date) AS ref_days_before,
           count(q.date) FILTER (q.date > p.date) AS ref_days_after
    FROM pm p CROSS JOIN ref_spike_rule k
    LEFT JOIN pm q ON q.city = p.city
     AND abs(date_diff('day', p.date, q.date)) BETWEEN k.guard_days + 1 AND k.window_days
    GROUP BY p.city, p.date, p.pm25
)
SELECT r.city, r.date, r.pm25, round(r.reference_pm25, 2) AS reference_pm25, r.ref_days_before, r.ref_days_after,
       r.ref_days_before >= k.min_days_each_side AND r.ref_days_after >= k.min_days_each_side AS is_evaluated,
       round(r.pm25 / r.reference_pm25, 2) AS ratio,
       coalesce(r.ref_days_before >= k.min_days_each_side AND r.ref_days_after >= k.min_days_each_side
                AND r.pm25 >= k.min_ratio * r.reference_pm25, false) AS is_spike_day
FROM ref r CROSS JOIN ref_spike_rule k;

-- Runs of consecutive spike days. A run counts as a short spike when it lasts 1 to max_days days and the days just
-- before and after it are measured and evaluated (so its length is known); other runs are kept here with the reason.
CREATE OR REPLACE TABLE mart_spike_runs AS
WITH s AS (
    SELECT city, date, pm25, reference_pm25,
           date - CAST(row_number() OVER (PARTITION BY city ORDER BY date) AS INTEGER) AS run_key
    FROM mart_spike_days WHERE is_spike_day
), runs AS (
    SELECT city, run_key, min(date) AS start_date, max(date) AS end_date, count(*) AS days,
           round(avg(pm25), 2) AS mean_pm25,
           arg_max(date, (pm25, -epoch(date))) AS peak_date
    FROM s GROUP BY city, run_key
)
SELECT r.city, r.start_date, r.end_date, CAST(r.days AS INTEGER) AS days, r.peak_date,
       p.pm25 AS peak_pm25, p.reference_pm25, round(p.pm25 / p.reference_pm25, 2) AS peak_ratio, r.mean_pm25,
       CASE WHEN r.days > k.max_days THEN 'longer than ' || k.max_days || ' days'
            WHEN NOT coalesce(b.is_evaluated, false) OR NOT coalesce(a.is_evaluated, false)
                THEN 'next to a day without a reading or reference'
            ELSE 'short spike' END AS outcome
FROM runs r CROSS JOIN ref_spike_rule k
JOIN mart_spike_days p ON p.city = r.city AND p.date = r.peak_date
LEFT JOIN mart_spike_days b ON b.city = r.city AND b.date = r.start_date - 1
LEFT JOIN mart_spike_days a ON a.city = r.city AND a.date = r.end_date + 1;

-- Short spike episodes: one row per counted run. A city value is a mean of stations, so each episode also says how
-- many of the stations measured on its peak day were themselves at least min_ratio times the city reference
-- (one of three, say, means a single monitor carries the spike).
CREATE OR REPLACE TABLE mart_spike_episodes AS
SELECT r.city || ' ' || strftime(r.start_date, '%Y-%m-%d') AS episode_id, r.city, r.start_date, r.end_date, r.days,
       year(r.start_date) AS year, month(r.start_date) AS month, strftime(r.start_date, '%b') AS month_name,
       r.peak_date, r.peak_pm25, r.reference_pm25, r.peak_ratio, r.mean_pm25,
       count(f.station_id) AS stations_on_peak_day,
       count(f.station_id) FILTER (f.aqi_value >= k.min_ratio * r.reference_pm25) AS stations_at_ratio
FROM mart_spike_runs r CROSS JOIN ref_spike_rule k
LEFT JOIN fact_measurement_daily f ON f.city = r.city AND f.date = r.peak_date AND f.pollutant = 'pm25'
 AND f.is_complete AND f.aqi_value IS NOT NULL
WHERE r.outcome = 'short spike'
GROUP BY ALL;

-- October-November against September and against December, per city and year. Levels are described; this dataset
-- has no emissions or weather data, so no cause is attributed.
CREATE OR REPLACE TABLE mart_octnov_season AS
WITH p AS (
    SELECT c.city, d.year, d.month, c.value
    FROM mart_city_daily_pollutant c JOIN dim_date d USING (date) WHERE c.pollutant = 'pm25'
), a AS (
    SELECT c.city, d.year, count(*) FILTER (d.is_oct_nov AND c.aqi > 300) AS octnov_days_very_poor_or_worse,
           count(*) FILTER (d.is_oct_nov) AS octnov_days_with_aqi
    FROM mart_city_daily_aqi c JOIN dim_date d USING (date) GROUP BY ALL
)
SELECT p.city, p.year,
       count(*) FILTER (month = 9) AS sep_days, round(avg(value) FILTER (month = 9), 1) AS sep_pm25,
       count(*) FILTER (month IN (10, 11)) AS octnov_days, round(avg(value) FILTER (month IN (10, 11)), 1) AS octnov_pm25,
       count(*) FILTER (month = 12) AS dec_days, round(avg(value) FILTER (month = 12), 1) AS dec_pm25,
       round(avg(value) FILTER (month IN (10, 11)) / avg(value) FILTER (month = 9), 2) AS octnov_vs_sep,
       any_value(a.octnov_days_with_aqi) AS octnov_days_with_aqi,
       any_value(a.octnov_days_very_poor_or_worse) AS octnov_days_very_poor_or_worse
FROM p LEFT JOIN a USING (city, year)
GROUP BY p.city, p.year;

-- Weekday and hour profile by city, pollutant and season, from city hourly values.
CREATE OR REPLACE TABLE mart_profile_hourly AS
SELECT h.city, h.pollutant, d.season, d.season_order, d.weekday_no, d.weekday_name, h.hour,
       round(avg(h.value), 2) AS mean_value, count(*) AS n_hours
FROM mart_city_hourly h JOIN dim_date d USING (date)
GROUP BY ALL;

-- Completeness per station, pollutant and month: valid hours out of the hours in the month.
CREATE OR REPLACE TABLE mart_station_completeness AS
WITH grid AS (
    SELECT s.station_id, s.city, p.pollutant, d.month_start, count(*) * 24 AS hours_in_month
    FROM dim_station s CROSS JOIN dim_pollutant p CROSS JOIN dim_date d GROUP BY ALL
), hrs AS (
    SELECT station_id, pollutant, CAST(date_trunc('month', date) AS DATE) AS month_start,
           count(*) AS valid_hours, sum(n_readings) AS valid_readings
    FROM fact_measurement_hourly GROUP BY ALL
), days AS (
    SELECT station_id, pollutant, CAST(date_trunc('month', date) AS DATE) AS month_start,
           count(*) FILTER (is_complete) AS complete_days
    FROM fact_measurement_daily GROUP BY ALL
), aside AS (
    SELECT station_id, pollutant, CAST(date_trunc('month', CAST(hour_ist AS DATE)) AS DATE) AS month_start,
           count(*) AS readings_set_aside
    FROM stg_readings WHERE disposition NOT IN ('valid', 'outside study window') GROUP BY ALL
)
SELECT g.station_id, g.city, g.pollutant, g.month_start, g.hours_in_month,
       coalesce(h.valid_hours, 0) AS valid_hours,
       round(100.0 * coalesce(h.valid_hours, 0) / g.hours_in_month, 1) AS completeness_pct,
       coalesce(d.complete_days, 0) AS complete_days,
       coalesce(h.valid_readings, 0) AS valid_readings, coalesce(x.readings_set_aside, 0) AS readings_set_aside
FROM grid g
LEFT JOIN hrs h USING (station_id, pollutant, month_start)
LEFT JOIN days d USING (station_id, pollutant, month_start)
LEFT JOIN aside x USING (station_id, pollutant, month_start);
