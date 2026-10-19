"""Serial inference and official grading, with per-instance resume checkpoints."""
from __future__ import annotations

from contextlib import contextmanager
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import uuid

from .common import (DEFAULT_INSTANCE, ENV_DEFAULTS, HARNESS_VERSION, ROOT,
                     TERMINAL, bytes_digest, check_row, digest, driver_digest,
                     git_metadata, latest_attempt, now, read_json, source_archive,
                     summarize, write_json)
from .containers import (cleanup_run_containers, docker_client, ensure_image,
                         infer, test_spec)


def say(text):
    print(text, file=sys.stderr, flush=True)


def require_harness():
    version = importlib.metadata.version("swebench")
    if version != HARNESS_VERSION:
        raise ValueError(f"Expected swebench=={HARNESS_VERSION}, got {version}. "
                         "Install benchmarks/swebench/requirements.txt in a separate environment.")


@contextmanager
def working_directory(path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def select_dataset(args):
    if args.dataset_file:
        source = args.dataset_file.expanduser().resolve()
        rows = read_json(source)
        metadata = {"file": str(source), "sha256": digest(source), "split": "test"}
    else:
        from datasets import load_dataset
        from huggingface_hub import HfApi
        revision = HfApi().dataset_info(args.dataset, revision=args.revision).sha
        dataset = load_dataset(args.dataset, split="test", revision=revision)
        rows = list(dataset)
        metadata = {"name": args.dataset, "revision": revision, "split": "test",
                    "fingerprint": dataset._fingerprint}
    if not isinstance(rows, list) or not rows:
        raise ValueError("Dataset must be a nonempty JSON list of official instances.")
    by_id = {}
    for row in rows:
        check_row(row)
        if row["instance_id"] in by_id:
            raise ValueError("Duplicate dataset instance_id.")
        by_id[row["instance_id"]] = row
    if args.all:
        ids = sorted(by_id)
    elif args.sample:
        # Deterministic across Python versions; spread the pilot over repositories.
        ids = sorted(by_id, key=lambda key: bytes_digest(f"{args.seed}:{key}".encode()))[:args.sample]
        if len(ids) != args.sample:
            raise ValueError("Sample size exceeds dataset size.")
    else:
        ids = args.instance_ids or [DEFAULT_INSTANCE]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate requested instance_id.")
        missing = set(ids) - set(by_id)
        if missing:
            raise ValueError(f"Unknown instance IDs: {', '.join(sorted(missing))}")
    metadata.update(total_instances=len(rows), selected=len(ids),
                    selection_seed=args.seed if args.sample else None)
    return [by_id[key] for key in ids], metadata


def initialize(args, directory):
    say("Loading and snapshotting benchmark instances...")
    rows, dataset = select_dataset(args)
    snapshot = source_archive()
    environment = {key: os.environ.get(key, default) for key, default in ENV_DEFAULTS.items()}
    environment["GOAL_EVALUATOR_MODEL"] = os.environ.get("GOAL_EVALUATOR_MODEL", environment["MODEL_NAME"])
    # Fail before creating an unusable run directory when basic settings are invalid.
    if not environment["MODEL_NAME"].strip():
        raise ValueError("MODEL_NAME must not be empty.")
    source = git_metadata()
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "agent-source.tar").write_bytes(snapshot)
    requirement = Path(__file__).with_name("agent-requirements.txt").read_bytes()
    (directory / "agent-requirements.txt").write_bytes(requirement)
    write_json(directory / "dataset.json", rows)
    manifest = {
        "schema_version": 2, "run_id": uuid.uuid4().hex[:12], "created_at": now(),
        "mode": args.command, "harness_version": HARNESS_VERSION,
        "driver_sha256": driver_digest(), "source": source,
        "source_sha256": bytes_digest(snapshot), "dataset": dataset,
        "dataset_sha256": digest(directory / "dataset.json"),
        "requirements_sha256": bytes_digest(requirement),
        "instance_ids": [row["instance_id"] for row in rows],
        "platform": "linux/amd64", "namespace": None if args.build_images else "swebench",
        "cache_level": args.cache_level or "env", "environment": environment,
        "goal": args.goal, "validate_gold": args.command == "validate" or not args.skip_gold,
        "execution_profile": "benchmark", "patch_collection": "runner",
        "permission_denial_limit": 3,
        "limits": {"max_turns": args.max_turns or 30, "max_calls": args.max_calls or 40,
                   "max_tokens": args.max_tokens or 300000, "timeout": args.timeout or 600},
        "setup_timeout": args.setup_timeout or 1800, "eval_timeout": args.eval_timeout or 1800,
        "host": {"system": platform.system(), "machine": platform.machine(),
                 "python": platform.python_version()},
    }
    write_json(directory / "manifest.json", manifest)
    summarize(directory)
    with (directory / "host-packages.txt").open("w", encoding="utf-8") as log:
        subprocess.run([sys.executable, "-m", "pip", "freeze"], stdout=log,
                       stderr=subprocess.STDOUT, check=True, timeout=60)
    return manifest


