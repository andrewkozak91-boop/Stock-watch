#!/usr/bin/env bash
set -e

python --version
pip install --upgrade pip setuptools wheel
pip install -r requirements.txt

# tiny smoke to show we installed
python - <<'PY'
import flask, requests
print("deps ok")
PY
