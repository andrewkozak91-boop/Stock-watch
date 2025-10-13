#!/usr/bin/env bash
set -euo pipefail

echo "=== Upgrading pip and base tools ==="
python -m pip install --upgrade pip setuptools wheel

echo "=== Installing wheel-based numpy/pandas first ==="
pip install --only-binary=:all: numpy==2.0.2 pandas==2.2.3

echo "=== Installing all remaining requirements ==="
pip install -r requirements.txt