def load_manifest(directory):
    manifest = read_json(directory / "manifest.json")
    if manifest.get("schema_version") != 2:
        raise ValueError("This driver uses runner-owned patches (manifest v2). "
                         "Start a new output directory, or use the original Git version for old runs.")
    if manifest["driver_sha256"] != driver_digest():
        raise ValueError("Benchmark driver changed. Use the original Git version to resume, "
                         "or start a new output directory.")
    for name, key in (("dataset.json", "dataset_sha256"), ("agent-source.tar", "source_sha256"),
                      ("agent-requirements.txt", "requirements_sha256")):
        if digest(directory / name) != manifest[key]:
            raise ValueError(f"Run snapshot changed: {name}. Start a new output directory.")
    return manifest


def prepare_wheels(directory):
    wheels = directory / "wheels"
    record = directory / "wheels.json"
    if record.exists():
        expected = read_json(record)
        actual = {path.name: digest(path) for path in wheels.glob("*.whl")}
        if actual != expected:
            raise ValueError("Agent wheel snapshot changed; start a new run.")
        return
    wheels.mkdir(exist_ok=True)
    say("Downloading Python 3.11 Linux x86_64 Agent wheels (once per run)...")
    command = [sys.executable, "-m", "pip", "download", "--only-binary=:all:",
               "--platform", "manylinux2014_x86_64", "--python-version", "3.11",
               "--implementation", "cp", "--abi", "cp311", "--dest", str(wheels),
               "--retries", "3", "--timeout", "30", "-r", str(directory / "agent-requirements.txt")]
    with (directory / "wheel-download.log").open("a", encoding="utf-8") as log:
        result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=1800)
    if result.returncode:
        raise RuntimeError("Agent wheel download failed; see wheel-download.log. Resume to retry.")
    write_json(record, {path.name: digest(path) for path in wheels.glob("*.whl")})


