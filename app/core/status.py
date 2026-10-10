"""The health picture of one capture folder, without the HTTP.

``/api/status`` started as a plain summary of the history folder: how many
captures, how big the index is, whether anything waits in the quiet-hours queue.
A monitor needs one more thing, though - the answer to "is this bot still doing
its job?" - so the same module also computes a small health verdict: the age of
the newest capture and whether it is older than the caller allows.

The logic lives here (Qt-free, stdlib only) so the HTTP layer and the engine's own
watchdog judge freshness with exactly the same rule.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from app.core import baseline, history, quiet

#: Health states: a fresh capture, nothing recorded yet, or nothing recent.
OK = "ok"
EMPTY = "empty"
STALE = "stale"
#: The ``status`` value a row carries when the capture itself worked.
SUCCESS = "success"


def age_seconds(stamp: str, now: datetime) -> int | None:
    """Seconds since ``stamp`` (an ISO timestamp), or ``None`` when unreadable."""
    try:
        moment = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return None
    return int((now - moment).total_seconds())


def epoch_seconds(stamp: str) -> int:
    """Unix time for an ISO timestamp (-1 when it cannot be read)."""
    try:
        return int(datetime.fromisoformat(str(stamp)).timestamp())
    except (TypeError, ValueError):
        return -1


def last_success(rows: list[dict[str, Any]], now: datetime) -> dict | None:
    """The newest row that actually captured something (``None`` when there is none).

    A monitor wants two different questions answered: "when did the bot last
    finish a run?" (the health verdict, which counts failures too) and "when did
    it last succeed?". A cron job that fails every night still looks fresh to the
    first one, so the failure counter is exported separately.
    """
    for row in rows:
        if str(row.get("status", "")).lower() != SUCCESS:
            continue
        stamp = str(row.get("timestamp", ""))
        return {
            "timestamp": stamp,
            "url": row.get("url", ""),
            "age_seconds": age_seconds(stamp, now),
            "seconds": epoch_seconds(stamp),
        }
    return None


def verdict(rows: list[dict[str, Any]], expect_max_age_minutes: int = 0, now=None) -> dict:
    """Judge freshness from already-loaded ``rows`` (newest first).

    ``expect_max_age_minutes <= 0`` means "do not judge": the state is then
    ``ok`` as soon as anything was ever captured, and a capture whose timestamp
    cannot be parsed counts as stale when a limit is given - better a false alarm
    than a silent bot.
    """
    moment = now or datetime.now()
    limit = max(0, int(expect_max_age_minutes or 0))
    newest = rows[0] if rows else None
    age = None if newest is None else age_seconds(newest.get("timestamp", ""), moment)
    if newest is None:
        state = EMPTY
    elif limit and (age is None or age > limit * 60):
        state = STALE
    else:
        state = OK
    return {
        "state": state,
        "captures": len(rows),
        "age_seconds": age,
        "last_capture": newest.get("url") if newest else None,
        "max_age_minutes": limit,
    }


def health(output_dir: str | Path, expect_max_age_minutes: int = 0, now=None) -> dict:
    """The freshness verdict for ``output_dir`` (see :func:`verdict`)."""
    return verdict(history.flat_rows(output_dir), expect_max_age_minutes, now)


def status_payload(
    output_dir: str | Path,
    quiet_hours: str = "",
    expect_max_age_minutes: int = 0,
    now=None,
    storage: dict | None = None,
) -> dict:
    """One small object for a monitoring probe: the body of ``/api/status``.

    ``quiet_hours`` is optional and only echoed back with the state it implies;
    ``expect_max_age_minutes`` adds the freshness verdict (the ``health`` block is
    always present, with ``max_age_minutes`` 0 when no limit was asked for).

    ``storage`` is the disk picture (:func:`app.core.dashboard.storage_stats`),
    passed in by whoever measured it. It is optional because measuring the folder
    costs a walk over every file: ``/metrics`` and ``metrics --check`` want it,
    a plain ``/api/status`` probe usually does not.
    """
    moment = now or datetime.now()
    rows = history.flat_rows(output_dir)
    sites = history.sites(output_dir)
    db = Path(output_dir) / "history.sqlite3"
    try:
        index_bytes = db.stat().st_size
    except OSError:
        index_bytes = 0

    queued = quiet.load(quiet.queue_path(output_dir))
    stamps = sorted(str(item.get("queued_at") or "") for item in queued)
    newest = rows[0] if rows else None

    payload: dict = {
        "generated_at": moment.isoformat(timespec="seconds"),
        "captures": len(rows),
        "sites": len(sites),
        "health": verdict(rows, expect_max_age_minutes, moment),
        "last_capture": (
            None
            if newest is None
            else {
                "timestamp": newest.get("timestamp", ""),
                "url": newest.get("url", ""),
                "status": newest.get("status", ""),
                "diff": newest.get("diff"),
            }
        ),
        "last_success": last_success(rows, moment),
        "index": {"file": db.name, "exists": db.exists(), "bytes": index_bytes},
        "pending_alerts": {
            "count": len(queued),
            "oldest": next((stamp for stamp in stamps if stamp), None),
        },
        "baselines": {"pinned": sum(1 for url in sites if baseline.has_baseline(output_dir, url))},
    }

    if storage:
        payload["storage"] = storage

    window = (quiet_hours or "").strip()
    if window:
        resumes = quiet.window_end(moment, window)
        payload["quiet"] = {
            "window": window,
            "active": quiet.in_window(moment, window),
            "resumes_at": resumes.isoformat(timespec="minutes") if resumes else None,
        }
    return payload


def success_age_seconds(payload: dict) -> int | None:
    """Seconds since the newest *successful* capture, or ``None`` when there is none.

    ``/api/status`` already carries this under ``last_success``; the helper exists
    so a caller (``metrics --check``) does not have to know the nesting.
    """
    return (payload.get("last_success") or {}).get("age_seconds")


def freshness(payload: dict, max_age_minutes: int = 0) -> dict:
    """Whether the last *successful* capture is recent enough.

    This is the dead-man's switch behind ``metrics --check``: a bot whose last
    five runs failed looks fresh to a "did anything arrive?" probe, but not to
    this one. ``max_age_minutes <= 0`` only asks that a success exists at all.
    """
    limit = max(0, int(max_age_minutes or 0))
    age = success_age_seconds(payload)
    if age is None:
        return {
            "ok": False,
            "age_seconds": None,
            "max_age_minutes": limit,
            "detail": "No successful capture has been recorded yet.",
        }
    minutes = max(0, int(age)) // 60
    if limit and age > limit * 60:
        return {
            "ok": False,
            "age_seconds": int(age),
            "max_age_minutes": limit,
            "detail": f"The last successful capture was {minutes} minute(s) ago, "
            f"more than the {limit} minute(s) allowed.",
        }
    return {
        "ok": True,
        "age_seconds": int(age),
        "max_age_minutes": limit,
        "detail": f"The last successful capture was {minutes} minute(s) ago.",
    }


def storage_metrics(storage: dict) -> list[str]:
    """The disk gauges: what the folder costs and how long the caps can last.

    ``capture_bot_days_to_cap`` is -1 when nobody said what the caps are (or
    nothing is growing): a rule can then use ``>= 0`` to mean "a forecast
    exists", instead of firing on a metric that merely looks small.
    """
    if not storage:
        return []
    caps = storage.get("caps") or {}
    days = storage.get("days_to_cap")
    return [
        "# HELP capture_bot_total_bytes Everything the capture folder holds.",
        "# TYPE capture_bot_total_bytes gauge",
        f"capture_bot_total_bytes {int(storage.get('total', 0) or 0)}",
        "# HELP capture_bot_history_bytes Reports plus the SQLite index (the history).",
        "# TYPE capture_bot_history_bytes gauge",
        f"capture_bot_history_bytes {int(storage.get('history', 0) or 0)}",
        "# HELP capture_bot_history_cap_bytes Configured history cap (0 = no cap set).",
        "# TYPE capture_bot_history_cap_bytes gauge",
        f"capture_bot_history_cap_bytes {int(caps.get('history_mb', 0) or 0) * 1024 * 1024}",
        "# HELP capture_bot_screenshots_cap_bytes Configured screenshot cap (0 = none).",
        "# TYPE capture_bot_screenshots_cap_bytes gauge",
        f"capture_bot_screenshots_cap_bytes {int(caps.get('screenshots_mb', 0) or 0) * 1024 * 1024}",
        "# HELP capture_bot_days_to_cap Days of room left under the tightest cap (-1 = unknown).",
        "# TYPE capture_bot_days_to_cap gauge",
        f"capture_bot_days_to_cap {-1 if days is None else round(float(days), 2)}",
        *site_cap_lines(storage.get("site_caps")),
    ]


def site_cap_lines(site_caps: Any) -> list[str]:
    """One ``capture_bot_site_cap_bytes{site=...}`` gauge per per-site budget.

    A per-site budget is the rule most likely to bite first (one chatty host), so
    it deserves its own series: an alert can then fire on the host that is about
    to be trimmed instead of on the folder as a whole. Accepts the parsed mapping
    or the text form (``"news.example.com=500,*=1000"``).
    """
    if not site_caps:
        return []
    from app.core.retention import SiteCapError, parse_site_caps

    try:
        parsed = parse_site_caps(site_caps)
    except SiteCapError:  # pragma: no cover - the CLI validates first
        return []
    lines = ["# HELP capture_bot_site_cap_bytes Screenshot budget of one site (0 = none)."]
    lines.append("# TYPE capture_bot_site_cap_bytes gauge")
    for host, mb in sorted(parsed.items()):
        label = str(host).replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'capture_bot_site_cap_bytes{{site="{label}"}} {int(mb * 1024 * 1024)}')
    return lines


def metrics_text(payload: dict, version: str = "") -> str:
    """The ``/metrics`` body: a status payload as Prometheus text exposition.

    Prometheus scrapes a fixed URL, so ``?expect_max_age_minutes=`` is of no use
    to it - the server-level ``--stale-after`` default is what the health state
    in here reflects. Every metric is a gauge; ages are -1 when there is nothing
    to measure, which is easier to alert on than a missing series.
    """
    health = payload.get("health") or {}
    state = str(health.get("state", "unknown"))
    age = health.get("age_seconds")
    pending = payload.get("pending_alerts") or {}
    oldest_pending = age_seconds(str(pending.get("oldest") or ""), datetime.now())

    lines = [
        "# HELP capture_bot_captures Rows in the capture history.",
        "# TYPE capture_bot_captures gauge",
        f"capture_bot_captures {int(payload.get('captures', 0) or 0)}",
        "# HELP capture_bot_sites Distinct monitored sites in the history.",
        "# TYPE capture_bot_sites gauge",
        f"capture_bot_sites {int(payload.get('sites', 0) or 0)}",
        "# HELP capture_bot_index_bytes Size of the SQLite history index.",
        "# TYPE capture_bot_index_bytes gauge",
        f"capture_bot_index_bytes {int((payload.get('index') or {}).get('bytes', 0) or 0)}",
        "# HELP capture_bot_pending_alerts Alerts held by the quiet-hours queue.",
        "# TYPE capture_bot_pending_alerts gauge",
        f"capture_bot_pending_alerts {int(pending.get('count', 0) or 0)}",
        "# HELP capture_bot_oldest_pending_alert_seconds Age of the oldest queued "
        "alert (-1 when the queue is empty).",
        "# TYPE capture_bot_oldest_pending_alert_seconds gauge",
        f"capture_bot_oldest_pending_alert_seconds "
        f"{oldest_pending if oldest_pending is not None else -1}",
        "# HELP capture_bot_last_capture_age_seconds Seconds since the newest capture "
        "(-1 when the history is empty).",
        "# TYPE capture_bot_last_capture_age_seconds gauge",
        f"capture_bot_last_capture_age_seconds {age if age is not None else -1}",
        "# HELP capture_bot_last_success_timestamp_seconds Unix time of the newest "
        "successful capture (-1 when there is none).",
        "# TYPE capture_bot_last_success_timestamp_seconds gauge",
        f"capture_bot_last_success_timestamp_seconds "
        f"{int((payload.get('last_success') or {}).get('seconds', -1) or -1)}",
        "# HELP capture_bot_health_state 1 for the current health state, 0 otherwise.",
        "# TYPE capture_bot_health_state gauge",
    ]
    lines += [
        f'capture_bot_health_state{{state="{candidate}"}} {1 if state == candidate else 0}'
        for candidate in (OK, EMPTY, STALE)
    ]
    lines += [
        "# HELP capture_bot_pinned_baselines Sites with a pinned baseline.",
        "# TYPE capture_bot_pinned_baselines gauge",
        f"capture_bot_pinned_baselines {int((payload.get('baselines') or {}).get('pinned', 0) or 0)}",
    ]
    lines += storage_metrics(payload.get("storage") or {})
    if version:
        lines += [
            "# HELP capture_bot_build_info Build information as a labelled 1.",
            "# TYPE capture_bot_build_info gauge",
            f'capture_bot_build_info{{version="{version}"}} 1',
        ]
    return "\n".join(lines) + "\n"


def describe(payload: dict) -> str:
    """One-line summary of a payload, for a log line or a console."""
    health_block = payload.get("health") or {}
    state = str(health_block.get("state", ""))
    captures = int(payload.get("captures", 0) or 0)
    pending = int((payload.get("pending_alerts") or {}).get("count", 0) or 0)
    parts = [f"history is {state} ({captures} capture(s))"]
    age = health_block.get("age_seconds")
    if age is not None:
        parts.append(f"newest {age // 60} minute(s) old")
    if pending:
        parts.append(f"{pending} alert(s) queued")
    return ", ".join(parts)
