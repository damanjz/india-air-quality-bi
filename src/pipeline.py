"""Build the DuckDB warehouse from the downloaded archive files, run the checks, export the tables.

    python src/pipeline.py            build, check, export CSVs to data/model and the workbook to excel/
The export is blocked if any check fails.
"""
import re
import sys
import time
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402

ROOT = config.ROOT
DB = ROOT / "data" / "aq.duckdb"
OUT = ROOT / "data" / "model"
EXPORT = ["dim_city", "dim_station", "dim_pollutant", "dim_date", "fact_measurement_daily",
          "mart_city_daily_aqi", "mart_city_daily_pollutant", "mart_city_month", "mart_city_season",
          "mart_exceedance_city_year", "mart_exceedance_station_year", "mart_spike_days", "mart_spike_episodes",
          "mart_octnov_season", "mart_profile_hourly", "mart_station_completeness", "mart_dq_issues"]


def run_sql(con, name):
    t = time.time()
    con.execute((ROOT / "sql" / name).read_text(encoding="utf-8"))
    print(f"built {name} ({time.time() - t:.0f}s)")


def load_checks():
    text = (ROOT / "sql" / "checks.sql").read_text(encoding="utf-8")
    return dict(re.findall(r"-- name: ([^\n]+)\n(.*?;)", text, flags=re.S))


def run_checks(con):
    failed = 0
    for name, sql in load_checks().items():
        bad = con.sql(sql).fetchall()
        failed += bool(bad)
        print(f"  {'FAIL' if bad else 'pass'}  {name}" + (f"  ({len(bad)} rows, e.g. {bad[0]})" if bad else ""))
    return failed


def export(con):
    OUT.mkdir(parents=True, exist_ok=True)
    for stale in OUT.glob("*.csv"):     # a table dropped from the model must not linger as a CSV
        if stale.stem not in EXPORT:
            stale.unlink()
    for t in EXPORT:
        con.execute(f"COPY (SELECT * FROM {t} ORDER BY ALL) TO '{(OUT / (t + '.csv')).as_posix()}' (HEADER)")
    print(f"exported {len(EXPORT)} tables to data/model")


def main():
    DB.unlink(missing_ok=True)
    con = duckdb.connect(str(DB))
    con.execute(f"SET file_search_path = '{ROOT.as_posix()}'")
    con.execute("SET preserve_insertion_order = true")
    import os
    os.chdir(ROOT)
    # the spike rule travels with the data, so the marts, the checks and the report all read the same values
    con.execute("CREATE TABLE ref_spike_rule AS SELECT CAST(? AS DOUBLE) AS min_ratio, CAST(? AS INTEGER) AS max_days, "
                "CAST(? AS INTEGER) AS guard_days, CAST(? AS INTEGER) AS window_days, "
                "CAST(? AS INTEGER) AS min_days_each_side",
                [config.SPIKE_MIN_RATIO, config.SPIKE_MAX_DAYS, config.SPIKE_GUARD_DAYS, config.SPIKE_WINDOW_DAYS,
                 config.SPIKE_MIN_DAYS_EACH_SIDE])
    for name in ("01_staging.sql", "02_model.sql", "03_marts.sql", "04_dq.sql"):
        run_sql(con, name)
    print("checks:")
    failed = run_checks(con)
    if failed:
        print(f"{failed} checks failed; nothing exported")
        con.close()
        sys.exit(1)
    export(con)
    con.close()
    import excel
    excel.build()


if __name__ == "__main__":
    main()
