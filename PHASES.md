# FullPage Capture Bot — Phases (5 logical stages)

## Phase 1: Core Engine & Recording
Files: app/core/engine.py, app/core/recorder.py, app/core/journey.py
- Full-page screenshot, scroll stitching, lazy-load trigger
- Smart recording with scroll/wait/press/enter
- Journey parsing (step_line grammar)

## Phase 2: UI & Step Editor
Files: app/ui/main_window.py, app/ui/step_editor.py, app/core/steplist.py
- Rows↔text bridge, edit-as-rows dialog
- Recipe picker, live status label, session import

## Phase 3: Crawl / Compare / Walk
Files: app/core/crawler.py, app/core/walkview.py, app/core/walkview.py (Screens report)
- crawl --compare (content comparison), --changed-threshold, --fail-on-change
- Before/after viewer, diff.json, crawl mode (landing/main/tools/full)

## Phase 4: Recipes, Sessions, Shared State
Files: app/core/journey.py (recipes, share_session), app/core/sessionfile.py, app/ui/main_window.py
- Custom recipes (~/.capture-bot/recipes/*.txt), versioned recipes
- Session import (audit/hash/freezing), inheritance
- Cross-site chaining (SSO / goto with shared session)

## Phase 5: Internationalization, AI APIs, Multi-language
Files: app/i18n/fa.json, app/core/ai_api.py, app/core/metrics.py, docs/tutorial.html, SETUP.md
- 5 AI providers (OpenAI/Gemini/Claude/Grok/Qwen)
- Bilingual UI (EN/FA), interactive tutorial page
- Metrics, audit log, adaptive budget, onboarding

--- Dependency Graph ---
sessionfile.py -> freeze_session / inherit_session / audit_hash
neural_layout.py -> suggest_selector (uses DOM density)
adaptive_budget.py -> budget_for (uses crawl depth/settings)
onboarding.py -> first_run (zero-config)
ai_api.py -> provider_for (5 APIs)
step_editor.py -> _suggest_for_bad_row -> neural_layout
main_window.py -> edit_steps_as_rows / session import / crawl mode dropdown
SETUP.md -> install instructions (Windows/Linux)
