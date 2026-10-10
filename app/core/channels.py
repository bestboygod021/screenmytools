"""Named alert channels: routing, quiet hours, mutes and thresholds in one file.

Webhook routing, quiet windows, mute lists and a change threshold each started as
their own setting string. Four comma-separated lists is a lot to get right once
a bot watches more than a handful of sites, so this module is the readable
version: one TOML file, one block per destination.

    # channels.toml
    [ops]
    kind = "webhook"
    url = "https://hooks.example.com/ops"
    match = "staging.example.com, shop.example.com"     # default: everything
    quiet = "22:00-07:00, fri18:00-mon09:00"            # optional
    mute = "newsletter, preview"                        # optional
    min_diff = 0.02                                     # optional

    [team]
    kind = "email"
    to = "ops@example.com"
    quiet = "22:00-07:00"

    [pager]
    kind = "command"                                   # anything curl cannot reach
    exec = ["/usr/local/bin/page-oncall", "--team", "ops"]
    timeout = 30                                       # optional, seconds
    heartbeat = "mon 09:00"                            # optional proof of life

A change goes to *every* channel that matches it - that is the point of naming
them - minus the channels that muted it or set a threshold above its change.
``alert_route_urls``/``alert_quiet_urls``/``alert_mute_urls`` still work and are
what a single-channel setup keeps using; a file is for the case where they no
longer fit in the user's head.

A channel whose quiet window is open does not drop its alerts: they join the same
pending queue the global quiet hours use, tagged with the channel's name, and are
delivered when its window closes.

``heartbeat = "mon 09:00"`` (or ``"09:00"`` for a daily pulse) makes the bot speak
even when nothing changed: if the moment comes round and the channel has heard
nothing since the previous one - no change alert, no test send - it sends a short
"still here" note with the week's numbers. A silent channel then means a broken
channel, not a quiet week.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import tomllib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app.core import alerts, quiet

#: The transports a channel can name. ``command`` runs a program of your own
#: (PagerDuty CLI, an SMS gateway, a shell script) with the payload on stdin.
KINDS = ("webhook", "email", "command")

#: How long a command channel may run before it counts as failed (seconds).
DEFAULT_COMMAND_TIMEOUT = 30.0
MAX_COMMAND_TIMEOUT = 600.0

#: State file (in the output folder) remembering when each channel last spoke.
BEAT_STATE_FILENAME = ".channel-heartbeats.json"

#: Weekday names accepted in a heartbeat schedule.
_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

#: Keys a channel table may hold, mapped to their defaults.
_FIELDS = (
    "kind",
    "url",
    "to",
    "exec",
    "timeout",
    "match",
    "mute",
    "quiet",
    "min_diff",
    "enabled",
    "heartbeat",
)


class ChannelError(ValueError):
    """A channels file that cannot be used, with a message worth reading."""


@dataclass(frozen=True)
class Heartbeat:
    """A weekly (or daily) "still here" moment.

    ``weekday`` is ``None`` for a daily beat; otherwise Monday is 0, like
    :meth:`datetime.datetime.weekday`. The bot sends a heartbeat when the moment
    comes round and the channel has not heard anything since the previous one.
    """

    hour: int
    minute: int
    weekday: int | None = None

    @property
    def text(self) -> str:
        """``"mon 09:00"`` (or ``"09:00"`` when it is daily)."""
        clock = f"{self.hour:02d}:{self.minute:02d}"
        return clock if self.weekday is None else f"{_WEEKDAYS[self.weekday]} {clock}"

    def previous(self, moment: datetime) -> datetime:
        """The most recent scheduled moment at or before ``moment``."""
        today = moment.replace(hour=self.hour, minute=self.minute, second=0, microsecond=0)
        if self.weekday is None:
            return today if today <= moment else today - timedelta(days=1)
        days = (moment.weekday() - self.weekday) % 7
        candidate = today - timedelta(days=days)
        if candidate > moment:
            candidate -= timedelta(days=7)
        return candidate

    def next_after(self, moment: datetime) -> datetime:
        """The next scheduled moment strictly after ``moment``."""
        if self.weekday is None:
            today = moment.replace(hour=self.hour, minute=self.minute, second=0, microsecond=0)
            return today if today > moment else today + timedelta(days=1)
        days = (self.weekday - moment.weekday()) % 7
        candidate = moment.replace(
            hour=self.hour, minute=self.minute, second=0, microsecond=0
        ) + timedelta(days=days)
        if candidate <= moment:
            candidate += timedelta(days=7)
        return candidate


@dataclass(frozen=True)
class Channel:
    """One destination, with the rules that decide what it hears about."""

    name: str
    kind: str = "webhook"
    url: str = ""
    to: str = ""
    #: URL/label fragments this channel wants (empty = everything).
    matches: tuple[str, ...] = ()
    quiet: str = ""
    mutes: tuple[str, ...] = ()
    min_diff: float | None = None
    enabled: bool = True
    #: Optional per-channel SMTP overrides for ``kind = "email"``.
    smtp: dict[str, Any] = field(default_factory=dict)
    #: ``kind = "command"``: the argv to run (never a shell string).
    command: tuple[str, ...] = ()
    timeout: float = DEFAULT_COMMAND_TIMEOUT
    #: Optional proof-of-life schedule: ``"mon 09:00"`` or ``"09:00"``.
    heartbeat: Heartbeat | None = None

    def target(self) -> str:
        """Where it sends: a webhook URL, an address, or the command itself."""
        if self.kind == "webhook":
            return self.url
        if self.kind == "email":
            return self.to
        return " ".join(self.command)

    def to_dict(self) -> dict[str, Any]:
        """Plain data (``channels --json``)."""
        return {
            "name": self.name,
            "kind": self.kind,
            "target": self.target(),
            "match": list(self.matches) or ["*"],
            "quiet": self.quiet,
            "mute": list(self.mutes),
            "min_diff": self.min_diff,
            "enabled": self.enabled,
            **({"exec": list(self.command), "timeout": self.timeout} if self.command else {}),
            "heartbeat": self.heartbeat.text if self.heartbeat else "",
        }

    def describe(self) -> str:
        """One aligned line for ``channels --file ...``."""
        rules = [f"match: {', '.join(self.matches) or '*'}"]
        if self.quiet:
            rules.append(f"quiet: {self.quiet}")
        if self.mutes:
            rules.append(f"mute: {', '.join(self.mutes)}")
        if self.min_diff is not None:
            rules.append(f"min diff: {self.min_diff:g}")
        if self.kind == "command":
            rules.append(f"timeout: {self.timeout:g}s")
        if self.heartbeat is not None:
            rules.append(f"heartbeat: {self.heartbeat.text}")
        if not self.enabled:
            rules.append("disabled")
        return f"  {self.name:<12} {self.kind:<8} {self.target():<40} {'; '.join(rules)}"


def _as_fragments(value: Any, name: str, key: str) -> tuple[str, ...]:
    """``"a, b"`` or ``["a", "b"]`` -> ``("a", "b")``; ``*`` means "everything".

    A fragment is a substring of the URL (or of the site label), so ``staging``
    is enough. A leading/trailing ``*`` is dropped - ``*.shop.example.com`` reads
    like a glob but means the same as ``shop.example.com`` here, and silently
    matching nothing would be a cruel joke.
    """
    if value is None:
        return ()
    items: Iterable[Any]
    if isinstance(value, str):
        items = value.replace(";", ",").split(",")
    elif isinstance(value, (list, tuple)):
        items = value
    else:
        raise ChannelError(f"Channel '{name}': {key} must be text or a list of texts.")
    cleaned = []
    for item in items:
        fragment = str(item).strip().lower().strip("*").strip(".")
        if not fragment:
            return ()  # a bare "*" (or an empty entry) means everything
        cleaned.append(fragment)
    return tuple(cleaned)


def _as_text(value: Any, name: str, key: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ChannelError(f"Channel '{name}': {key} must be text.")
    return str(value).strip()


def _as_bool(value: Any, name: str, key: str) -> bool:
    if isinstance(value, bool):
        return value
    raise ChannelError(f"Channel '{name}': {key} must be true or false.")


def _as_command(value: Any, name: str) -> tuple[str, ...]:
    """``exec = "prog --flag"`` or ``exec = ["prog", "--flag"]`` -> an argv tuple.

    The string form is split with :mod:`shlex` and is *never* handed to a shell:
    a channels file is user-written config, but "my config quotes itself safely"
    is exactly the assumption a payload would exploit.
    """
    if value is None:
        return ()
    if isinstance(value, str):
        parts = shlex.split(value)
    elif isinstance(value, (list, tuple)):
        parts = [str(part) for part in value]
    else:
        raise ChannelError(f"Channel '{name}': exec must be text or a list of texts.")
    cleaned = tuple(part for part in (piece.strip() for piece in parts) if part)
    if not cleaned:
        raise ChannelError(f"Channel '{name}': exec is empty.")
    return cleaned


def parse_heartbeat(value: Any, name: str = "") -> Heartbeat | None:
    """``"mon 09:00"`` / ``"monday 09:00"`` / ``"09:00"`` -> :class:`Heartbeat`.

    Raises :class:`ChannelError` naming the offending text, so a typo in a
    schedule is reported instead of quietly never firing.
    """
    where = f"Channel '{name}': " if name else ""
    if value is None or not str(value).strip():
        return None
    if not isinstance(value, str):
        raise ChannelError(f'{where}heartbeat must be text like "mon 09:00".')
    parts = str(value).replace(",", " ").split()
    if len(parts) > 2:
        raise ChannelError(f"{where}heartbeat = '{value}' has too many words; use \"mon 09:00\".")
    weekday: int | None = None
    if len(parts) == 2:
        wanted = parts[0].strip().lower()[:3]
        if wanted not in _WEEKDAYS:
            raise ChannelError(
                f"{where}heartbeat = '{value}': '{parts[0]}' is not a weekday "
                f"({', '.join(_WEEKDAYS)})."
            )
        weekday = _WEEKDAYS.index(wanted)
    clock = parts[-1]
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", clock)
    if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
        raise ChannelError(f"{where}heartbeat = '{value}' needs a time like 09:00.")
    return Heartbeat(hour=int(match.group(1)), minute=int(match.group(2)), weekday=weekday)


def _as_timeout(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ChannelError(f"Channel '{name}': timeout must be a number of seconds.")
    number = float(value)
    if not 0 < number <= MAX_COMMAND_TIMEOUT:
        raise ChannelError(
            f"Channel '{name}': timeout must be between 0 and {MAX_COMMAND_TIMEOUT:g} seconds."
        )
    return number


def _as_diff(value: Any, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ChannelError(f"Channel '{name}': min_diff must be a number between 0 and 1.")
    try:
        number = float(value)
    except ValueError as exc:
        raise ChannelError(
            f"Channel '{name}': min_diff must be a number between 0 and 1 (got {value!r})."
        ) from exc
    if not 0.0 <= number <= 1.0:
        raise ChannelError(f"Channel '{name}': min_diff must be between 0 and 1 (got {number}).")
    return number


def _channel_from(name: str, table: Any) -> Channel:
    """One ``[name]`` table, validated."""
    if not isinstance(table, dict):
        raise ChannelError(f"'{name}' must be a table of settings ([{name}] ...).")
    unknown = sorted(
        set(table) - set(_FIELDS) - {"smtp_host", "smtp_port", "smtp_user", "smtp_password"}
    )
    if unknown:
        raise ChannelError(
            f"Channel '{name}' has unknown key(s): {', '.join(unknown)}. Use: {', '.join(_FIELDS)}."
        )

    kind = str(table.get("kind", "webhook")).strip().lower()
    if kind not in KINDS:
        raise ChannelError(f"Channel '{name}' has kind '{kind}'; use one of: {', '.join(KINDS)}.")

    matches = _as_fragments(table.get("match"), name, "match")
    mutes = _as_fragments(table.get("mute"), name, "mute")
    windows = _as_text(table.get("quiet", ""), name, "quiet")
    if windows:
        broken = quiet.invalid_windows(windows)
        if broken:
            raise ChannelError(
                f"Channel '{name}' has quiet = '{broken[0]}'; use windows like 22:00-07:00 "
                "or fri18:00-mon09:00."
            )

    smtp = {
        key: str(table[key]).strip()
        for key in ("smtp_host", "smtp_user", "smtp_password")
        if str(table.get(key, "")).strip()
    }
    if str(table.get("smtp_port", "")).strip():
        try:
            smtp["smtp_port"] = int(table["smtp_port"])
        except (TypeError, ValueError) as exc:
            raise ChannelError(
                f"Channel '{name}': smtp_port must be a whole number (got {table['smtp_port']!r})."
            ) from exc

    channel = Channel(
        name=name,
        kind=kind,
        url=_as_text(table.get("url", ""), name, "url"),
        to=_as_text(table.get("to", ""), name, "to"),
        matches=matches,
        quiet=windows,
        mutes=mutes,
        min_diff=_as_diff(table.get("min_diff"), name),
        enabled=_as_bool(table.get("enabled", True), name, "enabled"),
        smtp=smtp,
        command=_as_command(table.get("exec"), name),
        timeout=_as_timeout(table.get("timeout", DEFAULT_COMMAND_TIMEOUT), name),
        heartbeat=parse_heartbeat(table.get("heartbeat"), name),
    )
    if kind == "webhook" and not channel.url:
        raise ChannelError(f"Channel '{name}' needs url = \"https://...\" for kind webhook.")
    if kind == "email" and not channel.to:
        raise ChannelError(f"Channel '{name}' needs to = \"you@example.com\" for kind email.")
    if kind == "command" and not channel.command:
        raise ChannelError(
            f'Channel \'{name}\' needs exec = ["page-oncall", "--team", "ops"] for kind command.'
        )
    if kind != "command" and channel.command:
        raise ChannelError(f"Channel '{name}': exec belongs to kind = \"command\".")
    return channel


def parse_channels(text: str) -> list[Channel]:
    """Parse a channels file's TOML. Raises :class:`ChannelError` with a reason."""
    try:
        data = tomllib.loads(text or "")
    except tomllib.TOMLDecodeError as exc:
        raise ChannelError(f"Could not read the channels file: {exc}") from exc
    if not isinstance(data, dict):
        raise ChannelError("The channels file must hold one table per channel.")
    channels = [_channel_from(str(name), table) for name, table in data.items()]
    if not channels:
        raise ChannelError(
            "There are no channels in this file: add a table per destination, "
            'e.g. [ops] kind = "webhook" url = "https://..."'
        )
    names = [channel.name for channel in channels]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:  # pragma: no cover - TOML cannot repeat a table name
        raise ChannelError(f"Duplicate channel name(s): {', '.join(duplicates)}.")
    return channels