def grade_patch(directory, attempt, row, manifest, *, gold=False):
    """Run the pinned official CLI in its own process and fresh result directory."""
    evaluation = attempt / ("gold" if gold else "grading") / uuid.uuid4().hex[:10]
    evaluation.mkdir(parents=True)
    run_id = f"akicode-{manifest['run_id']}-{evaluation.name}"
    model = "gold" if gold else "akicode"
    if not gold:
        prediction = read_json(attempt / "prediction.json")
        write_json(evaluation / "predictions.json", [prediction])
    command = [sys.executable, "-m", "swebench.harness.run_evaluation",
               "--dataset_name", str(directory / "dataset.json"), "--split", "test",
               "--instance_ids", row["instance_id"], "--predictions_path",
               "gold" if gold else str(evaluation / "predictions.json"),
               "--run_id", run_id, "--max_workers", "1",
               "--namespace", manifest["namespace"] or "none",
               "--cache_level", "instance", "--clean", "False",
               "--timeout", str(manifest["eval_timeout"])]
    # The grader gets no API key, .env or host project directory in its environment.
    env = {key: value for key, value in os.environ.items()
           if key not in {"DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY", "PYTHONPATH"}}
    env["PYTHON_DOTENV_DISABLED"] = "1"
    with (evaluation / "harness.log").open("wb") as log:
        try:
            process = subprocess.Popen(command, cwd=evaluation, env=env, stdout=log,
                                       stderr=subprocess.STDOUT)
            try:
                exit_code = process.wait(timeout=manifest["setup_timeout"] + manifest["eval_timeout"] + 120)
            except BaseException:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
                raise
        finally:
            # Official containers have deterministic names. On interrupt/timeout,
            # clean only this unique grader's exact container, not global Docker.
            client = docker_client()
            try:
                import docker
                try:
                    owned = client.containers.get(f"sweb.eval.{row['instance_id'].lower()}.{run_id}")
                except docker.errors.NotFound:
                    owned = None
                if owned is not None:
                    owned.remove(force=True)
            finally:
                client.close()
    report = evaluation / "logs" / "run_evaluation" / run_id / model / row["instance_id"] / "report.json"
    if exit_code or not report.exists():
        raise RuntimeError(f"Official evaluation produced no report; see {evaluation.relative_to(attempt)}/harness.log.")
    verdict = read_json(report)[row["instance_id"]]
    return {"resolved": bool(verdict["resolved"]), "report": str(report.relative_to(directory)),
            "tests_status": verdict.get("tests_status", {})}


def next_attempt(directory, instance_id):
    parent = directory / "instances" / instance_id
    parent.mkdir(parents=True, exist_ok=True)
    number = len(list(parent.glob("attempt-*"))) + 1
    attempt = parent / f"attempt-{number:04d}"
    attempt.mkdir()
    return attempt


def set_state(attempt, state, **changes):
    state.update(changes, updated_at=now())
    write_json(attempt / "state.json", state)


def agent_result(attempt, row):
    result_path = attempt / "agent" / "run" / "result.json"
    if result_path.exists():
        result = read_json(result_path)
        if result.get("base_commit") != row["base_commit"]:
            raise ValueError("Agent result does not match the expected base_commit.")
    else:
        progress = attempt / "agent" / "run" / "progress.json"
        result = read_json(progress) if progress.exists() else {}
        execution_path = attempt / "execution.json"
        execution = read_json(execution_path) if execution_path.exists() else {}
        status = execution.get("status", "error")
        status = (status if status in {"timeout", "interrupted"} else "error") if execution.get(
            "agent_started", True) else "not_started"
        result.update(status=status,
                      reason=execution.get("reason") or "Agent returned no result.json; see agent-console.log.",
                      result_available=False)
    return result


def submission(attempt, row, manifest):
    # The trusted runner's artifact is authoritative even if the Agent process
    # timed out or died before writing its own result.json.
    metadata_path = attempt / "patch.json"
    patch_path = attempt / "patch.diff"
    if not metadata_path.exists():
        raise RuntimeError("Runner collected no usable patch; see inference-error.log.")
    metadata = read_json(metadata_path)
    if (metadata.get("collector") != "runner" or metadata.get("base_commit") != row["base_commit"]
            or metadata.get("status") not in {"empty", "nonempty"}):
        raise ValueError(f"Runner patch is unavailable or invalid: {metadata.get('reason', '')}")
    if not patch_path.exists():
        raise ValueError("Runner patch file is missing.")
    patch_hash = digest(patch_path)
    if metadata.get("bytes") != patch_path.stat().st_size or metadata.get("sha256") != patch_hash:
        raise ValueError("Runner patch changed after collection. Start a new run.")
    nonempty = bool(patch_path.stat().st_size)
    if nonempty != (metadata["status"] == "nonempty"):
        raise ValueError("Runner patch status does not match its contents.")
    result = agent_result(attempt, row)
    patch = patch_path.read_text(encoding="utf-8")
    write_json(attempt / "prediction.json", {"instance_id": row["instance_id"],
                                            "model_name_or_path": "akicode", "model_patch": patch})
    return result, patch_hash, nonempty


