#!/bin/bash
# Azure Scheduled Events watcher. A Spot eviction gives 30 seconds of warning, so this
# polls often, stops the container as soon as a notice lands, and then acknowledges the
# event so Azure proceeds immediately instead of waiting out the rest of the window.
#
# Nothing has to be detached or unmounted: the eviction policy is Deallocate, so the
# disks stay with the VM and the next start resumes the task.
set -u
IMDS="http://169.254.169.254/metadata/scheduledevents?api-version=2020-07-01"

while true; do
  body=$(curl -fsS -H 'Metadata: true' "$IMDS" 2>/dev/null || true)
  if [ -n "$body" ]; then
    # Preempt is the Spot eviction; Terminate and Redeploy also take the VM away.
    event=$(jq -r '.Events[]? | select(.EventType=="Preempt" or .EventType=="Terminate" or .EventType=="Redeploy") | .EventId' <<<"$body" 2>/dev/null | head -n1)
    if [ -n "$event" ]; then
      type=$(jq -r --arg id "$event" '.Events[] | select(.EventId==$id) | .EventType' <<<"$body")
      logger -t coderbot-spot-watch "scheduled event $type ($event); stopping coderbot"
      systemctl stop coderbot.service
      sync
      logger -t coderbot-spot-watch "stopped; acknowledging $event"
      curl -fsS -H 'Metadata: true' -H 'Content-Type: application/json' \
        -X POST -d "{\"StartRequests\":[{\"EventId\":\"$event\"}]}" "$IMDS" >/dev/null 2>&1 || true
      exit 0
    fi
  fi
  sleep 2
done
