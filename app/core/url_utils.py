"""URL parsing, validation and filesystem-safe filename generation.

This module deliberately knows nothing about Playwright or Qt: it is pure
string/path logic that can be exhaustively unit-tested.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlparse, urlunparse

#: Characters that are illegal in Windows file names.
_WINDOWS_ILLEGAL_CHARS = '<>:"/\\|?*'

#: Legacy DOS device names that Windows still refuses as file names.
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

_SLUG_RE = re.compile(r"[^a-z0-9-]+")  # hyphen is legal on every OS we target
_MULTI_UNDERSCORE_RE = re.compile(r"_{2,}")

_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")

#: Schemes we are willing to screenshot.
_ALLOWED_SCHEMES = ("http", "https")


@dataclass(frozen=True)
class ParsedUrl:
    """The result of validating one raw line from the URL text area."""

    original: str
    #: Normalised, browser-ready URL (``""`` when :attr:`is_valid` is False).
    normalized: str
    host: str
    #: Short, filesystem-safe description of the URL used in file names.
    label: str
    is_valid: bool
    error: str | None = None

    def __bool__(self) -> bool:  # pragma: no cover - convenience
        return self.is_valid


def split_url_lines(text: str) -> list[str]:
    """Split a pasted blob into unique, meaningful URL candidates.

    * Blank lines and whitespace-only lines are dropped.
    * Lines starting with ``#`` or ``//`` are treated as comments.
    * Surrounding quotes, angle brackets and trailing commas are stripped
      (Markdown/Pasted-link friendly).
    * Duplicates are removed while preserving the original order.
    """
    seen: set[str] = set()
    urls: list[str] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#") or line.startswith("//"):
            continue

        line = _strip_wrappers(line)
        if not line:
            continue

        key = line.lower()
        if key in seen:
            continue
        seen.add(key)
        urls.append(line)

    return urls


def _strip_wrappers(line: str) -> str:
    """Peel off quotes, angle brackets and trailing punctuation, in any order.

    Pasted links often arrive as ``<https://a.com>,`` or ``"https://a.com";``,
    so the stripping is repeated until the string stops changing.
    """
    for _ in range(4):
        before = line
        line = line.strip()
        line = line.strip("<>").strip()
        line = line.strip('"').strip("'").strip()
        line = line.rstrip(",;").strip()
        if line == before:
            break
    return line


def normalize_url(raw: str) -> str:
    """Return a browser-ready absolute URL for ``raw``.

    Raises :class:`ValueError` with a human-readable reason when the input
    cannot be turned into a valid http(s) URL.
    """
    candidate = raw.strip()
    if not candidate:
        raise ValueError("URL is empty")

    if not _SCHEME_RE.match(candidate):
        # Be forgiving: "example.com/login" -> "https://example.com/login".
        if candidate.startswith("//"):
            candidate = "https:" + candidate
        else:
            candidate = "https://" + candidate

    parts = urlparse(candidate)

    scheme = (parts.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise ValueError(f"Unsupported scheme '{parts.scheme or 'none'}' (use http or https)")

    host = (parts.hostname or "").strip()
    if not host:
        raise ValueError("Missing host name")

    if " " in parts.netloc:
        raise ValueError("Host name contains a space")

    if not _looks_like_host(host):
        raise ValueError(f"'{host}' does not look like a domain or IP address")

    # Rebuild the authority so the host is lower-cased while the path keeps
    # its original casing (paths are case-sensitive, hosts are not).
    authority = f"[{host}]" if ":" in host else host
    if parts.port:
        authority = f"{authority}:{parts.port}"
    if parts.username is not None:
        userinfo = parts.username
        if parts.password:
            userinfo = f"{userinfo}:{parts.password}"
        authority = f"{quote(userinfo, safe='')}@{authority}"

    # Percent-encode anything a browser would reject in path/query/fragment
    # while leaving already-encoded sequences untouched.
    safe_path = quote(parts.path, safe="/%:@&=+$,;~")
    safe_query = quote(parts.query, safe="/%:@&=+$,;?~")
    safe_fragment = quote(parts.fragment, safe="/%:@&=+$,;?~")

    return urlunparse((scheme, authority, safe_path, parts.params, safe_query, safe_fragment))


def _looks_like_host(host: str) -> bool:
    """Very small host sanity check - good enough to catch typos early."""
    if not host or len(host) > 253:
        return False
    if host == "localhost":
        return True
    try:  # IPv4 or IPv6 literal
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass

    labels = host.split(".")
    if len(labels) < 2:
        return False
    return all(
        label and len(label) <= 63 and not label.startswith("-") and not label.endswith("-")
        for label in labels
    )


def validate_url(raw: str) -> ParsedUrl:
    """Validate ``raw`` without ever raising - errors are captured in the result."""
    try:
        normalized = normalize_url(raw)
    except ValueError as exc:
        return ParsedUrl(
            original=raw, normalized="", host="", label="", is_valid=False, error=str(exc)
        )

    parts = urlparse(normalized)
    host = parts.hostname or ""
    return ParsedUrl(
        original=raw,
        normalized=normalized,
        host=host,
        label=build_url_label(normalized),
        is_valid=True,
        error=None,
    )


def build_url_label(url: str) -> str:
    """Build a short, filesystem-safe label such as ``www_example_com_login``."""
    parts = urlparse(url)
    host = (parts.hostname or "unknown-host").lower()

    path_bits = [bit for bit in parts.path.split("/") if bit]
    tail = "_".join(path_bits[:3])  # keep file names readable

    label = f"{host}{'_' + tail if tail else ''}"
    return sanitize_component(label, max_length=70) or "page"


def sanitize_component(text: str, max_length: int = 70) -> str:
    """Make ``text`` safe to use as a Windows file name component."""
    cleaned = text
    for char in _WINDOWS_ILLEGAL_CHARS:
        cleaned = cleaned.replace(char, "_")
    cleaned = cleaned.replace("\n", " ").replace("\r", " ").replace("\t", " ")
    cleaned = _SLUG_RE.sub("_", cleaned.lower()).strip("._-")
    cleaned = _MULTI_UNDERSCORE_RE.sub("_", cleaned)

    stem = cleaned[:max_length].strip("._-") or "page"
    if stem.upper() in _WINDOWS_RESERVED_NAMES:
        stem = f"{stem}_"
    return stem


def download_name(name: str, max_length: int = 60) -> str:
    """A safe file name for something the *site* named, extension kept.

    ``sanitize_component`` is built for slugs - it lowercases and replaces the dot,
    which turns ``August Report.pdf`` into ``august_report_pdf``: a file with no
    type, which is how a download becomes useless. This keeps the suffix, drops any
    directory part (a site must not choose where a file lands), and stays inside
    the Windows rules.
    """
    original = Path(str(name or "").strip()).name
    stem = sanitize_component(Path(original).stem, max_length=max_length) or "file"
    suffix = re.sub(r"[^A-Za-z0-9.]", "", Path(original).suffix)[:12]
    if suffix and not suffix.startswith("."):  # pragma: no cover - defensive
        suffix = f".{suffix}"
    return f"{stem}{suffix.lower()}" if suffix else stem


def build_filename(
    index: int,
    url: str,
    extension: str,
    timestamp: str,
    prefix: str = "",
    width: int = 3,
) -> str:
    """Compose ``001_www_example_com_20261007-135012.png``-style file names."""
    label = build_url_label(url)
    prefix_slug = sanitize_component(prefix, max_length=30) if prefix else ""

    parts = [f"{index:0{width}d}"]
    if prefix_slug:
        parts.append(prefix_slug)
    parts.append(label)
    if timestamp:
        parts.append(sanitize_component(timestamp, max_length=30))

    stem = "_".join(parts)
    ext = (extension or "png").lower().lstrip(".")
    return f"{stem}.{ext}"


def unique_path(directory: Path, filename: str) -> Path:
    """Return ``directory/filename``, appending ``_2``, ``_3``... on collision."""
    candidate = directory / filename
    if not candidate.exists():
        return candidate

    stem, suffix = filename.rsplit(".", 1) if "." in filename else (filename, "")
    counter = 2
    while True:
        name = f"{stem}_{counter}.{suffix}" if suffix else f"{stem}_{counter}"
        candidate = directory / name
        if not candidate.exists():
            return candidate
        counter += 1
        if counter > 10_000:  # pragma: no cover - defensive
            raise OSError(f"Too many files named '{stem}' in {directory}")
