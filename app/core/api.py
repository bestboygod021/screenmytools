"""A tiny read-only HTTP API over the capture history (stdlib only).

``python -m app.cli serve --dir ./shots`` starts a local server so other tools
can poll the history as JSON instead of parsing report files themselves:

    GET /                -> the same HTML dashboard ``dashboard`` writes
    GET /api/schema      -> a machine-readable description of these endpoints
    GET /api/history     -> every capture row, newest first
    GET /api/export      -> the same rows as a CSV or JSON download
    GET /api/sites       -> the monitored URLs
    GET /api/trend       -> per-site totals + the diff series
    GET /api/digest      -> the digest statistics for a window (?days=7)
    GET /api/status      -> one-shot health: totals, index size, queued alerts,
                            freshness (?expect_max_age_minutes=90)
    GET /api/storage     -> disk usage of the folder + the cap forecasts
                            (?format=csv for a spreadsheet, ?history_mb=50 to
                            forecast the caps without configuring anything,
                            ?series=1 for the recorded size trend)
    GET /metrics         -> the same health as Prometheus text exposition, plus the
                            disk gauges (``--caps`` makes the forecast real)

The server only ever reads files. It binds 0.0.0.0 by default (so containers and
port-forwarded previews work), never raises on a bad request, and can require a
bearer token (``--token``) or allow browser access from other origins
(``--cors``) when you expose it beyond localhost.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from app.core import baseline, history
from app.core import status as status_module
from app.version import __version__

DEFAULT_HOST = "0.0.0.0"  # noqa: S104 - a local dev server must accept forwarded ports
DEFAULT_PORT = 8765
_JSON_HEADERS = {"Content-Type": "application/json; charset=utf-8"}

#: Query values that mean "yes" (``?series=1``, ``?series=true``, ...).
_TRUTHY = ("1", "true", "yes", "on")


def _truthy(values: list[str] | None) -> bool:
    """Whether a query parameter was set to something that reads as "yes"."""
    if not values:
        return False
    return str(values[0] or "").strip().lower() in _TRUTHY


API_SCHEMA: dict = {
    "name": "Capture Bot history API",
    "version": 1,
    "auth": "optional bearer token (Authorization: Bearer <token>)",
    "endpoints": [
        {"path": "/", "returns": "text/html", "description": "The trend dashboard."},
        {
            "path": "/api/schema",
            "returns": "application/json",
            "description": "This document.",
        },
        {
            "path": "/api/history",
            "returns": "application/json",
            "description": "Capture rows, newest first, wrapped in a page envelope.",
            "fields": ["timestamp", "url", "label", "status", "diff", "drift", "file"],
            "query": {
                "limit": "rows to return (0 = all)",
                "offset": "rows to skip (default 0)",
                "url": "only rows whose URL/label contains this text",
            },
            "envelope": ["total", "count", "limit", "offset", "rows"],
        },
        {
            "path": "/api/export",
            "returns": "text/csv or application/json",
            "description": "Download the (optionally filtered) history as a file.",
            "query": {
                "format": "csv or json (default json)",
                "url": "only rows whose URL/label contains this text",
                "limit": "rows to return (0 = all)",
                "offset": "rows to skip (default 0)",
            },
            "headers": [
                "ETag",
                "If-None-Match (an unchanged history answers 304 Not Modified)",
            ],
        },
        {
            "path": "/api/sites",
            "returns": "application/json",
            "description": "The monitored URLs.",
        },
        {
            "path": "/api/trend",
            "returns": "application/json",
            "description": "Per-site totals plus the diff series.",
            "fields": [
                "url",
                "captures",
                "changes",
                "baseline",
                "drift",
                "series",
                "drift_series",
            ],
        },
        {
            "path": "/api/digest",
            "returns": "application/json",
            "description": "Digest statistics for a window.",
            "query": {"days": "window size in days (default 7)"},
        },
        {
            "path": "/api/storage",
            "returns": "application/json (or text/csv with ?format=csv)",
            "description": "Disk usage of the folder: totals, growth per day and the "
            "days left under each configured cap.",
            "query": {
                "format": "json (default) or csv",
                "history_mb": "cap to forecast against (MB)",
                "screenshots_mb": "cap to forecast against (MB)",
                "series": "1 to include the recorded samples and their daily growth",
                "days": "with series=1: keep only the samples of the last N days",
                "limit": "with series=1: keep only the newest N samples",
            },
            "fields": [
                "generated_at",
                "total",
                "screenshots",
                "history",
                "index",
                "reports",
                "oldest",
                "caps",
                "archives",
                "growth",
                "forecasts",
                "days_to_cap",
                "sites",
                "series",
                "growth_by_day",
            ],
        },
        {
            "path": "/metrics",
            "returns": "text/plain; version=0.0.4",
            "description": "Prometheus exposition of the health probe and the disk "
            "gauges (capture_bot_days_to_cap is -1 without --caps).",
            "headers": ["the same bearer token / rate limit as the other routes"],
        },
        {
            "path": "/api/status",
            "returns": "application/json",
            "description": "One-shot health: totals, last capture, index size and the alert queue.",
            "fields": [
                "generated_at",
                "captures",
                "sites",
                "health",
                "last_capture",
                "index",
                "pending_alerts",
                "baselines",
                "quiet",
            ],
            "query": {
                "quiet_hours": "optional window (e.g. 22:00-07:00) to report the quiet state",
                "expect_max_age_minutes": (
                    "report health=stale when the newest capture is older than this"
                ),
            },
        },
    ],
}


def _site_payload(output_dir: str | Path) -> list[dict]:
    """Per-site totals plus the diff-over-time series."""
    counts = history.site_change_counts(output_dir)
    captures: dict[str, int] = {}
    for row in history.flat_rows(output_dir):
        captures[row["url"]] = captures.get(row["url"], 0) + 1

    payload = []
    for url in history.sites(output_dir):
        series = history.trend_for_url(output_dir, url)
        payload.append(
            {
                "url": url,
                "captures": captures.get(url, 0),
                "changes": counts.get(url, 0),
                "baseline": baseline.has_baseline(output_dir, url),
                "drift": baseline.baseline_drift(output_dir, url),
                "series": [point["diff"] for point in series],
                "drift_series": history.drift_for_url(output_dir, url),
            }
        )
    return payload


def history_page(output_dir: str | Path, limit: int = 0, offset: int = 0, url: str = "") -> dict:
    """A page of capture rows plus enough metadata to walk the whole list.

    ``limit <= 0`` means "everything from ``offset`` on", so a plain request
    without parameters returns the full history exactly as before, only wrapped
    in the envelope. ``url`` narrows the rows down to one site (substring,
    case-insensitive) and is applied before slicing, so ``total`` counts the
    matches only.
    """
    rows = history.filter_rows(history.flat_rows(output_dir), url)
    start = max(0, int(offset or 0))
    page = rows[start:]
    if limit and limit > 0:
        page = page[: int(limit)]
    return {
        "total": len(rows),
        "count": len(page),
        "limit": int(limit or 0),
        "offset": start,
        "rows": page,
    }


#: Formats ``/api/export`` understands.
EXPORT_FORMATS = ("csv", "json")


def format_export(page: dict, fmt: str = "json") -> tuple[bytes, str, str]:
    """A page envelope as a downloadable file: ``(body, content type, filename)``.

    Split out of :func:`export_payload` so the CLI can build the same bytes from
    a single page it already loaded (and report how many rows it wrote).
    """
    if fmt == "csv":
        return history.rows_to_csv(page.get("rows") or []), "text/csv; charset=utf-8", "history.csv"
    body = json.dumps(page, indent=2, ensure_ascii=False, default=str).encode("utf-8")
    return body, _JSON_HEADERS["Content-Type"], "history.json"


def etag_for(body: bytes) -> str:
    """A strong ETag for a download body (quoted, so it can be sent as-is)."""
    return '"' + hashlib.sha256(body).hexdigest()[:32] + '"'


def export_payload(
    output_dir: str | Path,
    fmt: str = "json",
    url: str = "",
    limit: int = 0,
    offset: int = 0,
) -> tuple[bytes, str, str]:
    """A downloadable history file: ``(body, content type, filename)``."""
    return format_export(history_page(output_dir, limit, offset, url), fmt)


def status_payload(
    output_dir: str | Path,
    quiet_hours: str = "",
    expect_max_age_minutes: int = 0,
    storage: dict | None = None,
) -> dict:
    """The body of ``/api/status`` (see :func:`app.core.status.status_payload`)."""
    return status_module.status_payload(
        output_dir, quiet_hours, expect_max_age_minutes, storage=storage
    )


def _digest_payload(output_dir: str | Path, days: int) -> dict:
    from app.core import digest

    return digest.collect_stats(output_dir, days)


class RateLimiter:
    """A per-client token bucket (requests per minute, 0 = unlimited).

    Reads are cheap but a runaway browser tab is not, so the API can cap how
    often one client may talk to it. The bucket refills continuously and the
    caller is told how long to wait when it is empty.
    """

    def __init__(self, per_minute: int = 0) -> None:
        self.per_minute = max(0, int(per_minute or 0))
        self._lock = threading.Lock()
        self._buckets: dict[str, tuple[float, float]] = {}  # client -> (tokens, stamp)

    def check(self, client: str, now: float | None = None) -> tuple[bool, float]:
        """Return ``(allowed, retry_after_seconds)`` for one request."""
        if self.per_minute <= 0:
            return True, 0.0
        moment = time.monotonic() if now is None else now
        rate = self.per_minute / 60.0
        with self._lock:
            tokens, stamp = self._buckets.get(client, (float(self.per_minute), moment))
            tokens = min(float(self.per_minute), tokens + max(0.0, moment - stamp) * rate)
            if tokens < 1.0:
                self._buckets[client] = (tokens, moment)
                return False, max(0.0, (1.0 - tokens) / rate)
            self._buckets[client] = (tokens - 1.0, moment)
            return True, 0.0


class HistoryHandler(BaseHTTPRequestHandler):
    """Routes the read-only endpoints. Config lives on the server object."""

    server_version = "CaptureBotHistory/1.0"

    # -- helpers -----------------------------------------------------------
    def _extra_headers(self) -> dict[str, str]:
        if not getattr(self.server, "cors", False):  # type: ignore[attr-defined]
            return {}
        return {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
            "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
        }

    def _respond(
        self,
        body: bytes,
        content_type: str,
        status: int = 200,
        extra: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        for key, value in self._extra_headers().items():
            self.send_header(key, value)
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(
        self, payload: object, status: int = 200, extra: dict[str, str] | None = None
    ) -> None:
        body = json.dumps(payload, indent=2, ensure_ascii=False, default=str).encode("utf-8")
        headers = dict(_JSON_HEADERS)
        headers.update(extra or {})
        self._respond(body, headers.pop("Content-Type"), status, headers)

    def _authorized(self) -> bool:
        token = getattr(self.server, "token", "")  # type: ignore[attr-defined]
        if not token:
            return True
        header = self.headers.get("Authorization", "")
        prefix = "Bearer "
        if not header.startswith(prefix):
            return False
        return hmac.compare_digest(header[len(prefix) :], token)

    # -- routes ------------------------------------------------------------
    def _not_modified(self, etag: str) -> bool:
        """True when the client already holds this exact body (sent as 304)."""
        asked = self.headers.get("If-None-Match", "")
        if not asked:
            return False
        candidates = [part.strip() for part in asked.split(",")]
        if etag not in candidates and "*" not in candidates:
            return False
        self.send_response(304)
        for key, value in self._extra_headers().items():
            self.send_header(key, value)
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        return True

    def _int_param(self, query: dict, key: str) -> int | None:
        """A non-negative integer from the query (0 when absent), or None after a 400."""
        raw = str(query.get(key, [""])[0] or "").strip()
        if not raw:
            return 0
        try:
            value = int(raw)
        except ValueError:
            self._send_json(
                {"error": "bad request", "message": f"{key} expects a whole number, got '{raw}'."},
                status=400,
            )
            return None
        if value < 0:
            self._send_json(
                {"error": "bad request", "message": f"{key} cannot be negative."}, status=400
            )
            return None
        return value

    def _mb_param(self, query: dict, key: str) -> int | None:
        """``key`` from the query as whole MB (0 when absent), or None after a 400."""
        raw = str(query.get(key, [""])[0] or "").strip()
        if not raw:
            return 0
        try:
            value = int(float(raw))
        except ValueError:
            self._send_json(
                {
                    "error": "bad request",
                    "message": f"{key} expects a number of MB, got '{raw}'.",
                },
                status=400,
            )
            return None
        if value < 0:
            self._send_json(
                {"error": "bad request", "message": f"{key} cannot be negative."},
                status=400,
            )
            return None
        return value

    def _stale_after(self, query: dict) -> int | None:
        """``expect_max_age_minutes`` from the query, or None after a 400."""
        raw = query.get("expect_max_age_minutes")
        if not raw:
            return 0
        try:
            minutes = int(str(raw[0]).strip())
        except (TypeError, ValueError):
            minutes = -1
        if minutes < 0:
            self._send_json(
                {
                    "error": "bad request",
                    "message": "expect_max_age_minutes must be a non-negative integer.",
                },
                status=400,
            )
            return None
        return minutes

    def _page_args(self, query: dict) -> tuple[int, int, str] | None:
        """``(limit, offset, url)`` from the query string, or None after a 400."""
        try:
            limit = int(query.get("limit", ["0"])[0])
            offset = int(query.get("offset", ["0"])[0])
        except (TypeError, ValueError):
            self._send_json(
                {"error": "bad request", "message": "limit and offset must be integers."},
                status=400,
            )
            return None
        if limit < 0 or offset < 0:
            self._send_json(
                {"error": "bad request", "message": "limit and offset cannot be negative."},
                status=400,
            )
            return None
        url = str(query.get("url", [""])[0] or "").strip()
        return limit, offset, url

    def do_OPTIONS(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if not getattr(self.server, "cors", False):  # type: ignore[attr-defined]
            self._send_json({"error": "cors disabled"}, status=405)
            return
        self._respond(b"", "text/plain; charset=utf-8", status=204)

    def _public(self, route: str) -> bool:
        """True when ``route`` may be served without a token.

        With ``--public-dashboard`` the read-only HTML page stays open (for a
        wall display) while every ``/api/*`` route remains protected.
        """
        if not getattr(self.server, "public_dashboard", False):  # type: ignore[attr-defined]
            return False
        return route in ("/", "/index.html")

    def _rate_limited(self) -> bool:
        limiter: RateLimiter | None = getattr(self.server, "limiter", None)  # type: ignore[attr-defined]
        if limiter is None:
            return False
        client = self.client_address[0] if self.client_address else "?"
        allowed, retry_after = limiter.check(client)
        if allowed:
            return False
        self._send_json(
            {"error": "rate limited", "message": "Too many requests; slow down."},
            status=429,
            extra={"Retry-After": f"{max(1, int(retry_after + 0.999))}"},
        )
        return True

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urlparse(self.path)
        route = parsed.path.rstrip("/") or "/"

        if not self._public(route) and not self._authorized():
            self._send_json(
                {"error": "unauthorized", "message": "Provide Authorization: Bearer <token>."},
                status=401,
                extra={"WWW-Authenticate": "Bearer"},
            )
            return

        if self._rate_limited():
            return

        output_dir = self.server.output_dir  # type: ignore[attr-defined]

        try:
            query = parse_qs(parsed.query)
            if route in ("/", "/index.html"):
                from app.core.dashboard import render_dashboard

                url_filter = str(query.get("url", [""])[0] or "").strip()
                page = render_dashboard(output_dir, url_filter=url_filter, downloads=True).encode(
                    "utf-8"
                )
                self._respond(page, "text/html; charset=utf-8")
            elif route == "/api/schema":
                self._send_json(API_SCHEMA)
            elif route == "/api/history":
                args = self._page_args(query)
                if args is None:
                    return
                limit, offset, url = args
                page = history_page(output_dir, limit, offset, url)
                self._send_json(page, extra={"X-Total-Count": str(page["total"])})
            elif route == "/api/export":
                args = self._page_args(query)
                if args is None:
                    return
                limit, offset, url = args
                fmt = str(query.get("format", ["json"])[0] or "json").strip().lower()
                if fmt not in EXPORT_FORMATS:
                    self._send_json(
                        {
                            "error": "bad request",
                            "message": "format must be one of: " + ", ".join(EXPORT_FORMATS),
                        },
                        status=400,
                    )
                    return
                body, content_type, filename = export_payload(output_dir, fmt, url, limit, offset)
                etag = etag_for(body)
                if self._not_modified(etag):
                    return
                self._respond(
                    body,
                    content_type,
                    extra={
                        "Content-Disposition": f'attachment; filename="{filename}"',
                        "ETag": etag,
                        "Cache-Control": "no-cache",
                    },
                )
            elif route == "/api/sites":
                self._send_json(history.sites(output_dir))
            elif route == "/api/trend":
                self._send_json(_site_payload(output_dir))
            elif route == "/api/status":
                window = str(query.get("quiet_hours", [""])[0] or "").strip()
                expected = self._stale_after(query)
                if expected is None:
                    return
                if not expected:  # no query parameter: fall back to the --stale-after default
                    expected = int(getattr(self.server, "stale_after_minutes", 0) or 0)  # type: ignore[attr-defined]
                self._send_json(status_payload(output_dir, window, expected))
            elif route == "/api/storage":
                from app.core.dashboard import STORAGE_FORMATS, storage_export

                # The query string wins; otherwise the caps the server was
                # started with (``serve --caps 500,50``).
                caps = _caps_from_server(self.server)
                for key in ("screenshots_mb", "history_mb"):
                    value = self._mb_param(query, key)
                    if value is None:
                        return
                    if value:
                        caps[key] = value
                fmt = str(query.get("format", ["json"])[0] or "json").strip().lower()
                if fmt not in STORAGE_FORMATS:
                    self._send_json(
                        {
                            "error": "bad request",
                            "message": "format must be one of: " + ", ".join(STORAGE_FORMATS),
                        },
                        status=400,
                    )
                    return
                series = _truthy(query.get("series"))
                days = self._int_param(query, "days")
                if days is None:
                    return
                limit = self._int_param(query, "limit")
                if limit is None:
                    return
                try:
                    body, content_type, filename = storage_export(
                        output_dir, caps, fmt, series=series, days=days, limit=limit
                    )
                except ValueError as exc:
                    self._send_json({"error": "bad request", "message": str(exc)}, status=400)
                    return
                self._respond(
                    body,
                    content_type,
                    extra={"Content-Disposition": f'attachment; filename="{filename}"'},
                )
            elif route == "/metrics":
                # Prometheus scrapes one fixed URL, so the server default decides
                # what "stale" means here (there is no query string to pass it in).
                expected = int(getattr(self.server, "stale_after_minutes", 0) or 0)  # type: ignore[attr-defined]
                from app.core.dashboard import storage_stats

                payload = status_payload(
                    output_dir,
                    "",
                    expected,
                    storage=storage_stats(output_dir, _caps_from_server(self.server)),
                )
                body = status_module.metrics_text(payload, version=__version__).encode("utf-8")
                self._respond(body, "text/plain; version=0.0.4; charset=utf-8")
            elif route == "/api/digest":
                days = int(query.get("days", ["7"])[0])
                self._send_json(_digest_payload(output_dir, days))
            else:
                self._send_json({"error": "not found", "path": route}, status=404)
        except Exception as exc:  # noqa: BLE001 - a bad request must not kill the server
            self._send_json({"error": exc.__class__.__name__, "message": str(exc)}, status=500)

    def do_HEAD(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self.do_GET()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        """Keep the console quiet unless the caller asked for logs."""


def build_tls_context(cert: str | Path, key: str | Path) -> ssl.SSLContext:
    """A server-side TLS context for a certificate/key pair.

    Raises ``ValueError`` when only one of the two paths is given, and
    ``OSError`` when the files cannot be read - the CLI turns both into a clean
    error message instead of a traceback.
    """
    if not cert or not key:
        raise ValueError("TLS needs both a certificate and a private key.")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certfile=str(cert), keyfile=str(key))
    return context


def _caps_from_server(server: ThreadingHTTPServer) -> dict[str, Any]:
    """The caps the server was started with (``serve --caps 500,50``).

    ``site_caps`` travels as-is: it is a mapping (or the text form), not a size,
    and the storage helpers know what to do with it.
    """
    caps = getattr(server, "caps", None) or {}
    wanted: dict[str, Any] = {}
    for key, value in caps.items():
        if str(key) == "site_caps":
            if value:
                wanted["site_caps"] = value
            continue
        try:
            number = int(value or 0)
        except (TypeError, ValueError):  # pragma: no cover - the CLI validates first
            continue
        if number > 0:
            wanted[str(key)] = number
    return wanted


def create_server(
    output_dir: str | Path,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    token: str = "",
    cors: bool = False,
    public_dashboard: bool = False,
    rate_limit: int = 0,
    stale_after: int = 0,
    tls_cert: str = "",
    tls_key: str = "",
    caps: dict | None = None,
) -> ThreadingHTTPServer:
    """Bind (but do not start) the history server. Useful for tests/stubs.

    With ``tls_cert``/``tls_key`` the socket is wrapped so the bearer token never
    travels in clear text. ``stale_after`` is the default freshness limit
    ``/api/status`` reports, so a monitor can poll a bare URL. ``caps`` are the
    configured size limits: with them ``/metrics`` can publish a real
    ``capture_bot_days_to_cap`` instead of ``-1``.
    """
    server = ThreadingHTTPServer((host, port), HistoryHandler)
    server.output_dir = str(output_dir)  # type: ignore[attr-defined]
    server.stale_after_minutes = max(0, int(stale_after or 0))  # type: ignore[attr-defined]
    server.token = token or ""  # type: ignore[attr-defined]
    server.cors = bool(cors)  # type: ignore[attr-defined]
    server.public_dashboard = bool(public_dashboard)  # type: ignore[attr-defined]
    server.caps = dict(caps or {})  # type: ignore[attr-defined]
    server.limiter = RateLimiter(rate_limit)  # type: ignore[attr-defined]
    server.tls = bool(tls_cert and tls_key)  # type: ignore[attr-defined]
    if server.tls:
        server.socket = build_tls_context(tls_cert, tls_key).wrap_socket(
            server.socket, server_side=True
        )
    return server


def serve(
    output_dir: str | Path,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    token: str = "",
    cors: bool = False,
    public_dashboard: bool = False,
    rate_limit: int = 0,
    stale_after: int = 0,
    tls_cert: str = "",
    tls_key: str = "",
    caps: dict | None = None,
) -> None:
    """Serve until interrupted (Ctrl+C)."""
    server = create_server(
        output_dir,
        host,
        port,
        token=token,
        cors=cors,
        public_dashboard=public_dashboard,
        rate_limit=rate_limit,
        stale_after=stale_after,
        tls_cert=tls_cert,
        tls_key=tls_key,
        caps=caps,
    )
    shown = server.server_address[1] if server.server_address[1] else port
    scheme = "https" if getattr(server, "tls", False) else "http"
    print(f"History API on {scheme}://{host}:{shown} (folder: {Path(output_dir)})")
    print(
        "Endpoints: /, /api/schema, /api/history, /api/export, /api/sites, "
        "/api/trend, /api/digest?days=7, /api/status, /api/storage, /metrics"
    )
    if stale_after:
        print(f"Health: /api/status reports stale after {stale_after} minute(s) without a capture.")
    if token:
        print("Auth: send 'Authorization: Bearer <token>'.")
    if cors:
        print("CORS: cross-origin GET allowed.")
    if public_dashboard and token:
        print("Scope: the HTML dashboard is public, /api/* needs the token.")
    if rate_limit:
        print(f"Rate limit: {rate_limit} request(s) per minute per client.")
    if getattr(server, "tls", False):
        print("TLS: enabled (the bearer token is never sent in clear text).")
    try:
        server.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover - interactive stop
        print("\nStopped.")
    finally:
        server.server_close()
