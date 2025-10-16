#!/usr/bin/env bash
set -e
python --version
pip install --upgrade pip setuptools wheel
pip install -r requirements.txt
python - <<'PY'
import flask, requests
print("Dependencies OK. Deploying Stock Game 7.5.1...")
PY
