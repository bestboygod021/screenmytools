"""Tests for the read-only history API (real HTTP round-trips on a fake server)."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from app.core import api


def _seed(directory):
    for stamp, url, diff in (
        ("2026-01-01T00:00:00", "https://a.example.com", 0.0),
        ("2026-01-02T00:00:00", "https://a.example.com", 0.4),
        ("2026-01-02T01:00:00", "https://b.example.com", 0.1),
    ):
        name = stamp.replace(":", "").replace("-", "").replace("T", "-")
        (directory / f"capture-report-{name}.json").write_text(
            json.dumps(
                {
                    "generated_at": stamp,
                    "results": [{"url": url, "status": "success", "diff": diff, "file_path": ""}],
                }
            ),
            encoding="utf-8",
        )


@pytest.fixture
def server(tmp_path):
    """A live server on an ephemeral port, torn down after the test."""
    _seed(tmp_path)
    httpd = api.create_server(tmp_path, host="127.0.0.1", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _get(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310 - local test URL
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


class TestEndpoints:
    def test_history_rows_are_json(self, server):
        status, body = _get(f"{server}/api/history")
        assert status == 200
        page = json.loads(body)
        assert page["total"] == 3 and page["count"] == 3
        rows = page["rows"]
        assert rows[0]["timestamp"] >= rows[-1]["timestamp"]  # newest first

    def test_sites_lists_each_url_once(self, server):
        status, body = _get(f"{server}/api/sites")
        assert status == 200
        assert sorted(json.loads(body)) == ["https://a.example.com", "https://b.example.com"]

    def test_trend_exposes_totals_and_series(self, server):
        status, body = _get(f"{server}/api/trend")
        assert status == 200
        payload = json.loads(body)
        site = next(item for item in payload if item["url"] == "https://a.example.com")
        assert site["captures"] == 2
        assert site["changes"] == 1
        assert site["series"] == [0.0, 0.4]
        assert site["baseline"] is False and site["drift"] is None

    def test_digest_honours_the_days_parameter(self, server):
        status, body = _get(f"{server}/api/digest?days=0")
        assert status == 200
        assert json.loads(body)["captures"] == 0
        status, body = _get(f"{server}/api/digest?days=36500")
        assert json.loads(body)["captures"] == 3

    def test_root_serves_the_dashboard_html(self, server):
        status, body = _get(f"{server}/")
        assert status == 200
        assert "<!doctype html>" in body.lower()
        assert "a.example.com" in body

    def test_unknown_route_is_a_json_404(self, server):
        status, body = _get(f"{server}/api/nope")
        assert status == 404
        assert json.loads(body)["error"] == "not found"

    def test_head_requests_work(self, server):
        request = urllib.request.Request(f"{server}/api/sites", method="HEAD")  # noqa: S310
        with urllib.request.urlopen(request, timeout=5) as response:
            assert response.status == 200
            assert response.read() == b""


class TestEmptyFolder:
    def test_endpoints_work_without_history(self, tmp_path):
        httpd = api.create_server(tmp_path, host="127.0.0.1", port=0)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{httpd.server_address[1]}"
            page = json.loads(_get(f"{base}/api/history")[1])
            assert page["total"] == 0 and page["rows"] == []
            assert json.loads(_get(f"{base}/api/trend")[1]) == []
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)


class TestCli:
    def test_serve_subcommand_starts_the_server(self, tmp_path, monkeypatch):
        from app import cli

        started = {}
        monkeypatch.setattr(
            api,
            "serve",
            lambda directory, host, port, **kwargs: started.update(
                dir=directory, host=host, port=port, **kwargs
            ),
        )
        code = cli.main(["serve", "--dir", str(tmp_path), "--port", "9999"])
        assert code == 0
        assert started == {
            "dir": str(tmp_path),
            "host": api.DEFAULT_HOST,
            "port": 9999,
            "token": "",
            "cors": False,
            "public_dashboard": False,
            "rate_limit": 0,
            "stale_after": 0,
            "tls_cert": "",
            "tls_key": "",
            "caps": {},
        }

    def test_serve_passes_the_token_and_cors_flags(self, tmp_path, monkeypatch):
        from app import cli

        started = {}
        monkeypatch.setattr(
            api,
            "serve",
            lambda directory, host, port, **kwargs: started.update(kwargs),
        )
        code = cli.main(["serve", "--dir", str(tmp_path), "--token", "s3cret", "--cors"])
        assert code == 0
        assert started == {
            "token": "s3cret",
            "cors": True,
            "public_dashboard": False,
            "rate_limit": 0,
            "stale_after": 0,
            "tls_cert": "",
            "tls_key": "",
            "caps": {},
        }

    def test_serve_reports_a_bind_error(self, tmp_path, monkeypatch, capsys):
        from app import cli

        def boom(directory, host, port, **kwargs):
            raise OSError("address already in use")

        monkeypatch.setattr(api, "serve", boom)
        assert cli.main(["serve", "--dir", str(tmp_path)]) == 2
        assert "Could not start the server" in capsys.readouterr().err


class TestSchema:
    def test_schema_describes_every_endpoint(self, server):
        status, body = _get(f"{server}/api/schema")
        assert status == 200
        schema = json.loads(body)
        paths = {entry["path"] for entry in schema["endpoints"]}
        assert {
            "/",
            "/api/schema",
            "/api/history",
            "/api/export",
            "/api/sites",
            "/api/trend",
            "/api/digest",
        } <= paths
        history_entry = next(e for e in schema["endpoints"] if e["path"] == "/api/history")
        assert "timestamp" in history_entry["fields"]
        assert set(history_entry["query"]) == {"limit", "offset", "url"}
        assert "rows" in history_entry["envelope"]


def _request(url: str, headers: dict | None = None, method: str = "GET"):
    request = urllib.request.Request(url, headers=headers or {}, method=method)  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, dict(response.headers), response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read().decode("utf-8")


@pytest.fixture
def guarded(tmp_path):
    """A server that requires a bearer token."""
    _seed(tmp_path)
    httpd = api.create_server(tmp_path, host="127.0.0.1", port=0, token="s3cret")
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


class TestAuth:
    def test_missing_token_is_401(self, guarded):
        status, headers, body = _request(f"{guarded}/api/sites")
        assert status == 401
        assert json.loads(body)["error"] == "unauthorized"
        assert "Bearer" in headers.get("WWW-Authenticate", "")

    def test_wrong_token_is_401(self, guarded):
        status, _headers, _body = _request(f"{guarded}/api/sites", {"Authorization": "Bearer nope"})
        assert status == 401

    def test_correct_token_is_allowed(self, guarded):
        status, _headers, body = _request(
            f"{guarded}/api/sites", {"Authorization": "Bearer s3cret"}
        )
        assert status == 200
        assert "a.example.com" in body

    def test_html_root_is_guarded_too(self, guarded):
        assert _request(f"{guarded}/")[0] == 401
        assert _request(f"{guarded}/", {"Authorization": "Bearer s3cret"})[0] == 200

    def test_without_a_token_everything_is_open(self, server):
        assert _request(f"{server}/api/sites")[0] == 200


@pytest.fixture
def cors_server(tmp_path):
    _seed(tmp_path)
    httpd = api.create_server(tmp_path, host="127.0.0.1", port=0, cors=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


class TestCors:
    def test_headers_are_added_when_enabled(self, cors_server):
        status, headers, _body = _request(f"{cors_server}/api/trend")
        assert status == 200
        assert headers.get("Access-Control-Allow-Origin") == "*"
        assert "Authorization" in headers.get("Access-Control-Allow-Headers", "")

    def test_preflight_is_answered(self, cors_server):
        status, headers, _body = _request(f"{cors_server}/api/trend", method="OPTIONS")
        assert status == 204
        assert headers.get("Access-Control-Allow-Origin") == "*"

    def test_no_cors_headers_by_default(self, server):
        _status, headers, _body = _request(f"{server}/api/trend")
        assert "Access-Control-Allow-Origin" not in headers

    def test_preflight_without_cors_is_rejected(self, server):
        assert _request(f"{server}/api/trend", method="OPTIONS")[0] == 405


@pytest.fixture
def scoped(tmp_path):
    """A token-protected server whose HTML dashboard stays public."""
    _seed(tmp_path)
    httpd = api.create_server(
        tmp_path, host="127.0.0.1", port=0, token="s3cret", public_dashboard=True
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


class TestAuthScopes:
    def test_dashboard_is_public(self, scoped):
        status, _headers, body = _request(f"{scoped}/")
        assert status == 200
        assert "<!doctype html>" in body.lower()

    def test_api_still_needs_the_token(self, scoped):
        assert _request(f"{scoped}/api/trend")[0] == 401
        assert _request(f"{scoped}/api/trend", {"Authorization": "Bearer s3cret"})[0] == 200

    def test_schema_and_history_stay_guarded(self, scoped):
        assert _request(f"{scoped}/api/schema")[0] == 401
        assert _request(f"{scoped}/api/history")[0] == 401

    def test_scope_does_nothing_without_a_token(self, tmp_path):
        httpd = api.create_server(tmp_path, host="127.0.0.1", port=0, public_dashboard=True)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{httpd.server_address[1]}"
            assert _request(f"{base}/api/sites")[0] == 200
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)


class TestRateLimiter:
    def test_disabled_by_default(self):
        limiter = api.RateLimiter(0)
        for _ in range(50):
            assert limiter.check("1.2.3.4")[0] is True

    def test_allows_burst_then_blocks(self):
        limiter = api.RateLimiter(3)
        allowed = [limiter.check("1.2.3.4", now=100.0)[0] for _ in range(3)]
        assert allowed == [True, True, True]
        ok, retry_after = limiter.check("1.2.3.4", now=100.0)
        assert ok is False and retry_after > 0

    def test_refills_over_time(self):
        limiter = api.RateLimiter(60)  # bucket of 60, refilling one per second
        for _ in range(60):
            assert limiter.check("a", now=0.0)[0] is True
        assert limiter.check("a", now=0.0)[0] is False
        assert limiter.check("a", now=0.5)[0] is False  # not a full token yet
        assert limiter.check("a", now=1.5)[0] is True  # one token accrued

    def test_buckets_are_per_client(self):
        limiter = api.RateLimiter(1)
        assert limiter.check("a", now=0.0)[0] is True
        assert limiter.check("a", now=0.0)[0] is False
        assert limiter.check("b", now=0.0)[0] is True  # a different client is unaffected


class TestRateLimitedServer:
    def test_requests_are_capped_with_429(self, tmp_path):
        _seed(tmp_path)
        httpd = api.create_server(tmp_path, host="127.0.0.1", port=0, rate_limit=2)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{httpd.server_address[1]}"
            assert _request(f"{base}/api/sites")[0] == 200
            assert _request(f"{base}/api/sites")[0] == 200
            status, headers, body = _request(f"{base}/api/sites")
            assert status == 429
            assert json.loads(body)["error"] == "rate limited"
            assert int(headers.get("Retry-After", "0")) >= 1
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def test_unlimited_server_never_429s(self, server):
        for _ in range(10):
            assert _request(f"{server}/api/sites")[0] == 200


class TestServeFlagWiring:
    def test_public_dashboard_and_rate_limit_reach_serve(self, tmp_path, monkeypatch):
        from app import cli

        started = {}
        monkeypatch.setattr(
            api, "serve", lambda directory, host, port, **kwargs: started.update(kwargs)
        )
        code = cli.main(
            ["serve", "--dir", str(tmp_path), "--public-dashboard", "--rate-limit", "30"]
        )
        assert code == 0
        assert started["public_dashboard"] is True and started["rate_limit"] == 30


class TestPagination:
    def _page(self, url: str) -> dict:
        status, _headers, body = _request(url)
        assert status == 200
        return json.loads(body)

    def test_limit_caps_the_rows_but_reports_the_total(self, server):
        page = self._page(f"{server}/api/history?limit=2")
        assert page["total"] == 3 and page["count"] == 2 and page["limit"] == 2
        assert len(page["rows"]) == 2

    def test_offset_skips_rows(self, server):
        first = self._page(f"{server}/api/history?limit=1")
        second = self._page(f"{server}/api/history?limit=1&offset=1")
        assert first["rows"][0] != second["rows"][0]
        assert second["offset"] == 1

    def test_walking_the_pages_covers_every_row(self, server):
        seen = []
        offset = 0
        while True:
            page = self._page(f"{server}/api/history?limit=1&offset={offset}")
            if not page["rows"]:
                break
            seen.extend(page["rows"])
            offset += 1
        assert len(seen) == 3
        assert {row["url"] for row in seen} == {
            "https://a.example.com",
            "https://b.example.com",
        }

    def test_offset_past_the_end_is_an_empty_page(self, server):
        page = self._page(f"{server}/api/history?offset=99")
        assert page["count"] == 0 and page["rows"] == [] and page["total"] == 3

    def test_total_count_header_is_sent(self, server):
        _status, headers, _body = _request(f"{server}/api/history?limit=1")
        assert headers.get("X-Total-Count") == "3"

    def test_non_numeric_limit_is_a_400(self, server):
        status, _headers, body = _request(f"{server}/api/history?limit=lots")
        assert status == 400
        assert json.loads(body)["error"] == "bad request"

    def test_negative_values_are_rejected(self, server):
        assert _request(f"{server}/api/history?limit=-1")[0] == 400
        assert _request(f"{server}/api/history?offset=-5")[0] == 400

    def test_page_helper_keeps_drift_fields(self, server):
        page = self._page(f"{server}/api/history?limit=1")
        assert "drift" in page["rows"][0]


def _make_certificate(tmp_path) -> tuple[str, str] | None:
    """A throwaway self-signed cert for 127.0.0.1, or None when openssl is absent."""
    import shutil
    import subprocess

    if shutil.which("openssl") is None:  # pragma: no cover - openssl is in CI images
        return None
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    proc = subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "2",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=IP:127.0.0.1,DNS:localhost",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:  # pragma: no cover - openssl refused
        return None
    return str(cert), str(key)


class TestTls:
    @pytest.fixture
    def https_server(self, tmp_path):
        pair = _make_certificate(tmp_path)
        if pair is None:  # pragma: no cover
            pytest.skip("openssl is not available to mint a test certificate")
        cert, key = pair
        _seed(tmp_path)
        httpd = api.create_server(
            tmp_path, host="127.0.0.1", port=0, token="s3cret", tls_cert=cert, tls_key=key
        )
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"https://127.0.0.1:{httpd.server_address[1]}", cert
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def _get_tls(self, url: str, cert: str, headers: dict | None = None):
        import ssl as ssl_mod

        context = ssl_mod.create_default_context(cafile=cert)
        request = urllib.request.Request(url, headers=headers or {})  # noqa: S310
        try:
            with urllib.request.urlopen(request, timeout=10, context=context) as response:
                return response.status, response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8")

    def test_serves_https_and_guards_the_api(self, https_server):
        base, cert = https_server
        assert self._get_tls(f"{base}/api/sites", cert)[0] == 401
        status, body = self._get_tls(f"{base}/api/sites", cert, {"Authorization": "Bearer s3cret"})
        assert status == 200 and "a.example.com" in body

    def test_an_untrusted_client_refuses_the_certificate(self, https_server):
        # A client that only trusts the public roots must reject the self-signed cert.
        import ssl as ssl_mod

        base, _cert = https_server
        strict = ssl_mod.create_default_context()  # trusts the public roots only
        with pytest.raises(urllib.error.URLError) as captured:
            urllib.request.urlopen(  # noqa: S310
                urllib.request.Request(f"{base}/api/sites"),  # noqa: S310
                timeout=10,
                context=strict,
            )
        assert "CERTIFICATE_VERIFY_FAILED" in str(captured.value)

    def test_plain_http_against_the_https_port_fails(self, https_server):
        import http.client

        base, _cert = https_server
        port = int(base.rsplit(":", 1)[1])
        with pytest.raises((http.client.HTTPException, OSError, ValueError)):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            connection.request("GET", "/api/sites")
            connection.getresponse()

    def test_tls_context_requires_both_files(self, tmp_path):
        with pytest.raises(ValueError):
            api.build_tls_context(str(tmp_path / "cert.pem"), "")

    def test_tls_context_reports_a_missing_file(self, tmp_path):
        with pytest.raises(OSError):
            api.build_tls_context(str(tmp_path / "nope.pem"), str(tmp_path / "nope.key"))


class TestTlsCli:
    def test_partial_tls_flags_are_rejected(self, tmp_path, capsys):
        from app import cli

        code = cli.main(["serve", "--dir", str(tmp_path), "--tls-cert", "cert.pem"])
        assert code == 2
        assert "both --tls-cert and --tls-key" in capsys.readouterr().err

    def test_tls_flags_reach_serve(self, tmp_path, monkeypatch):
        from app import cli

        started = {}
        monkeypatch.setattr(
            api, "serve", lambda directory, host, port, **kwargs: started.update(kwargs)
        )
        code = cli.main(
            [
                "serve",
                "--dir",
                str(tmp_path),
                "--tls-cert",
                "cert.pem",
                "--tls-key",
                "key.pem",
            ]
        )
        assert code == 0
        assert started["tls_cert"] == "cert.pem" and started["tls_key"] == "key.pem"

    def test_a_bad_certificate_is_a_clean_error(self, tmp_path, capsys):
        from app import cli

        code = cli.main(
            [
                "serve",
                "--dir",
                str(tmp_path),
                "--tls-cert",
                str(tmp_path / "nope.pem"),
                "--tls-key",
                str(tmp_path / "nope.key"),
            ]
        )
        assert code == 2
        assert "Could not start the server" in capsys.readouterr().err


class TestExport:
    def test_csv_export_has_a_header_and_quoted_rows(self, server):
        status, body = _get(f"{server}/api/export?format=csv")
        assert status == 200
        lines = body.strip().splitlines()
        assert lines[0] == "timestamp,url,label,status,diff,drift,file"
        assert len(lines) == 4  # header + the three seeded captures
        assert any('"https://a.example.com"' in line for line in lines[1:])

    def test_csv_export_sets_a_download_filename(self, server):
        with urllib.request.urlopen(f"{server}/api/export?format=csv", timeout=5) as response:
            assert "text/csv" in response.headers["Content-Type"]
            assert response.headers["Content-Disposition"] == 'attachment; filename="history.csv"'

    def test_json_export_is_the_page_envelope(self, server):
        status, body = _get(f"{server}/api/export")
        assert status == 200
        page = json.loads(body)
        assert page["total"] == 3 and len(page["rows"]) == 3

    def test_the_url_filter_narrows_the_export(self, server):
        status, body = _get(f"{server}/api/export?format=csv&url=a.example")
        assert status == 200
        assert body.count("a.example.com") == 2
        assert "b.example.com" not in body

    def test_limit_and_offset_still_apply(self, server):
        status, body = _get(f"{server}/api/export?limit=1&offset=1")
        assert status == 200
        page = json.loads(body)
        assert page["count"] == 1 and page["total"] == 3 and page["offset"] == 1

    def test_an_unknown_format_is_a_clean_400(self, server):
        status, body = _get(f"{server}/api/export?format=xlsx")
        assert status == 400
        assert "format" in json.loads(body)["message"]

    def test_a_negative_offset_is_a_clean_400(self, server):
        status, body = _get(f"{server}/api/export?offset=-1")
        assert status == 400 and "negative" in json.loads(body)["message"]

    def test_a_bad_limit_is_a_clean_400(self, server):
        status, body = _get(f"{server}/api/export?limit=soon")
        assert status == 400 and "integers" in json.loads(body)["message"]

    def test_a_filter_without_matches_is_an_empty_file(self, server):
        status, body = _get(f"{server}/api/export?format=csv&url=zzz")
        assert status == 200
        assert body.strip().splitlines() == ["timestamp,url,label,status,diff,drift,file"]


class TestHistoryUrlFilter:
    def test_the_filter_narrows_the_rows_and_the_total(self, server):
        status, body = _get(f"{server}/api/history?url=b.example")
        assert status == 200
        page = json.loads(body)
        assert page["total"] == 1 and page["count"] == 1
        assert page["rows"][0]["url"] == "https://b.example.com"

    def test_x_total_count_follows_the_filter(self, server):
        with urllib.request.urlopen(f"{server}/api/history?url=a.example", timeout=5) as response:
            assert response.headers["X-Total-Count"] == "2"

    def test_no_filter_keeps_everything(self, server):
        _status, body = _get(f"{server}/api/history")
        assert json.loads(body)["total"] == 3


class TestDashboardFilter:
    def test_the_html_route_honours_url(self, server):
        status, body = _get(f"{server}/?url=b.example")
        assert status == 200
        assert "b.example.com" in body and "a.example.com" not in body

    def test_the_schema_documents_export(self, server):
        _status, body = _get(f"{server}/api/schema")
        paths = [endpoint["path"] for endpoint in json.loads(body)["endpoints"]]
        assert "/api/export" in paths

    def test_the_schema_documents_the_export_query(self, server):
        _status, body = _get(f"{server}/api/schema")
        entry = next(
            endpoint
            for endpoint in json.loads(body)["endpoints"]
            if endpoint["path"] == "/api/export"
        )
        assert {"format", "url", "limit", "offset"} <= set(entry["query"])


class TestStatus:
    def test_totals_and_the_last_capture(self, server):
        status, body = _get(f"{server}/api/status")
        assert status == 200
        payload = json.loads(body)
        assert payload["captures"] == 3 and payload["sites"] == 2
        assert payload["last_capture"]["timestamp"] == "2026-01-02T01:00:00"
        assert payload["last_capture"]["url"] == "https://b.example.com"

    def test_the_index_block_reports_the_file(self, server):
        import pathlib

        status, body = _get(f"{server}/api/status")
        payload = json.loads(body)
        assert payload["index"]["file"] == "history.sqlite3"
        # the fixture seeds no index: the file is reported as missing, size 0
        assert payload["index"]["exists"] is False and payload["index"]["bytes"] == 0
        assert pathlib.Path(payload["index"]["file"]).name == "history.sqlite3"

    def test_pending_alerts_come_from_the_queue(self, server, tmp_path):
        from app.core import quiet

        quiet.append(quiet.queue_path(tmp_path), [{"url": "https://a.example.com"}])
        _status, body = _get(f"{server}/api/status")
        queued = json.loads(body)["pending_alerts"]
        assert queued["count"] == 1 and queued["oldest"]

    def test_no_queue_means_nothing_pending(self, server):
        _status, body = _get(f"{server}/api/status")
        assert json.loads(body)["pending_alerts"] == {"count": 0, "oldest": None}

    def test_quiet_hours_are_reported_when_asked(self, server):
        from datetime import datetime, timedelta

        now = datetime.now()
        window = f"{(now - timedelta(hours=2)):%H:%M}-{(now + timedelta(hours=2)):%H:%M}"
        _status, body = _get(f"{server}/api/status?quiet_hours={window}")
        reported = json.loads(body)["quiet"]
        assert reported["window"] == window
        assert reported["active"] is True  # the window spans 'now', even across midnight
        assert reported["resumes_at"]

    def test_a_never_active_window_resumes_nowhere(self, server):
        _status, body = _get(f"{server}/api/status?quiet_hours=00:00-00:00")
        reported = json.loads(body)["quiet"]
        assert reported["active"] is False and reported["resumes_at"] is None

    def test_without_the_parameter_there_is_no_quiet_block(self, server):
        _status, body = _get(f"{server}/api/status")
        assert "quiet" not in json.loads(body)

    def test_a_broken_window_never_crashes_the_route(self, server):
        _status, body = _get(f"{server}/api/status?quiet_hours=tonight")
        assert json.loads(body)["quiet"]["active"] is False

    def test_an_empty_history_reports_nulls(self, tmp_path):
        httpd = api.create_server(tmp_path / "empty", host="127.0.0.1", port=0)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            _status, body = _get(f"http://127.0.0.1:{httpd.server_address[1]}/api/status")
            payload = json.loads(body)
            assert payload["captures"] == 0 and payload["last_capture"] is None
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def test_the_schema_documents_status(self, server):
        _status, body = _get(f"{server}/api/schema")
        entry = next(
            endpoint
            for endpoint in json.loads(body)["endpoints"]
            if endpoint["path"] == "/api/status"
        )
        assert "quiet_hours" in entry["query"] and "pending_alerts" in entry["fields"]


def _serve(directory) -> tuple[str, object]:
    httpd = api.create_server(directory, host="127.0.0.1", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return f"http://127.0.0.1:{httpd.server_address[1]}", httpd


class TestStatusHealth:
    """The freshness verdict a monitor polls for."""

    def test_the_health_block_is_always_there(self, server):
        _status, body = _get(f"{server}/api/status")
        health = json.loads(body)["health"]
        assert health["state"] == "ok"  # no limit was asked for
        assert health["max_age_minutes"] == 0
        assert health["age_seconds"] > 0  # the fixture is dated in the past
        assert health["captures"] == 3

    def test_an_old_capture_is_stale(self, server):
        _status, body = _get(f"{server}/api/status?expect_max_age_minutes=5")
        health = json.loads(body)["health"]
        assert health["state"] == "stale" and health["max_age_minutes"] == 5

    def test_a_fresh_capture_is_ok(self, tmp_path):
        from datetime import datetime

        (tmp_path / "capture-report-now.json").write_text(
            json.dumps(
                {
                    "generated_at": datetime.now().isoformat(timespec="seconds"),
                    "results": [{"url": "https://a.example.com", "status": "success", "diff": 0.0}],
                }
            ),
            encoding="utf-8",
        )
        base, httpd = _serve(tmp_path)
        try:
            _status, body = _get(f"{base}/api/status?expect_max_age_minutes=5")
            assert json.loads(body)["health"]["state"] == "ok"
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_an_empty_history_is_empty(self, tmp_path):
        base, httpd = _serve(tmp_path / "empty")
        try:
            _status, body = _get(f"{base}/api/status?expect_max_age_minutes=5")
            health = json.loads(body)["health"]
            assert health["state"] == "empty" and health["age_seconds"] is None
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_a_bad_limit_is_a_clean_400(self, server):
        status, body = _get(f"{server}/api/status?expect_max_age_minutes=soon")
        assert status == 400
        assert "non-negative integer" in json.loads(body)["message"]

    def test_a_negative_limit_is_a_clean_400(self, server):
        status, body = _get(f"{server}/api/status?expect_max_age_minutes=-5")
        assert status == 400 and "non-negative" in json.loads(body)["message"]

    def test_the_server_default_applies(self, tmp_path):
        _seed(tmp_path)
        httpd = api.create_server(tmp_path, host="127.0.0.1", port=0, stale_after=30)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            _status, body = _get(f"http://127.0.0.1:{httpd.server_address[1]}/api/status")
            health = json.loads(body)["health"]
            assert health["max_age_minutes"] == 30 and health["state"] == "stale"
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_the_query_wins_over_the_server_default(self, tmp_path):
        _seed(tmp_path)
        httpd = api.create_server(tmp_path, host="127.0.0.1", port=0, stale_after=30)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{httpd.server_address[1]}/api/status?expect_max_age_minutes=1"
            _status, body = _get(url)
            assert json.loads(body)["health"]["max_age_minutes"] == 1
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_the_schema_documents_the_limit(self, server):
        _status, body = _get(f"{server}/api/schema")
        entry = next(
            endpoint
            for endpoint in json.loads(body)["endpoints"]
            if endpoint["path"] == "/api/status"
        )
        assert "expect_max_age_minutes" in entry["query"] and "health" in entry["fields"]


class TestExportConditional:
    """A polling script should be able to ask 'did anything change?' for free."""

    def test_the_download_carries_an_etag(self, server):
        _status, headers, body = _request(f"{server}/api/export?format=csv")
        assert headers["ETag"].startswith('"') and headers["ETag"].endswith('"')
        assert headers["Cache-Control"] == "no-cache"
        assert "a.example.com" in body

    def test_a_matching_etag_answers_304(self, server):
        _status, headers, _body = _request(f"{server}/api/export?format=csv")
        status, reply, body = _request(
            f"{server}/api/export?format=csv", {"If-None-Match": headers["ETag"]}
        )
        assert status == 304 and body == ""
        assert reply["ETag"] == headers["ETag"]

    def test_a_stale_etag_gets_the_file_again(self, server):
        status, _headers, body = _request(
            f"{server}/api/export?format=csv", {"If-None-Match": '"000000"'}
        )
        assert status == 200 and "a.example.com" in body

    def test_a_wildcard_etag_answers_304(self, server):
        status, _headers, body = _request(
            f"{server}/api/export?format=json", {"If-None-Match": "*"}
        )
        assert status == 304 and body == ""

    def test_a_list_of_etags_is_understood(self, server):
        _status, headers, _body = _request(f"{server}/api/export?format=json")
        status, _reply, _body = _request(
            f"{server}/api/export", {"If-None-Match": f'"other", {headers["ETag"]}'}
        )
        assert status == 304

    def test_different_content_gets_a_different_etag(self, server):
        _status, csv_headers, _body = _request(f"{server}/api/export?format=csv")
        _status, json_headers, _body = _request(f"{server}/api/export?format=json")
        assert csv_headers["ETag"] != json_headers["ETag"]

    def test_a_filtered_export_has_its_own_etag(self, server):
        _status, all_headers, _body = _request(f"{server}/api/export?format=json")
        _status, filtered_headers, _body = _request(
            f"{server}/api/export?format=json&url=a.example"
        )
        assert all_headers["ETag"] != filtered_headers["ETag"]

    def test_a_new_capture_changes_the_etag(self, server, tmp_path):
        _status, headers, _body = _request(f"{server}/api/export?format=json")
        (tmp_path / "capture-report-20260301-000000.json").write_text(
            json.dumps(
                {
                    "generated_at": "2026-03-01T00:00:00",
                    "results": [{"url": "https://c.example.com", "status": "success", "diff": 0.2}],
                }
            ),
            encoding="utf-8",
        )
        status, fresh, _body = _request(
            f"{server}/api/export?format=json", {"If-None-Match": headers["ETag"]}
        )
        assert status == 200 and fresh["ETag"] != headers["ETag"]

    def test_the_schema_documents_the_header(self, server):
        _status, body = _get(f"{server}/api/schema")
        entry = next(
            endpoint
            for endpoint in json.loads(body)["endpoints"]
            if endpoint["path"] == "/api/export"
        )
        assert any("If-None-Match" in header for header in entry["headers"])


class TestMetricsEndpoint:
    """``/metrics``: the health probe in the shape Prometheus scrapes."""

    def _metrics(self, base):
        with urllib.request.urlopen(f"{base}/metrics", timeout=5) as response:  # noqa: S310
            return (
                response.status,
                response.headers["Content-Type"],
                response.read().decode("utf-8"),
            )

    def test_the_gauges_are_exposed(self, server):
        status, content_type, body = self._metrics(server)
        assert status == 200
        assert content_type.startswith("text/plain")
        assert "application/openmetrics" not in content_type
        for name in (
            "capture_bot_captures",
            "capture_bot_sites",
            "capture_bot_index_bytes",
            "capture_bot_pending_alerts",
            "capture_bot_last_capture_age_seconds",
            "capture_bot_health_state",
        ):
            assert f"# TYPE {name} gauge" in body
        assert 'capture_bot_health_state{state="ok"} 1' in body
        assert 'capture_bot_health_state{state="stale"} 0' in body
        assert 'capture_bot_build_info{version="' in body
        assert body.endswith("\n")

    def test_an_empty_folder_reports_the_empty_state(self, tmp_path):
        httpd = api.create_server(tmp_path, host="127.0.0.1", port=0)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            _status, _type, body = self._metrics(f"http://127.0.0.1:{httpd.server_address[1]}")
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)
        assert 'capture_bot_health_state{state="empty"} 1' in body
        assert "capture_bot_last_capture_age_seconds -1" in body
        assert "capture_bot_oldest_pending_alert_seconds -1" in body

    def test_the_server_default_decides_staleness(self, tmp_path):
        _seed(tmp_path)  # seeded captures are from 2026-01-01: old by any clock
        httpd = api.create_server(tmp_path, host="127.0.0.1", port=0, stale_after=1)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            _status, _type, body = self._metrics(f"http://127.0.0.1:{httpd.server_address[1]}")
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)
        assert 'capture_bot_health_state{state="stale"} 1' in body

    def test_the_schema_documents_the_endpoint(self, server):
        _status, body = _get(f"{server}/api/schema")
        entry = next(
            endpoint for endpoint in json.loads(body)["endpoints"] if endpoint["path"] == "/metrics"
        )
        assert entry["returns"] == "text/plain; version=0.0.4"
        assert "Prometheus" in entry["description"]


class TestStorageEndpoint:
    """``/api/storage``: the Storage panel as JSON/CSV for another machine."""

    def test_the_numbers_are_json(self, server):
        status, body = _get(f"{server}/api/storage")
        payload = json.loads(body)
        assert status == 200
        assert payload["reports"] == 3
        assert payload["history"] > 0
        assert payload["total"] >= payload["history"]
        assert payload["screenshots"] == payload["total"] - payload["history"]
        assert payload["history"] >= payload["index"]
        assert payload["growth"]["history_bytes_per_day"] > 0
        assert payload["forecasts"] == {}  # no cap was asked for
        assert payload["days_to_cap"] is None
        assert payload["archives"] == {"count": 0, "bytes": 0}

    def test_the_caps_in_the_query_fill_the_forecast(self, server):
        _status, body = _get(f"{server}/api/storage?history_mb=1&screenshots_mb=5")
        payload = json.loads(body)
        assert payload["caps"] == {"history_mb": 1, "screenshots_mb": 5}
        assert payload["forecasts"]["history"] is not None
        assert payload["days_to_cap"] == payload["forecasts"]["history"]

    def test_the_csv_is_a_download(self, server):
        status, headers, body = _request(f"{server}/api/storage?format=csv")
        assert status == 200
        assert headers["Content-Type"].startswith("text/csv")
        assert headers["Content-Disposition"] == 'attachment; filename="storage.csv"'
        lines = body.splitlines()
        assert lines[0] == "key,value"
        assert "reports,3" in lines

    def test_a_bad_cap_is_a_clean_400(self, server):
        status, body = _get(f"{server}/api/storage?history_mb=lots")
        assert status == 400
        assert "expects a number of MB" in json.loads(body)["message"]

    def test_a_negative_cap_is_a_clean_400(self, server):
        status, body = _get(f"{server}/api/storage?screenshots_mb=-2")
        assert status == 400
        assert "cannot be negative" in json.loads(body)["message"]

    def test_a_bad_format_is_a_clean_400(self, server):
        status, body = _get(f"{server}/api/storage?format=xml")
        assert status == 400
        assert "format must be one of" in json.loads(body)["message"]

    def test_the_schema_documents_it(self, server):
        _status, body = _get(f"{server}/api/schema")
        entry = next(e for e in json.loads(body)["endpoints"] if e["path"] == "/api/storage")
        assert "days_to_cap" in entry["fields"]
        assert entry["query"]["format"].startswith("json")

    def test_the_served_dashboard_links_to_it(self, server):
        _status, body = _get(f"{server}/")
        assert "/api/storage?format=csv" in body
        assert "/api/storage?format=json" in body

    def test_the_written_dashboard_has_no_download_links(self, tmp_path):
        from app.core.dashboard import build_dashboard

        _seed(tmp_path)
        page = build_dashboard(tmp_path, tmp_path / "dashboard.html").read_text(encoding="utf-8")
        assert "/api/storage" not in page


class TestStorageSeries:
    """``/api/storage?series=1``: the recorded size trend, not just today."""

    @staticmethod
    def _seed_samples(folder, days=4, step=25_000):
        from datetime import datetime, timedelta

        from app.core.store import HistoryStore

        base = datetime.now() - timedelta(days=days - 1)
        with HistoryStore(folder / "history.sqlite3") as store:
            for index in range(days):
                history = 100_000 + step * index
                store.add_storage_sample(
                    (base + timedelta(days=index)).isoformat(timespec="seconds"),
                    {
                        "total": 500_000 + step * 2 * index,
                        "screenshots": 400_000 + step * index,
                        "history": history,
                        "index": 4096,
                        "reports": index + 1,
                    },
                )

    def test_the_series_is_opt_in(self, server, tmp_path):
        self._seed_samples(tmp_path)
        status, body = _get(f"{server}/api/storage")
        payload = json.loads(body)
        assert status == 200
        assert "series" not in payload
        # ... but the measured growth is used either way.
        assert payload["growth"]["source"] == "samples"
        assert payload["growth"]["samples"] == 4
        assert payload["growth"]["history_bytes_per_day"] == 25000.0

    def test_series_adds_the_samples_and_their_days(self, server, tmp_path):
        self._seed_samples(tmp_path)
        status, body = _get(f"{server}/api/storage?series=1")
        payload = json.loads(body)
        assert status == 200
        assert len(payload["series"]) == 4
        assert payload["series"][0]["taken_at"] < payload["series"][-1]["taken_at"]
        assert payload["growth_by_day"][-1]["bytes"] == 25_000

    def test_a_short_window_only_trims_the_series(self, server, tmp_path):
        self._seed_samples(tmp_path)
        status, body = _get(f"{server}/api/storage?series=1&days=2&limit=1")
        payload = json.loads(body)
        assert status == 200
        assert len(payload["series"]) == 1
        # The rates still come from every sample, so the forecast cannot change
        # just because the caller asked for less history.
        assert payload["growth"]["samples"] == 4
        assert payload["growth"]["history_bytes_per_day"] == 25000.0

    def test_a_csv_series_is_refused_with_an_explanation(self, server, tmp_path):
        status, body = _get(f"{server}/api/storage?format=csv&series=1")
        assert status == 400
        assert "series is only available as JSON" in json.loads(body)["message"]

    def test_a_bad_window_is_a_bad_request(self, server):
        status, body = _get(f"{server}/api/storage?series=1&days=soon")
        assert status == 400
        assert "days expects a whole number" in json.loads(body)["message"]

    def test_a_negative_limit_is_refused(self, server):
        status, body = _get(f"{server}/api/storage?series=1&limit=-5")
        assert status == 400
        assert "cannot be negative" in json.loads(body)["message"]

    def test_the_schema_documents_the_series(self, server):
        _status, body = _get(f"{server}/api/schema")
        entry = next(e for e in json.loads(body)["endpoints"] if e["path"] == "/api/storage")
        assert "series" in entry["query"]
        assert "series" in entry["fields"] and "growth_by_day" in entry["fields"]

    @pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
    def test_readable_yeses(self, value):
        assert api._truthy([value]) is True

    @pytest.mark.parametrize("value", ["", "0", "no", "maybe"])
    def test_everything_else_is_no(self, value):
        assert api._truthy([value]) is False

    def test_a_missing_parameter_is_no(self):
        assert api._truthy(None) is False
        assert api._truthy([]) is False


class TestServerCaps:
    """``serve --caps 500,50`` teaches the server what the limits are."""

    @staticmethod
    def _serve(tmp_path, **kwargs):
        httpd = api.create_server(tmp_path, host="127.0.0.1", port=0, **kwargs)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        return httpd, f"http://127.0.0.1:{httpd.server_address[1]}", thread

    def test_the_storage_endpoint_reports_them(self, tmp_path):
        _seed(tmp_path)
        httpd, url, thread = self._serve(tmp_path, caps={"screenshots_mb": 500, "history_mb": 50})
        try:
            status, _headers, body = _request(f"{url}/api/storage")
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)
        assert status == 200
        assert json.loads(body)["caps"] == {"screenshots_mb": 500, "history_mb": 50}

    def test_a_query_string_still_wins(self, tmp_path):
        _seed(tmp_path)
        httpd, url, thread = self._serve(tmp_path, caps={"history_mb": 50})
        try:
            status, _headers, body = _request(f"{url}/api/storage?history_mb=5")
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)
        assert status == 200
        assert json.loads(body)["caps"] == {"history_mb": 5}

    def test_the_metrics_endpoint_publishes_the_cap(self, tmp_path):
        _seed(tmp_path)
        httpd, url, thread = self._serve(tmp_path, caps={"history_mb": 50})
        try:
            status, _headers, body = _request(f"{url}/metrics")
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)
        assert status == 200
        assert "capture_bot_history_cap_bytes 52428800" in body
        assert "capture_bot_total_bytes " in body


class TestSiteCapEndpoint:
    """A per-site budget becomes its own Prometheus series."""

    def test_the_server_caps_reach_the_gauges(self, tmp_path):
        import threading
        import urllib.request

        from app.core import api

        httpd = api.create_server(
            tmp_path,
            host="127.0.0.1",
            port=0,
            caps={"screenshots_mb": 500, "site_caps": {"news.example.com": 200.0, "*": 1000.0}},
        )
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            with urllib.request.urlopen(  # noqa: S310
                f"http://127.0.0.1:{httpd.server_address[1]}/metrics", timeout=5
            ) as response:
                body = response.read().decode("utf-8")
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)
        assert 'capture_bot_site_cap_bytes{site="news.example.com"} 209715200' in body
        assert 'capture_bot_site_cap_bytes{site="*"} 1048576000' in body
        assert "capture_bot_screenshots_cap_bytes 524288000" in body
