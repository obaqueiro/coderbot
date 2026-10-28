"""Run the e2e suite and record evidence (Playwright video or Newman report) for a feature."""
import logging
import os
import shutil
import subprocess
import tempfile
import json
from pathlib import Path

import config
import operations
import artifact_manifest
from execution_identity import digest, snapshot
from execution_store import ExecutionStore
import delivery_checkpoint

log = logging.getLogger(__name__)
_recording_passed = False
_recording_output = ""


def valid_media(path: Path) -> bool:
    """Probe media instead of treating nonempty bytes as playable video."""
    path = Path(path)
    if not path.is_file() or not path.stat().st_size or not shutil.which("ffprobe"):
        return False
    try:
        proc = operations.run(["ffprobe", "-v", "error", "-show_streams", "-show_format",
                               "-of", "json", str(path)], timeout=30)
        value = json.loads(proc.stdout)
        return proc.returncode == 0 and float(value.get("format", {}).get("duration", 0)) > 0 and any(
            stream.get("codec_type") == "video" for stream in value.get("streams", []))
    except (OSError, ValueError, TypeError, subprocess.TimeoutExpired, TimeoutError):
        return False


def valid_paths(paths) -> bool:
    return bool(paths) and all(Path(p).is_file() and Path(p).stat().st_size and (
        Path(p).suffix.lower() not in (".mp4", ".webm") or valid_media(Path(p))) for p in paths)


def supplied_artifacts(files: list[Path], content: str) -> list[Path]:
    """Only reuse files attested by a matching successful run manifest."""
    wanted = {p.resolve() for p in files}
    found = []
    root = config.DATA_DIR / "outbox"
    for manifest in root.rglob("evidence-manifest.json") if root.exists() else []:
        try:
            entries = artifact_manifest.load(manifest, snapshot=content)
        except (ValueError, OSError, KeyError):
            continue
        for entry in entries:
            path = Path(entry["path"])
            if path.resolve() in wanted and path not in found and valid_paths([path]):
                found.append(path)
    return found


def report_video_path(report: Path, raw: str) -> Path:
    """Map only the documented container results mount; reject traversal/symlinks."""
    path = Path(raw)
    if ".." in path.parts:
        raise ValueError("Unsafe report video path")
    if path.is_absolute() and path.is_relative_to(Path("/results")):
        path = report.parent / path.relative_to("/results")
    elif not path.is_absolute():
        path = report.parent / path
    resolved = path.resolve()
    if not resolved.is_relative_to(report.parent.resolve() / "test-results"):
        raise ValueError("Video outside current run")
    return resolved


def validate_playwright_report(path: Path) -> bool:
    """Validate selected results, never infer full-suite success from a demo run."""
    try:
        report = json.loads(path.read_text())
        stats = report["stats"]
        if not stats.get("expected") or any(stats.get(k, 0) for k in ("skipped", "unexpected", "flaky")) or report.get("errors"):
            return False
        def cases(suite):
            for spec in suite.get("specs", []):
                for test in spec.get("tests", []):
                    yield spec, test
            for nested in suite.get("suites", []):
                yield from cases(nested)
        tests = [case for suite in report["suites"] for case in cases(suite)]
        if len(tests) != stats["expected"]:
            return False
        root = path.parent.resolve() / "test-results"
        used = set()
        for spec, test in tests:
            results = test.get("results", [])
            if test.get("status") != "expected" or test.get("expectedStatus") != "passed" or len(results) != 1:
                return False
            result = results[0]
            if result.get("status") != "passed" or result.get("retry", 0) or result.get("errors"):
                return False
            videos = [report_video_path(path, a["path"]) for a in result.get("attachments", []) if a.get("contentType") == "video/webm"]
            if not videos:
                return False
            for video in videos:
                if not video.is_relative_to(root) or video in used or not valid_paths([video]):
                    return False
                used.add(video)
        return True
    except (ValueError, KeyError, TypeError, OSError):
        return False


def _text(value):
    return value.decode(errors="replace") if isinstance(value, bytes) else value or ""

