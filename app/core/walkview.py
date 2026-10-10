"""A visual, self-contained report of the newest walk (journey or crawl).

A folder full of numbered screenshots answers "what did it capture?" but not
"where was that, and how did it get there?". This reads back the report a walk
already wrote (``crawl-*/report.json`` or ``journey-report-*.json``) and turns it
into one page: every screen, the clicks that led to it, and a thumbnail - with
the images linked by relative path, so the file can be opened from the folder,
copied elsewhere with its images, or served.

Qt-free on purpose: the app opens it in the desktop browser, the CLI prints the
same thing as text, and a test only needs a folder and a JSON file.
"""

from __future__ import annotations

import html
import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

#: Extensions a thumbnail is generated for (everything else is listed as a link).
_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".avif", ".gif")


@dataclass
class WalkScreen:
    """One screen of a walk: what it is, how it was reached, what it looks like."""

    index: int = 0
    name: str = ""
    url: str = ""
    clicks: list[str] = field(default_factory=list)
    file_path: str = ""
    title: str = ""
    error: str = ""
    depth: int = 0

    @property
    def is_image(self) -> bool:
        return Path(self.file_path).suffix.lower() in _IMAGE_SUFFIXES

    @property
    def route(self) -> str:
        return " -> ".join(self.clicks) if self.clicks else "landing page"

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "name": self.name,
            "url": self.url,
            "clicks": list(self.clicks),
            "route": self.route,
            "file": self.file_path,
            "title": self.title,
            "depth": self.depth,
            "error": self.error,
        }


@dataclass
class Walk:
    """The newest walk found in an output folder."""

    kind: str = ""  # "crawl" or "journey"
    folder: str = ""
    source: str = ""  # the report file it was read from
    generated_at: str = ""
    url: str = ""
    screens: list[WalkScreen] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    diff: dict[str, Any] | None = None  # crawl --compare, when it was run

    @property
    def found(self) -> bool:
        return bool(self.screens)

    def summary(self) -> str:
        if not self.found:
            return "No walk (journey or crawl) has been run in this folder yet."
        lines = [
            f"{self.kind} walk in {self.folder}",
            f"{len(self.screens)} screen(s)",
        ]
        for screen in self.screens:
            mark = f"{screen.index:03d}"
            shape = f"  [{screen.error}]" if screen.error else ""
            lines.append(f"  {mark} {screen.name}  [{screen.route}]{shape}")
        if self.skipped:
            lines.append(f"{len(self.skipped)} candidate(s) skipped:")
            for item in self.skipped[:10]:
                lines.append(f"  - {item.get('text') or item.get('href')}: {item.get('reason')}")
        if self.diff:
            lines.append(
                f"compared to an earlier crawl: {len(self.diff.get('added') or [])} new, "
                f"{len(self.diff.get('removed') or [])} gone"
            )
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "folder": self.folder,
            "source": self.source,
            "generated_at": self.generated_at,
            "url": self.url,
            "screens": [screen.to_dict() for screen in self.screens],
            "skipped": list(self.skipped),
            "errors": list(self.errors),
            "diff": self.diff,
        }


def _journey_walk(report_path: Path) -> Walk:
    """Build a walk from a ``journey-report-*.json``."""
    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return Walk()
    if not isinstance(data, dict):
        return Walk()
    walk = Walk(
        kind="journey",
        folder=str(data.get("output_dir") or report_path.parent),
        source=str(report_path),
        generated_at=str(data.get("generated_at") or ""),
        url=str(data.get("url") or ""),
    )
    for run in data.get("journeys") or []:
        if not isinstance(run, dict):
            continue
        walk.url = walk.url or str(run.get("url") or "")
        pending: list[str] = []
        for step in run.get("steps") or []:
            if not isinstance(step, dict):
                continue
            if step.get("skipped"):
                continue
            if step.get("file"):
                walk.screens.append(
                    WalkScreen(
                        index=len(walk.screens) + 1,
                        name=Path(str(step["file"])).stem,
                        url=str(step.get("url") or run.get("url") or ""),
                        clicks=list(pending),
                        file_path=str(step["file"]),
                        title=str(run.get("name") or ""),
                        error="" if step.get("ok", True) else str(step.get("message") or ""),
                    )
                )
                continue
            if not step.get("ok", True):
                walk.errors.append(f"{run.get('name')}: {step.get('detail') or step.get('action')}")
                continue
            if step.get("action") != "capture":
                pending.append(str(step.get("detail") or step.get("action") or ""))
    return walk


