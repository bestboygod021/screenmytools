"""Build (and optionally send) a periodic digest of capture activity.

The digest is a plain-text summary computed from the report files: how many runs
happened, the change rate, the busiest sites, and any stale baselines. It reuses
the SMTP settings the change alerts already use, so no extra configuration is
needed to receive it by email.

A weekly cron job that mails the same numbers every week is noise, so the digest
also keeps a marker: ``digest_marker`` fingerprints the rows it would summarise
and ``should_skip_digest`` compares it with the fingerprint stored after the last
send (``.digest-marker.json`` in the output folder). ``digest
--skip-if-unchanged`` uses it to stay quiet until something was actually
captured.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app.core import baseline, history


def _within_window(row: dict, cutoff: datetime) -> bool:
    stamp = row.get("timestamp") or ""
    if not stamp:
        return True
    try:
        return datetime.fromisoformat(str(stamp)) >= cutoff
    except ValueError:
        return True


def marker_row_count(output_dir: str | Path) -> int:
    """How many rows the current marker covers (stored with it, for the log line)."""
    return len(history.flat_rows(output_dir))


def collect_stats(output_dir: str | Path, days: int = 7) -> dict[str, Any]:
    """Summarise the recent history: runs, captures, changes and top sites."""
    rows = history.flat_rows(output_dir)
    cutoff = datetime.now() - timedelta(days=max(int(days or 0), 0))
    recent = [row for row in rows if _within_window(row, cutoff)]

    changes: dict[str, int] = {}
    for row in recent:
        diff = row.get("diff")
        if diff is not None and diff > 0:
            changes[row["url"]] = changes.get(row["url"], 0) + 1

    failed = sum(1 for row in recent if str(row.get("status", "")).lower() not in ("success", ""))
    top_sites = sorted(changes.items(), key=lambda item: (-item[1], item[0]))[:5]
    sites = history.sites(output_dir)
    stale = baseline.stale_baselines(output_dir, sites, days)

    drift: list[tuple[str, float]] = []
    for url in sites:
        value = baseline.baseline_drift(output_dir, url)
        if value is not None and value > 0:
            drift.append((url, value))
    drift.sort(key=lambda item: (-item[1], item[0]))

    return {
        "days": int(days or 0),
        "captures": len(recent),
        "changed": sum(changes.values()),
        "failed": failed,
        "sites": len(sites),
        "top_sites": top_sites,
        "top_drift": drift[:5],
        "stale_baselines": stale,
    }


def format_digest(stats: dict[str, Any]) -> str:
    """Render the stats as a short plain-text email body."""
    days = stats.get("days", 0)
    captures = stats.get("captures", 0)
    changed = stats.get("changed", 0)
    failed = stats.get("failed", 0)
    rate = (changed / captures * 100.0) if captures else 0.0

    lines = [
        f"Capture Bot digest - last {days} day(s)",
        "",
        f"Captures recorded : {captures}",
        f"Changes detected  : {changed} ({rate:.0f}% of captures)",
        f"Failures          : {failed}",
        f"Monitored sites   : {stats.get('sites', 0)}",
    ]

    top = stats.get("top_sites") or []
    lines.append("")
    lines.append("Most changed sites:")
    if top:
        lines.extend(f"  {count:>3} change(s)  {url}" for url, count in top)
    else:
        lines.append("  (none)")

    stale = stats.get("stale_baselines") or []
    if stale:
        lines.append("")
        lines.append(f"Stale baselines ({len(stale)}) - consider re-pinning:")
        lines.extend(f"  {url}" for url in stale)

    drift = stats.get("top_drift") or []
    if drift:
        lines.append("")
        lines.append("Top drift vs pinned baseline:")
        lines.extend(f"  {value:>4.0%}  {url}" for url, value in drift)

    lines.append("")
    lines.append("-- FullPage Capture Bot")
    return "\n".join(lines)


def build_digest(output_dir: str | Path, days: int = 7) -> str:
    """The digest text for ``output_dir`` over the last ``days`` days."""
    return format_digest(collect_stats(output_dir, days))


#: What ``--attach`` accepts, singly or comma-separated (``none`` = nothing).
ATTACH_KINDS = ("dashboard", "csv", "drift", "pdf", "none")


def history_csv(output_dir: str | Path) -> bytes:
    """The capture rows as CSV bytes (timestamp,url,label,status,diff,drift,file)."""
    return history.rows_to_csv(history.flat_rows(output_dir))


#: Where the last digest fingerprint is kept (inside the output folder).
MARKER_FILENAME = ".digest-marker.json"


def digest_marker(output_dir: str | Path) -> str:
    """A fingerprint of everything the digest would summarise.

    It hashes the same CSV bytes the ``csv`` attachment carries, so "the marker
    did not change" really does mean "this mail would carry the same rows". Old
    rows ageing out of the digest window do **not** change it: the point is to
    answer "was anything captured since last time?".
    """
    return hashlib.sha256(history_csv(output_dir)).hexdigest()[:32]


def marker_path(output_dir: str | Path) -> Path:
    """Where the digest fingerprint lives: ``.digest-marker.json`` next to the history."""
    return Path(output_dir) / MARKER_FILENAME


def last_marker(output_dir: str | Path) -> dict[str, Any]:
    """The fingerprint stored by the last digest; ``{}`` when there is none."""
    try:
        data = json.loads(marker_path(output_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def record_marker(output_dir: str | Path, days: int = 7, rows: int = 0) -> Path | None:
    """Store the current fingerprint so the next digest can compare with it.

    Returns the written path, or ``None`` when the folder is not writable - a
    digest that has been sent must never be held back by a bookkeeping failure.
    """
    target = marker_path(output_dir)
    payload = {
        "marker": digest_marker(output_dir),
        "days": int(days or 0),
        "rows": int(rows or 0),
        "digest_at": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError:  # pragma: no cover - read-only folder
        return None
    return target


def should_skip_digest(output_dir: str | Path) -> tuple[bool, str, str]:
    """``(skip, current marker, previous marker)`` for ``--skip-if-unchanged``.

    Skipping needs both a stored marker and an identical current one: the very
    first digest always goes out, and an unreadable marker file is treated as
    "nothing stored" rather than as an excuse to stay silent.
    """
    current = digest_marker(output_dir)
    previous = str(last_marker(output_dir).get("marker") or "")
    return (bool(previous) and previous == current, current, previous)


def parse_kinds(attach: str | Sequence[str] | None) -> list[str]:
    """The attachment kinds ``attach`` asks for, in order and without duplicates.

    ``attach`` is normally the CLI value, so ``"dashboard,drift"`` means both and
    ``"none"`` means neither. Unknown names are ignored here and reported by
    :func:`unknown_kinds`, which the CLI uses to fail cleanly.
    """
    if attach is None:
        return ["dashboard"]
    if isinstance(attach, str):
        text = attach.strip().lower()
        if not text:
            return ["dashboard"]
        items = [part.strip() for part in text.replace(";", ",").split(",")]
    else:
        items = [str(part).strip().lower() for part in attach]
    kinds: list[str] = []
    for item in items:
        if item in ATTACH_KINDS and item != "none" and item not in kinds:
            kinds.append(item)
    return kinds


def unknown_kinds(attach: str | Sequence[str] | None) -> list[str]:
    """Names in ``attach`` that are not attachment kinds (for a clean CLI error)."""
    if attach is None:
        return []
    if isinstance(attach, str):
        text = attach.strip().lower()
        if not text:
            return []
        items = [part.strip() for part in text.replace(";", ",").split(",")]
    else:
        items = [str(part).strip().lower() for part in attach]
    return [item for item in items if item and item not in ATTACH_KINDS]


def _attachment_payload(output_dir: str | Path, kind: str) -> tuple[str, bytes, str] | None:
    """``(filename, data, subtype)`` for one kind, or ``None`` when it has no data."""
    kind = (kind or "").strip().lower()
    if kind == "csv":
        return ("history.csv", history_csv(output_dir), "csv")
    if kind == "drift":
        from app.core.chart import render_drift_chart

        chart = render_drift_chart(output_dir)
        return None if chart is None else ("drift-chart.png", chart, "png")
    if kind == "pdf":
        from app.core.pdfreport import build_trend_pdf_bytes

        report = build_trend_pdf_bytes(output_dir)
        return None if report is None else ("trend-report.pdf", report, "pdf")
    if kind == "dashboard":
        from app.core.dashboard import render_dashboard

        return ("dashboard.html", render_dashboard(output_dir).encode("utf-8"), "html")
    return None


def build_attachment(
    output_dir: str | Path, kind: str = "dashboard", max_bytes: int = 0
) -> tuple[str, bytes, str] | None:
    """The attachment for ``kind``: ``(filename, data, subtype)`` or ``None``.

    ``max_bytes > 0`` drops the attachment when it is too large, so a big history
    can never make the mail bounce off an SMTP size limit. ``drift`` yields a PNG
    line chart of the recorded drift and ``pdf`` the one-page trend report; both
    are skipped when the history has nothing to draw yet.
    """
    kind = (kind or "dashboard").strip().lower()
    if kind == "none":
        return None
    attachment = _attachment_payload(output_dir, kind)
    if attachment is None:
        return None
    if max_bytes and len(attachment[1]) > max_bytes:
        return None
    return attachment


def build_attachments(
    output_dir: str | Path, attach: str | Sequence[str] | None = "dashboard", max_bytes: int = 0
) -> list[tuple[str, bytes, str]]:
    """Every attachment ``attach`` asks for (e.g. ``"dashboard,drift"``)."""
    found = []
    for kind in parse_kinds(attach):
        attachment = build_attachment(output_dir, kind, max_bytes)
        if attachment is not None:
            found.append(attachment)
    return found


def attachment_skip_note(
    output_dir: str | Path, attach: str | Sequence[str] | None = "dashboard", max_bytes: int = 0
) -> str:
    """Explain every requested attachment that could not travel, one line each.

    Two reasons exist: the payload is bigger than the limit (reported with both
    sizes) or there was nothing to draw yet (the drift chart).
    """
    notes: list[str] = []
    for kind in parse_kinds(attach):
        payload = _attachment_payload(output_dir, kind)
        if payload is None:
            if kind == "drift":
                notes.append("Note: no drift data recorded yet, so the drift chart was skipped.")
            elif kind == "pdf":
                notes.append("Note: no history recorded yet, so the trend PDF was skipped.")
            continue
        if max_bytes and len(payload[1]) > max_bytes:
            notes.append(
                f"Note: the {kind} attachment was skipped ({len(payload[1]) / 1024:.0f} KB > "
                f"{max_bytes / 1024:.0f} KB limit)."
            )
    return "\n".join(notes)


def compare_stats(base: dict, head: dict) -> list[str]:
    """Human-readable deltas between two :func:`collect_stats` results."""

    def delta(key: str) -> str:
        before, after = int(base.get(key, 0)), int(head.get(key, 0))
        change = after - before
        return f"{before} -> {after} ({change:+d})" if change else f"{before} -> {after} (0)"

    lines = [
        f"Captures : {delta('captures')}",
        f"Changes  : {delta('changes')}",
        f"Failures : {delta('failed')}",
        f"Sites    : {delta('sites')}",
    ]

    base_top = {url for url, _count in (base.get("top_sites") or [])}
    head_top = {url for url, _count in (head.get("top_sites") or [])}
    newly_busy = sorted(head_top - base_top)
    if newly_busy:
        lines.append("Newly busy sites: " + ", ".join(newly_busy))

    newly_stale = sorted(
        set(head.get("stale_baselines") or []) - set(base.get("stale_baselines") or [])
    )
    if newly_stale:
        lines.append("Newly stale baselines: " + ", ".join(newly_stale))

    return lines


def build_comparison(head_dir: str | Path, base_dir: str | Path, days: int = 7) -> str:
    """A short 'base branch vs this branch' summary of the two histories."""
    base = collect_stats(base_dir, days)
    head = collect_stats(head_dir, days)
    return "vs base branch\n" + "\n".join(compare_stats(base, head))


def comment_marker(name: str) -> str:
    """The HTML marker that makes one profile's comment recognisable again."""
    slug = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in (name or "profile"))
    return f"<!-- capture-bot-weekly-digest:{slug} -->"