def record_submission(attempt, state, row, manifest):
    set_state(attempt, state, agent=agent_result(attempt, row))
    result, patch_hash, nonempty = submission(attempt, row, manifest)
    set_state(attempt, state, agent=result, patch_sha256=patch_hash,
              patch_status="nonempty" if nonempty else "empty", phase="grading",
              evaluation_status="running" if nonempty else "not_run")
    if not nonempty:
        set_state(attempt, state, status="empty_patch", phase="finished",
                  reason=result.get("reason") or "Runner collected no working-tree changes.")
    return nonempty


def process_instance(client, row, manifest, directory, api_key):
    attempt = next_attempt(directory, row["instance_id"])
    state = {"instance_id": row["instance_id"], "started_at": now(), "status": "running", "phase": "image",
             "patch_status": "not_collected", "evaluation_status": "not_run"}
    set_state(attempt, state)
    image = None
    preexisting = True
    try:
        spec = test_spec(row, manifest)
        # Official local builders write logs relative to cwd.
        with working_directory(attempt):
            image, preexisting = ensure_image(client, spec, attempt / "image-build.log")
        write_json(attempt / "image.json", {"name": spec.instance_image_key, "id": image.id,
                   "repo_digests": image.attrs.get("RepoDigests", []), "preexisting": preexisting,
                   "architecture": image.attrs.get("Architecture")})
        if image.attrs.get("Architecture") != "amd64":
            raise ValueError("Expected an amd64 instance image.")
        if manifest["validate_gold"]:
            set_state(attempt, state, phase="gold")
            verdict = grade_patch(directory, attempt, row, manifest, gold=True)
            set_state(attempt, state, gold=verdict)
            if not verdict["resolved"]:
                raise RuntimeError("Reference patch failed; Agent was not started. Inspect the gold report.")
        if manifest["mode"] == "validate":
            set_state(attempt, state, status="validated", phase="finished")
            return
        set_state(attempt, state, phase="agent")
        try:
            infer(client, image, row, manifest, directory, attempt, api_key)
        except Exception as exc:
            # infer's finally block still tries to collect while the disposable
            # container exists. A valid partial patch can be graded independently.
            set_state(attempt, state, inference_error=f"{type(exc).__name__}: {exc}")
        set_state(attempt, state, phase="collection")
        if record_submission(attempt, state, row, manifest):
            verdict = grade_patch(directory, attempt, row, manifest)
            set_state(attempt, state, evaluation=verdict,
                      status="resolved" if verdict["resolved"] else "unresolved",
                      evaluation_status="resolved" if verdict["resolved"] else "unresolved", phase="finished")
    except KeyboardInterrupt:
        if (attempt / "patch.json").exists():
            state["patch_status"] = read_json(attempt / "patch.json")["status"]
        if (attempt / "execution.json").exists():
            state["agent"] = agent_result(attempt, row)
        if state["evaluation_status"] == "running":
            state["evaluation_status"] = "interrupted"
        set_state(attempt, state, status="interrupted", reason="Interrupted by user.")
        raise
    except Exception as exc:
        if state["phase"] == "grading":
            state["evaluation_status"] = "error"
        if state["phase"] == "collection":
            state["patch_status"] = "unavailable"
        set_state(attempt, state, status="error", reason=f"{type(exc).__name__}: {exc}")
        say(f"  {row['instance_id']}: {state['reason']}")
    finally:
        if image is not None and not preexisting and manifest["cache_level"] == "env":
            try:
                client.images.remove(spec.instance_image_key)
            except Exception as exc:
                set_state(attempt, state, cleanup_warning=f"Image cleanup failed: {type(exc).__name__}")
        summarize(directory)


def resume_arguments(args):
    configurable = ("instance_ids", "all", "sample", "revision", "dataset_file", "build_images",
                    "skip_gold", "goal", "cache_level", "max_turns", "max_calls", "max_tokens",
                    "timeout", "setup_timeout", "eval_timeout")
    if any(getattr(args, key) is not None for key in configurable) or args.dataset != "SWE-bench/SWE-bench_Lite" or args.seed != 17:
        raise ValueError("Resume uses the stored selection and configuration. Supply only --output-dir, "
                         "--resume and optionally --retry-errors/--env-file.")


