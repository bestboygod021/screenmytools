"""Recording a session and turning it into steps."""

from __future__ import annotations

from app.core import journey, recorder


class TestTranslating:
    """What the page saw, as lines of the step language."""

    def test_a_click_uses_the_label_when_the_selector_is_generic(self):
        steps = recorder.steps_for(
            [recorder.Event(kind="click", tag="a", selector="a", text="Products")]
        )
        # the click, then a screenshot of the screen it landed on
        assert steps == ['click "Products"', "capture products"]

    def test_a_click_uses_an_id_when_there_is_one(self):
        steps = recorder.steps_for(
            [recorder.Event(kind="click", tag="button", selector="#filters", text="Filters")]
        )
        assert steps == ["click #filters", "capture filters"]

    def test_a_click_uses_a_named_attribute(self):
        steps = recorder.steps_for(
            [recorder.Event(kind="click", tag="input", selector='input[name="go"]', text="")]
        )
        assert steps == ['click input[name="go"]', "capture go"]

    def test_typing_and_choosing_become_fill_and_select(self):
        events = [
            recorder.Event(kind="fill", tag="input", selector="#q", value="shoes"),
            recorder.Event(kind="select", tag="input", selector="#country", value="NL"),
            recorder.Event(kind="check", tag="input", selector="#tos"),
        ]
        assert recorder.steps_for(events) == [
            "fill #q = shoes",
            "select #country = NL optional",
            "check #tos optional",
            "capture tos",
        ]

    def test_submitting_a_form_photographs_the_screen_behind_it(self):
        events = [
            recorder.Event(kind="click", tag="button", selector="#login", text="Sign in"),
            recorder.Event(kind="fill", tag="input", selector="#email", value="me@x.com"),
            recorder.Event(kind="submit", tag="form"),
        ]
        steps = recorder.steps_for(events)
        # after a submit the interesting screen is the one the button led to
        assert steps == [
            "click #login",
            "fill #email = me@x.com",
            "press Enter optional",
            "capture sign-in",
        ]

    def test_navigating_after_the_first_step_is_a_goto(self):
        events = [
            recorder.Event(kind="click", tag="a", selector="a", text="Docs"),
            recorder.Event(kind="goto", url="https://example.com/docs"),
        ]
        assert recorder.steps_for(events) == [
            'click "Docs"',
            "goto https://example.com/docs",
            "capture docs",
        ]

    def test_the_screen_the_person_stopped_on_is_the_last_capture(self):
        events = [
            recorder.Event(kind="click", selector="a", text="Products"),
            recorder.Event(kind="fill", selector="#q", value="shoes"),
            recorder.Event(kind="submit", tag="form"),
            recorder.Event(kind="click", selector="#filters", text="Filters"),
        ]
        steps = recorder.steps_for(events)
        assert steps[-1] == "capture filters"
        assert steps.count("press Enter optional") == 1

    def test_scrolling_becomes_a_scroll_step(self):
        events = [
            recorder.Event(kind="click", selector="#products", text="Products", t=0),
            recorder.Event(kind="scroll", value="down", t=2000),
            recorder.Event(kind="click", selector="#filters", text="Filters", t=4000),
        ]
        steps = recorder.steps_for(events)
        # A pause before the scroll and one before the click: both are real, and a
        # replay that skipped them photographs a page that had not loaded yet.
        assert steps == [
            "click #products",
            "wait 2000",
            "scroll down",
            "wait 2000",
            "click #filters",
            "capture filters",
        ]

    def test_scrolling_back_up_is_its_own_step(self):
        events = [
            recorder.Event(kind="scroll", value="down", t=0),
            recorder.Event(kind="scroll", value="up", t=1500),
        ]
        assert recorder.steps_for(events) == [
            "scroll down",
            "wait 1500",
            "scroll up",
            "capture up",
        ]

    def test_a_lazy_loading_page_replays_the_pauses(self):
        """The whole point: the pause is what let the page load."""
        events = [
            recorder.Event(kind="click", selector="#more", text="More", t=10_000),
            recorder.Event(kind="scroll", value="down", t=12_000),  # a human read for 2s
            recorder.Event(kind="scroll", value="down", t=20_000),
        ]
        steps = recorder.steps_for(events)
        assert steps == [
            "click #more",
            "wait 2000",
            "scroll down",
            "wait 8000",
            "scroll down",
            "capture more",
        ]

    def test_a_quick_tap_is_not_a_pause(self):
        events = [
            recorder.Event(kind="click", selector="#a", text="A", t=1000),
            recorder.Event(kind="click", selector="#b", text="B", t=1100),
        ]
        # An id beats a label as a reference: it survives a reworded button.
        assert recorder.steps_for(events) == ["click #a", "click #b", "capture b"]

    def test_a_coffee_break_is_capped(self):
        events = [
            recorder.Event(kind="click", selector="#a", text="A", t=0),
            recorder.Event(kind="click", selector="#b", text="B", t=15 * 60 * 1000),
        ]
        assert "wait 20000" in recorder.steps_for(events)

    def test_a_pause_before_nothing_is_dropped(self):
        # A click the page reported with no element to aim at produces no step, and
        # the pause that came before it would otherwise be the last thing in the list.
        events = [
            recorder.Event(kind="click", selector="#a", text="A", t=0),
            recorder.Event(kind="click", selector="", text="", t=20_000),
        ]
        assert recorder.steps_for(events) == ["click #a", "capture a"]

    def test_the_enter_key_alone_is_pressed(self):
        events = [recorder.Event(kind="press", tag="body", key="Enter", t=500)]
        assert recorder.steps_for(events) == ["press Enter", "capture final"]

    def test_a_key_the_page_reports_is_a_step(self):
        # The page script reports Enter only outside a form; inside one it reports a
        # submit, which is the step that photographs the screen behind it.
        events = [recorder.Event(kind="press", tag="body", key="Enter", t=500)]
        assert recorder.steps_for(events)[0] == "press Enter"

    def test_the_waits_are_rounded_to_the_tenth_of_a_second(self):
        events = [
            recorder.Event(kind="click", selector="#a", text="A", t=0),
            recorder.Event(kind="click", selector="#b", text="B", t=1234),
        ]
        assert "wait 1200" in recorder.steps_for(events)

    def test_a_recording_without_anything_recorded_is_empty(self):
        assert recorder.steps_for([]) == []
        assert recorder.recording_from_events("https://x", []).found is False
        assert "Nothing was recorded" in recorder.recording_from_events("https://x", []).summary()

    def test_every_recording_is_a_runnable_journey(self):
        events = [
            recorder.Event(kind="click", tag="a", selector="a", text="Products"),
            recorder.Event(kind="fill", tag="input", selector="#q", value="shoes"),
            recorder.Event(kind="submit", tag="form"),
            recorder.Event(kind="click", tag="button", selector="#filters", text="Filters"),
        ]
        recording = recorder.recording_from_events("https://example.com", events)

        spec = journey.journey_from_text("recorded", recording.url, recording.text())

        assert [step.action for step in spec.steps] == [
            "click",
            "fill",
            "press",
            "capture",
            "click",
            "capture",
        ]
        assert recording.captures == 2
        assert "2 capture(s)" in recording.summary()
        assert recording.to_dict()["events"][0]["kind"] == "click"

    def test_two_captures_of_the_same_label_do_not_collide(self):
        events = [
            recorder.Event(kind="click", selector="#a", text="Open"),
            recorder.Event(kind="goto", url="https://example.com/1"),
            recorder.Event(kind="click", selector="#b", text="Open"),
            recorder.Event(kind="goto", url="https://example.com/2"),
        ]
        steps = recorder.steps_for(events)
        captures = [step.split(" ", 1)[1] for step in steps if step.startswith("capture")]
        assert captures == ["open", "open-2"]


