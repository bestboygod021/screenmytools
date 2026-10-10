"""Generate a self-contained, read-only HTML dashboard from the capture history.

The output is a single static ``.html`` file (no network assets; a few lines of
inline JavaScript power the search box) with a per-site summary table and an
inline SVG sparkline of each site's visual diff over time. A ``url_filter`` (the
``?url=`` parameter ``serve`` accepts) narrows the table down server-side. It can be opened in any browser or served from CI artifacts so
the team can review change trends without running the desktop app.
"""

from __future__ import annotations

import csv
import html
import io
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app.core import baseline, history, retention

_CSS = """
:root { color-scheme: light dark; }
body { font: 14px/1.5 system-ui, Segoe UI, Roboto, sans-serif; margin: 2rem; }
h1 { font-size: 1.4rem; margin-bottom: .25rem; }
.meta { color: #888; margin-bottom: 1.5rem; }
table { border-collapse: collapse; width: 100%; max-width: 56rem; }
th, td { text-align: left; padding: .5rem .75rem; border-bottom: 1px solid #ddd3; }
th { font-weight: 600; }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
td.stale { color: #c0392b; font-weight: 600; }
.bar { display: inline-block; width: 72px; height: 8px; background: #8883;
       border-radius: 4px; overflow: hidden; vertical-align: middle; }
.bar > span { display: block; height: 100%; background: #4a9eff; }
.drift-num { margin-left: .4rem; font-variant-numeric: tabular-nums; color: #666; }
.spark { display: block; }
.empty { color: #888; }
.toolbar { display: flex; align-items: center; gap: .5rem; margin-bottom: .75rem; }
.toolbar input { font: inherit; padding: .35rem .6rem; border-radius: 6px;
                 border: 1px solid #8884; background: transparent; color: inherit; min-width: 16rem; }
.toolbar input:focus { outline: 2px solid #4a9eff; outline-offset: 1px; }
.storage { max-width: 56rem; margin-bottom: 1.5rem; }
.storage h2 { font-size: 1rem; margin: 0 0 .35rem; }
.storage dl { display: grid; grid-template-columns: max-content 1fr; gap: .15rem 1rem; margin: 0; }
.storage dt { color: #888; }
.storage dd { margin: 0; font-variant-numeric: tabular-nums; }
.storage .warn { color: #d08b2c; }
.storage a { color: #4a9eff; }
""".strip()


def _sparkline(diffs: list[float | None], width: int = 160, height: int = 32) -> str:
    """Return an inline SVG polyline for a diff series (values clamped to 0..1)."""
    points = [0.0 if d is None else max(0.0, min(1.0, d)) for d in diffs]
    if not points:
        return ""
    if len(points) == 1:
        points = [*points, points[0]]
    step = width / (len(points) - 1)
    coords = " ".join(f"{i * step:.1f},{height - p * height:.1f}" for i, p in enumerate(points))
    return (
        f'<svg class="spark" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="diff trend">'
        f'<polyline fill="none" stroke="#4a9eff" stroke-width="1.5" points="{coords}"/>'
        f"</svg>"
    )


def _drift_cell(output_dir: str | Path, url: str, history_points: list | None = None) -> str:
    """Current drift vs the baseline: a bar, a percentage, and the recorded trend.

    ``history_points`` are the drift values recorded by previous runs, so the
    cell shows whether a site is drifting steadily or settled again.
    """
    drift = baseline.baseline_drift(output_dir, url)
    if drift is None:
        return "-"
    percent = max(0.0, min(1.0, drift)) * 100.0
    width = f"{percent:.0f}%"
    amount = f"{percent:.0f}%"
    recorded = [point for point in (history_points or []) if point is not None]
    chart = _sparkline(recorded, width=90, height=18) if len(recorded) >= 2 else ""
    return (
        f"<div class='bar'><span style='width:{width}'></span></div>"
        f"<span class='drift-num'>{amount}</span>"
        f"{chart}"
    )


