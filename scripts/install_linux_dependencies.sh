#!/usr/bin/env bash
# Public-only installation; never call with private credentials in the environment.
set -euo pipefail
python scripts/verify_linux_dependencies.py --locks-only
research_python_dir="${RUNNER_TEMP:?}/ashare-private-python"
test ! -e "$research_python_dir"
python -m venv "$research_python_dir"
"$research_python_dir/bin/python" -m pip --isolated install --disable-pip-version-check \
  --no-cache-dir --index-url https://pypi.org/simple --require-hashes --only-binary=:all: \
  -r requirements-linux-bootstrap.lock
"$research_python_dir/bin/python" -m pip --isolated install --disable-pip-version-check \
  --no-cache-dir --index-url https://pypi.org/simple --require-hashes --no-build-isolation \
  -r requirements-linux.lock
"$research_python_dir/bin/python" -m pip --isolated check
"$research_python_dir/bin/python" scripts/verify_linux_dependencies.py
# No editable build is required: the wrapper fixes PYTHONPATH to backend/ and
# launches the worker through this isolated interpreter's -m module entrypoint.