E2E_DIR = config.REPO_PATH / "e2e"
# The compose file run.sh brings up (relative to E2E_DIR); torn down by codebot itself
# when a timed-out run.sh could not (see _teardown_stack).
E2E_COMPOSE_FILE = os.environ.get("CODEBOT_E2E_COMPOSE_FILE") or "docker-compose.e2e.yaml"

_SPEC_GLOBS = {
    "playwright": "e2e/tests/*.spec.ts",
    "newman": "e2e/collections/*.postman_collection.json",
}


def reported_specs(output: str) -> list[str]:
    """Extract E2E_SPEC paths without carrying Markdown code delimiters into run.sh."""
    return [
        spec
        for line in output.splitlines() if line.startswith("E2E_SPEC:")
        if (spec := line[len("E2E_SPEC:"):].strip().strip("`"))
    ]


def normalize_spec_files(spec_files: list[str]) -> list[str]:
    """Remove presentation-only Markdown delimiters from persisted spec names."""
    return [spec for value in spec_files if (spec := value.strip().strip("`"))]


def detect_branch_specs(kind: str | None) -> list[str]:
    """Spec/collection files added/changed on this branch vs the base branch (fallback
    for evidence).

    Used when the implementation output never reported E2E_SPEC lines, so the
    evidence step still records evidence from the feature's own e2e tests instead
    of silently producing nothing.
    """
    glob = _SPEC_GLOBS.get(kind, _SPEC_GLOBS["playwright"])
    for base in (f"origin/{config.BASE_BRANCH}", config.BASE_BRANCH):
        proc = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=d", f"{base}...HEAD", "--", glob],
            cwd=config.REPO_PATH, capture_output=True, text=True,
        )
        if proc.returncode == 0:
            specs = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
            if specs:
                log.info("detect_branch_specs: %d spec(s) vs %s: %s", len(specs), base, specs)
            return specs
    log.warning("detect_branch_specs: could not diff against %s", config.BASE_BRANCH)
    return []


