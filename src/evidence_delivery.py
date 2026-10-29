"""Controller-owned, restart-safe evidence publication independent of product phases."""
from pathlib import Path

import artifact_manifest
import delivery_checkpoint
import drive_client
import evidence
import config
import json
import operations
from execution_identity import digest, snapshot


def implementation_snapshot(repo):
    """Archiving planning documents must not invalidate unchanged demo code/tests."""
    names = operations.tool(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=repo, check=True, timeout=30).stdout.split("\0")
    return snapshot(repo, inputs=[name for name in names if name and not name.startswith("openspec/")] or ["__no_implementation__"])


def register_verified(state, repo, files):
    """Capture artifacts only after the controller accepts the verification gate."""
    videos = [Path(p).resolve() for p in files if Path(p).suffix.lower() == ".mp4"]
    root = (config.DATA_DIR / "outbox").resolve()
    videos = [p for p in videos if p.is_relative_to(root) and evidence.valid_media(p)]
    if not videos:
        return
    content = implementation_snapshot(repo)
    directory = root / "evidence" / digest([state.get("pr_url"), content])
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / "verified-delivery.json"
    artifact_manifest.write(manifest, run_id=digest([content, [str(p) for p in videos]]),
        snapshot=content, status="pass", artifacts=[{"path": str(p)} for p in videos])
    data = json.loads(manifest.read_text())
    data["pr_url"] = state.get("pr_url")
    manifest.write_text(json.dumps(data))


def reusable_files(state, repo):
    root = config.DATA_DIR / "outbox/evidence"
    manifests = list(root.rglob("verified-delivery.json")) if root.exists() else []
    if not manifests:
        return []
    content = implementation_snapshot(repo)
    for manifest in manifests:
        try:
            metadata = json.loads(manifest.read_text())
            if metadata.get("pr_url") != state.get("pr_url"):
                continue
            files = [Path(entry["path"]) for entry in artifact_manifest.load(manifest, snapshot=content)]
            if evidence.valid_paths(files):
                return files
        except (ValueError, OSError, KeyError):
            continue
    return []


def reconcile(state, repo, pr_body):
    """Adopt only attested current videos whose remote file and PR link agree."""
    root = config.DATA_DIR / "outbox/evidence"
    candidates = list(root.rglob("verified-delivery.json")) if root.exists() else []
    if not candidates:
        return None
    content = implementation_snapshot(repo)
    for manifest in candidates:
        try:
            metadata = json.loads(manifest.read_text())
            if metadata.get("pr_url") != state.get("pr_url"):
                continue
            entries = artifact_manifest.load(manifest, snapshot=content)
            for entry in entries:
                video = Path(entry["path"])
                if not evidence.valid_media(video):
                    continue
                # External delivery receipts are inputs, not authority: recheck Drive.
                receipt_path = video.with_suffix(".delivery.json")
                if not receipt_path.is_file():
                    receipt_path = video.parent / "drive-entrega.json"  # Legacy agent receipt.
                if not receipt_path.is_file():
                    continue
                receipt = json.loads(receipt_path.read_text())
                if receipt.get("sha256") != entry["hash"]:
                    continue
                file_id = receipt["file"]["id"]
                url = receipt["file"]["webViewLink"]
                if url not in pr_body:
                    continue
                result = drive_client.verify_existing(file_id, video, config.DRIVE_FOLDER_ID or receipt["folder_id"])
                if result.get("status") == "complete" and result.get("url") == url:
                    state["evidence_url"] = url
                    state["evidence_delivery"] = result
                    return result
        except (ValueError, OSError, KeyError, TypeError):
            continue
    return None


