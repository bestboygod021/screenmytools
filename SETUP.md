# Local Setup — FullPage Capture Bot

## Quick start (Linux / macOS)
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
python -m app.cli journey --recipe shop --url https://example.com --out ./shots

## Windows
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium
python main.py

## Test (no browser needed for unit tests)
LD_LIBRARY_PATH=/tmp/qtshim .venv/bin/python -m pytest tests/test_engine.py -q

## Preview server
python -m http.server 8080 --bind 0.0.0.0 --directory .