def run(args):
    require_harness()
    directory = args.output_dir.expanduser().resolve()
    if args.resume:
        resume_arguments(args)
        manifest = load_manifest(directory)
        if manifest["mode"] != args.command:
            raise ValueError("Resume must use the original run/validate command.")
    else:
        if args.retry_errors:
            raise ValueError("--retry-errors requires --resume.")
        manifest = initialize(args, directory)
    from filelock import FileLock
    # OS-backed lock is released automatically if the host process crashes.
    with FileLock(str(directory / ".lock"), timeout=0):
        rows = read_json(directory / "dataset.json")
        pending = []
        for index, row in enumerate(rows, 1):
            previous = latest_attempt(directory, row["instance_id"])
            if previous:
                state = read_json(previous / "state.json")
                if state["status"] in TERMINAL and not (
                        args.retry_errors and state["status"] in {"error", "interrupted"}):
                    say(f"[{index}/{len(rows)}] {row['instance_id']}: skip {state['status']}")
                    continue
            pending.append((index, row))
        if not pending:
            return summarize(directory)
        client = docker_client()
        try:
            cleanup_run_containers(client, manifest["run_id"])
            api_key = os.environ.get("DEEPSEEK_API_KEY", "")
            if manifest["mode"] == "run":
                if not api_key:
                    raise ValueError("Set DEEPSEEK_API_KEY via the environment or --env-file.")
                prepare_wheels(directory)
            for index, row in pending:
                say(f"[{index}/{len(rows)}] {row['instance_id']}: starting")
                process_instance(client, row, manifest, directory, api_key)
            return summarize(directory)
        finally:
            try:
                cleanup_run_containers(client, manifest["run_id"])
                summarize(directory)
            finally:
                client.close()


def grade(args):
    require_harness()
    directory = args.output_dir.expanduser().resolve()
    manifest = load_manifest(directory)
    if manifest["mode"] != "run":
        raise ValueError("Reference-only validation has no Agent submissions to grade.")
    from filelock import FileLock
    with FileLock(str(directory / ".lock"), timeout=0):
        for row in read_json(directory / "dataset.json"):
            attempt = latest_attempt(directory, row["instance_id"])
            if not attempt or not (attempt / "patch.json").exists():
                continue
            state = read_json(attempt / "state.json")
            if state["status"] in {"resolved", "unresolved", "empty_patch"}:
                continue
            patch = attempt / "patch.diff"
            if state.get("patch_sha256") and digest(patch) != state["patch_sha256"]:
                raise ValueError("Submission changed; start a new run instead of mixing results.")
            image = client = None
            preexisting = True
            try:
                if not record_submission(attempt, state, row, manifest):
                    continue
                client = docker_client()
                cleanup_run_containers(client, manifest["run_id"])
                spec = test_spec(row, manifest)
                with working_directory(attempt):
                    image, preexisting = ensure_image(client, spec, attempt / "grade-image.log")
                verdict = grade_patch(directory, attempt, row, manifest)
                set_state(attempt, state, evaluation=verdict, phase="finished", reason="",
                          status="resolved" if verdict["resolved"] else "unresolved",
                          evaluation_status="resolved" if verdict["resolved"] else "unresolved")
            except KeyboardInterrupt:
                set_state(attempt, state, status="interrupted", reason="Grading interrupted.",
                          evaluation_status="interrupted")
                raise
            except Exception as exc:
                set_state(attempt, state, status="error", reason=f"{type(exc).__name__}: {exc}",
                          evaluation_status="error" if state.get("phase") == "grading" else "not_run")
            finally:
                try:
                    if image is not None and not preexisting and manifest["cache_level"] == "env":
                        try:
                            client.images.remove(spec.instance_image_key)
                        except Exception:
                            set_state(attempt, state, cleanup_warning="Image cleanup failed.")
                finally:
                    if client is not None:
                        client.close()
                    summarize(directory)
        return summarize(directory)
