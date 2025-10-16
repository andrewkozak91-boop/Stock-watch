#!/usr/bin/env bash
set -e

# Show the Python version so we can confirm 3.11.x
python --version

# Keep pip tooling fresh
pip install --upgrade pip setuptools wheel

# 100% force wheels for numpy/pandas first
pip install --only-binary=:all: numpy==1.26.4 pandas==2.2.2

# Then install the rest of your deps without re-resolving numpy/pandas
pip install --no-deps -r requirements.txt

# tiny smoke test
python - <<'PY'
import flask, requests, numpy, pandas
print("deps ok")
PY