def _crawl_walk(report_path: Path) -> Walk:
    """Build a walk from a ``crawl-*/report.json``."""
    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return Walk()
    if not isinstance(data, dict):
        return Walk()
    walk = Walk(
        kind="crawl",
        folder=str(report_path.parent),
        source=str(report_path),
        generated_at=str(data.get("generated_at") or ""),
        url=str(data.get("url") or ""),
        skipped=[item for item in (data.get("skipped") or []) if isinstance(item, dict)],
        errors=[str(item) for item in (data.get("errors") or [])],
    )
    for raw in data.get("pages") or []:
        if not isinstance(raw, dict):
            continue
        walk.screens.append(
            WalkScreen(
                index=int(raw.get("index") or len(walk.screens) + 1),
                name=str(raw.get("name") or ""),
                url=str(raw.get("url") or ""),
                clicks=[str(item) for item in (raw.get("path") or [])],
                file_path=str(raw.get("file") or ""),
                title=str(raw.get("title") or ""),
                depth=int(raw.get("depth") or 0),
                error=str(raw.get("error") or ""),
            )
        )
    diff_path = report_path.parent / "diff.json"
    if diff_path.exists():
        try:
            loaded = json.loads(diff_path.read_text(encoding="utf-8"))
            walk.diff = loaded if isinstance(loaded, dict) else None
        except (OSError, json.JSONDecodeError):
            walk.diff = None
    return walk


def _walk_in(root: Path) -> Walk:
    """The newest walk whose reports live directly in ``root``."""
    crawls = sorted(
        (path for path in root.glob("crawl-*/report.json")), key=lambda item: item.parent.name
    )
    if crawls:
        walk = _crawl_walk(crawls[-1])
        if walk.found:
            return walk

    reports = sorted(root.glob("journey-report-*.json"), key=lambda item: item.name)
    if reports:
        walk = _journey_walk(reports[-1])
        if walk.found:
            return walk
    return Walk()


def latest_walk(output_dir: str | Path) -> Walk:
    """The most recently written walk (crawl first, then journey) in a folder.

    A journey writes its report to the output folder but its screenshots to
    ``journeys/``, so pointing at the screenshots folder is the natural mistake -
    and a useful thing to accept. Hence the one-level fallback to the parent.
    """
    root = Path(output_dir)
    if not root.is_dir():
        return Walk()

    walk = _walk_in(root)
    if walk.found:
        return walk
    if root.parent != root:
        return _walk_in(root.parent)
    return Walk()


