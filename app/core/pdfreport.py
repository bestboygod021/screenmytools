"""Build a PDF summary of a capture run (Qt-free, Pillow only).

Produces a cover page with the run statistics plus thumbnail pages showing each
captured screenshot. Pillow's multi-page PDF writer keeps this dependency-free
beyond what the project already ships.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from app.core.url_utils import build_url_label

PAGE_W, PAGE_H = 1240, 1754  # A4 portrait @ ~150 dpi
MARGIN = 60


def _font(size: int) -> Any:
    try:
        return ImageFont.truetype("DejaVuSans.ttf", size)
    except Exception:  # pragma: no cover - fall back to the bundled bitmap font
        return ImageFont.load_default()


def _new_page() -> Image.Image:
    return Image.new("RGB", (PAGE_W, PAGE_H), (255, 255, 255))


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: Any, max_width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = (current + " " + word).strip()
        if draw.textlength(candidate, font=font) <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [""]


def _status_text(status: Any) -> str:
    return getattr(status, "value", str(status))


def _cover_page(summary: Any, results: list, only_changed: bool = False) -> Image.Image:
    page = _new_page()
    draw = ImageDraw.Draw(page)
    title_font = _font(34)
    body_font = _font(18)
    small_font = _font(14)

    heading = (
        "FullPage Capture Bot - Changed pages" if only_changed else "FullPage Capture Bot - Report"
    )
    draw.text((MARGIN, MARGIN), heading, font=title_font, fill=(20, 20, 20))
    y = MARGIN + 70
    for line in (
        f"Total: {summary.total}",
        f"Succeeded: {summary.succeeded}",
        f"Failed: {summary.failed}",
        f"Shown: {len(results)}",
        f"Output: {summary.output_dir}",
    ):
        draw.text((MARGIN, y), line, font=body_font, fill=(40, 40, 40))
        y += 30

    y += 20
    for result in results:
        detail = f"{build_url_label(result.url)}  -  {_status_text(result.status)}"
        if result.diff is not None:
            detail += f"  (diff {result.diff:.2f})"
        for wrapped in _wrap(draw, detail, small_font, PAGE_W - 2 * MARGIN):
            if y > PAGE_H - MARGIN:
                return page
            draw.text((MARGIN, y), wrapped, font=small_font, fill=(60, 60, 60))
            y += 20
    return page


def _thumbnail_pages(results: list) -> list[Image.Image]:
    usable = [r for r in results if r.file_path and Path(r.file_path).exists()]
    if not usable:
        return []

    cell_w = (PAGE_W - 3 * MARGIN) // 2
    cell_h = (PAGE_H - 3 * MARGIN) // 2 - 40
    header = 50
    positions = [
        (MARGIN, MARGIN + header),
        (MARGIN + cell_w + MARGIN, MARGIN + header),
        (MARGIN, MARGIN + header + cell_h + 40),
        (MARGIN + cell_w + MARGIN, MARGIN + header + cell_h + 40),
    ]

    small_font = _font(14)
    pages: list[Image.Image] = []
    page: Image.Image | None = None
    draw: ImageDraw.ImageDraw | None = None

    for index, result in enumerate(usable):
        slot = index % 4
        if slot == 0:
            page = _new_page()
            draw = ImageDraw.Draw(page)
            pages.append(page)
        x, y = positions[slot]
        try:
            with Image.open(result.file_path) as image:
                image.thumbnail((cell_w, cell_h))
                thumbnail = image.convert("RGB").copy()
        except Exception:  # pragma: no cover - unreadable image is skipped
            continue
        page.paste(thumbnail, (x, y))  # type: ignore[union-attr]
        caption = f"{build_url_label(result.url)} ({_status_text(result.status)})"
        draw.text((x, y + thumbnail.height + 6), caption, font=small_font, fill=(60, 60, 60))  # type: ignore[union-attr]

    return pages


def _changed_results(summary: Any) -> list:
    """Pages whose diff met the threshold (a real visual change)."""
    return [r for r in summary.results if r.diff is not None and not r.unchanged]


def _draw_sparkline(
    draw: ImageDraw.ImageDraw,
    values: list,
    x: int,
    y: int,
    w: int,
    h: int,
    color: tuple[int, int, int] = (74, 158, 255),
) -> None:
    """Draw a small value-over-time polyline (values clamped to 0..1)."""
    points = [0.0 if value is None else max(0.0, min(1.0, value)) for value in values]
    if len(points) == 1:
        points = [*points, points[0]]
    if len(points) < 2:
        return
    step = w / (len(points) - 1)
    coords = [(x + i * step, y + h - value * h) for i, value in enumerate(points)]
    draw.line(coords, fill=color, width=2)


def _trend_page(output_dir: str) -> Image.Image | None:
    """A per-site trend table with sparklines, or ``None`` when there is no history."""
    from app.core import history

    try:
        rows = history.flat_rows(output_dir)
    except OSError:  # pragma: no cover - unreadable history is simply skipped
        return None
    if not rows:
        return None
    counts = history.site_change_counts(output_dir)
    captures: dict[str, int] = {}
    latest: dict[str, dict] = {}
    for row in rows:  # newest-first
        url = row["url"]
        captures[url] = captures.get(url, 0) + 1
        if url not in latest:
            latest[url] = row

    page = _new_page()
    draw = ImageDraw.Draw(page)
    draw.text((MARGIN, MARGIN), "Trend summary", font=_font(28), fill=(20, 20, 20))
    y = MARGIN + 60
    draw.text(
        (MARGIN, y),
        "Site / captures / changes / last diff / drift  (blue = diff, amber = drift vs baseline)",
        font=_font(15),
        fill=(90, 90, 90),
    )
    y += 34
    small = _font(14)
    chart_x = PAGE_W - MARGIN - 190
    for url, last in latest.items():
        drift_points = history.drift_for_url(output_dir, url)
        drift_series = [point["drift"] for point in drift_points]
        current_drift = drift_series[-1] if drift_series else None
        charted_drift = len(drift_series) >= 2
        row_height = 34 if charted_drift else 30
        if y + row_height > PAGE_H - MARGIN:
            break

        diff = last["diff"]
        diff_text = "-" if diff is None else f"{diff:.2f}"
        drift_text = "-" if current_drift is None else f"{current_drift:.2f}"
        line = (
            f"{build_url_label(url)}   {captures[url]} cap   {counts.get(url, 0)} chg"
            f"   last {diff_text}   drift {drift_text}"
        )
        draw.text((MARGIN, y), line[:80], font=small, fill=(50, 50, 50))

        series = [point["diff"] for point in history.trend_for_url(output_dir, url)]
        if charted_drift:
            _draw_sparkline(draw, series, chart_x, y - 2, 180, 14)
            _draw_sparkline(draw, drift_series, chart_x, y + 14, 180, 14, color=(224, 158, 60))
        else:
            _draw_sparkline(draw, series, chart_x, y - 4, 180, 24)
        y += row_height
    return page


def _compare_page(report: Any, limit: int = 12) -> Image.Image:
    """The two-period comparison as one page (the numbers, then the movers)."""
    from app.core.dashboard import human_bytes

    page = _new_page()
    draw = ImageDraw.Draw(page)
    draw.text((MARGIN, MARGIN), "Period comparison", font=_font(28), fill=(20, 20, 20))
    y = MARGIN + 56
    small = _font(14)
    draw.text(
        (MARGIN, y),
        f"A: {report.first.text()}    B: {report.second.text()}",
        font=_font(15),
        fill=(90, 90, 90),
    )
    y += 40
    headers = ("SITE", "CAPTURES", "CHANGES", "BYTES", "STATUS")
    columns = (MARGIN, MARGIN + 250, MARGIN + 400, MARGIN + 540, MARGIN + 740)
    for title, x in zip(headers, columns, strict=False):
        draw.text((x, y), title, font=small, fill=(120, 120, 120))
    y += 26
    draw.line([(MARGIN, y), (PAGE_W - MARGIN, y)], fill=(210, 210, 210), width=1)
    y += 10

    def line(entry: Any, colour: tuple[int, int, int]) -> None:
        nonlocal y
        cells = (
            entry.site[:30],
            f"{entry.captures_a}->{entry.captures_b} ({entry.delta_captures:+d})",
            f"{entry.changes_a}->{entry.changes_b} ({entry.delta_changes:+d})",
            f"{human_bytes(entry.bytes_a)}->{human_bytes(entry.bytes_b)}",
            entry.status(),
        )
        for value, x in zip(cells, columns, strict=False):
            draw.text((x, y), value, font=small, fill=colour)
        y += 28

    if not report.busy:
        draw.text(
            (MARGIN, y), "No captures recorded in either period.", font=small, fill=(90, 90, 90)
        )
        return page
    for entry in report.movers(limit):
        if y > PAGE_H - MARGIN - 40:
            break
        colour = (30, 30, 30)
        if entry.status() == "new":
            colour = (36, 130, 76)
        elif entry.status() == "gone":
            colour = (170, 60, 60)
        line(entry, colour)
    total = report.totals
    y += 6
    draw.line([(MARGIN, y), (PAGE_W - MARGIN, y)], fill=(210, 210, 210), width=1)
    y += 10
    line(total, (20, 20, 20))
    return page


def write_compare_pdf(report: Any, out_path: str | Path, limit: int = 12) -> Path:
    """Render a :class:`app.core.compare.Comparison` to a one-page PDF."""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    _compare_page(report, limit).save(out, "PDF", resolution=150.0)
    return out


def build_trend_pdf_bytes(output_dir: str | Path) -> bytes | None:
    """The per-site trend page as a one-page PDF, or ``None`` without history.

    Bytes rather than a path, because the weekly digest attaches it to an email.
    """
    page = _trend_page(str(output_dir))
    if page is None:
        return None
    buffer = io.BytesIO()
    page.save(buffer, "PDF", resolution=150.0)
    return buffer.getvalue()


def build_pdf_report(
    summary: Any, out_path: str | Path, only_changed: bool = False, include_trend: bool = False
) -> Path:
    """Render ``summary`` to a PDF at ``out_path`` and return the path.

    With ``only_changed`` the report is limited to pages that visually changed.
    With ``include_trend`` a per-site trend page (table + sparklines) is appended.
    """
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    results = _changed_results(summary) if only_changed else list(summary.results)
    pages = [_cover_page(summary, results, only_changed)]
    pages.extend(_thumbnail_pages(results))
    if include_trend:
        trend = _trend_page(getattr(summary, "output_dir", "") or "")
        if trend is not None:
            pages.append(trend)

    head, *rest = pages
    head.save(str(out), "PDF", resolution=150.0, save_all=True, append_images=rest)
    return out
