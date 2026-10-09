"""Prove that no text on the report is clipped, cut off with an ellipsis or pushed into a scrollbar.

Reads every visual in the PBIR report and measures each piece of text with the report's real fonts
(measure_text.ps1, GDI+ at 96 dpi): titles, headings, rail questions and notes (wrapped: line count against the box),
answer lines and KPI values (evaluated in the open Power BI window under every slicer combination of their page and
the widest result kept), KPI labels, slicer values, season chips, matrix cells and headers, and the issue log
(wrapped cells; the rows must fit the visual's height). Matrix and table grids are also checked for room as a whole.

A single line fails if its width plus a 6% margin is wider than its room. Needs open_pbip.ps1 -Keep.
Exit code 1 on any failure.

    python powerbi/check_fit.py [--db data/aq.duckdb]
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import duckdb

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import build_pbip as B  # noqa: E402

PAGES = B.RP / "definition" / "pages"
MARGIN = 1.06
CELL_PAD = 14            # table and matrix cell padding, as calibrated in SaaS Revenue Leak
TEXTBOX_PAD = 16         # textbox inner padding, left + right
FORMATS = {name: fmt for name, _, fmt, _ in B.MEASURES}


def ps(script, *args):
    r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(HERE / script), *args],
                       capture_output=True, text=True, encoding="utf-8")
    if not r.stdout.strip():
        raise RuntimeError(r.stderr.strip()[:400])
    return json.loads(r.stdout.strip())


def dax_table(query):
    rows = ps("query_desktop.ps1", "-Dax", query)
    return rows if isinstance(rows, list) else [rows]


def formatted(m):
    """DAX: the measure as its visual shows it (blank stays blank)."""
    fmt = FORMATS.get(m)
    return f'IF(NOT ISBLANK([{m}]), FORMAT([{m}], "{fmt.replace(chr(34), chr(34) * 2)}"))' if fmt else f"[{m}]"


def lit(v):
    return v["expr"]["Literal"]["Value"]


def text_lit(v):
    return lit(v).strip("'").replace("''", "'")


# ---------------------------------------------------------------- slicer states per page
DB = sys.argv[sys.argv.index("--db") + 1] if "--db" in sys.argv else str(B.DB)
con = duckdb.connect(DB, read_only=True)
CITIES = [r[0] for r in con.sql("SELECT city FROM dim_city ORDER BY city_order").fetchall()]
YEARS = [r[0] for r in con.sql("SELECT DISTINCT year(date) FROM mart_city_daily_aqi ORDER BY 1").fetchall()]
POLLUTANTS = [r[0] for r in con.sql("SELECT pollutant_name FROM dim_pollutant ORDER BY pollutant_order").fetchall()]
SEASONS = [r[0] for r in con.sql("SELECT DISTINCT season, season_order FROM dim_date ORDER BY 2").fetchall()]
ISSUES = con.sql("SELECT DISTINCT issue, action FROM mart_dq_issues").fetchall()
con.close()


def contexts(page):
    city = [None] + [f'City[city] = "{c}"' for c in CITIES]
    if page in ("bad", "when", "spikes"):
        second = [None] + [f"Year[year] = {y}" for y in YEARS]
    elif page == "hours":
        second = [f'Pollutant[pollutant_name] = "{p}"' + (f', Season[season] = "{s}"' if s else "")
                  for p in POLLUTANTS for s in [None] + SEASONS]
    else:
        second = [None] + [f'Pollutant[pollutant_name] = "{p}"' for p in POLLUTANTS]
    return [", ".join(x for x in (c, s) if x) for c in city for s in second]


def widest_values(page, m, n=3):
    expr = formatted(m)
    vals = []
    ctx = contexts(page)
    for i in range(0, len(ctx), 24):
        cols = ", ".join(f'"c{j}", ' + (f"CALCULATE({expr}, {c})" if c else expr) for j, c in enumerate(ctx[i:i + 24]))
        row = dax_table(f"EVALUATE ROW({cols})")[0]
        vals += [str(v) for v in row.values() if v is not None]
    return sorted(set(vals), key=len, reverse=True)[:n] if vals else []


# ---------------------------------------------------------------- collect
items, layout = [], []      # items: one line of text; layout: (page, what, needed px, available px)


def add(page, where, text, font, pt, room, bold=False):
    items.append({"id": len(items), "page": page, "where": where, "text": text, "font": font, "pt": float(pt),
                  "bold": bold, "room": room})


wraps = []                  # (page, where, text, font, pt, width, max_lines)


def collect():
    order = json.loads((PAGES / "pages.json").read_text(encoding="utf-8"))["pageOrder"]
    for page in order:
        for vf in sorted((PAGES / page / "visuals").glob("*/visual.json")):
            v = json.loads(vf.read_text(encoding="utf-8"))
            name, w, h = vf.parent.name, v["position"]["width"], v["position"]["height"]
            vis = v["visual"]
            vtype, objs = vis["visualType"], vis.get("objects", {})
            title = vis.get("visualContainerObjects", {}).get("title", [{}])[0].get("properties", {})
            if title.get("text"):
                add(page, f"{name} title", text_lit(title["text"]), B.SANS, B.TITLE_PT, w - 16)
            qs = vis.get("query", {}).get("queryState", {})
            if vtype == "textbox":
                for para in objs["general"][0]["properties"]["paragraphs"]:
                    for r in para["textRuns"]:
                        if not r["value"].strip():
                            continue
                        st = r["textStyle"]
                        pt = float(st["fontSize"].rstrip("pt"))
                        if name.endswith("Text") or name in ("railNote", "method"):
                            line_h = max(17.0, pt * 4 / 3 * 1.2)
                            wraps.append((page, name, r["value"], st["fontFamily"], pt, w - TEXTBOX_PAD,
                                          int((h - 8) // line_h)))
                        else:
                            add(page, name, r["value"], st["fontFamily"], pt, w - TEXTBOX_PAD)
            elif vtype == "cardVisual":
                projs = qs["Data"]["projections"]
                if len(projs) == 1 and objs.get("label", [{}])[0].get("properties", {}).get("show"):   # answer line
                    m = projs[0]["field"]["Measure"]["Property"]
                    for text in widest_values(page, m):
                        add(page, f"{name} [{m}]", text, B.SERIF, B.ANSWER_PT, w - 20)
                else:
                    n = len(projs)
                    room = (w - 12 * (n - 1)) / n - 22
                    for pj in projs:
                        add(page, f"{name} label", pj["displayName"], B.SANS, 10, room)
                        m = pj["field"]["Measure"]["Property"]
                        for text in widest_values(page, m):
                            add(page, f"{name} [{m}]", text, B.SERIF, B.KPI_PT, room)
            elif vtype == "slicer":
                col = qs["Values"]["projections"][0]["field"]["Column"]["Property"]
                values = {"city": CITIES, "year": [str(y) for y in YEARS], "pollutant_name": POLLUTANTS,
                          "season": SEASONS}[col] + ["All"]
                hdr = objs.get("header", [{}])[0].get("properties", {})
                if hdr.get("text") and text_lit(hdr["text"]):
                    add(page, f"{name} header", text_lit(hdr["text"]), B.SANS, 9, w - 16)
                if col == "season":               # chips: one tile per value across the width
                    for t in SEASONS:
                        add(page, f"{name} chip", t, B.SANS, 10, w / len(SEASONS) - 24)
                else:
                    for t in values:
                        add(page, f"{name} value", t, B.SANS, 10, w - 44)        # dropdown chevron
            elif vtype == "pivotTable":
                m = qs["Values"]["projections"][0]["field"]["Measure"]["Property"]
                rows_col = qs["Rows"]["projections"][0]["field"]["Column"]
                cols = [p["field"]["Column"] for p in qs["Columns"]["projections"]]
                cell = float(lit(objs["columnWidth"][0]["properties"]["value"]).rstrip("D"))
                font = float(lit(objs["values"][0]["properties"]["fontSize"]).rstrip("D"))
                hfont = float(lit(objs["columnHeaders"][0]["properties"]["fontSize"]).rstrip("D"))
                pad = float(lit(objs["grid"][0]["properties"]["rowPadding"]).rstrip("L"))
                hidden = "fontColor" in objs["values"][1]["properties"] and \
                    objs["values"][1]["properties"]["fontColor"] == objs["values"][1]["properties"]["backColor"]
                ent = lambda c: c["Expression"]["SourceRef"]["Entity"]  # noqa: E731
                colrefs = ", ".join(f"'{ent(c)}'[{c['Property']}]" for c in cols)
                grid = dax_table(f"EVALUATE SUMMARIZECOLUMNS('{ent(rows_col)}'[{rows_col['Property']}], {colrefs}, "
                                 f'"t", {formatted(m)})')
                if page == "hours":
                    grid = []
                    for p in POLLUTANTS:
                        grid += dax_table(f"EVALUATE SUMMARIZECOLUMNS('{ent(rows_col)}'[{rows_col['Property']}], {colrefs}, "
                                          f'TREATAS({{"{p}"}}, Pollutant[pollutant_name]), "t", {formatted(m)})')
                if not hidden:
                    for t in sorted({str(r["[t]"]) for r in grid}, key=len, reverse=True)[:5]:
                        add(page, f"{name} cell", t, B.SANS, font, cell - CELL_PAD)
                leaf = cols[-1]["Property"]
                heads = sorted({str(r[f"{ent(cols[-1])}[{leaf}]"]) for r in grid}, key=len, reverse=True)
                for t in heads[:5]:
                    add(page, f"{name} header", t, B.SANS, hfont, cell - 4)
                if len(cols) == 2:                # a year header spans its months
                    top = cols[0]["Property"]
                    span = {}
                    for r in grid:
                        span.setdefault(str(r[f"{ent(cols[0])}[{top}]"]), set()).add(str(r[f"{ent(cols[1])}[{leaf}]"]))
                    for y, ms in span.items():
                        add(page, f"{name} year header {y} ({len(ms)} months)", y, B.SANS, hfont, len(ms) * cell - 6)
                rowvals = sorted({str(r[f"{ent(rows_col)}[{rows_col['Property']}]"]) for r in grid}, key=len, reverse=True)
                n_cols = len({tuple(str(r[f"{ent(c)}[{c['Property']}]"]) for c in cols) for r in grid})
                ids = []
                for t in rowvals[:3]:
                    ids.append(len(items))
                    add(page, f"{name} row header", t, B.SANS, 9, B.ROW_HEADER_PX - 12)
                # the row header column sizes itself to its widest label (+ cell padding)
                grid_widths.append((page, f"{name} width ({n_cols} columns)", ids, n_cols * cell, w - 16))
                n_rows = len(rowvals) + len(cols)
                base = int(font * 4 / 3 + 7.5)
                layout.append((page, f"{name} height ({n_rows} rows)", B.TITLE_H + n_rows * (base + 2 * pad), h))
            elif vtype == "tableEx":
                widths = [float(lit(c["properties"]["value"]).rstrip("D")) for c in objs["columnWidth"]]
                layout.append((page, f"{name} column widths", sum(widths), w - 16))
                for pj, cw in zip(qs["Values"]["projections"], widths):
                    add(page, f"{name} header", pj["displayName"], B.SANS, 9, cw - CELL_PAD)
                pad = float(lit(objs["grid"][0]["properties"]["rowPadding"]).rstrip("L"))
                if name == "issues":
                    counts = [str(r["[t]"]) for r in dax_table('EVALUATE SUMMARIZECOLUMNS(Issues[issue], Issues[action], '
                                                             '"t", [Issue count text])')]
                    for t in sorted(counts, key=len, reverse=True)[:3]:
                        add(page, f"{name} count", t, B.SANS, 9, widths[2] - CELL_PAD)
                    table_rows.append((page, name, widths, h, pad))
                else:
                    # largest spikes: one line per cell. Every episode's values are measured (a slicer can bring any
                    # of them into the table), and the table must hold its TOP_SPIKES rows without a scrollbar.
                    cells = dax_table('EVALUATE SELECTCOLUMNS(Spikes, "0", Spikes[city], '
                                      '"1", FORMAT(Spikes[start_date], "d mmm yyyy"), "2", FORMAT(Spikes[days], "0"), '
                                      '"3", FORMAT(Spikes[peak_pm25], "0"), "4", FORMAT(Spikes[peak_ratio], "0.0""x"""), '
                                      '"5", FORMAT(Spikes[stations_at_ratio], "0") & " of " & FORMAT(Spikes[stations_on_peak_day], "0"))')
                    for i, cw in enumerate(widths):
                        for t in sorted({str(r[f"[{i}]"]) for r in cells}, key=len, reverse=True)[:3]:
                            add(page, f"{name} column {i + 1} cell", t, B.SANS, 9, cw - CELL_PAD)
                    shown = len(dax_table('EVALUATE FILTER(SUMMARIZECOLUMNS(Spikes[city], Spikes[start_date], '
                                          '"r", [Top spike ratio]), NOT ISBLANK([r]))'))
                    layout.append((page, f"{name} {shown} rows", B.TABLE_HEAD_PX + shown * (1 + 2 * pad + B.TABLE_ROW_PX), h))


