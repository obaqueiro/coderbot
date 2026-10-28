#!/usr/bin/env bash
# Run the unit tests on the production runtime (the image's Python 3.12), without
# .env, ~/.claude or the target repo: the default entrypoint would start the bot.
#   scripts/test.sh                 # rebuild the image, then run every test
#   scripts/test.sh tests.test_x    # extra args go to unittest (default: discover)
#   CODERBOT_TEST_IMAGE=coderbot-codebot:latest scripts/test.sh   # reuse an image
set -euo pipefail
cd "$(dirname "$0")/.."
image="${CODERBOT_TEST_IMAGE:-coderbot-test}"
if [ -z "${CODERBOT_TEST_IMAGE:-}" ]; then
  docker build -q -t "$image" . >/dev/null
fi
if [ "$#" -eq 0 ]; then
  set -- discover -s tests -t .
fi
# A shell is PID 1, not python: some tests exercise healthcheck.sh, whose hard-stale path
# runs `kill 1`. PID 1 without a TERM handler ignores it; python as PID 1 (or docker-init,
# which forwards it) would be killed mid-suite.
exec docker run --rm -v "$PWD":/app -w /app -e HOME=/tmp -e PYTHONDONTWRITEBYTECODE=1 \
  --entrypoint bash "$image" -c 'python3 -m unittest "$@" & child=$!
    while kill -0 "$child" 2>/dev/null; do wait "$child"; status=$?; done; exit "$status"' \
  test.sh "$@"
