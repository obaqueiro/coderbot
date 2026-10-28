#!/usr/bin/env bash
# Container healthcheck. Exit 0 healthy, 1 unhealthy.
#
# A daemon thread in src/main.py touches the heartbeat file every poll interval as long as the
# current tick is younger than CODEBOT_HEARTBEAT_MAX_TICK, so a legitimate multi-hour agent
# call stays healthy. A stale heartbeat therefore means the loop is wedged beyond any real
# operation. Past a hard threshold we kill PID 1 so `restart: unless-stopped` takes over
# (Docker does not restart merely-unhealthy containers on its own). PID 1 is docker-init
# (`init: true` in compose), which forwards the signal to main.py and exits with it.
set -u

DATA_DIR="${CODEBOT_DATA_DIR:-/app/data}"
HEARTBEAT="${DATA_DIR}/heartbeat"
STALE="${CODEBOT_HEARTBEAT_STALE:-360}"   # ~3x poll interval: report unhealthy for visibility
HARD="${CODEBOT_HEARTBEAT_HARD:-600}"     # Supervisor deadlines handle legitimate long work.
if [ "${CODEBOT_SELF_HEAL:-off}" != "off" ]; then
  HEARTBEAT="$DATA_DIR/supervisor/heartbeat.json"
  STALE=60
fi

# No heartbeat yet: the process may still be starting (start-period covers this).
[ -f "$HEARTBEAT" ] || exit 0

now="$(date +%s)"
mtime="$(stat -c %Y "$HEARTBEAT" 2>/dev/null || echo 0)"
age=$(( now - mtime ))

if [ "$age" -ge "$HARD" ]; then
  echo "heartbeat stale ${age}s >= ${HARD}s; killing PID 1 to force a restart" >&2
  kill 1 2>/dev/null || true
  exit 1
fi
if [ "$age" -ge "$STALE" ]; then
  echo "heartbeat stale ${age}s >= ${STALE}s" >&2
  exit 1
fi
exit 0