class TestRecordingAgainstAFakeBrowser:
    """The browser half, driven by the same fake the other suites use."""

    @staticmethod
    def _engine(tmp_path, script):
        from app.core.engine import CaptureEngine
        from app.core.settings import CaptureSettings
        from tests.fakes import CallLog, make_factory

        calls = CallLog()
        settings = CaptureSettings(
            output_dir=str(tmp_path), settle_delay_ms=0, network_idle_timeout_ms=0
        )
        settings.validate()
        engine = CaptureEngine(
            settings, playwright_factory=make_factory(script, calls), log=lambda *_: None
        )
        return engine, calls

    def test_the_events_are_read_and_the_session_closes(self, tmp_path):
        from tests.fakes import PageScript

        script = PageScript(page_width=1000, page_height=800)
        script.recorded_events = [
            {"kind": "click", "tag": "a", "selector": "a", "text": "Products"},
            {"kind": "submit", "tag": "form"},
        ]
        engine, calls = self._engine(tmp_path, script)

        recording = recorder.Recorder(engine, poll_seconds=0.05).record(
            "https://example.com", on_tick=lambda recording, seconds: True
        )

        assert [event.kind for event in recording.events] == ["click", "submit"]
        assert recording.steps == ['click "Products"', "press Enter optional", "capture products"]
        assert calls.browser_closed, "the browser must be closed when the recording ends"

    def test_a_download_during_the_recording_is_saved(self, tmp_path):
        from tests.fakes import FakeDownload, PageScript

        script = PageScript(page_width=1000, page_height=800)
        engine, calls = self._engine(tmp_path, script)
        seen: list[recorder.Recording] = []

        def tick(recording, seconds):
            page = calls.browsers[-1].contexts[0].pages[0]
            page.emit("download", FakeDownload("August-report.pdf"))
            seen.append(recording)
            return True

        recording = recorder.Recorder(engine, poll_seconds=0.05).record(
            "https://example.com", on_tick=tick
        )

        saved = list((tmp_path / "downloads").glob("*.pdf"))
        assert saved, "the file the user fetched is kept"
        assert saved[0].read_bytes() == b"%PDF-fake"
        assert saved[0].suffix == ".pdf", "a download keeps its type"
        assert recording.downloads == [str(saved[0])]

    def test_a_download_that_cannot_be_saved_does_not_stop_the_recording(self, tmp_path):
        from tests.fakes import FakeDownload, PageScript

        script = PageScript(page_width=1000, page_height=800)
        engine, calls = self._engine(tmp_path, script)
        messages: list[str] = []
        engine.log = lambda level, message: messages.append(f"{level}: {message}")

        def tick(recording, seconds):
            page = calls.browsers[-1].contexts[0].pages[0]
            broken = FakeDownload("locked.pdf")
            broken.failure = "the file is locked"
            page.emit("download", broken)
            return True

        recording = recorder.Recorder(engine, poll_seconds=0.05).record(
            "https://example.com", on_tick=tick
        )

        assert recording.downloads == []
        assert any("could not be saved" in line for line in messages)

    def test_a_tab_opened_during_the_recording_is_noticed(self, tmp_path):
        from tests.fakes import PageScript

        script = PageScript(page_width=1000, page_height=800)
        engine, calls = self._engine(tmp_path, script)
        ticks: list[int] = []
        seen: list[recorder.Recording] = []

        def tick(recording, seconds):
            ticks.append(len(ticks))
            page = calls.browsers[-1].contexts[0].pages[0]
            if len(ticks) == 1:
                tab = page._context.new_page()
                tab._navigate("https://example.com/pricing")
                tab._script = script  # the very same fake page behaviour
                return False
            seen.append(recording)
            return True

        recording = recorder.Recorder(engine, poll_seconds=0.02).record(
            "https://example.com", on_tick=tick
        )

        assert any(event.kind == "new_tab" for event in recording.events)
        new_tab = [event for event in recording.events if event.kind == "new_tab"][0]
        assert new_tab.url.endswith("/pricing")
        assert "goto https://example.com/pricing" in recording.steps

    def test_the_tick_callback_can_ask_for_a_stop(self, tmp_path):
        from tests.fakes import PageScript

        script = PageScript(page_width=1000, page_height=800)
        script.recorded_events = [{"kind": "click", "tag": "a", "selector": "a", "text": "Go"}]
        engine, _calls = self._engine(tmp_path, script)
        seen: list[float] = []

        recording = recorder.Recorder(engine, poll_seconds=0.05).record(
            "https://example.com",
            on_tick=lambda current, seconds: seen.append(seconds) or True,
        )

        assert seen, "the app has to be told how long the recording has run"
        assert recording.found is True

    def test_a_broken_poll_stops_instead_of_raising(self, tmp_path):
        from tests.fakes import PageScript

        script = PageScript(page_width=1000, page_height=800)
        script.evaluate_error_for = "__captureBotRecorder"
        script.evaluate_error = RuntimeError("window closed")
        engine, calls = self._engine(tmp_path, script)

        recording = recorder.Recorder(engine, poll_seconds=0.05, max_seconds=1.0).record(
            "https://example.com"
        )

        assert recording.found is False
        assert calls.browser_closed, "a broken session still closes the browser"

    def test_the_time_budget_ends_the_session(self, tmp_path):
        from tests.fakes import PageScript

        script = PageScript(page_width=1000, page_height=800)
        engine, calls = self._engine(tmp_path, script)

        recorder.Recorder(engine, poll_seconds=0.05, max_seconds=0.2).record("https://example.com")

        assert calls.browser_closed