def load_channels(path: str | Path) -> list[Channel]:
    """Read and validate a channels file. Raises :class:`ChannelError`."""
    target = Path(str(path))
    if not str(path).strip():
        raise ChannelError("No channels file was given.")
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise ChannelError(f"Could not read {target}: {exc}") from exc
    try:
        return parse_channels(text)
    except ChannelError as exc:
        raise ChannelError(f"{target}: {exc}") from exc


def summary(channels: Sequence[Channel]) -> str:
    """A small table: one line per channel (``channels --file ...``)."""
    active = [channel for channel in channels if channel.enabled]
    lines = [f"{len(channels)} channel(s) ({len(active)} enabled):"]
    lines += [channel.describe() for channel in channels]
    return "\n".join(lines)


def matches(channel: Channel, item: dict[str, Any]) -> bool:
    """Whether the channel asked for this item (empty match list = everything)."""
    if not channel.matches:
        return True
    url = str(item.get("url", "")).lower()
    label = str(item.get("label", "")).lower()
    return any(fragment in url or fragment in label for fragment in channel.matches)


def wants(channel: Channel, item: dict[str, Any]) -> bool:
    """``matches`` plus the channel's own mutes and change threshold."""
    if not channel.enabled or not matches(channel, item):
        return False
    if channel.mutes and alerts.is_muted(item, channel.mutes):
        return False
    if channel.min_diff is not None and float(item.get("diff") or 0.0) < channel.min_diff:
        return False
    return True