# --------------------------------------------------------------------------- #
# the page
# --------------------------------------------------------------------------- #
_CSS = """
:root { color-scheme: dark; }
body { margin: 0; padding: 28px; background: #16171d; color: #e7e9ee;
       font: 15px/1.55 "Segoe UI", system-ui, sans-serif; }
h1 { margin: 0 0 4px; font-size: 22px; font-weight: 650; }
p.meta { margin: 0 0 22px; color: #9aa1b1; font-size: 13px; }
.grid { display: grid; gap: 18px; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); }
.card { background: #1e2027; border: 1px solid #2c2f38; border-radius: 12px; overflow: hidden; }
.card .head { padding: 10px 14px; border-bottom: 1px solid #2c2f38; }
.pair { display: grid; grid-template-columns: 1fr 1fr; gap: 4px; background: #111218; }
.card h2 { margin: 0; font-size: 15px; font-weight: 600; }
.route { margin: 3px 0 0; color: #9aa1b1; font-size: 12px; }
.shot { display: block; background: #111218; }
.shot img { display: block; width: 100%; height: auto; cursor: zoom-in; }
/* A before/after wipe: the two screenshots stacked in one box, the older one on
   top with its right edge at --at. The handle is a range input stretched over the
   whole image, so a drag anywhere moves the edge. */
.wipe { grid-column: 1 / -1; background: #1e2027; border: 1px solid #2c2f38;
        border-radius: 12px; overflow: hidden; }
.wipe .head { padding: 10px 14px; border-bottom: 1px solid #2c2f38; }
.wipe h3 { margin: 0; font-size: 15px; font-weight: 600; }
.wipe .hint { margin: 0; padding: 8px 14px 12px; color: #9aa1b1; font-size: 12px; }
.slider { position: relative; background: #111218; touch-action: none; }
.slider img { display: block; width: 100%; height: auto; }
.slider .over { position: absolute; inset: 0; overflow: hidden;
                width: var(--at, 50%); border-right: 2px solid #7cb7ff; }
.slider .over img { width: auto; max-width: none; height: 100%; }
.slider .handle { position: absolute; inset: 0; width: 100%; height: 100%;
                  margin: 0; appearance: none; background: transparent;
                  cursor: ew-resize; }
.slider .handle::-webkit-slider-thumb { appearance: none; width: 14px; height: 100%;
                  background: transparent; }
.slider .tag { position: absolute; top: 8px; padding: 2px 8px; border-radius: 999px;
               background: #0f1015cc; color: #e7e9ee; font-size: 11px; }
.slider .tag.left { left: 8px; }
.slider .tag.right { right: 8px; }
@media (prefers-reduced-motion: no-preference) {
  .slider .over { will-change: width; }
}
/* Full size: a plain dialog, so Esc and the back button both work. */
dialog.lightbox { border: 0; padding: 0; background: #0f1015; max-width: 96vw;
                  max-height: 96vh; }
dialog.lightbox img { display: block; max-width: 96vw; max-height: 92vh;
                      width: auto; height: auto; cursor: zoom-out; }
dialog.lightbox::backdrop { background: #000000cc; }
.empty { padding: 26px 14px; color: #9aa1b1; font-size: 13px; }
.bad { color: #ff8f8f; }
.new { color: #7ce38b; }
.gone { color: #ff9d9d; }
ul { margin: 8px 0 0; padding-left: 18px; color: #b9bfcc; font-size: 13px; }
a { color: #7cb7ff; text-decoration: none; }
"""


_SCRIPT = """
// The page is a file on disk: no framework, a few lines of vanilla JS, and it
// still works when opened straight from the folder or served by hand.
(() => {
  const box = document.getElementById('lightbox');
  const big = box ? box.querySelector('img') : null;

  document.addEventListener('input', (event) => {
    const handle = event.target.closest('.handle');
    if (!handle) return;
    const slider = handle.closest('.slider');
    if (slider) slider.style.setProperty('--at', handle.value + '%');
  });

  document.addEventListener('click', (event) => {
    const image = event.target.closest('img');
    if (!image || !box || !big) return;
    big.src = image.currentSrc || image.src;
    box.showModal();
  });

  if (box) {
    box.addEventListener('click', () => box.close());
    box.addEventListener('close', () => { big.removeAttribute('src'); });
  }
})();
"""


