#!/usr/bin/env bash
set -e

echo "Python version:"
python --version

echo "Upgrading pip..."
python -m pip install --upgrade pip

echo "Installing requirements..."
pip install -r requirements.txt

echo "Build complete."