def _baseline_cell(output_dir: str | Path, url: str, max_age_days: int | float) -> tuple[str, str]:
    """Return ``(text, css_class)`` describing the site's pinned baseline."""
    if not baseline.has_baseline(output_dir, url):
        return "-", ""
    if baseline.is_baseline_stale(output_dir, url, max_age_days):
        age = baseline.baseline_age_days(output_dir, url) or 0.0
        return f"stale ({age:.0f}d)", "stale"
    return "pinned", ""


def _rows(
    output_dir: str | Path,
    baseline_max_age_days: int | float = 0,
    url_filter: str = "",
) -> list[dict[str, Any]]:
    """One summary row per site: captures, changes, last diff, and the series.

    ``url_filter`` keeps only sites whose URL contains that text, which is what a
    ``?url=`` request to ``serve`` passes through.
    """
    flat = history.filter_rows(history.flat_rows(output_dir), url_filter)  # newest-first
    counts = history.site_change_counts(output_dir)
    captures: dict[str, int] = {}
    latest: dict[str, dict[str, Any]] = {}
    for row in flat:
        url = row["url"]
        captures[url] = captures.get(url, 0) + 1
        if url not in latest:
            latest[url] = row
    rows: list[dict[str, Any]] = []
    for url, last in latest.items():
        series = [point["diff"] for point in history.trend_for_url(output_dir, url)]
        diff = last["diff"]
        baseline_text, baseline_class = _baseline_cell(output_dir, url, baseline_max_age_days)
        rows.append(
            {
                "url": url,
                "captures": captures[url],
                "changes": counts.get(url, 0),
                "last_diff": diff,
                "last_diff_text": "-" if diff is None else f"{diff:.2f}",
                "series": series,
                "baseline_text": baseline_text,
                "baseline_class": baseline_class,
                "drift": _drift_cell(
                    output_dir,
                    url,
                    [point.get("drift") for point in history.drift_for_url(output_dir, url)],
                ),
            }
        )
    return rows


def human_bytes(size: int | float) -> str:
    """A byte count as a short human string: ``1536 -> "1.5 KB"``."""
    value = float(max(size, 0))
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def _size_of(path: Path) -> int:
    """A file's size in bytes (0 when it cannot be measured)."""
    try:
        return path.stat().st_size
    except OSError:  # pragma: no cover - a file that vanished mid-render
        return 0


def _oldest_label(path: Path) -> str:
    """``capture-report-20260501-090000.json`` -> ``"2026-05-01 09:00"``.

    The stamp is in the file name, so this costs nothing; a differently named
    report falls back to the file's modification time.
    """
    stamp = path.stem[len(retention.REPORT_PREFIX) :]
    if len(stamp) == 15 and stamp[8] == "-":
        return f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]} {stamp[9:11]}:{stamp[11:13]}"
    try:  # pragma: no cover - only for hand-renamed reports
        return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    except OSError:  # pragma: no cover - the file vanished mid-render
        return ""


