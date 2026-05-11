#!/bin/bash
# Install ARM64 dependencies for AgentCore Runtime deployment
set -e

cd "$(dirname "$0")"
rm -rf _deps
pip3 install --no-cache-dir -r requirements.txt \
    --target _deps \
    --platform manylinux2014_aarch64 \
    --only-binary=:all: \
    --python-version 3.12 \
    --quiet
echo "✅ ARM64 dependencies installed in agent/_deps/ ($(du -sh _deps | cut -f1))"