def plan(
    channels: Sequence[Channel], changed: Sequence[dict[str, Any]]
) -> list[tuple[Channel, list[dict[str, Any]]]]:
    """Group the changes per channel: every matching channel gets the item.

    Unlike :func:`app.core.alerts.route_for` (first match wins, one endpoint per
    change), named channels fan out: the ops webhook and the weekly email both
    hear about a staging change if both ask for it.
    """
    groups: list[tuple[Channel, list[dict[str, Any]]]] = []
    for channel in channels:
        items = [item for item in changed if wants(channel, item)]
        if items:
            groups.append((channel, items))
    return groups


def _tagged(item: dict[str, Any]) -> list[str]:
    """The channel names an item was held for (empty when it was never held)."""
    tags = item.get("channels")
    if isinstance(tags, (list, tuple)):
        return [str(tag) for tag in tags]
    return []


def split_window(
    channel: Channel, items: Sequence[dict[str, Any]], moment: datetime | None = None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(due now, held for later)`` for one channel.

    An item that was queued for a *different* channel is simply not this
    channel's business; an item that was queued for this one is held again if the
    window is still open (it stays in the queue) and sent once it closes.
    """
    moment = moment or datetime.now()
    wanted_items = [item for item in items if not _tagged(item) or channel.name in _tagged(item)]
    if not channel.quiet or not quiet.in_window(moment, channel.quiet):
        return wanted_items, []
    return [], wanted_items


def _routing(settings: Any, channel: Channel) -> tuple[str, int, str, str]:
    """``(host, port, user, password)``: the channel's own SMTP, else the settings'.

    The password may come from the OS secret store rather than the settings, so
    a channels file is still safe to keep under version control.
    """
    from app.core import secrets

    return (
        str(channel.smtp.get("smtp_host") or getattr(settings, "smtp_host", "") or ""),
        int(channel.smtp.get("smtp_port") or getattr(settings, "smtp_port", 587)),
        str(channel.smtp.get("smtp_user") or getattr(settings, "smtp_user", "") or ""),
        str(channel.smtp.get("smtp_password") or secrets.smtp_password(settings) or ""),
    )


def _send_email(channel: Channel, settings: Any, subject: str, body: str) -> bool:
    """Send one message through the channel's own SMTP, else the settings'."""
    host, port, user, password = _routing(settings, channel)
    return alerts.send_email(host, port, user, password, channel.to, subject, body)


def send_command(channel: Channel, payload: dict[str, Any], timeout: float | None = None) -> bool:
    """Run a ``kind = "command"`` channel. Returns whether it exited 0.

    The payload arrives as JSON on stdin and the parts of it you would reach for
    first are in the environment (``CAPTURE_BOT_EVENT``, ``CAPTURE_BOT_SUBJECT``,
    ``CAPTURE_BOT_COUNT``, ``CAPTURE_BOT_CHANNEL``), so a one-line script needs no
    JSON parser. The program runs without a shell: ``exec = ["prog", "arg"]`` is
    an argv, not a string something else can re-interpret.
    """
    body = json.dumps(payload, ensure_ascii=False)
    env = dict(os.environ)
    env.update(
        {
            "CAPTURE_BOT_CHANNEL": channel.name,
            "CAPTURE_BOT_EVENT": str(payload.get("event", "")),
            "CAPTURE_BOT_SUBJECT": str(payload.get("title") or payload.get("subject") or ""),
            "CAPTURE_BOT_COUNT": str(payload.get("count", len(payload.get("items", []) or []))),
        }
    )
    try:
        done = subprocess.run(  # noqa: S603 - the user's own argv, never a shell
            list(channel.command),
            input=body,
            capture_output=True,
            text=True,
            timeout=float(timeout or channel.timeout),
            env=env,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"[{channel.name}] command failed to run: {exc}", file=sys.stderr)
        return False
    if done.returncode != 0:
        reason = (done.stderr or done.stdout or "").strip().splitlines()
        print(
            f"[{channel.name}] {channel.command[0]} exited {done.returncode}"
            + (f": {reason[0][:200]}" if reason else "."),
            file=sys.stderr,
        )
        return False
    return True


def deliver(
    channel: Channel,
    settings: Any,
    payload: dict[str, Any],
    changed: Sequence[dict[str, Any]] = (),
) -> bool:
    """Hand one payload to the channel's transport. Raises only on a broken setup.

    Every caller (change alerts, ``channels --test``, the weekly heartbeat) goes
    through here, so a new transport is one branch in one place instead of four.
    """
    if channel.kind == "webhook":
        return alerts.send_webhook(channel.url, payload)
    if channel.kind == "command":
        return send_command(channel, payload)
    subject = str(payload.get("title") or f"[Capture Bot] {len(changed)} page(s) changed")
    body = (
        alerts.format_email_body(changed)
        if changed
        else str(payload.get("body") or payload.get("text") or "")
    )
    return _send_email(channel, settings, subject, body)


def beat_state_path(folder: str | Path) -> Path:
    """Where the "last time we heard from this channel" timestamps live."""
    return Path(folder) / BEAT_STATE_FILENAME


def load_beats(folder: str | Path) -> dict[str, datetime]:
    """``{channel: last spoken}``; unreadable or absent state means "never"."""
    try:
        data = json.loads(beat_state_path(folder).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    beats: dict[str, datetime] = {}
    if isinstance(data, dict):
        for name, stamp in data.items():
            try:
                beats[str(name)] = datetime.fromisoformat(str(stamp))
            except ValueError:
                continue
    return beats


def record_beats(folder: str | Path, names: Sequence[str], moment: datetime | None = None) -> None:
    """Remember that these channels just heard something. Never raises."""
    if not names:
        return
    moment = moment or datetime.now()
    beats = load_beats(folder)
    for name in names:
        beats[str(name)] = moment
    try:
        beat_state_path(folder).write_text(
            json.dumps(
                {
                    name: stamp.isoformat(timespec="seconds")
                    for name, stamp in sorted(beats.items())
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    except OSError:  # pragma: no cover - a read-only folder must not break a run
        pass


def beats_due(
    channels: Sequence[Channel], folder: str | Path, moment: datetime | None = None
) -> list[tuple[Channel, datetime]]:
    """The channels whose heartbeat moment has come round without a word since.

    A channel with no recorded activity is due as soon as the first moment
    passes: the first - sometimes the only - heartbeat is how the owner finds out
    the webhook URL is right.
    """
    moment = moment or datetime.now()
    beats = load_beats(folder)
    due: list[tuple[Channel, datetime]] = []
    for channel in channels:
        if not channel.enabled or channel.heartbeat is None:
            continue
        previous = channel.heartbeat.previous(moment)
        last = beats.get(channel.name)
        if last is None or last < previous:
            due.append((channel, previous))
    return due


def heartbeat_summary(folder: str | Path, days: int = 7, moment: datetime | None = None) -> str:
    """One short paragraph of "what happened lately" for a heartbeat note.

    Counts come from the report files (the source of truth), so a pruned index
    never changes the story, and the storage numbers come from the same helpers
    the dashboard uses.
    """
    from app.core import dashboard, history

    folder = Path(folder)
    moment = moment or datetime.now()
    since = moment - timedelta(days=max(1, int(days)))
    runs = history.load_runs(folder)
    stamps = [
        str(run.get("generated_at", ""))
        for run in runs
        if str(run.get("generated_at", "")) and _parsed(str(run.get("generated_at", ""))) >= since
    ]
    sites: set[str] = set()
    changed = 0
    rows_seen = 0
    for run in runs:
        stamp = str(run.get("generated_at", ""))
        if not stamp or _parsed(stamp) < since:
            continue
        for row in run.get("results") or []:
            rows_seen += 1
            sites.add(str(row.get("label") or row.get("url") or ""))
            diff = row.get("diff")
            if diff is not None and float(diff or 0) > 0:
                changed += 1
    try:
        stats = dashboard.storage_stats(folder)
        disk = f"{dashboard.human_bytes(int(stats.get('total', 0)))} in the folder"
        days_left = stats.get("days_to_cap")
        if isinstance(days_left, (int, float)) and days_left >= 0:
            disk += f", about {days_left:.0f} day(s) of room left"
    except Exception:  # noqa: BLE001 - a summary must never be the thing that breaks
        disk = "the folder could not be measured"
    return (
        f"Still here: {len(stamps)} run(s) and {rows_seen} capture(s) of "
        f"{len(sites)} site(s) in the last {days} day(s), {changed} page(s) changed - "
        f"{disk}."
    )


def _parsed(stamp: str) -> datetime:
    """A report stamp as a datetime; an unreadable one sorts far in the past."""
    try:
        return datetime.fromisoformat(stamp.replace("Z", ""))
    except ValueError:
        return datetime.min


def run_heartbeats(
    settings: Any, moment: datetime | None = None, dry_run: bool = False, days: int = 7
) -> list[tuple[str, bool]]:
    """Send a heartbeat through every channel that is due for one.

    ``dry_run`` reports what *would* go out (``ok`` stays true) without sending
    or recording anything, which is what ``channels --heartbeat --dry-run`` and
    a nervous first setup want.
    """
    folder = str(getattr(settings, "output_dir", "") or ".").strip() or "."
    parsed = load_channels(str(getattr(settings, "alert_channels", "") or "").strip())
    moment = moment or datetime.now()
    due = beats_due(parsed, folder, moment)
    if not due:
        return []
    body = heartbeat_summary(folder, days, moment)
    kind = getattr(settings, "alert_webhook_kind", "generic")
    results: list[tuple[str, bool]] = []
    for channel, scheduled in due:
        label = f"{channel.name}:heartbeat"
        if dry_run:
            results.append((label, True))
            continue
        payload = alerts.build_note_payload("Capture Bot heartbeat", body, kind)
        payload["scheduled"] = scheduled.isoformat(timespec="seconds")
        payload["count"] = 0
        try:
            ok = deliver(channel, settings, payload)
        except Exception:  # noqa: BLE001 - a heartbeat must never break a run
            ok = False
        results.append((label, ok))
        if ok:
            record_beats(folder, [channel.name], moment)
    return results


def test_notify(settings: Any, title: str, body: str) -> list[tuple[str, bool]]:
    """Send a note through every enabled channel (``channels --test``).

    A config that parses is not the same as one that works: this is the button
    that proves the webhook URL and the SMTP login *before* a page changes.
    """
    parsed = load_channels(str(getattr(settings, "alert_channels", "") or "").strip())
    payload = alerts.build_note_payload(
        title, body, getattr(settings, "alert_webhook_kind", "generic")
    )
    folder = str(getattr(settings, "output_dir", "") or ".").strip() or "."
    results: list[tuple[str, bool]] = []
    for channel in parsed:
        if not channel.enabled:
            continue
        try:
            ok = deliver(channel, settings, payload)
        except Exception:  # noqa: BLE001 - a test send reports failure, never raises
            ok = False
        if ok:
            record_beats(folder, [channel.name])
        results.append((f"{channel.name}:{channel.kind}", ok))
    return results


def notify(
    settings: Any,
    changed: Sequence[dict[str, Any]],
    run_id: str = "",
    moment: datetime | None = None,
) -> list[tuple[str, bool]]:
    """Deliver ``changed`` through the channels file. Never raises.

    Returns ``[(label, ok), ...]`` where the label says which channel and which
    transport was used; items held by a quiet window are reported as
    ``"<name>:queued"`` (the engine turns that into a friendlier log line).
    """
    if not changed:
        return []
    path = str(getattr(settings, "alert_channels", "") or "").strip()
    try:
        channels = load_channels(path)
    except ChannelError:
        # A broken file must never silence the bot silently: the caller logs the
        # failed result, exactly like a webhook that refused the payload.
        return [("channels", False)]

    folder = str(getattr(settings, "output_dir", "") or ".").strip() or "."
    queue_file = quiet.queue_path(folder)
    moment = moment or datetime.now()
    default_kind = getattr(settings, "alert_webhook_kind", "generic")
    results: list[tuple[str, bool]] = []

    for channel, items in plan(channels, changed):
        due, held = split_window(channel, items, moment)
        if held:
            quiet.append(
                queue_file,
                [
                    dict(
                        item,
                        channels=[channel.name],
                        queued_at=moment.isoformat(timespec="seconds"),
                    )
                    for item in held
                ],
            )
            results.append((f"{channel.name}:queued", True))
        if not due:
            continue
        payload = alerts.build_payload(due, run_id, default_kind)
        try:
            ok = deliver(channel, settings, payload, due)
        except Exception:  # noqa: BLE001 - alerting must never break a run
            ok = False
        if ok:
            # A real alert is proof of life too: the heartbeat stays quiet until
            # the channel has been silent for a whole period.
            record_beats(folder, [channel.name], moment)
        results.append((f"{channel.name}:{channel.kind}", ok))
    return results
