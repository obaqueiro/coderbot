"""Provider-neutral delivery of durable supervisor events through the façade."""
import json
import fcntl
import logging
import os


def flush():
    import config
    folder = config.DATA_DIR / "supervisor/notifications"
    if not folder.exists():
        return
    # Worker and supervisor bridge may both retry, but only one sends at a time.
    with (folder.parent / "notifications.lock").open("a") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        # Rotate attempts even if a provider hangs and the bridge is killed.
        for path in sorted(folder.glob("*.json"), key=lambda path: path.stat().st_mtime):
            try:
                os.utime(path, None)
                _deliver(path)
            except (Exception, SystemExit):
                logging.getLogger(__name__).warning("Repair notification pending: %s", path.name)


def _deliver(path):
    import config
    import conversation
    import message_delivery
    import phase_checkpoint
    event = json.loads(path.read_text())
    if event.get("channel") not in (None, config.COMM_CHANNEL):
        return  # Never reroute private incident notifications to another channel.
    # Isolate supervisor receipts from the worker's generic task retry loop.
    state = {"execution_task_id": "supervisor:" + (event.get("task_id") or "runtime"),
             "item": "Coderbot repair", "thread_id": event.get("conversation")}
    body = "Coderbot: " + event["kind"].replace("_", " ") + "\n" + event.get("detail", "")
    if event.get("files"):
        body += "\nChanged files:\n" + "\n".join(event["files"])
    receipt = message_delivery.deliver(phase_checkpoint.store(), state, config.REPO_PATH,
        config.DATA_DIR, conversation._backend(), "Coderbot self-healing", body,
        event.get("conversation"), identity="supervisor:" + event["id"])
    if receipt["complete"]:
        path.rename(path.with_suffix(".delivered"))


if __name__ == "__main__":
    flush()