def storage_stats(
    output_dir: str | Path, caps: dict | None = None, samples: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """What the folder costs and what keeps it in check.

    ``caps`` is an optional ``{"screenshots_mb": N, "history_mb": M}`` mapping
    (the engine passes the configured limits, other callers usually cannot know
    them) so the panel can show the rule next to the reality.

    ``samples`` are the rows recorded by the engine in the ``storage_samples``
    table (read from the index when not given). When enough of them exist the
    growth is *measured* (a least-squares fit over the real folder sizes, see
    :func:`app.core.retention.series_rate`) instead of estimated from the size
    of recent runs; the ``growth.source`` key says which one answered.
    """
    folder = Path(output_dir)
    reports = retention.report_files(folder)  # oldest first
    history_bytes = retention.history_footprint(folder)
    total = retention.folder_bytes(folder)
    try:
        index_bytes = (folder / retention.INDEX_FILENAME).stat().st_size
    except OSError:
        index_bytes = 0
    # Only the numeric caps decorate the panel; a per-site budget is carried
    # through to the metrics endpoint as its own field instead of a cap.
    site_caps: dict[str, float] = {}
    raw_site_caps = (caps or {}).get("site_caps")
    if raw_site_caps:
        try:
            site_caps = retention.parse_site_caps(raw_site_caps)
        except retention.SiteCapError:  # pragma: no cover - the CLI validates first
            site_caps = {}
    wanted: dict[str, int] = {}
    for key, value in (caps or {}).items():
        try:
            number = int(value or 0)
        except (TypeError, ValueError):
            continue
        if number > 0:
            wanted[key] = number
    archives = retention.archive_files(folder)
    # "Which host costs the most?" is the question that leads to a decision, so the
    # per-site breakdown travels with the numbers (TOP_SITES keeps it small).
    sites = [space.to_dict() for space in retention.site_space(folder, limit=TOP_SITES)]

    # A cap without a forecast is a rule nobody can plan around, so the recent
    # growth is turned into "days of room left": measured from the recorded
    # samples when there are enough of them, estimated from the runs otherwise.
    history_rate = retention.history_growth_per_day(folder)
    screenshot_rate = retention.screenshot_growth_per_day(folder)
    source = "reports"
    # Callers that already have the samples pass them in (the panel and the API
    # do, so a request reads the index once); everyone else gets them read here.
    recorded = storage_samples(output_dir) if samples is None else list(samples)
    measured_history = retention.series_rate(
        _sample_series(recorded, "history"), retention.MIN_SPAN_DAYS
    )
    if measured_history is not None:
        measured_total = retention.series_rate(
            _sample_series(recorded, "total"), retention.MIN_SPAN_DAYS
        )
        history_rate = measured_history
        # The screenshots are everything the history is not, so their share of
        # the folder's growth is the difference of the two fitted slopes.
        screenshot_rate = max(0.0, (measured_total or measured_history) - measured_history)
        source = "samples"
    forecasts: dict[str, float | None] = {}
    if wanted.get("history_mb"):
        forecasts["history"] = retention.days_to_cap(
            history_bytes, wanted["history_mb"] * 1024 * 1024, history_rate
        )
    if wanted.get("screenshots_mb"):
        # The screenshot cap measures the folder, so both growths count.
        forecasts["screenshots"] = retention.days_to_cap(
            total, wanted["screenshots_mb"] * 1024 * 1024, screenshot_rate + history_rate
        )
    known = [days for days in forecasts.values() if days is not None]

    return {
        "total": total,
        "screenshots": max(total - history_bytes, 0),
        "site_caps": site_caps,
        "sites": sites,
        "history": history_bytes,
        "index": index_bytes,
        "reports": len(reports),
        "oldest": _oldest_label(reports[0]) if reports else "",
        "caps": wanted,
        "archives": {"count": len(archives), "bytes": sum(_size_of(p) for p in archives)},
        "growth": {
            "history_bytes_per_day": history_rate,
            "screenshots_bytes_per_day": screenshot_rate,
            "total_bytes_per_day": history_rate + screenshot_rate,
            "source": source,
            "samples": len(recorded),
        },
        "forecasts": forecasts,
        "days_to_cap": min(known) if known else None,
    }


def _sample_series(samples: list[dict[str, Any]], key: str) -> list[tuple[datetime, int]]:
    """``(moment, bytes)`` points for one column of the recorded samples."""
    points: list[tuple[datetime, int]] = []
    for sample in samples:
        try:
            moment = datetime.fromisoformat(str(sample.get("taken_at", "")))
        except ValueError:  # pragma: no cover - the engine writes ISO stamps
            continue
        try:
            points.append((moment, int(sample.get(key, 0) or 0)))
        except (TypeError, ValueError):  # pragma: no cover - defensive
            continue
    return points


def storage_samples(output_dir: str | Path, days: int = 0, limit: int = 0) -> list[dict[str, Any]]:
    """The recorded storage samples, or ``[]`` when the index cannot answer.

    Reading the index is best-effort on purpose: a dashboard must still render
    when the SQLite file is missing, locked or corrupt.
    """
    folder = Path(output_dir)
    if not (folder / retention.INDEX_FILENAME).is_file():
        return []
    from app.core.store import HistoryStore

    try:
        with HistoryStore(folder / retention.INDEX_FILENAME) as store:
            return store.storage_series(days=days, limit=limit)
    except Exception:  # noqa: BLE001 - a broken index is not a broken dashboard
        return []


def _storage_chart(deltas: list[tuple[str, int]], width: int = 560, height: int = 96) -> str:
    """Inline SVG bars for the net bytes per day (positive up, negative down).

    The zero line sits low in the frame so a day that freed space (a retention
    rule firing) still shows up, below the axis, instead of being clamped away.
    """
    if not deltas:
        return ""
    peak = max([abs(delta) for _day, delta in deltas] + [1])
    zero = height * 0.7
    slot = width / len(deltas)
    bar_width = max(2.0, min(24.0, slot - 2.0))
    bars = []
    for index, (day, delta) in enumerate(deltas):
        span = (abs(delta) / peak) * (zero - 4 if delta >= 0 else height - zero - 12)
        x = index * slot + (slot - bar_width) / 2
        top = zero - span if delta >= 0 else zero
        colour = "#4a9eff" if delta >= 0 else "#e26464"
        title = f"{day}: {'+' if delta >= 0 else '-'}{abs(delta)} bytes"
        bars.append(
            f'<rect x="{x:.1f}" y="{top:.1f}" width="{bar_width:.1f}" '
            f'height="{max(1.0, span):.1f}" fill="{colour}" rx="1">'
            f"<title>{html.escape(title)}</title></rect>"
        )
    first, last = deltas[0][0], deltas[-1][0]
    return (
        f'<svg class="spark" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="history bytes per day from {first} to {last}">'
        f'<line x1="0" y1="{zero:.1f}" x2="{width}" y2="{zero:.1f}" '
        'stroke="#8a929e" stroke-width="1"/>' + "".join(bars) + "</svg>"
    )


def _bytes_per_day(output_dir: str | Path, samples: list[dict[str, Any]]) -> list[tuple[str, int]]:
    """Which growth series the chart should draw: samples first, runs second."""
    measured = retention.daily_delta(_sample_series(samples, "history"))
    return measured if len(measured) >= 2 else retention.daily_growth(output_dir)


#: How many sites :func:`storage_stats` reports individually (biggest first).
TOP_SITES = 10
#: Below this many days of room the panel warns about the cap.
SOON_DAYS = 7.0
#: The formats :func:`storage_export` can produce (``/api/storage?format=``).
STORAGE_FORMATS = ("json", "csv")


def storage_rows(stats: dict[str, Any]) -> list[tuple[str, Any]]:
    """Flatten a :func:`storage_stats` dict into ``(key, value)`` rows.

    One flat list of numbers is what a spreadsheet column, a CSV reader and a
    shell script all understand; the numbers stay raw (bytes, not "1.5 MB") so
    a cron job on another machine can add them up. An unknown forecast is an
    empty value rather than a zero - "no idea" and "today" are different.
    """
    growth = stats.get("growth") or {}
    archives = stats.get("archives") or {}
    forecasts = stats.get("forecasts") or {}
    caps = stats.get("caps") or {}

    def days(key: str) -> Any:
        value = forecasts.get(key)
        return "" if value is None else round(float(value), 2)

    return [
        ("total_bytes", int(stats.get("total", 0))),
        ("screenshot_bytes", int(stats.get("screenshots", 0))),
        ("history_bytes", int(stats.get("history", 0))),
        ("index_bytes", int(stats.get("index", 0))),
        ("reports", int(stats.get("reports", 0))),
        ("oldest_run", stats.get("oldest", "")),
        ("screenshots_cap_mb", int(caps.get("screenshots_mb", 0))),
        ("history_cap_mb", int(caps.get("history_mb", 0))),
        ("archive_count", int(archives.get("count", 0))),
        ("archive_bytes", int(archives.get("bytes", 0))),
        ("history_bytes_per_day", round(float(growth.get("history_bytes_per_day", 0.0)), 2)),
        (
            "screenshots_bytes_per_day",
            round(float(growth.get("screenshots_bytes_per_day", 0.0)), 2),
        ),
        ("days_to_cap", round(float(stats["days_to_cap"]), 2) if stats.get("days_to_cap") else ""),
        ("days_to_history_cap", days("history")),
        ("days_to_screenshots_cap", days("screenshots")),
        ("total_bytes_per_day", round(float(growth.get("total_bytes_per_day", 0.0)), 2)),
        ("growth_source", str(growth.get("source", ""))),
        ("samples", int(growth.get("samples", 0) or 0)),
    ] + [
        row
        # Three sites is enough to see where the disk goes without turning a
        # column of numbers into a directory listing.
        for index, site in enumerate((stats.get("sites") or [])[:3], start=1)
        for row in (
            (f"site_{index}_label", site.get("label", "")),
            (f"site_{index}_bytes", int(site.get("bytes", 0))),
            (f"site_{index}_files", int(site.get("files", 0))),
        )
    ]


def storage_export(
    output_dir: str | Path,
    caps: dict | None = None,
    fmt: str = "json",
    series: bool = False,
    days: int = 0,
    limit: int = 0,
    samples: list[dict[str, Any]] | None = None,
) -> tuple[bytes, str, str]:
    """``(body, content type, filename)`` for a machine-readable storage report.

    This is the body of ``/api/storage`` and of ``dashboard --storage-json``:
    the same numbers the Storage panel shows, in a shape a cron job can collect
    from every machine and add up (see ``tools/storage_report.py``).

    ``series=True`` adds the recorded samples (``?series=1``), optionally
    windowed with ``days``/``limit``. The *rates* always come from every sample,
    so asking for a shorter window never changes the forecast: the window only
    decides how much of the history travels. A series is not flat data, so it is
    JSON-only - asking for it as CSV is a bad request rather than a silently
    truncated file.
    """
    fmt = (fmt or "json").strip().lower()
    if fmt not in STORAGE_FORMATS:
        raise ValueError("format must be one of: " + ", ".join(STORAGE_FORMATS))
    recorded = storage_samples(output_dir) if samples is None else list(samples)
    stats = storage_stats(output_dir, caps, recorded)
    if series and fmt != "json":
        raise ValueError("series is only available as JSON (drop format=csv)")
    if fmt == "csv":
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(("key", "value"))
        writer.writerows(storage_rows(stats))
        return buffer.getvalue().encode("utf-8"), "text/csv; charset=utf-8", "storage.csv"
    payload: dict[str, Any] = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        **stats,
    }
    if series:
        windowed = _window_samples(recorded, days, limit)
        payload["series"] = windowed
        payload["growth_by_day"] = [
            {"day": day, "bytes": delta}
            for day, delta in retention.daily_delta(_sample_series(windowed, "history"))
        ]
    body = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
    return body, "application/json; charset=utf-8", "storage.json"