table_rows = []
grid_widths = []            # (page, what, row-header item ids, cells px, available px)


def measure(entries):
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(entries, f)
        tmp = f.name
    out = ps("measure_text.ps1", "-InFile", tmp)
    Path(tmp).unlink(missing_ok=True)
    out = out if isinstance(out, list) else [out]
    return {o["id"]: o["px"] for o in out}


def wrap_lines(words, width, space, widths):
    lines, cur = 1, 0.0
    for wd in words:
        wpx = widths[wd] * MARGIN
        if cur and cur + space + wpx > width:
            lines, cur = lines + 1, wpx
        else:
            cur = cur + space + wpx if cur else wpx
    return lines


def main():
    collect()
    # words of wrapped texts and of issue-log cells, measured once each
    word_entries = {}
    for _, _, text, font, pt, _, _ in wraps:
        for wd in text.split() + [" "]:
            word_entries[(wd, font, pt)] = None
    for i, a in ISSUES:
        for wd in (i + " " + a).split() + [" "]:
            word_entries[(wd, B.SANS, 9.0)] = None
    entries = [{"id": f"i{it['id']}", "text": it["text"], "font": it["font"], "pt": it["pt"], "bold": it["bold"]}
               for it in items]
    keys = list(word_entries)
    entries += [{"id": f"w{k}", "text": t, "font": f, "pt": p, "bold": False} for k, (t, f, p) in enumerate(keys)]
    px = measure(entries)
    wpx = {key: px[f"w{k}"] for k, key in enumerate(keys)}

    fails = 0
    tight = []
    for it in items:
        need = px[f"i{it['id']}"] * MARGIN
        slack = it["room"] - need
        tight.append((slack, it))
        if slack < 0:
            fails += 1
            print(f"  TOO WIDE  {it['page']:6s} {it['where']:44s} needs {need:5.0f}px, has {it['room']:5.0f}px: {it['text']}")
    wrap_fails = 0
    for page, where, text, font, pt, width, max_lines in wraps:
        ws = {wd: wpx[(wd, font, pt)] for wd in text.split()}
        lines = wrap_lines(text.split(), width, wpx[(" ", font, pt)], ws)
        if lines > max_lines:
            fails += 1
            wrap_fails += 1
            print(f"  TOO LONG  {page:6s} {where:44s} {lines} lines, room for {max_lines}: {text}")
    for page, name, widths, h, pad in table_rows:
        total = B.TABLE_HEAD_PX                  # title and header row, measured on the Desktop render
        for i, a in ISSUES:
            li = max(wrap_lines(i.split(), widths[0] - CELL_PAD, wpx[(" ", B.SANS, 9.0)], {w: wpx[(w, B.SANS, 9.0)] for w in i.split()}),
                     wrap_lines(a.split(), widths[1] - CELL_PAD, wpx[(" ", B.SANS, 9.0)], {w: wpx[(w, B.SANS, 9.0)] for w in a.split()}))
            total += 1 + 2 * pad + B.TABLE_ROW_PX * li   # a 9 pt row: 16 px per line + padding (Desktop render: 23 px at padding 3)
        layout.append((page, f"{name} {len(ISSUES)} wrapped rows", total, h))
    for page, what, ids, cells_px, room in grid_widths:
        layout.append((page, what, max(px[f"i{i}"] for i in ids) + CELL_PAD + cells_px, room))
    bad_layout = 0
    for page, what, need, room in layout:
        if need > room:
            bad_layout += 1
            print(f"  NO ROOM   {page:6s} {what:44s} needs {need:5.0f}px, has {room:5.0f}px")
    fails += bad_layout
    print(f"{len(items) - sum(1 for s, _ in tight if s < 0)} of {len(items)} single lines fit (6% margin).")
    print(f"{len(wraps) - wrap_fails} of {len(wraps)} wrapped texts fit their boxes.")
    print(f"{len(layout) - bad_layout} of {len(layout)} grids fit without scrollbars.")
    tight.sort(key=lambda x: x[0])
    print("tightest fits:")
    for slack, it in tight[:8]:
        if slack >= 0:
            print(f"  {slack:5.0f}px spare  {it['page']:6s} {it['where']:44s} {it['text'][:80]}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
