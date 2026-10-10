"""Deterministically draw the application icon (no external assets).

Produces ``assets/icon.png`` and a multi-resolution ``assets/icon.ico`` so the
PyInstaller build has a proper Windows icon. Re-run any time with::

    python tools/make_icon.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"

WINDOW = "#15171C"
CARD = "#1D2027"
ACCENT = "#3B82F6"
ACCENT_LIGHT = "#60A5FA"
TEXT = "#E8EAF0"


def draw_icon(size: int = 512) -> Image.Image:
    """A 'full page + down arrow' mark on a rounded dark tile."""
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    radius = int(size * 0.20)
    draw.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=WINDOW)
    draw.rounded_rectangle([4, 4, size - 5, size - 5], radius=radius - 4, fill=CARD)

    # The "page": a rounded outline occupying the upper two thirds.
    m = int(size * 0.18)  # outer margin
    page_bottom = int(size * 0.62)
    pr = int(size * 0.06)
    draw.rounded_rectangle(
        [m, m, size - m, page_bottom],
        radius=pr,
        outline=ACCENT,
        width=max(3, size // 32),
    )

    # Text lines inside the page.
    line_x0 = m + int(size * 0.09)
    line_x1 = size - m - int(size * 0.09)
    lw = max(2, size // 48)
    for i, y in enumerate((0.30, 0.40, 0.50)):
        width = line_x1 if i != 2 else line_x1 - int(size * 0.16)
        draw.line(
            [(line_x0, int(size * y)), (width, int(size * y))],
            fill=TEXT,
            width=lw,
        )

    # A bold down arrow below the page: "capture the whole page".
    cx = size // 2
    stem_top = page_bottom + int(size * 0.05)
    stem_bottom = size - int(size * 0.20)
    aw = max(4, size // 24)
    draw.line([(cx, stem_top), (cx, stem_bottom)], fill=ACCENT_LIGHT, width=aw * 2)
    head = int(size * 0.14)
    draw.polygon(
        [
            (cx - head, stem_bottom - head // 2),
            (cx + head, stem_bottom - head // 2),
            (cx, stem_bottom + head),
        ],
        fill=ACCENT_LIGHT,
    )

    return image


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)

    png = draw_icon(512)
    png.save(ASSETS / "icon.png", format="PNG")

    icon = draw_icon(256)
    icon.save(
        ASSETS / "icon.ico",
        format="ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )

    print(f"wrote {ASSETS / 'icon.png'} and {ASSETS / 'icon.ico'}")


if __name__ == "__main__":
    main()