def publish(store, state, repo, video):
    """Common publication step used by every controller delivery path."""
    if not config.EVIDENCE_UPLOAD:
        return {"status": "blocked", "stage": "upload", "error": "Evidence upload is disabled"}
    if not evidence.valid_media(video):
        return {"status": "retryable", "stage": "conversion", "error": "Video is not playable"}
    identity = digest([state.get("pr_url"), artifact_manifest.file_hash(video), "verified-upload-v2",
                       config.DRIVE_FOLDER_ID])
    task = store.task_identity(state, repo)
    name = f"{state.get('branch', 'evidence')}-{identity[:12]}.mp4"
    uploaded = delivery_checkpoint.step(store, task, identity, "UPLOAD",
        lambda: drive_client.publish_evidence(video, name, upload_only=True),
        validate=lambda r: r.get("status") == "complete" and bool(r.get("id")) and bool(r.get("url")))
    if uploaded.get("status") != "complete":
        return uploaded
    access_identity = digest([identity, uploaded["id"], "inherited-v2"])
    result = delivery_checkpoint.step(store, task, access_identity, "ACCESS",
        lambda: drive_client.publish_evidence(video, name, uploaded=uploaded),
        validate=lambda r: r.get("status") == "complete" and r.get("access") and bool(r.get("url")))
    if result.get("status") == "complete":
        register_verified(state, repo, [video])
        receipt = {"file": {"id": result["id"], "webViewLink": result["url"]},
                   "folder_id": result["folder_id"], "sha256": artifact_manifest.file_hash(video)}
        # Canonical receipt accompanies the artifact without changing media bytes.
        video.with_suffix(".delivery.json").write_text(json.dumps(receipt))
    return result


def prepare(store, state, repo, *, identity=None):
    """Shared recording/reuse checkpoint, independent of transport and PR state."""
    content = snapshot(repo) if identity is None else identity
    task = store.task_identity(state, repo)
    identity = identity or digest([state.get("pr_url"), content, state.get("e2e_specs", []), "verified-evidence-v1"])
    return delivery_checkpoint.step(store, task, identity, "RECORD",
        lambda: [str(p) for p in (reusable_files(state, repo) or
            evidence.record_evidence(state.get("e2e_specs", []), state.get("e2e_kind")))],
        validate=evidence.valid_paths)


def review_files(store, state, repo, *, identity=None):
    """No empty/partial video delivery: reuse capture and retry MP4 conversion."""
    paths = prepare(store, state, repo, identity=identity)
    if not evidence.valid_paths(paths):
        return []
    if state.get("e2e_kind") == "newman":
        return [Path(p) for p in paths if Path(p).suffix.lower() == ".html"]
    videos = [Path(p) for p in paths if Path(p).suffix.lower() == ".mp4"]
    if videos:
        return videos
    clips = [Path(p) for p in paths if Path(p).suffix.lower() == ".webm"]
    if not clips:
        return []
    converted = delivery_checkpoint.step(store, store.task_identity(state, repo),
        digest([snapshot(repo), [(str(p), artifact_manifest.file_hash(p)) for p in clips]]),
        "CONVERT", lambda: str(p) if (p := evidence.stitch_playwright_clips(clips)) else None,
        validate=lambda p: bool(p) and evidence.valid_media(Path(p)))
    return [Path(converted)] if converted and evidence.valid_media(Path(converted)) else []


def recovery_details(repo):
    """Actual current-run logs, not a misleading cleanup stderr tail."""
    content = snapshot(repo)
    root = config.DATA_DIR / "outbox/evidence"
    details = []
    for path in sorted(root.rglob("diagnostic.json") if root.exists() else [],
                       key=lambda p: p.stat().st_mtime_ns, reverse=True):
        try:
            value = json.loads(path.read_text())
            if value.get("snapshot") != content:
                continue
            details.append({"stage": value.get("stage"), "report": value.get("report"),
                            "clips": value.get("clips", []), "detail": value.get("detail", "")})
            if len(details) == 2:
                break
        except (ValueError, OSError):
            continue
    return json.dumps(details, ensure_ascii=False) if details else "No current recording report: inspect spec detection, missing demo tests and recording configuration."


def sync(store, state, repo, url, sync_pr):
    return delivery_checkpoint.step(store, store.task_identity(state, repo),
        digest([state["pr_url"], url]), "PR_SYNC", lambda: _sync(sync_pr, state, url), validate=bool)


