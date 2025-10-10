#!/usr/bin/env bash
set -e

echo "Python:"
python --version

# Keep pip modern
python -m pip install --upgrade pip wheel setuptools

# 1) Preinstall binary wheels for the heavy libs (NO compiling)
#    --only-binary=:all: forces wheels, --prefer-binary picks a wheel if multiple match
pip install --only-binary=:all: --prefer-binary \
    numpy==1.26.4 pandas==2.2.2

# 2) Install the rest normally (they’re pure-python)
pip install -r requirements.txt

echo "Build complete."