def comment_body(name: str, text: str, run_url: str = "") -> str:
    """The markdown body of a profile's sticky comment (marker included)."""
    lines = [
        comment_marker(name),
        f"### Weekly digest - {name}",
        "",
        "```",
        text.rstrip(),
        "```",
    ]
    if run_url:
        lines += ["", f"_Updated by [this run]({run_url})._"]
    return "\n".join(lines) + "\n"


def link_lines(base_url: str, days: int = 7) -> list[str]:
    """Download links for the history API, so a big report can be fetched instead of attached.

    ``base_url`` is the address ``serve`` is reachable at (e.g.
    ``http://reports.local:8765``).
    """
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return []
    return [
        "Fetch the full data instead of an attachment:",
        f"  dashboard : {base}/",
        f"  this window (JSON) : {base}/api/history",
        f"  per-site trends    : {base}/api/trend",
        f"  digest stats       : {base}/api/digest?days={int(days or 0)}",
    ]


def send_digest(
    settings: Any,
    output_dir: str | Path,
    days: int = 7,
    attach: str = "dashboard",
    extra: str = "",
    max_attachment_bytes: int = 0,
    link_base: str = "",
) -> bool:
    """Email the digest using the alert SMTP settings. Returns True when sent.

    ``attach`` picks what travels with the mail: the current ``dashboard`` HTML,
    a ``csv`` of the rows, a ``drift`` chart PNG or the ``pdf`` trend report -
    comma-separated for several - or ``none``. ``extra`` is appended to the body (e.g.
    a base-branch comparison). ``link_base`` adds download links to a running
    ``serve`` instance, which is how large reports are shared instead of as
    attachments.

    Raises ``ValueError`` when SMTP is not configured, so the caller can explain
    what is missing instead of failing silently.
    """
    from app.core import alerts

    host = getattr(settings, "smtp_host", "") or ""
    to = getattr(settings, "alert_email_to", "") or ""
    if not host or not to:
        raise ValueError("Digest needs an SMTP host and a recipient email.")

    body = build_digest(output_dir, days)
    note = attachment_skip_note(output_dir, attach, max_attachment_bytes)
    if note:
        body = f"{body}\n\n{note}"
    if extra:
        body = f"{body}\n\n{extra}"
    links = link_lines(link_base, days)
    if links:
        body = f"{body}\n\n" + "\n".join(links)
    attachments = build_attachments(output_dir, attach, max_attachment_bytes)
    from app.core import secrets

    return alerts.send_email(
        host,
        int(getattr(settings, "smtp_port", 587)),
        getattr(settings, "smtp_user", ""),
        secrets.smtp_password(settings),
        to,
        f"[Capture Bot] Weekly digest - {days} day(s)",
        body,
        attachments=attachments or None,
    )
