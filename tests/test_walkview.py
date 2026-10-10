"""The visual walk report: what a journey/crawl did, from its own report file."""

from __future__ import annotations

import json
import os

from app.core import walkview


def write_crawl(folder, entries, skipped=None, diff=None):
    folder.mkdir(parents=True, exist_ok=True)
    payload = {
        "url": "https://example.com/",
        "output_dir": str(folder.parent),
        "generated_at": "2026-01-01T09:00:00",
        "pages": entries,
        "skipped": skipped or [],
        "errors": [],
    }
    (folder / "report.json").write_text(json.dumps(payload), encoding="utf-8")
    if diff is not None:
        (folder / "diff.json").write_text(json.dumps(diff), encoding="utf-8")
    return folder


def page(index, name, path=None, file="", error=""):
    return {
        "index": index,
        "name": name,
        "url": f"https://example.com/{name}",
        "title": name.title(),
        "file": file,
        "depth": len(path or []),
        "path": path or [],
        "signature": name,
        "elapsed_ms": 10,
        "error": error,
    }


class TestLatestWalk:
    """Finding the walk, whichever of the two flavours it was."""

    def test_nothing_ran_yet(self, tmp_path):
        assert walkview.latest_walk(tmp_path).found is False
        assert "No walk" in walkview.latest_walk(tmp_path).summary()

    def test_the_newest_crawl_wins(self, tmp_path):
        write_crawl(tmp_path / "crawl-example_com-20260101-000000", [page(1, "old")])
        write_crawl(tmp_path / "crawl-example_com-20260201-000000", [page(1, "new")])
        walk = walkview.latest_walk(tmp_path)
        assert walk.kind == "crawl"
        assert [screen.name for screen in walk.screens] == ["new"]

    def test_a_journey_report_is_read_when_there_is_no_crawl(self, tmp_path):
        report = {
            "output_dir": str(tmp_path),
            "generated_at": "2026-01-01T09:00:00",
            "journeys": [
                {
                    "name": "shop",
                    "url": "https://example.com/",
                    "steps": [
                        {"index": 1, "action": "capture", "ok": True, "file": "a.png"},
                        {"index": 2, "action": "click", "detail": 'click "Products"', "ok": True},
                        {"index": 3, "action": "capture", "ok": False, "message": "gone"},
                    ],
                }
            ],
        }
        (tmp_path / "journey-report-20260101-090000.json").write_text(
            json.dumps(report), encoding="utf-8"
        )
        walk = walkview.latest_walk(tmp_path)
        assert walk.kind == "journey"
        assert [screen.name for screen in walk.screens] == ["a"]
        assert walk.screens[0].route == "landing page"

    def test_a_broken_report_is_not_a_crash(self, tmp_path):
        folder = tmp_path / "crawl-example_com-20260101-000000"
        folder.mkdir()
        (folder / "report.json").write_text("{not json", encoding="utf-8")
        assert walkview.latest_walk(tmp_path).found is False


