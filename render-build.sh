#!/usr/bin/env bash
set -e

echo "🔧 Starting Render build process..."
python -V
pip install --upgrade pip setuptools wheel
pip install -r requirements.txt
echo "✅ Render build complete."
