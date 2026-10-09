"""Break the built data on purpose, one rule at a time, and confirm the matching check fails.

Works on a copy of data/aq.duckdb, so the real build is untouched. Run after src/pipeline.py.
"""
import shutil
import sys
import time
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
from pipeline import load_checks  # noqa: E402

DB, COPY = config.ROOT / "data" / "aq.duckdb", config.ROOT / "data" / "mutation.duckdb"

# One valid PM2.5 reading, hour and day to break, chosen deterministically.
PICK_READING = ("(SELECT min(rowid) FROM stg_readings WHERE disposition = 'valid' AND pollutant = 'pm25' "
                "AND hour_ist = (SELECT min(hour_ist) FROM stg_readings WHERE disposition = 'valid' AND pollutant = 'pm25'))")
PICK_DAY = ("station_id = (SELECT min(station_id) FROM fact_measurement_daily WHERE is_complete AND pollutant = 'pm25') "
            "AND pollutant = 'pm25' AND date = (SELECT min(date) FROM fact_measurement_daily WHERE is_complete "
            "AND pollutant = 'pm25' AND station_id = (SELECT min(station_id) FROM fact_measurement_daily "
            "WHERE is_complete AND pollutant = 'pm25'))")
PICK_CITY_DAY = "city = 'Delhi' AND date = (SELECT min(date) FROM mart_city_daily_aqi WHERE city = 'Delhi')"