def _window_samples(
    samples: list[dict[str, Any]], days: int = 0, limit: int = 0
) -> list[dict[str, Any]]:
    """The newest samples, optionally inside a day window and a row limit."""
    rows = list(samples)
    if int(days or 0) > 0:
        cutoff = datetime.now() - timedelta(days=int(days))
        kept = []
        for row in rows:
            try:
                moment = datetime.fromisoformat(str(row.get("taken_at", "")))
            except ValueError:  # pragma: no cover - the engine writes ISO stamps
                continue
            if moment >= cutoff:
                kept.append(row)
        rows = kept
    if int(limit or 0) > 0:
        rows = rows[-int(limit) :]
    return rows


def _soonest_cap(stats: dict[str, Any]) -> tuple[str, float] | None:
    """``(which cap, days left)`` for the cap that will bite first, if any."""
    known = [
        (label, days) for label, days in (stats.get("forecasts") or {}).items() if days is not None
    ]
    return min(known, key=lambda item: item[1]) if known else None


def _storage_panel(
    output_dir: str | Path, caps: dict | None = None, downloads: bool = False
) -> str:
    """A small definition list, not a table - the search box counts table rows."""
    samples = storage_samples(output_dir)
    stats = storage_stats(output_dir, caps, samples)
    rows = [
        ("On disk", human_bytes(stats["total"])),
        ("Screenshots", human_bytes(stats["screenshots"])),
        (
            "History (reports + index)",
            f"{human_bytes(stats['history'])} in {stats['reports']} report file(s)",
        ),
    ]
    if stats["oldest"]:
        rows.append(("Oldest run", stats["oldest"]))
    if stats["caps"]:
        shown = ", ".join(
            f"{key.removesuffix('_mb')} {value} MB" for key, value in sorted(stats["caps"].items())
        )
        rows.append(("Caps", shown))
    growth = stats["growth"]
    if growth["history_bytes_per_day"] or growth["screenshots_bytes_per_day"]:
        rows.append(
            (
                "Growth",
                f"~{human_bytes(growth['history_bytes_per_day'])}/day history, "
                f"~{human_bytes(growth['screenshots_bytes_per_day'])}/day screenshots",
            )
        )
    soonest = _soonest_cap(stats)
    if soonest is not None:
        label, days = soonest
        rows.append(
            (
                "Forecast",
                f"{label} cap already reached"
                if days < 1.0
                else f"~{days:.0f} day(s) until the {label} cap",
            )
        )
    sites = stats.get("sites") or []
    biggest = ", ".join(f"{site['label']} {human_bytes(site['bytes'])}" for site in sites[:3])
    if biggest:
        rows.append(("Top sites", biggest))
    archives = stats.get("archives") or {}
    if archives.get("count"):
        rows.append(("Archives", f"{archives['count']} zip(s), {human_bytes(archives['bytes'])}"))
    if samples:
        rows.append(
            (
                "Samples",
                f"{len(samples)} since {str(samples[0].get('taken_at', ''))[:16].replace('T', ' ')}",
            )
        )
    items = "".join(
        f"<dt>{html.escape(label)}</dt><dd>{html.escape(value)}</dd>" for label, value in rows
    )
    chart = _storage_chart(_bytes_per_day(output_dir, samples))
    if chart:  # raw on purpose: it is SVG we just built, not user text
        items += f"<dt>Bytes/day</dt><dd>{chart}</dd>"
    if downloads:
        items += (
            "<dt>Download</dt><dd><a href='/api/storage?format=csv'>storage.csv</a> &middot; "
            "<a href='/api/storage?format=json'>storage.json</a></dd>"
        )
    warning = ""
    if soonest is not None and soonest[1] < SOON_DAYS:
        label = html.escape(soonest[0])
        if soonest[1] < 1.0:
            warning = (
                f"<p class='warn'>The {label} cap is already reached - raise it or shorten "
                "the retention window.</p>"
            )
        else:
            warning = (
                f"<p class='warn'>Cap in sight: at this rate the {label} cap is "
                f"~{soonest[1]:.0f} day(s) away - raise it or shorten the retention window.</p>"
            )
    return f"<section class='storage'><h2>Storage</h2><dl>{items}</dl>{warning}</section>"


