#!/usr/bin/env bash
set -e
export PIP_ONLY_BINARY=:all:
export PIP_NO_BUILD_ISOLATION=1
python -V
pip install --upgrade pip setuptools wheel
pip install --no-cache-dir -r requirements.txt
echo "✅ Render build complete."
