# FullPage Capture Bot — 100 Capabilities / 10 Phases
# ساختار ۵ فاز اصلی + ۵ فاز پیشرفته (مجموع ۱۰۰ قابلیت)

## Phase 1: Core Engine & Recording (1-20)
Files: app/core/engine.py, app/core/recorder.py, app/core/journey.py
- Full-page screenshot, scroll stitching, lazy-load
- Smart recording (scroll/wait/press/enter)
- Event queue, recording steps
- Session storage_state
- Basic CLI

## Phase 2: UI & Step Editor (21-40)
Files: app/ui/main_window.py, app/ui/step_editor.py, app/core/steplist.py
- Row editor (rows↔text)
- StepEditorDialog
- Live status label
- Recipe picker + custom recipes
- Interactive tutorial page

## Phase 3: Crawl / Compare / Walk (41-60)
Files: app/core/crawler.py, app/core/walkview.py
- crawl --compare (content diff)
- Before/after viewer
- Crawl mode (landing/main/tools/full)
- Crawl resume from walk.json
- Slow-motion frames
- Multi-region nodes

## Phase 4: Recipes, Sessions, Shared State (61-80)
Files: app/core/journey.py, app/core/sessionfile.py
- Recipe save/load/version
- Session import/export/freezing
- Inheritance (parent→child)
- Cross-site SSO chaining
- Shared session (TOML)

## Phase 5: Internationalization, AI APIs, Multi-language (81-100)
Files: app/i18n/fa.json, app/core/ai_api.py, app/core/metrics.py, docs/tutorial.html, SETUP.md
- 5 AI providers (OpenAI/Gemini/Claude/Grok/Qwen)
- Multilingual prompts (FA/EN)
- Bilingual tutorial (interactive)
- Metrics / audit / adaptive budget / onboarding

--- Design Notes ---
Each phase builds on the previous. Phase 5 connects to Phase 2 (AI suggests selectors) and Phase 3 (AI predicts crawl budget).
