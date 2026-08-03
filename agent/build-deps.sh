#!/bin/bash
# Install ARM64 dependencies for AgentCore Runtime deployment
set -e

cd "$(dirname "$0")"
rm -rf _deps
# --no-compile is load-bearing, not an optimization. Newer pip byte-compiles into
# --target by default: a rebuild on 2026-08-03 produced 295 __pycache__ dirs and
# +23MB where the previously-shipping tree had zero, and a __pycache__ under agent/
# is the known cause of an AgentCore Runtime image rejection. The belt-and-braces
# sweep below covers a pip that ignores the flag.
pip3 install --no-cache-dir -r requirements.txt \
    --target _deps \
    --platform manylinux2014_aarch64 \
    --only-binary=:all: \
    --python-version 3.12 \
    --no-compile \
    --quiet
find _deps -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find _deps -name '*.pyc' -delete 2>/dev/null || true
echo "✅ ARM64 dependencies installed in agent/_deps/ ($(du -sh _deps | cut -f1))"
