"""Draw the case-study charts as SVG straight from the warehouse.

    python docs/charts.py
writes docs/img/coverage.svg, docs/img/fault-985.svg and docs/img/spike-method.svg from data/aq.duckdb (read only).
No figure is typed in, so a rebuild of the warehouse redraws them.
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
IMG = ROOT / "docs" / "img"
INK, RUST, TEAL, OCHRE = "#2f55b0", "#c0533a", "#1f8f7a", "#c27a1a"
PAPER, RAIL, RULE, TEXT, MUTED = "#fbfaf7", "#f2efe7", "#e3ded2", "#1d2330", "#5b6170"
FONT = "Segoe UI, system-ui, sans-serif"


def svg(w, h, body, label):
    return "\n".join([f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" font-family="{FONT}" '
                      f'role="img" aria-label="{label}">', f'<rect width="{w}" height="{h}" fill="{PAPER}"/>', *body,
                      "</svg>"])


def coverage(con):
    """Stations with at least one valid reading, per city and month: the archive gap and the growing station sets."""
    cities = [r[0] for r in con.sql("SELECT city FROM dim_city ORDER BY city_order").fetchall()]
    total = dict(con.sql("SELECT city, count(*) FROM dim_station GROUP BY city").fetchall())
    rows = con.sql("""SELECT city, month_start, count(DISTINCT station_id) FROM mart_station_completeness
                      WHERE valid_hours > 0 GROUP BY ALL""").fetchall()
    n = {(c, m): k for c, m, k in rows}
    months = [r[0] for r in con.sql("SELECT DISTINCT month_start FROM dim_date ORDER BY 1").fetchall()]
    empty = [m for m in months if not any((c, m) in n for c in cities)]
    W, left, right, top, row, gap = 820, 92, 70, 30, 35, 6
    H = top + row * len(cities) + 30
    step = (W - left - right) / len(months)
    x = lambda i: left + i * step
    out = []
    for y_ in sorted({m.year for m in months}):
        i = months.index(date(y_, 1, 1))
        out.append(f'<line x1="{x(i):.1f}" y1="{top - 10}" x2="{x(i):.1f}" y2="{top + row * len(cities)}" stroke="{RULE}"/>')
        out.append(f'<text x="{x(i) + 3:.1f}" y="{top - 14}" font-size="10.5" fill="{MUTED}">{y_}</text>')
    for r in range(len(cities)):
        base = top + row * (r + 1) - gap
        out.append(f'<line x1="{left}" y1="{base}" x2="{W - right}" y2="{base}" stroke="{RULE}"/>')
    # the empty run in the middle of the window, shaded across every city
    runs, start = [], None
    for i, m in enumerate(months):
        if m in empty and start is None:
            start = i
        if m not in empty and start is not None:
            runs.append((start, i - 1)); start = None
    if start is not None:
        runs.append((start, len(months) - 1))
    long_run = max(runs, key=lambda r: r[1] - r[0])
    for a, b in runs:
        out.append(f'<rect x="{x(a):.1f}" y="{top - 6}" width="{(b - a + 1) * step:.1f}" height="{row * len(cities) + 6}" '
                   f'fill="{RAIL}"/>')
    a, b = long_run
    out.append(f'<text x="{(x(a) + x(b + 1)) / 2:.1f}" y="{top + row * len(cities) / 2 - 6:.1f}" font-size="11.5" '
               f'fill="{MUTED}" text-anchor="middle">no Indian station data</text>')
    out.append(f'<text x="{(x(a) + x(b + 1)) / 2:.1f}" y="{top + row * len(cities) / 2 + 10:.1f}" font-size="11.5" '
               f'fill="{MUTED}" text-anchor="middle">in the archive, {months[a]:%b %Y} to {months[b]:%b %Y}</text>')
    for r, c in enumerate(cities):
        base = top + row * (r + 1) - gap
        h = row - gap - 4
        out.append(f'<text x="0" y="{base - h / 2 + 4:.1f}" font-size="12" fill="{TEXT}">{c}</text>')
        for i, m in enumerate(months):
            k = n.get((c, m), 0)
            if k:
                bh = h * k / total[c]
                out.append(f'<rect x="{x(i) + 0.5:.1f}" y="{base - bh:.1f}" width="{step - 1:.1f}" height="{bh:.1f}" fill="{INK}"/>')
        out.append(f'<text x="{W - right + 8}" y="{base - h / 2 + 4:.1f}" font-size="10.5" fill="{MUTED}">{total[c]} stations</text>')
    out.append(f'<text x="{left}" y="{H - 6}" font-size="10.5" fill="{MUTED}">bar height: stations with at least one valid '
               f'reading that month, as a share of the city\'s stations in the study</text>')
    (IMG / "coverage.svg").write_text(svg(W, H, out, "Stations reporting per city and month, 2019 to 2026"),
                                      encoding="utf-8", newline="\n")


def fault_985(con):
    """How often each whole-number PM value near 985 occurs in the staged readings."""
    rows = con.sql("""SELECT CAST(value_num AS INTEGER), count(*) FROM stg_readings
                      WHERE pollutant IN ('pm25', 'pm10') AND value_num BETWEEN 975 AND 995 AND value_num = round(value_num)
                      GROUP BY 1 ORDER BY 1""").fetchall()
    n = dict(rows)
    vals = list(range(975, 996))
    W, H, left, right, top, bottom = 820, 158, 56, 16, 18, 118
    peak = max(n.values())
    step = (W - left - right) / len(vals)
    y = lambda v: bottom - (bottom - top) * v / peak
    out = []
    unit = 10 ** (len(str(peak)) - 1)
    for v in range(0, peak + 1, unit):
        out.append(f'<line x1="{left}" x2="{W - right}" y1="{y(v):.1f}" y2="{y(v):.1f}" stroke="{RULE}"/>')
        out.append(f'<text x="{left - 6}" y="{y(v) + 3.5:.1f}" font-size="10" fill="{MUTED}" text-anchor="end">{v:,.0f}</text>')
    for i, v in enumerate(vals):
        k = n.get(v, 0)
        col = RUST if v == 985 else INK
        cx = left + i * step + step / 2
        if k:
            out.append(f'<rect x="{cx - step * 0.32:.1f}" y="{y(k):.1f}" width="{step * 0.64:.1f}" '
                       f'height="{max(bottom - y(k), 1):.1f}" fill="{col}"/>')
        weight = "600" if v == 985 else "400"
        out.append(f'<text x="{cx:.1f}" y="{bottom + 14}" font-size="10" font-weight="{weight}" fill="{MUTED}" '
                   f'text-anchor="middle">{v}</text>')
        if v in (984, 985, 986):
            out.append(f'<text x="{cx + (0 if v == 985 else 0):.1f}" y="{y(k) - 5:.1f}" font-size="10.5" fill="{col}" '
                       f'text-anchor="middle">{k:,}</text>')
    out.append(f'<text x="{left}" y="{H - 4}" font-size="10.5" fill="{MUTED}">PM2.5 and PM10 readings with exactly this '
               f'whole-number value, µg/m³, all stations and years</text>')
    (IMG / "fault-985.svg").write_text(svg(W, H, out, "Count of PM readings at each whole value from 975 to 995"),
                                       encoding="utf-8", newline="\n")


def spike_method(con):
    """One short spike, drawn the way the rule sees it: the days around it, the reference and twice the reference."""
    rule = con.sql("SELECT min_ratio, guard_days, window_days FROM ref_spike_rule").fetchone()
    ratio, guard, window = rule
    ep = con.sql("""SELECT city, start_date, end_date, peak_date, peak_ratio, stations_at_ratio, stations_on_peak_day
                    FROM mart_spike_episodes WHERE stations_on_peak_day >= 3 AND 2 * stations_at_ratio > stations_on_peak_day
                    ORDER BY peak_ratio DESC, episode_id LIMIT 1""").fetchone()
    city, s, e, peak = ep[0], ep[1], ep[2], ep[3]
    lo, hi = s - timedelta(days=window), e + timedelta(days=window)
    days = con.sql(f"""SELECT date, pm25, reference_pm25, is_spike_day FROM mart_spike_days
                       WHERE city = '{city}' AND date BETWEEN DATE '{lo}' AND DATE '{hi}' ORDER BY date""").fetchall()
    ref_peak = next(r[2] for r in days if r[0] == peak)
    W, H, left, right, top = 820, 370, 44, 16, 20
    bottom = H - 52
    top_v = max(max(r[1] or 0 for r in days), ref_peak * ratio) * 1.05
    span = (hi - lo).days + 1
    step = (W - left - right) / span
    x = lambda d: left + (d - lo).days * step
    y = lambda v: bottom - (bottom - top) * v / top_v
    out = []
    tick = 50 if top_v > 150 else 25
    for t in range(0, int(top_v) + 1, tick):
        out.append(f'<line x1="{left}" x2="{W - right}" y1="{y(t):.1f}" y2="{y(t):.1f}" stroke="{RULE}"/>')
        out.append(f'<text x="{left - 6}" y="{y(t) + 3.5:.1f}" font-size="10" fill="{MUTED}" text-anchor="end">{t}</text>')
    # reference windows for the peak day: days guard+1..window either side
    for a, b in ((peak - timedelta(days=window), peak - timedelta(days=guard + 1)),
                 (peak + timedelta(days=guard + 1), peak + timedelta(days=window))):
        a, b = max(a, lo), min(b, hi)
        out.append(f'<rect x="{x(a):.1f}" y="{bottom + 4}" width="{x(b) - x(a) + step:.1f}" height="6" fill="{RULE}"/>')
    out.append(f'<text x="{x(peak - timedelta(days=window)):.1f}" y="{bottom + 22}" font-size="10.5" fill="{MUTED}">'
               f'reference days for the peak: {guard + 1} to {window} days either side</text>')
    for d, v, ref, spike in days:
        if v is None:
            continue
        col = INK if spike and s <= d <= e else RULE if abs((d - peak).days) <= guard else "#c9c3b4"
        out.append(f'<rect x="{x(d) + 1:.1f}" y="{y(v):.1f}" width="{step - 2:.1f}" height="{bottom - y(v):.1f}" fill="{col}"/>')
    out.append(f'<line x1="{left}" x2="{W - right}" y1="{y(ref_peak):.1f}" y2="{y(ref_peak):.1f}" stroke="{TEXT}" '
               f'stroke-dasharray="4 3"/>')
    out.append(f'<text x="{W - right}" y="{y(ref_peak) - 5:.1f}" font-size="10.5" fill="{TEXT}" text-anchor="end">'
               f'reference (median) {ref_peak:.0f}</text>')
    out.append(f'<line x1="{left}" x2="{W - right}" y1="{y(ref_peak * ratio):.1f}" y2="{y(ref_peak * ratio):.1f}" '
               f'stroke="{RUST}" stroke-dasharray="4 3"/>')
    out.append(f'<text x="{W - right}" y="{y(ref_peak * ratio) - 5:.1f}" font-size="10.5" fill="{RUST}" text-anchor="end">'
               f'{ratio:.1f}x reference</text>')
    pv = next(r[1] for r in days if r[0] == peak)
    out.append(f'<text x="{x(peak) + step + 4:.1f}" y="{y(pv) + 10:.1f}" font-size="10.5" fill="{INK}">'
               f'{city}, {peak.day} {peak:%b %Y}: {pv:.0f} µg/m³, {ep[4]:.1f}x</text>')
    out.append(f'<text x="{left}" y="{H - 4}" font-size="10.5" fill="{MUTED}">city daily mean PM2.5, µg/m³; '
               f'blue: the spike days; pale: the {guard} days either side of the peak, left out of its reference</text>')
    (IMG / "spike-method.svg").write_text(svg(W, H, out, f"Daily PM2.5 in {city} around one short spike"),
                                          encoding="utf-8", newline="\n")
    return ep


def main():
    IMG.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(ROOT / "data" / "aq.duckdb"), read_only=True)
    coverage(con)
    fault_985(con)
    spike_method(con)
    print("wrote docs/img/coverage.svg, docs/img/fault-985.svg, docs/img/spike-method.svg")


if __name__ == "__main__":
    main()
