#!/usr/bin/env bash
# Run Doorman integration tests from a clean slate.
# Usage: ./tests/integration/run.sh
set -euo pipefail
cd "$(dirname "$0")"

echo "==> Tearing down previous containers..."
docker-compose down -v 2>/dev/null || true

echo "==> Cleaning HA runtime state..."
find ha_config -mindepth 1 \
  ! -name 'configuration.yaml' \
  ! -name '.gitignore' \
  ! -name 'blueprints' \
  ! -path 'ha_config/blueprints/*' \
  -exec rm -rf {} + 2>/dev/null || true

echo "==> Building and starting services..."
docker-compose up -d --build --wait

echo "==> Running integration tests..."
docker-compose --profile test run --rm test-runner
EXIT_CODE=$?

echo "==> Tearing down..."
docker-compose down -v

exit $EXIT_CODE