class TestThePage:
    """The HTML: thumbnails by relative path, the route, and the skips."""

    def test_thumbnails_are_linked_relative_to_the_page(self, tmp_path):
        shots = tmp_path / "crawl-example_com-20260101-000000"
        folder = write_crawl(
            shots,
            [
                page(1, "home", file=str(shots / "001-home.png")),
                page(2, "products", path=["Products"], file=str(shots / "002-p.png")),
            ],
            skipped=[{"text": "Delete account", "reason": "looks dangerous"}],
            diff={"added": [{"name": "pricing"}], "removed": [{"name": "careers"}]},
        )
        walk = walkview.latest_walk(tmp_path)
        html = walkview.render_walk(walk, folder)

        assert "src='001-home.png'" in html
        assert "Products" in html  # the route of the second screen
        assert "Delete account" in html and "looks dangerous" in html
        assert "+ pricing" in html and "- careers" in html
        assert "<img" in html

    def test_a_changed_screen_is_shown_before_and_after(self, tmp_path):
        from PIL import Image

        shots = tmp_path / "crawl-example_com-20260201-000000"
        previous = tmp_path / "crawl-example_com-20260101-000000"
        previous.mkdir(parents=True)
        before = previous / "001-home.png"
        after = shots / "001-home.png"
        shots.mkdir(parents=True)
        Image.new("RGB", (40, 40), (255, 255, 255)).save(before)
        Image.new("RGB", (40, 40), (0, 0, 0)).save(after)
        write_crawl(
            shots,
            [page(1, "home", file=str(after))],
            diff={
                "added": [],
                "removed": [],
                "renamed": [],
                "changed": [
                    {
                        "url": "https://example.com/home",
                        "name": "home",
                        "diff": 0.5,
                        "before": str(before),
                        "after": str(after),
                    }
                ],
                "unchanged": 0,
            },
        )

        html = walkview.render_walk(walkview.latest_walk(tmp_path), shots)

        assert "before / after" in html
        assert "50% different" in html
        assert "../crawl-example_com-20260101-000000/001-home.png" in html
        assert "001-home.png" in html

    def test_a_changed_screen_gets_a_drag_handle_and_a_lightbox(self, tmp_path):
        from PIL import Image

        shots = tmp_path / "crawl-example_com-20260201-000000"
        previous = tmp_path / "crawl-example_com-20260101-000000"
        previous.mkdir(parents=True)
        before = previous / "001-home.png"
        after = shots / "001-home.png"
        shots.mkdir(parents=True)
        Image.new("RGB", (40, 40), (255, 255, 255)).save(before)
        Image.new("RGB", (40, 40), (0, 0, 0)).save(after)
        write_crawl(
            shots,
            [page(1, "home", file=str(after))],
            diff={
                "added": [],
                "removed": [],
                "renamed": [],
                "changed": [
                    {
                        "url": "https://example.com/home",
                        "name": "home",
                        "diff": 0.25,
                        "before": str(before),
                        "after": str(after),
                    }
                ],
                "unchanged": 0,
            },
        )

        html = walkview.render_walk(walkview.latest_walk(tmp_path), shots)

        assert "class='slider'" in html
        assert "--at:50%" in html, "the wipe starts in the middle"
        assert "type='range'" in html, "the handle is draggable"
        assert os.path.relpath(before, shots) in html
        assert "id='lightbox'" in html, "full size on click"
        assert "showModal" in html

    def test_a_half_pruned_change_shows_the_side_that_survived(self, tmp_path):
        from PIL import Image

        shots = tmp_path / "crawl-example_com-20260201-000000"
        after = shots / "001-home.png"
        shots.mkdir(parents=True)
        Image.new("RGB", (40, 40), (0, 0, 0)).save(after)
        write_crawl(
            shots,
            [page(1, "home", file=str(after))],
            diff={
                "added": [],
                "removed": [],
                "renamed": [],
                "changed": [
                    {
                        "url": "https://example.com/home",
                        "name": "home",
                        "diff": 0.5,
                        "before": str(tmp_path / "gone" / "001-home.png"),
                        "after": str(after),
                    }
                ],
                "unchanged": 0,
            },
        )

        html = walkview.render_walk(walkview.latest_walk(tmp_path), shots)

        assert "after only" in html
        assert "class='slider'" not in html, "nothing to wipe against"

    def test_a_screen_without_a_photo_is_still_listed(self, tmp_path):
        folder = write_crawl(
            tmp_path / "crawl-example_com-20260101-000000", [page(1, "home", error="timeout")]
        )
        html = walkview.render_walk(walkview.latest_walk(tmp_path), folder)
        assert "was not photographed" in html
        assert "timeout" in html

    def test_build_writes_the_page_next_to_the_walk(self, tmp_path):
        folder = write_crawl(
            tmp_path / "crawl-example_com-20260101-000000",
            [
                page(
                    1,
                    "home",
                    file=str(tmp_path / "crawl-example_com-20260101-000000" / "001-home.png"),
                )
            ],
        )
        target = walkview.build_walk_report(tmp_path)
        assert target == folder / "walk-report.html"
        assert "001-home.png" in target.read_text(encoding="utf-8")

    def test_build_returns_nothing_without_a_walk(self, tmp_path):
        assert walkview.build_walk_report(tmp_path) is None

    def test_the_json_is_machine_readable(self, tmp_path):
        write_crawl(
            tmp_path / "crawl-example_com-20260101-000000", [page(1, "home", path=["Sign in"])]
        )
        payload = walkview.latest_walk(tmp_path).to_dict()
        assert payload["kind"] == "crawl"
        assert payload["screens"][0]["route"] == "Sign in"