def _diff_block(diff: dict[str, Any]) -> list[str]:
    """The "what changed since the last walk" panel.

    Added and gone screens are names; a *changed* screen is a picture, because
    "the page is still there but moved" is only believable next to the two
    screenshots it is talking about.
    """
    added = [item for item in (diff.get("added") or []) if isinstance(item, dict)]
    removed = [item for item in (diff.get("removed") or []) if isinstance(item, dict)]
    renamed = [item for item in (diff.get("renamed") or []) if isinstance(item, dict)]
    changed = [item for item in (diff.get("changed") or []) if isinstance(item, dict)]
    if not (added or removed or renamed or changed):
        return []

    parts = ["<h2 style='margin:0 0 8px;font-size:16px'>Since the previous walk</h2><ul>"]
    for item in added:
        parts.append(f"<li class='new'>+ {html.escape(str(item.get('name') or ''))}</li>")
    for item in removed:
        parts.append(f"<li class='gone'>- {html.escape(str(item.get('name') or ''))}</li>")
    for item in renamed:
        parts.append(
            f"<li>~ {html.escape(str(item.get('url') or ''))}: "
            f"&lsquo;{html.escape(str(item.get('was') or ''))}&rsquo; is now "
            f"&lsquo;{html.escape(str(item.get('now') or ''))}&rsquo;</li>"
        )
    for item in changed:
        percent = round(float(item.get("diff") or 0) * 100)
        parts.append(
            f"<li>! {html.escape(str(item.get('name') or ''))} changed - "
            f"{percent}% different - {html.escape(str(item.get('url') or ''))}</li>"
        )
    parts.append("</ul>")

    if changed:
        base = Path(diff.get("page_dir") or ".")
        parts.append("<div class='grid'>")
        for number, item in enumerate(changed, start=1):
            card = _wipe_pair(item, base, number)
            if card:
                parts.append(card)
        parts.append("</div>")
    return parts


def _wipe_pair(item: dict[str, Any], base: Path, number: int) -> str:
    """One changed screen as a drag-to-compare pair of its two screenshots.

    Two thumbnails side by side answer "did it change"; they do not answer *what*
    changed, because the eye cannot line up two scaled copies of a long page. A
    wipe - one image on top of the other, with a handle that slides the top one
    aside - does: whatever moves under the handle is the difference.
    """
    before = _link_or_empty(str(item.get("before") or ""), base)
    after = _link_or_empty(str(item.get("after") or ""), base)
    percent = round(float(item.get("diff") or 0) * 100)
    name = html.escape(str(item.get("name") or ""))
    if not before and not after:
        return ""

    if not before or not after:
        # One side was pruned: show the one that survived rather than nothing.
        link = before or after
        label = "before" if before else "after"
        return (
            f"<article class='wipe' data-diff='{percent}'>"
            f"<div class='head'><h3>{name} &middot; {label} only</h3>"
            f"<p class='route'>{percent}% different &middot; "
            f"{html.escape(str(item.get('url') or ''))}</p></div>"
            f"<img loading='lazy' src='{html.escape(link)}' alt='{label}'></article>"
        )

    return (
        f"<article class='wipe' data-diff='{percent}'>"
        f"<div class='head'><h3>{name} &middot; before / after</h3>"
        f"<p class='route'>{percent}% different &middot; "
        f"{html.escape(str(item.get('url') or ''))}</p></div>"
        f"<div class='slider' style='--at:50%'>"
        f"<img class='under' loading='lazy' src='{html.escape(after)}' alt='after'>"
        f"<div class='over'><img loading='lazy' src='{html.escape(before)}' alt='before'></div>"
        f"<input class='handle' type='range' min='0' max='100' value='50' "
        f"aria-label='Drag to compare before and after' "
        f"data-before='{html.escape(before)}' data-after='{html.escape(after)}'>"
        f"<span class='tag left'>before</span><span class='tag right'>after</span>"
        f"</div>"
        f"<p class='hint'>Drag the handle: {html.escape(str(item.get('url') or ''))} "
        f"&middot; click a screenshot for full size</p></article>"
    )


def _link_or_empty(target: str, base: Path) -> str:
    """A relative link, but only while the file is still there.

    A diff names the screenshots of the *previous* walk as well, and retention may
    have deleted them in the meantime. A broken image would say "this is what it
    looked like", which is worse than saying nothing.
    """
    if not target:
        return ""
    try:
        if Path(target).exists():
            return _relative(target, base)
    except OSError:  # pragma: no cover - a path the OS refuses to look at
        return ""
    return ""