def _teardown_stack() -> None:
    """Force-remove the e2e compose stack. run.sh typically tears the stack down via a
    bash `trap cleanup EXIT`, but our subprocess timeout SIGKILLs run.sh and SIGKILL does
    not run traps — so a timed-out run leaks the stack's containers/network, which then
    collide with every later run. Python must do the cleanup the killed shell couldn't.
    No-op when the harness has no such compose file."""
    if not (E2E_DIR / E2E_COMPOSE_FILE).exists():
        log.info("no %s in %s; nothing to tear down", E2E_COMPOSE_FILE, E2E_DIR)
        return
    try:
        proc = subprocess.run(
            ["docker", "compose", "-f", E2E_COMPOSE_FILE, "down", "-v", "--remove-orphans"],
            cwd=E2E_DIR, capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            log.warning("e2e stack teardown rc=%d: %s", proc.returncode, proc.stderr[-500:])
        else:
            log.info("e2e stack torn down after timeout")
    except Exception:
        log.exception("e2e stack teardown failed")


def run_suite() -> tuple[bool, str]:
    """Run the full e2e suite via run.sh. Returns (passed, output tail)."""
    log.info("running full e2e suite in %s", E2E_DIR)
    try:
        proc = operations.run(
            ["./run.sh"], cwd=E2E_DIR,
            timeout=config.E2E_TIMEOUT_SECONDS,
            exclusive=[str(E2E_DIR)],
        )
    except subprocess.TimeoutExpired as exc:
        log.error("e2e suite timed out after %ss; tearing down leaked stack",
                  config.E2E_TIMEOUT_SECONDS)
        _teardown_stack()
        tail = (_text(exc.stdout) + "\n" + _text(exc.stderr))[-6000:]
        return False, f"e2e suite TIMED OUT after {config.E2E_TIMEOUT_SECONDS}s\n{tail}"
    output = (proc.stdout + "\n" + proc.stderr)[-8000:]
    log.info("e2e suite exit=%d", proc.returncode)
    return proc.returncode == 0, output


def record_evidence(spec_files: list[str], kind: str | None) -> list[Path]:
    """Re-run the given spec/collection files with evidence recording forced on;
    return the recorded evidence file(s) — a stitched Playwright video, or the
    generated Newman run report."""
    spec_files = normalize_spec_files(spec_files)
    detected = detect_branch_specs(kind)
    spec_files = list(dict.fromkeys([*spec_files, *detected]))
    if not spec_files:
        log.info("record_evidence: no spec files given — falling back to branch spec detection")
        spec_files = detect_branch_specs(kind)
    if not spec_files:
        log.warning("record_evidence: no spec/collection files found — no evidence will be produced")
        return []
    manifest = os.environ.get("CODEBOT_ARTIFACT_MANIFEST")
    content = snapshot(config.REPO_PATH)
    if manifest and Path(manifest).is_file():
        try:
            entries = artifact_manifest.load(manifest, snapshot=content)
        except (ValueError, OSError, KeyError):
            entries = []
        if entries:
            paths = [Path(entry["path"]) for entry in entries]
            if valid_paths(paths):
                return paths
    store = ExecutionStore(config.DATA_DIR / "execution.sqlite")
    identity = digest({"content": content, "specs": spec_files, "kind": kind})
    recorded = store.list("artifact", "evidence", identity=identity, status="recorded")
    for row in recorded:
        paths = row["data"].get("paths", [])
        if row["data"].get("snapshot") == content and valid_paths(paths) and all(
                row["data"].get("hashes", {}).get(p) == artifact_manifest.file_hash(p) for p in paths):
            return [Path(p) for p in paths]
        store.update("artifact", row, status="retryable", reason="empty, stale or unverified artifacts")
    root = config.DATA_DIR / "outbox/evidence"
    for manifest in root.rglob("evidence-manifest.json") if root.exists() else []:
        try:
            metadata = json.loads(manifest.read_text())
            if [Path(p).name for p in metadata.get("specs", [])] != [Path(p).name for p in spec_files]:
                continue
            entries = artifact_manifest.load(manifest, snapshot=content)
            paths = [Path(entry["path"]) for entry in entries]
            mp4s = [p for p in paths if p.suffix.lower() == ".mp4"]
            if valid_paths(mp4s):
                return mp4s
            clips = [p for p in paths if p.suffix.lower() == ".webm"]
            if valid_paths(clips):
                stitched = stitch_playwright_clips(clips)
                if stitched:
                    artifact_manifest.write(manifest, run_id=metadata["run_id"], snapshot=content, status="pass",
                        artifacts=[{"path": str(p), "report": metadata.get("report")} for p in [*clips, stitched]])
                    _manifest_specs(manifest, spec_files)
                    return [stitched]
        except (ValueError, KeyError, OSError):
            continue
    # Mark the attempt before running: interruptions do not cause endless re-recording.
    record_id = store.put("artifact", task_id="evidence", identity=identity, status="running", data={"paths": []})
    if kind == "newman":
        paths = _record_newman_report(spec_files)
    else:
        paths = _record_playwright_video(spec_files)
    store.put("artifact", task_id="evidence", identity=identity, id=record_id,
              status="recorded" if paths else "retryable",
              data={"paths": [str(p) for p in paths], "snapshot": content,
                    "hashes": {str(p): artifact_manifest.file_hash(p) for p in paths}})
    return paths


def _run_recording(specs: list[str], extra_args: list[str]) -> str:
    """One run.sh invocation with video forced on; best-effort (clips are harvested
    after). Returns the run's output tail for diagnostics."""
    global _recording_passed, _recording_output
    _recording_passed = False
    _recording_output = ""
    try:
        proc = operations.run(
            ["./run.sh", *specs, *extra_args], cwd=E2E_DIR,
            timeout=config.E2E_TIMEOUT_SECONDS,
            exclusive=[str(E2E_DIR)],
            # Harnesses may opt in to Playwright's video setting via PW_VIDEO.
            # Preserve any other repository-configured environment variables.
            env={**os.environ, "PW_VIDEO": "on"},
        )
    except subprocess.TimeoutExpired:
        # Evidence is best-effort: tear down the leaked stack and harvest whatever
        # clips were produced before the timeout rather than crashing the PR flow.
        log.error("evidence run timed out after %ss; tearing down leaked stack",
                  config.E2E_TIMEOUT_SECONDS)
        _teardown_stack()
        return f"evidence run TIMED OUT after {config.E2E_TIMEOUT_SECONDS}s"
    if proc.returncode != 0:
        log.warning("evidence run exit=%d; stderr tail:\n%s", proc.returncode, proc.stderr[-1500:])
    _recording_passed = proc.returncode == 0
    _recording_output = proc.stdout + proc.stderr
    return (proc.stdout + proc.stderr)[-1500:]


def _record_playwright_video(spec_files: list[str], *, full_suite: bool = False) -> list[Path]:
    """Re-run the given specs with video forced on; return the recorded .webm files.

    Prefers the dedicated `@evidence` demo test (the IMPLEMENT prompt requires one per
    feature): it is written to be watchable, so recording only it keeps loosely related
    tests out of the delivered video. If the filtered run is incomplete or rejected,
    generate the complete selected specs and validate that fresh run before delivery.
    """
    def clips() -> dict[Path, tuple]:
        files = set()
        root = E2E_DIR / "test-results"
        if root.exists():
            files.update(p for p in root.rglob("*.webm") if "html" not in p.relative_to(root).parts)
        return {p: (p.stat().st_mtime_ns, p.stat().st_size, artifact_manifest.file_hash(p)) for p in files}

    before = clips()
    content = snapshot(config.REPO_PATH)
    specs = [Path(s).name for s in spec_files]
    log.info("recording evidence: re-running %s of %s with video on",
             "complete specs" if full_suite else "@evidence tests", specs)
    tail = _run_recording(specs, [] if full_suite else ["--grep", "@evidence"])
    after = clips()
    new = sorted(p for p, identity in after.items() if before.get(p) != identity)
    reports = set()
    for p in new:
        for parent in p.parents:
            candidate = parent / "results.json"
            if candidate.is_file():
                reports.add(candidate)
                break
    if not reports or not all(validate_playwright_report(p) for p in reports):
        _passed = False
    else:
        _passed = _recording_passed
    diagnostics_dir = config.DATA_DIR / "outbox/evidence"
    diagnostics_dir.mkdir(parents=True, exist_ok=True)
    run_id = digest([content, [(str(p), after[p]) for p in new], tail])
    run_dir = diagnostics_dir / run_id
    run_dir.mkdir(exist_ok=True)
    report = run_dir / "recording.log"
    report.write_text(_recording_output or tail)
    diagnostic = {"run_id": run_id, "snapshot": content, "report": str(report),
                  "status": "pass" if _passed else "retryable",
                  "stage": "validation" if new and not _passed else "capture",
                  "clips": [str(p) for p in new], "detail": tail}
    (run_dir / "diagnostic.json").write_text(json.dumps(diagnostic))
    if not _passed:
        log.warning("evidence %s failed; %d captured clip(s) preserved. Report: %s",
                    diagnostic["stage"], len(new), report)
        if not full_suite and snapshot(config.REPO_PATH) == content:
            log.info("evidence incomplete/rejected; generating complete selected specs before delivery")
            return _record_playwright_video(spec_files, full_suite=True)
        return []
    if not new:
        log.warning("no NEW .webm files after evidence run (before=%d, after=%d); "
                    "check PW_VIDEO wiring and test-results mount, and whether "
                    "the spec skipped itself (bare `./run.sh <spec>` invocation, no extra "
                    "flags/env). Run output tail:\n%s", len(before), len(after), tail)
    videos = new
    # Only selected report videos, not unrelated current-run files, are candidates.
    selected = set()
    demo_specs = set()
    for report_path in reports:
        def attachments(suite):
            for spec in suite.get("specs", []):
                if "@evidence" not in spec.get("title", ""):
                    continue
                demo_specs.add(Path(spec.get("file", suite.get("file", ""))).name)
                for test in spec.get("tests", []):
                    for result in test.get("results", []):
                        for item in result.get("attachments", []):
                            if item.get("contentType") == "video/webm":
                                yield report_video_path(report_path, item["path"])
            for nested in suite.get("suites", []):
                yield from attachments(nested)
        selected.update(p for suite in json.loads(report_path.read_text())["suites"] for p in attachments(suite))
    if not set(specs).issubset(demo_specs):
        diagnostic.update(status="retryable", stage="implementation",
                          detail="Missing @evidence demos for: " + ", ".join(sorted(set(specs) - demo_specs)))
        (run_dir / "diagnostic.json").write_text(json.dumps(diagnostic))
        log.warning(diagnostic["detail"])
        return []
    kept, hashes = [], set()
    for v in videos:
        if v.resolve() in selected and valid_media(v) and (hash_value := artifact_manifest.file_hash(v)) not in hashes:
            kept.append(v)
            hashes.add(hash_value)
    for v in kept:
        log.info("evidence clip: %s (%d bytes)", v, v.stat().st_size)
    if {v.resolve() for v in kept} != selected:
        diagnostic.update(status="retryable", stage="validation",
                          detail="Not all selected demo videos are fresh, unique and playable")
        (run_dir / "diagnostic.json").write_text(json.dumps(diagnostic))
        log.warning(diagnostic["detail"])
        if not full_suite and snapshot(config.REPO_PATH) == content:
            return _record_playwright_video(spec_files, full_suite=True)
        return []
    dropped = [v for v in videos if v.stat().st_size == 0]
    if dropped:
        log.warning("dropped %d zero-byte clip(s): %s", len(dropped), [str(v) for v in dropped])
    if not kept:
        log.info("_record_playwright_video: no clips to stitch")
        return []
    if snapshot(config.REPO_PATH) != content:
        log.warning("implementation changed during evidence capture; recording cannot be published")
        return []
    artifact_manifest.write(run_dir / "evidence-manifest.json", run_id=run_id,
        snapshot=content, status="pass", artifacts=[{"path": str(p), "report": str(report)} for p in kept])
    _manifest_specs(run_dir / "evidence-manifest.json", spec_files)
    store = ExecutionStore(config.DATA_DIR / "execution.sqlite")
    identity = digest([(str(p), artifact_manifest.file_hash(p)) for p in kept])
    converted = delivery_checkpoint.step(store, "evidence", identity, "CONVERT",
        lambda: str(result) if (result := _stitch_to_mp4(kept)) else None,
        validate=lambda p: bool(p) and valid_media(Path(p)))
    stitched = Path(converted) if converted else None
    if stitched:
        artifact_manifest.write(run_dir / "evidence-manifest.json", run_id=run_id,
            snapshot=content, status="pass", artifacts=[{"path": str(p), "report": str(report)} for p in [*kept, stitched]])
        _manifest_specs(run_dir / "evidence-manifest.json", spec_files)
        log.info("_record_playwright_video: returning stitched %s (%d bytes)", stitched, stitched.stat().st_size)
        return [stitched]
    log.warning("_record_playwright_video: stitching unavailable/failed; returning %d raw webm clip(s)", len(kept))
    return kept


def _manifest_specs(path, specs):
    value = json.loads(path.read_text())
    value["specs"] = specs
    path.write_text(json.dumps(value))


def _stitch_to_mp4(clips: list[Path]) -> Path | None:
    """Concatenate .webm clips into one H.264 .mp4 via ffmpeg. None on failure.

    Each clip is scaled/padded to a common 1280x720 frame so clips recorded at
    different viewport sizes concatenate cleanly.

    ffmpeg writes to a scratch dir under /tmp and the finished file is moved into
    config.DATA_DIR afterwards — never into e2e/test-results (created by the
    root-running playwright container, so the codebot uid cannot write there on a
    Linux host; Docker Desktop on macOS masks this), and not straight into data/
    either (that failed with access denied on a deployed host). /tmp is always
    writable for the codebot uid; if the move fails the /tmp file is returned rather
    than losing the evidence.
    """
    if not shutil.which("ffmpeg"):
        log.warning("ffmpeg not found; cannot stitch/convert evidence videos")
        return None
    scratch_dir = Path(tempfile.mkdtemp(prefix="codebot-evidence-", dir="/tmp"))
    identity = digest([artifact_manifest.file_hash(p) for p in clips])
    dest_dir = config.DATA_DIR / "outbox/evidence" / identity
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "evidence.mp4"
    if valid_media(dest):
        shutil.rmtree(scratch_dir)
        return dest
    out = scratch_dir / "evidence.mp4"
    w, h = 1280, 720
    inputs: list[str] = []
    filters = []
    for i, clip in enumerate(clips):
        inputs += ["-i", str(clip.resolve())]
        filters.append(
            f"[{i}:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30[v{i}]"
        )
    labels = "".join(f"[v{i}]" for i in range(len(clips)))
    filter_complex = ";".join(filters) + f";{labels}concat=n={len(clips)}:v=1:a=0[out]"
    cmd = ["ffmpeg", "-y", *inputs, "-filter_complex", filter_complex,
           "-map", "[out]", "-c:v", "libx264", "-pix_fmt", "yuv420p",
           "-movflags", "+faststart", str(out)]
    log.info("stitching %d clip(s) into %s via ffmpeg (scratch dir %s)",
             len(clips), out.name, scratch_dir)
    returned_from_tmp = False  # the scratch dir only survives if we return its file
    try:
        try:
            proc = operations.run(cmd, timeout=config.E2E_TIMEOUT_SECONDS)
        except (subprocess.TimeoutExpired, TimeoutError, OSError):
            log.warning("evidence conversion unavailable or timed out")
            return None
        if proc.returncode != 0 or not valid_media(out):
            log.warning("ffmpeg stitch failed rc=%d; stderr tail:\n%s",
                        proc.returncode, proc.stderr[-1500:])
            return None
        try:
            shutil.move(str(out), dest)  # move handles cross-device (/tmp -> data)
        except OSError:
            log.exception("could not move %s into %s; returning the /tmp file",
                          out, config.DATA_DIR)
            returned_from_tmp = True
            return out
        return dest
    finally:
        if not returned_from_tmp:
            shutil.rmtree(scratch_dir, ignore_errors=True)


def stitch_playwright_clips(files: list[Path]) -> Path | None:
    """Stitch non-empty Playwright clips supplied by an agent into one MP4."""
    clips = [file for file in files if file.suffix.lower() == ".webm" and file.exists()
             and file.stat().st_size > 0]
    return _stitch_to_mp4(clips) if clips else None


def _record_newman_report(spec_files: list[str]) -> list[Path]:
    """Re-run the given Postman collections; return the newest generated report file.

    Newman has no UI to record, so the evidence analog is whatever report file
    e2e/run.sh's Newman invocation writes (e.g. an HTML report) — same
    before/after-diff pattern as the Playwright video path, without stitching.
    """
    results_dir = E2E_DIR / "test-results"
    before = set(results_dir.rglob("*.html")) if results_dir.exists() else set()
    collections = [Path(s).name for s in spec_files]
    log.info("recording Newman evidence: re-running collection(s) %s", collections)
    try:
        proc = subprocess.run(
            ["./run.sh", *collections], cwd=E2E_DIR, capture_output=True, text=True,
            timeout=config.E2E_TIMEOUT_SECONDS,
        )
        tail = (proc.stdout + proc.stderr)[-1500:]
        if proc.returncode != 0:
            log.warning("Newman evidence run exit=%d; stderr tail:\n%s",
                        proc.returncode, proc.stderr[-1500:])
    except subprocess.TimeoutExpired:
        log.error("Newman evidence run timed out after %ss; tearing down leaked stack",
                  config.E2E_TIMEOUT_SECONDS)
        _teardown_stack()
        tail = "TIMED OUT"
    after = set(results_dir.rglob("*.html")) if results_dir.exists() else set()
    new = sorted(after - before)
    if not new:
        log.warning("no NEW report file after Newman evidence run (before=%d, after=%d); "
                    "check the reporter wiring and test-results mount, and whether the "
                    "collection skipped itself (bare `./run.sh <collection>` invocation, no "
                    "extra flags/env). Run output tail:\n%s",
                    len(before), len(after), tail)
        return []
    newest = max(new, key=lambda p: p.stat().st_mtime)
    if newest.stat().st_size == 0:
        log.warning("newest Newman report %s is zero bytes; dropping it", newest)
        return []
    log.info("evidence report: %s (%d bytes)", newest, newest.stat().st_size)
    return [newest]
