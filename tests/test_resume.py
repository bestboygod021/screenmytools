"""Resuming a crawl from its last screen."""

from __future__ import annotations
from app.core import crawler


def test_resume_reads_last_screen_and_remaining_depth(tmp_path):
    import json

    previous = tmp_path / "walk.json"
    previous.write_text(
        json.dumps(
            {
                "url": "https://shop.example.com",
                "screens": [{"url": "https://shop.example.com/pricing", "name": "pricing"}],
            }
        )
    )
    url, depth = crawler.resume_from_walk(previous)
    assert url == "https://shop.example.com/pricing"
    assert depth == 1  # default max_depth 2 - 1 screen done
