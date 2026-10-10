#!/bin/bash
# Health probe: .venv must exist with python
if [[ ! -f ".venv/bin/python" ]] && [[ ! -f ".venv/bin/python3" ]]; then
    echo "WARNING: .venv missing. Use system python3 + install ruff."
    exit 1
fi
echo "ENV OK"
