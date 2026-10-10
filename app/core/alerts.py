"""Change alerts: fire a webhook and/or email when pages change (Qt-free).

Only *changed* pages (from visual change detection) trigger an alert. Both
channels are best-effort: failures are captured, never raised, so an alerting
problem can't break a capture run.
"""

from __future__ import annotations

import json
import re
import smtplib
import urllib.request
from collections.abc import Sequence
from email.message import EmailMessage
from typing import Any

WEBHOOK_KINDS = ("generic", "slack", "teams")


def _items_text(changed: Sequence[dict]) -> str:
    """A short human-readable bullet list of the changed pages."""
    lines = []
    for item in changed:
        label = item.get("label") or item.get("url") or "?"
        diff = float(item.get("diff") or 0.0)
        lines.append(f"- {label}: {diff:.0%}")
    return "\n".join(lines)


def build_payload(
    changed: Sequence[dict], run_id: str = "", kind: str = "generic"
) -> dict[str, Any]:
    """Assemble the JSON body describing what changed.

    ``kind`` picks the shape the receiving service expects: our own ``generic``
    schema, a Slack incoming webhook (``{"text": ...}``) or a Teams connector
    message card.
    """
    kind = (kind or "generic").strip().lower()
    if kind == "slack":
        text = f"*Capture Bot*: {len(changed)} page(s) changed\n{_items_text(changed)}"
        return {"text": text}
    if kind == "teams":
        return {
            "@type": "MessageCard",
            "@context": "https://schema.management.azure.com/schemas/2016/06/17/messagecard.json",
            "summary": f"{len(changed)} page(s) changed",
            "title": "Capture Bot: visual changes",
            "text": _items_text(changed),
        }
    return {
        "event": "visual_change",
        "run_id": run_id,
        "count": len(changed),
        "items": list(changed),
    }


def format_email_body(changed: Sequence[dict]) -> str:
    lines = [
        f"{item.get('label', item.get('url', '?'))}: {item.get('diff', 0):.3f}" for item in changed
    ]
    return "Changed pages:\n" + "\n".join(lines)


def parse_mute_list(text: str) -> list[str]:
    """Split a comma/space separated mute list into lowercase fragments."""
    return [part.lower() for part in re.split(r"[,\s]+", str(text or "").strip()) if part]


def is_muted(item: dict, mutes: Sequence[str]) -> bool:
    """True when the item's URL or label contains one of the muted fragments."""
    haystack = f"{item.get('url', '')} {item.get('label', '')}".lower()
    return any(mute in haystack for mute in mutes)


def split_muted(changed: Sequence[dict], text: str) -> tuple[list[dict], list[dict]]:
    """``(kept, muted)`` for one mute list; an empty list mutes nothing."""
    mutes = parse_mute_list(text)
    if not mutes:
        return list(changed), []
    kept = [item for item in changed if not is_muted(item, mutes)]
    muted = [item for item in changed if is_muted(item, mutes)]
    return kept, muted


#: Route lists are ``fragment=https://target`` entries separated by semicolons.
_ROUTE_SPLIT = re.compile(r"[;\n]+")


def split_routes(text: str) -> list[str]:
    """The individual ``fragment=url`` entries of a route list, in order."""
    return [part.strip() for part in _ROUTE_SPLIT.split(str(text or "")) if part.strip()]


def route_entry(entry: str) -> tuple[str, str] | None:
    """``(fragment, url)`` for one entry, or ``None`` when it is malformed."""
    fragment, separator, url = str(entry).partition("=")
    fragment, url = fragment.strip().lower(), url.strip()
    if not separator or not fragment or not url.startswith(("http://", "https://")):
        return None
    return fragment, url


def invalid_routes(text: str) -> list[str]:
    """The entries that are not ``fragment=http(s)://url`` (empty when all are fine)."""
    return [entry for entry in split_routes(text) if route_entry(entry) is None]


def valid_routes(text: str) -> tuple[tuple[str, str], ...]:
    """The usable ``(fragment, url)`` rules; malformed entries are ignored.

    Callers that dispatch alerts use this one: a typo in a settings file must
    never stop a change from being reported.
    """
    return tuple(entry for entry in (route_entry(part) for part in split_routes(text)) if entry)


def parse_routes(text: str) -> tuple[tuple[str, str], ...]:
    """Compile a route list, raising ``ValueError`` on the first bad entry.

    ``"staging.example.com=https://hooks/x; *=https://hooks/all"`` sends changes
    on the staging host to one endpoint and everything else to another. ``*``
    matches every URL, so it works as the catch-all rule; without any rule the
    built-in ``alert_webhook_url`` is used. This is the validating twin of
    :func:`valid_routes`, used by the settings form and the CLI.
    """
    broken = invalid_routes(text)
    if broken:
        raise ValueError(f"entry {broken[0]!r} is not fragment=http(s)://url")
    return valid_routes(text)


def route_for(url: str, routes: Sequence[tuple[str, str]]) -> str:
    """The webhook a change to ``url`` should reach ('' when no rule matches)."""
    haystack = str(url or "").lower()
    for fragment, target in routes:
        if fragment == "*" or fragment in haystack:
            return target
    return ""


def webhook_targets(settings: Any, changed: Sequence[dict]) -> list[tuple[str, list[dict]]]:
    """Group ``changed`` items by the webhook each one should be posted to.

    The first matching rule wins; an item nobody claims falls back to
    ``alert_webhook_url``. Endpoints nobody needs are dropped, so a route list
    without a catch-all can leave some changes un-sent to webhooks on purpose.
    """
    routes = valid_routes(getattr(settings, "alert_route_urls", "") or "")
    default = getattr(settings, "alert_webhook_url", "") or ""
    groups: dict[str, list[dict]] = {}
    for item in changed:
        target = route_for(item.get("url", ""), routes) or default
        if target:
            groups.setdefault(target, []).append(item)
    return list(groups.items())


