"""Draw the drift-vs-baseline history as a small PNG (Pillow, Qt-free).

The HTML dashboard charts drift with an inline SVG, which is perfect on screen
but useless in an email. This module renders the same information to a PNG so the
weekly digest can show *how far* the monitored pages have wandered without the
reader opening a browser. Nothing here raises on a missing history: a folder with
no recorded drift simply produces ``None``.
"""

from __future__ import annotations

import io
from datetime import datetime, timedelta
from pathlib import Path

from app.core import history

BACKGROUND = (255, 255, 255)
GRID = (226, 230, 236)
AXIS = (150, 156, 166)
TEXT = (58, 64, 74)
#: One colour per site, cycled when a chart holds more sites than colours.
PALETTE = (
    (74, 158, 255),
    (224, 158, 60),
    (90, 180, 120),
    (196, 110, 190),
    (226, 100, 100),
    (110, 200, 210),
    (170, 150, 90),
    (120, 130, 230),
)


def drift_series(output_dir: str | Path, days: int = 0) -> dict[str, list[float]]:
    """Oldest-first drift values per site, for sites that recorded at least one.

    ``days > 0`` keeps only the points recorded inside that window, so a chart
    can describe the same period as the digest text.
    """
    cutoff = datetime.now() - timedelta(days=int(days)) if int(days or 0) > 0 else None
    series: dict[str, list[float]] = {}
    for url in history.sites(output_dir):
        values: list[float] = []
        for point in history.drift_for_url(output_dir, url):
            if cutoff is not None:
                try:
                    if datetime.fromisoformat(str(point.get("timestamp") or "")) < cutoff:
                        continue
                except ValueError:
                    pass  # an unreadable stamp must not hide the point
            values.append(float(point["drift"]))
        if values:
            series[url] = values
    return series


def _font(size: int = 12):
    """The default bitmap font, at ``size`` when the Pillow build supports it."""
    from PIL import ImageFont

    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # pragma: no cover - Pillow < 10.1
        return ImageFont.load_default()


def _scale(values: list[float]) -> float:
    """A friendly top-of-axis value: at least 10% so a flat line stays visible."""
    peak = max([0.0, *values])
    for step in (0.1, 0.2, 0.25, 0.5, 0.75, 1.0):
        if peak <= step:
            return step
    return 1.0


def _short(url: str, limit: int = 28) -> str:
    text = url.replace("https://", "").replace("http://", "").rstrip("/")
    return text if len(text) <= limit else "..." + text[-limit + 3 :]


def render_drift_chart(
    output_dir: str | Path,
    days: int = 0,
    width: int = 720,
    height: int = 280,
    max_sites: int = 6,
) -> bytes | None:
    """A PNG line chart of drift vs baseline, or ``None`` when nothing is recorded."""
    series = drift_series(output_dir, days)
    if not series:
        return None

    # The sites that moved the most are the ones worth a line and a legend entry.
    ranked = sorted(series.items(), key=lambda item: (-max(item[1]), item[0]))[: max(1, max_sites)]
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (width, height), BACKGROUND)
    draw = ImageDraw.Draw(image)
    font = _font(12)
    small = _font(11)

    legend_width = 190
    left, right = 52, max(width - legend_width, 120)
    top, bottom = 34, height - 28
    peak = _scale([value for _url, values in ranked for value in values])

    title = "Drift vs pinned baseline"
    if int(days or 0) > 0:
        title += f" - last {int(days)} day(s)"
    draw.text((left - 34, 10), title, fill=TEXT, font=font)

    # horizontal grid at 0/25/50/75/100% of the axis
    for fraction in (0.0, 0.25, 0.5, 0.75, 1.0):
        y = bottom - (bottom - top) * fraction
        draw.line((left, y, right, y), fill=GRID if fraction else AXIS)
        draw.text((6, y - 7), f"{peak * fraction:.0%}", fill=TEXT, font=small)
    draw.line((left, top, left, bottom), fill=AXIS)

    longest = max(len(values) for _url, values in ranked)
    span = max(longest - 1, 1)

    for index, (url, values) in enumerate(ranked):
        colour = PALETTE[index % len(PALETTE)]
        points = [
            (
                left if longest == 1 else left + (right - left) * (i / span),
                bottom - (bottom - top) * min(1.0, value / peak),
            )
            for i, value in enumerate(values)
        ]
        if len(points) > 1:
            draw.line(points, fill=colour, width=2, joint="curve")
        for x, y in points:
            draw.ellipse((x - 2.5, y - 2.5, x + 2.5, y + 2.5), fill=colour)
        legend_y = top - 4 + index * 22
        draw.rectangle((right + 12, legend_y + 3, right + 22, legend_y + 13), fill=colour)
        label = _short(url)
        if len(values) == 1:
            label += " (1 run)"
        draw.text((right + 28, legend_y), label, fill=TEXT, font=small)

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()
