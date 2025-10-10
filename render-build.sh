#!/usr/bin/env bash
set -e

python --version
python -m pip install --upgrade pip wheel setuptools

# Heavy libs via wheels (works on Python 3.12)
pip install --only-binary=:all: --prefer-binary \
  numpy==1.26.4 pandas==2.2.2

# Rest from requirements
pip install -r requirements.txt

echo "Build complete."