_SEARCH_BOX = """
<div class="toolbar">
  <input id="site-search" type="search" placeholder="Filter sites..." aria-label="Filter sites">
  <span id="match-count" class="meta" aria-live="polite"></span>
</div>
<script>
(function () {
  var box = document.getElementById("site-search");
  var out = document.getElementById("match-count");
  var rows = Array.prototype.slice.call(document.querySelectorAll("tbody tr"));
  function apply() {
    var query = box.value.trim().toLowerCase();
    var shown = 0;
    rows.forEach(function (row) {
      var hit = row.cells[0].textContent.toLowerCase().indexOf(query) !== -1;
      row.hidden = !hit;
      if (hit) { shown += 1; }
    });
    out.textContent = (query && shown !== rows.length) ? shown + " of " + rows.length + " site(s)" : "";
  }
  box.addEventListener("input", apply);
  apply();
})();
</script>
"""


def render_dashboard(
    output_dir: str | Path,
    baseline_max_age_days: int | float = 0,
    url_filter: str = "",
    caps: dict | None = None,
    downloads: bool = False,
) -> str:
    """The dashboard HTML as a string (used by the file writer and the API).

    ``caps`` only decorates the storage panel: sizes are always measured, the
    configured limits are shown when the caller knows them. ``downloads`` adds
    links to ``/api/storage``, which only make sense when the page is served by
    ``serve`` (a static file has nothing to link to).
    """
    rows = _rows(output_dir, baseline_max_age_days, url_filter)
    generated = datetime.now().isoformat(timespec="seconds")
    wanted = (url_filter or "").strip()

    if rows:
        body_rows = "\n".join(
            "<tr>"
            f"<td>{html.escape(row['url'])}</td>"
            f"<td class='num'>{row['captures']}</td>"
            f"<td class='num'>{row['changes']}</td>"
            f"<td class='num'>{row['last_diff_text']}</td>"
            f"<td class='{row['baseline_class']}'>{row['baseline_text']}</td>"
            f"<td>{row['drift']}</td>"
            f"<td>{_sparkline(row['series'])}</td>"
            "</tr>"
            for row in rows
        )
        table = (
            "<table><thead><tr><th>Site</th><th>Captures</th><th>Changes</th>"
            "<th>Last diff</th><th>Baseline</th><th>Drift vs baseline</th><th>Trend</th>"
            "</tr></thead>"
            f"<tbody>{body_rows}</tbody></table>"
        )
        table = _SEARCH_BOX + table
    elif wanted:
        table = f"<p class='empty'>No site matches {html.escape(wanted)!r}.</p>"
    else:
        table = "<p class='empty'>No history yet.</p>"

    document = (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>FullPage Capture Bot - trend dashboard</title>"
        f"<style>{_CSS}</style></head><body>"
        "<h1>FullPage Capture Bot</h1>"
        f"<div class='meta'>Trend dashboard &middot; generated {html.escape(generated)}"
        + (f" &middot; filtered by {html.escape(wanted)!r}" if wanted else "")
        + "</div>"
        + _storage_panel(output_dir, caps, downloads)
        + f"{table}"
        "</body></html>"
    )

    return document


def build_dashboard(
    output_dir: str | Path,
    html_path: str | Path,
    baseline_max_age_days: int | float = 0,
    url_filter: str = "",
    caps: dict | None = None,
    downloads: bool = False,
) -> Path:
    """Write the dashboard to ``html_path`` and return that path.

    ``baseline_max_age_days`` marks long-unrefreshed baselines as stale,
    ``url_filter`` writes a dashboard for a single site (or URL substring),
    ``caps`` labels the storage panel with the configured limits and
    ``downloads`` adds the ``/api/storage`` links (served pages only).
    """
    target = Path(html_path)
    if target.parent and not target.parent.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        render_dashboard(output_dir, baseline_max_age_days, url_filter, caps, downloads),
        encoding="utf-8",
    )
    return target
