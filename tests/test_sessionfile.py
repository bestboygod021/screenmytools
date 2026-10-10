"""Importing a saved browser session: what is in it, and is it still alive."""

from __future__ import annotations

import json
import time

import pytest

from app.core import sessionfile


def write(tmp_path, payload, name="storage-state.json"):
    path = tmp_path / name
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
    return path


def session(cookies=(), origins=()):
    return {"cookies": list(cookies), "origins": list(origins)}


def cookie(domain=".example.com", expires=None):
    entry = {"name": "sid", "value": "abc", "domain": domain, "path": "/"}
    if expires is not None:
        entry["expires"] = expires
    return entry


class TestReading:
    """A file in, a description out - or a refusal with a reason."""

    def test_a_real_session_is_summarised(self, tmp_path):
        path = write(
            tmp_path,
            session(
                cookies=[
                    cookie(".example.com", time.time() + 86400 * 5),
                    cookie("app.example.com", -1),
                ],
                origins=[{"origin": "https://app.example.com", "localStorage": [{"name": "k"}]}],
            ),
        )

        info = sessionfile.read_session(path)

        assert info.cookies == 2
        assert info.origins == 1
        assert info.local_storage_keys == 1
        assert info.hosts == ["example.com", "app.example.com", "https://app.example.com"]
        assert info.expired is False
        assert 4.5 < (info.days_left() or 0) < 5.5
        assert "2 cookie(s)" in info.summary()

    def test_a_session_without_expiry_never_expires(self, tmp_path):
        path = write(tmp_path, session(cookies=[cookie()]))
        info = sessionfile.read_session(path)
        assert info.expires_at is None
        assert info.days_left() is None
        assert "no expiry date" in info.summary()
        assert sessionfile.expiring_soon(info) is False

    def test_an_expired_session_says_so(self, tmp_path):
        path = write(tmp_path, session(cookies=[cookie(expires=time.time() - 60)]))
        info = sessionfile.read_session(path)
        assert info.expired is True
        assert "sign in again" in info.summary()
        assert sessionfile.expiring_soon(info) is True

    def test_a_session_expiring_tomorrow_is_flagged(self, tmp_path):
        path = write(tmp_path, session(cookies=[cookie(expires=time.time() + 86_400)]))
        info = sessionfile.read_session(path)
        assert sessionfile.expiring_soon(info, days=3) is True
        assert sessionfile.expiring_soon(info, days=0.5) is False

    def test_the_earliest_expiry_is_the_one_that_counts(self, tmp_path):
        path = write(
            tmp_path,
            session(
                cookies=[
                    cookie(expires=time.time() + 86400 * 30),
                    cookie(expires=time.time() + 86400 * 2),
                ]
            ),
        )
        assert 1.5 < (sessionfile.read_session(path).days_left() or 0) < 2.5

    def test_a_json_file_that_is_not_a_session_is_refused(self, tmp_path):
        path = write(tmp_path, {"hello": "world"})
        with pytest.raises(sessionfile.SessionFileError, match="not a storage state"):
            sessionfile.read_session(path)

    def test_a_file_that_is_not_json_is_refused(self, tmp_path):
        path = write(tmp_path, "not json at all")
        with pytest.raises(sessionfile.SessionFileError, match="not valid JSON"):
            sessionfile.read_session(path)

    def test_an_empty_file_is_refused(self, tmp_path):
        path = write(tmp_path, "   ")
        with pytest.raises(sessionfile.SessionFileError, match="is empty"):
            sessionfile.read_session(path)

    def test_an_empty_session_is_allowed_but_says_it_is_empty(self, tmp_path):
        info = sessionfile.read_session(write(tmp_path, session()))
        assert (
            info.cookies == 0
            and info.summary() == "The file holds no cookies and no local storage."
        )

    def test_a_missing_file_is_a_readable_error(self, tmp_path):
        with pytest.raises(sessionfile.SessionFileError, match="Could not read"):
            sessionfile.read_session(tmp_path / "nope.json")

    def test_cookies_that_are_not_objects_are_refused(self, tmp_path):
        path = write(tmp_path, session(cookies=["just a string"]))
        with pytest.raises(sessionfile.SessionFileError, match="Every cookie"):
            sessionfile.read_session(path)

    def test_local_storage_that_is_not_a_list_is_refused(self, tmp_path):
        path = write(
            tmp_path, session(origins=[{"origin": "https://x.test", "localStorage": "nope"}])
        )
        with pytest.raises(sessionfile.SessionFileError, match="localStorage"):
            sessionfile.read_session(path)

    def test_a_nonsense_expiry_is_ignored_rather_than_fatal(self, tmp_path):
        info = sessionfile.read_session(
            write(tmp_path, session(cookies=[cookie(expires="whenever")]))
        )
        assert info.expires_at is None


class TestImporting:
    """Copying it in: Downloads is not a place a run can rely on."""

    def test_the_file_is_copied_into_the_app_folder(self, tmp_path):
        source = write(tmp_path, session(cookies=[cookie()]), name="from-browser.json")
        info = sessionfile.read_session(source)

        destination, imported = sessionfile.import_session(source, tmp_path / "sessions")

        assert destination == tmp_path / "sessions" / "from-browser.json"
        assert destination.exists()
        assert imported.cookies == info.cookies
        assert imported.path == str(destination)

    def test_importing_from_the_folder_itself_does_not_copy_twice(self, tmp_path):
        folder = tmp_path / "sessions"
        folder.mkdir()
        source = write(folder, session(cookies=[cookie()]))
        destination, _info = sessionfile.import_session(source, folder)
        assert destination == source
        assert destination.read_text(encoding="utf-8") == source.read_text(encoding="utf-8")

    def test_a_file_that_is_not_a_session_never_lands(self, tmp_path):
        source = write(tmp_path, {"nope": True})
        with pytest.raises(sessionfile.SessionFileError):
            sessionfile.import_session(source, tmp_path / "sessions")
        assert not (tmp_path / "sessions").exists()

    def test_the_expiry_has_a_readable_date(self, tmp_path):
        path = write(tmp_path, session(cookies=[cookie(expires=time.time() + 86400)]))
        text = sessionfile.expires_text(sessionfile.read_session(path))
        assert "UTC" in text and len(text) == len("2026-10-09 12:00 UTC")
        assert (
            sessionfile.expires_text(sessionfile.read_session(write(tmp_path, session(), "e.json")))
            == "no expiry date"
        )

def test_freeze_creates_dated_copy(tmp_path):
    source = tmp_path / "session.json"
    source.write_text('{"cookies":[{"name":"sid","domain":".x","expires":9999999999}]}', encoding="utf-8")
    frozen = sessionfile.freeze_session(source, tmp_path / "archive")
    assert frozen.exists()
    assert frozen.name.startswith("session-")
    assert frozen.suffix == ".json"

def test_freeze_creates_dated_copy(tmp_path):
    import time
    source = tmp_path / "session.json"
    source.write_text('{"cookies":[{"name":"sid","domain":".x","expires":9999999999}]}', encoding="utf-8")
    frozen = sessionfile.freeze_session(source, tmp_path / "archive")
    assert frozen.exists() and frozen.name.startswith("session-")
