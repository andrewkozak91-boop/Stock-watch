#!/usr/bin/env bash
set -e

python --version
pip install --upgrade pip setuptools wheel
pip install -r requirements.txt

# tiny smoke test to confirm dependencies installed correctly
python - <<'PY'
import flask, requests
print("Dependencies installed successfully.")
PY