def send_webhook(url: str, payload: dict, timeout: float = 10.0) -> bool:
    """POST the payload as JSON; True on a 2xx response."""
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": "FullPageCaptureBot"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (caller-supplied URL)
        return 200 <= response.status < 300


# (filename, data, subtype) - the main type is always "text" for our payloads.
Attachment = tuple[str, bytes, str]


def send_email(
    host: str,
    port: int,
    user: str,
    password: str,
    to: str,
    subject: str,
    body: str,
    timeout: float = 10.0,
    attachments: Sequence[Attachment] | None = None,
) -> bool:
    """Send a plain-text email via SMTP, optionally with attachments.

    Each attachment is ``(filename, data, subtype)``, e.g. ``("dashboard.html",
    html_bytes, "html")``. Returns True on success.
    """
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = user or "fullpagecapture@localhost"
    message["To"] = to
    message.set_content(body)
    for filename, data, subtype in attachments or ():
        maintype = "text" if subtype in ("html", "csv", "plain") else "application"
        message.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    with smtplib.SMTP(host, port, timeout=timeout) as server:
        if user:
            server.login(user, password or "")
        server.send_message(message)
    return True


def _password(settings: Any) -> str:
    """The SMTP password: the settings value, else the OS secret store.

    The desktop app keeps the password in the platform keychain instead of
    ``QSettings``; a CLI flag or a profile still wins, so a one-off send from a
    script never has to touch the store.
    """
    from app.core import secrets

    return secrets.smtp_password(settings)


def build_note_payload(title: str, body: str, kind: str = "generic") -> dict[str, Any]:
    """A one-off message (no change list) in the shape the webhook expects.

    Change alerts describe pages; a note describes the bot itself ("the history
    cap just dropped three runs"). Slack and Teams get the same kinds of bodies
    they already receive, so no new webhook configuration is needed.
    """
    kind = (kind or "generic").strip().lower()
    if kind == "slack":
        return {"text": f"*Capture Bot*: {title}\n{body}"}
    if kind == "teams":
        return {
            "@type": "MessageCard",
            "@context": "https://schema.management.azure.com/schemas/2016/06/17/messagecard.json",
            "summary": title,
            "title": f"Capture Bot: {title}",
            "text": body,
        }
    return {"event": "notice", "title": title, "body": body}


def notify_note(settings: Any, title: str, body: str) -> list[tuple[str, bool]]:
    """Send a one-off note through the alert channels. Returns [(channel, ok), ...].

    Used for operational warnings (a retention cap eating history); it goes out
    immediately, because holding a storage warning until the morning helps
    nobody. Like :func:`notify`, it never raises.
    """
    results: list[tuple[str, bool]] = []
    payload = build_note_payload(title, body, getattr(settings, "alert_webhook_kind", "generic"))

    webhook_url = getattr(settings, "alert_webhook_url", "") or ""
    if webhook_url:
        try:
            results.append(("webhook", send_webhook(webhook_url, payload)))
        except Exception:  # noqa: BLE001 - alerting must never break a run
            results.append(("webhook", False))

    email_to = getattr(settings, "alert_email_to", "") or ""
    smtp_host = getattr(settings, "smtp_host", "") or ""
    if email_to and smtp_host:
        try:
            ok = send_email(
                smtp_host,
                int(getattr(settings, "smtp_port", 587)),
                getattr(settings, "smtp_user", ""),
                _password(settings),
                email_to,
                f"[Capture Bot] {title}",
                body,
            )
            results.append(("email", ok))
        except Exception:  # noqa: BLE001
            results.append(("email", False))

    return results


def notify(settings: Any, changed: Sequence[dict], run_id: str = "") -> list[tuple[str, bool]]:
    """Dispatch alerts for ``changed`` items. Returns [(channel, ok), ...].

    Webhooks are sent per route (``alert_route_urls``), email stays one message
    for the whole batch - a mail client already threads by subject, and splitting
    it would mean one SMTP connection per host. Never raises: each channel is
    independently guarded.

    With ``alert_channels`` pointing at a TOML file the whole dispatch comes from
    there instead - named channels carry their own destination, match list,
    quiet window, mutes and threshold, so the file replaces the four settings
    strings rather than adding to them.
    """
    results: list[tuple[str, bool]] = []
    if not changed:
        return results

    channels_file = str(getattr(settings, "alert_channels", "") or "").strip()
    if channels_file:
        from app.core import channels as channels_module

        return channels_module.notify(settings, changed, run_id)

    kind = getattr(settings, "alert_webhook_kind", "generic")
    groups = webhook_targets(settings, changed)
    for target, items in groups:
        # One POST per endpoint: the Slack channel that watches the staging host
        # should not receive the newsletter's changes.
        payload = build_payload(items, run_id, kind)
        label = "webhook" if len(groups) == 1 else f"webhook[{target}]"
        try:
            results.append((label, send_webhook(target, payload)))
        except Exception:  # noqa: BLE001 - alerting must never break a run
            results.append((label, False))

    email_to = getattr(settings, "alert_email_to", "") or ""
    smtp_host = getattr(settings, "smtp_host", "") or ""
    if email_to and smtp_host:
        try:
            ok = send_email(
                smtp_host,
                int(getattr(settings, "smtp_port", 587)),
                getattr(settings, "smtp_user", ""),
                _password(settings),
                email_to,
                f"[Capture Bot] {len(changed)} page(s) changed",
                format_email_body(changed),
            )
            results.append(("email", ok))
        except Exception:  # noqa: BLE001
            results.append(("email", False))

    return results