MUTATIONS = {
    "every raw row of an included station is staged or out of scope, once":
        f"DELETE FROM stg_readings WHERE rowid = {PICK_READING}",
    "every staged reading has exactly one known disposition":
        f"UPDATE stg_readings SET disposition = 'looks fine' WHERE rowid = {PICK_READING}",
    "valid readings are on IST, in the window, in range and unique per station, pollutant and time":
        f"UPDATE stg_readings SET value = -5 WHERE rowid = {PICK_READING}",
    "\"ppb\" gas labels are CPCB units, not ppb (CO median under 20, NO2 level matches the same stations before)":
        "UPDATE stg_readings SET value_num = value_num * 1000 WHERE pollutant = 'co' AND units_label = 'ppb'",
    "hourly values equal the mean of their valid readings, with the reading count":
        "UPDATE fact_measurement_hourly SET value = value + 1 WHERE (station_id, pollutant, hour_ist) = "
        "(SELECT (station_id, pollutant, hour_ist) FROM fact_measurement_hourly ORDER BY ALL LIMIT 1)",
    "no hour or day is invented: every fact row has valid readings behind it":
        "INSERT INTO fact_measurement_hourly SELECT station_id, pollutant, hour_ist + INTERVAL 1 DAY * 4000, "
        "CAST(hour_ist + INTERVAL 1 DAY * 4000 AS DATE), hour, value, n_readings "
        "FROM fact_measurement_hourly ORDER BY ALL LIMIT 1",
    "daily 24-hour means and valid-hour counts recompute from the hourly fact":
        f"UPDATE fact_measurement_daily SET mean_24h = mean_24h * 2 WHERE {PICK_DAY}",
    "a day is complete exactly when it has 16 or more valid hours, and only complete days get a sub-index":
        f"UPDATE fact_measurement_daily SET valid_hours = 15 WHERE {PICK_DAY}",
    "PM2.5 and PM10 sub-indices match an independent implementation of the CPCB breakpoints":
        "UPDATE mart_station_daily_subindex SET sub_index = sub_index + 7 WHERE (station_id, date) = "
        "(SELECT (station_id, date) FROM mart_station_daily_subindex WHERE pollutant = 'pm10' "
        "AND aqi_value BETWEEN 120 AND 200 ORDER BY ALL LIMIT 1) AND pollutant = 'pm10'",
    "every sub-index is between 0 and 500 and rises with concentration":
        "UPDATE mart_station_daily_subindex SET sub_index = 0 WHERE (station_id, date, pollutant) = "
        "(SELECT (station_id, date, pollutant) FROM mart_station_daily_subindex WHERE pollutant = 'no2' "
        "ORDER BY aqi_value DESC, station_id, date LIMIT 1)",
    "station AQI is the highest sub-index, from three or more pollutants including PM":
        "UPDATE mart_station_daily_aqi SET aqi = aqi - 1 WHERE (station_id, date) = "
        "(SELECT (station_id, date) FROM mart_station_daily_aqi ORDER BY ALL LIMIT 1)",
    "city AQI is the rounded mean of the station AQIs, with the right station count and category":
        f"UPDATE mart_city_daily_aqi SET category = 'Good', aqi = 400 WHERE {PICK_CITY_DAY}",
    "city-month figures recount from the city daily tables":
        "UPDATE mart_city_month SET days_severe = days_severe + 1 WHERE city = 'Delhi' AND month_start = DATE '2019-11-01'",
    "exceedance days recount from daily values against the NAAQS limits":
        "UPDATE mart_exceedance_city_year SET days_over_limit = days_over_limit - 1 "
        "WHERE city = 'Delhi' AND year = 2019 AND pollutant = 'pm25'",
    "spike days: the reference is the median of the surrounding measured days, and a spike day is at least the set ratio above it":
        "UPDATE mart_spike_days SET is_spike_day = true WHERE (city, date) = (SELECT (city, date) FROM mart_spike_days "
        "WHERE is_evaluated AND NOT is_spike_day ORDER BY ratio DESC, city, date LIMIT 1)",
    "spike episodes are whole runs of 1 to the set maximum of spike days, with evaluated ordinary days on both sides":
        "DELETE FROM mart_spike_episodes WHERE episode_id = (SELECT min(episode_id) FROM mart_spike_episodes)",
    "spike episode peaks, ratios and station counts recompute from the spike days and stations":
        "UPDATE mart_spike_episodes SET peak_ratio = peak_ratio + 0.5 "
        "WHERE episode_id = (SELECT min(episode_id) FROM mart_spike_episodes)",
    "October-November and September levels recount from the city daily PM2.5":
        "UPDATE mart_octnov_season SET octnov_pm25 = octnov_pm25 + 1 WHERE city = 'Delhi' AND year = 2019",
    "profile hours add up to the city hourly table":
        "DELETE FROM mart_profile_hourly WHERE (city, pollutant, season, weekday_no, hour) = "
        "(SELECT (city, pollutant, season, weekday_no, hour) FROM mart_profile_hourly ORDER BY ALL LIMIT 1)",
    "completeness counts valid hours correctly and stays between 0 and 100 percent":
        "UPDATE mart_station_completeness SET valid_hours = valid_hours + 24 WHERE (station_id, pollutant, month_start) = "
        "(SELECT (station_id, pollutant, month_start) FROM mart_station_completeness ORDER BY ALL LIMIT 1)",
    "data-quality counts reconcile with the staged readings":
        "UPDATE stg_readings SET disposition = 'stuck value' WHERE rowid = " + PICK_READING,
    "every included location belongs to one station and every station to one of the six cities":
        "UPDATE dim_station SET city = 'Pune' WHERE station_id = (SELECT min(station_id) FROM dim_station)",
    "the calendar is continuous over the window":
        "DELETE FROM dim_date WHERE date = DATE '2024-02-29'",
}


def main():
    checks = load_checks()
    assert set(checks) == set(MUTATIONS), set(checks) ^ set(MUTATIONS)
    missed, t0 = 0, time.time()
    for name, mutation in MUTATIONS.items():
        shutil.copy(DB, COPY)
        con = duckdb.connect(str(COPY))
        assert not con.sql(checks[name]).fetchall(), f"check fails before mutation: {name}"
        con.execute(mutation)
        caught = bool(con.sql(checks[name]).fetchall())
        missed += not caught
        print(f"  {'caught' if caught else 'MISSED'}  {name}")
        con.close()
    COPY.unlink()
    print(f"{len(MUTATIONS) - missed} of {len(MUTATIONS)} mutations caught ({time.time() - t0:.0f}s)")
    sys.exit(1 if missed else 0)


if __name__ == "__main__":
    main()