def notification(store, state, repo, identity, send):
    """Persist only confirmed transport outcomes; provider receipts reconcile retries."""
    def action():
        send()
        if state.get("last_delivery", {}).get("complete") is False:
            raise RuntimeError("Evidence notification is not fully confirmed")
        return True
    return delivery_checkpoint.step(store, store.task_identity(state, repo), identity, "NOTIFY", action, validate=bool)


def deliver(store, state, repo, *, sync_pr, notify, operations=None, read_pr=None):
    operations = operations or ["record", "convert", "upload", "pr_sync", "notify"]
    if read_pr and state.get("pr_url"):
        adopted = reconcile(state, repo, read_pr(state))
        if adopted:
            task = store.task_identity(state, repo)
            delivery_checkpoint.step(store, task, digest([state["pr_url"], adopted["url"]]), "NOTIFY",
                lambda: _notify(notify, state, adopted["url"]), validate=bool)
            return {**adopted, "stage": "delivered"}
    content = snapshot(repo)
    task = store.task_identity(state, repo)
    identity = digest([state.get("pr_url"), content, state.get("e2e_specs", []), "verified-evidence-v1"])
    paths = prepare(store, state, repo, identity=identity)
    if not evidence.valid_paths(paths):
        return {"status": "retryable", "stage": "capture/validation", "error": "No valid current evidence recorded"}
    if not any(op in operations for op in ("convert", "upload", "pr_sync")):
        delivery_checkpoint.step(store, task, identity, "NOTIFY_FILES",
            lambda: _notify_files(notify, state, paths), validate=bool)
        return {"status": "complete", "stage": "delivered", "files": paths}
    videos = [Path(p) for p in paths if Path(p).suffix.lower() in (".mp4", ".webm")]
    if not videos:
        return {"status": "not_applicable", "stage": "video", "files": paths}
    def convert():
        result = next((p for p in videos if p.suffix.lower() == ".mp4"), None)
        result = result or evidence.stitch_playwright_clips(videos)
        return str(result) if result else None
    converted = delivery_checkpoint.step(store, task, identity, "CONVERT", convert,
        validate=lambda p: bool(p) and p != "None" and evidence.valid_media(Path(p)))
    if not converted or not evidence.valid_media(Path(converted)):
        return {"status": "retryable", "stage": "conversion", "error": "MP4 conversion or media verification failed"}
    if snapshot(repo) != content:
        return {"status": "retryable", "stage": "provenance", "error": "Implementation changed before publication"}
    video = Path(converted)
    if not any(op in operations for op in ("upload", "pr_sync")):
        delivery_checkpoint.step(store, task, identity, "NOTIFY_MP4",
            lambda: _notify_files(notify, state, [str(video)]), validate=bool)
        return {"status": "complete", "stage": "delivered", "files": [str(video)]}
    published = publish(store, state, repo, video)
    if published.get("status") != "complete":
        return published
    state["evidence_url"] = published["url"]
    upload_identity = digest([identity, artifact_manifest.file_hash(video), published["url"]])
    if "pr_sync" in operations:
        synced = sync(store, state, repo, published["url"], sync_pr)
        if not synced:
            return {"status": "retryable", "stage": "pr_sync", "url": published["url"]}
    notification(store, state, repo, upload_identity, lambda: _notify(notify, state, published["url"]))
    return {**published, "stage": "delivered"}


def _sync(sync, state, url):
    sync(state, url)
    return True


def _notify(notify, state, url):
    notify(state, "evidence delivered", f"Video: {url}\nPR: {state['pr_url']}")
    if not state.get("thread_id"):
        raise RuntimeError("Evidence link message delivery returned no task thread")
    return True


def _notify_files(notify, state, paths):
    notify(state, "evidence recorded", "Validated implementation evidence attached.", [Path(p) for p in paths])
    if state.get("last_delivery", {}).get("complete") is False:
        raise RuntimeError("Evidence attachments were not fully delivered")
    return True
