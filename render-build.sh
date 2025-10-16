#!/usr/bin/env bash
set -e

python --version
pip install --upgrade pip setuptools wheel

# Install everything from requirements (prefer wheels; fail if no wheel for np/pd)
pip install --only-binary=:all: numpy pandas
pip install -r requirements.txt

# tiny smoke to show deps are importable
python - <<'PY'
import flask, requests, numpy, pandas, yfinance
print("deps ok")
PY