def _relative(target: str, base: Path) -> str:
    """A link to ``target`` from the folder the page will live in."""
    if not target:
        return ""
    try:
        return Path(os.path.relpath(target, base)).as_posix()
    except ValueError:  # pragma: no cover - different drives on Windows
        return Path(target).as_uri()


def render_walk(walk: Walk, page_dir: str | Path | None = None) -> str:
    """The walk as one self-contained HTML page."""
    base = Path(page_dir) if page_dir else Path(walk.folder or ".")
    if walk.diff is not None:
        # _relative() needs the folder the page will live in, and it is the same
        # one for the screenshots of the current walk and of the previous one.
        walk.diff = {**walk.diff, "page_dir": str(base)}
    parts = [
        "<!doctype html>",
        "<html lang='en'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1'>",
        f"<title>{html.escape(walk.kind or 'walk')} walk</title>",
        f"<style>{_CSS}</style></head><body>",
        f"<h1>{html.escape(walk.kind.title() or 'Walk')} - {len(walk.screens)} screen(s)</h1>",
        f"<p class='meta'>{html.escape(walk.url or '')} &middot; "
        f"{html.escape(walk.generated_at or '')} &middot; "
        f"<a href='{html.escape(_relative(walk.source, base))}'>report</a></p>",
    ]

    if walk.diff is not None:
        parts.extend(_diff_block(walk.diff))

    parts.append("<div class='grid'>")
    for screen in walk.screens:
        parts.append("<article class='card'>")
        parts.append("<div class='head'>")
        parts.append(f"<h2>{screen.index:03d} &nbsp;{html.escape(screen.name)}</h2>")
        route = html.escape(screen.route)
        parts.append(f"<p class='route'>{route}</p>")
        if screen.url:
            parts.append(
                f"<p class='route'><a href='{html.escape(screen.url)}'>{html.escape(screen.url)}</a></p>"
            )
        if screen.error:
            parts.append(f"<p class='route bad'>{html.escape(screen.error)}</p>")
        parts.append("</div>")
        link = _relative(screen.file_path, base)
        if screen.is_image and link:
            parts.append(
                f"<a class='shot' href='{html.escape(link)}'>"
                f"<img loading='lazy' src='{html.escape(link)}' alt='{html.escape(screen.name)}'>"
                "</a>"
            )
        elif link:
            parts.append(
                f"<div class='empty'><a href='{html.escape(link)}'>open the file</a></div>"
            )
        else:
            parts.append("<div class='empty'>this screen was not photographed</div>")
        parts.append("</article>")
    parts.append("</div>")

    if walk.skipped:
        parts.append("<h2 style='margin-top:26px;font-size:16px'>Skipped on purpose</h2><ul>")
        for item in walk.skipped:
            label = item.get("text") or item.get("href") or item.get("kind") or "?"
            parts.append(
                f"<li>{html.escape(str(label))} - {html.escape(str(item.get('reason')))}</li>"
            )
        parts.append("</ul>")

    parts.append(
        f"<p class='meta' style='margin-top:26px'>Generated {datetime.now().isoformat(timespec='seconds')} "
        "by FullPage Capture Bot.</p>"
    )
    parts.append("<dialog class='lightbox' id='lightbox'><img alt=''></dialog>")
    parts.append(f"<script>{_SCRIPT}</script>")
    parts.append("</body></html>")
    return "\n".join(parts)


def build_walk_report(output_dir: str | Path, html_path: str | Path | None = None) -> Path | None:
    """Write the newest walk as an HTML page (``None`` when there is no walk)."""
    walk = latest_walk(output_dir)
    if not walk.found:
        return None
    target = Path(html_path) if html_path else Path(walk.folder) / "walk-report.html"
    if target.parent and not target.parent.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_walk(walk, target.parent), encoding="utf-8")
    return target
