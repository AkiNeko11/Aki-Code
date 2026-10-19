"""Isolated Linux inference containers; host paths are never bind-mounted."""
from __future__ import annotations

import io
import json
from pathlib import Path, PurePosixPath
import re
import shlex
import tarfile
import threading
import time
import traceback

from .common import task_text, write_json


PLATFORM = "linux/amd64"
LABEL = "org.aki-code.swebench-run"


def docker_client():
    import docker
    client = docker.from_env(timeout=120)
    info = client.info()
    if info.get("OSType") != "linux":
        client.close()
        raise ValueError("Docker must be running Linux containers.")
    return client


def test_spec(row, manifest):
    from swebench.harness.test_spec.test_spec import make_test_spec
    return make_test_spec(row, namespace=manifest["namespace"], arch="x86_64")


def ensure_image(client, spec, log_path):
    import docker
    import requests
    preexisting = None
    with log_path.open("w", encoding="utf-8") as log:
        for attempt in range(1, 4):
            phase = "lookup"
            log.write(f"attempt={attempt}/3 phase={phase} image={spec.instance_image_key}\n")
            log.flush()
            try:
                try:
                    image = client.images.get(spec.instance_image_key)
                except docker.errors.ImageNotFound:
                    preexisting = False
                else:
                    return image, preexisting is not False
                phase = "build" if spec.namespace is None else "pull"
                log.write(f"phase={phase}\n")
                log.flush()
                if spec.namespace is None:
                    from contextlib import redirect_stderr, redirect_stdout
                    from swebench.harness.docker_build import build_instance_images
                    # The runner isolates each worker's cwd and locks local builds.
                    with redirect_stdout(log), redirect_stderr(log):
                        _, failures = build_instance_images(client, [spec], False, 1)
                    if failures:
                        raise RuntimeError("Official image build failed; see image-build.log.")
                else:
                    for event in client.api.pull(spec.instance_image_key, platform=PLATFORM,
                                                 stream=True, decode=True):
                        log.write(json.dumps(event) + "\n")
                        log.flush()
                        if event.get("error"):
                            raise RuntimeError("Image pull failed; see image-build.log.")
                phase = "verify"
                return client.images.get(spec.instance_image_key), False
            except Exception as exc:
                log.write(f"phase={phase} failed\n{traceback.format_exc()}\n")
                transient = isinstance(exc, (requests.ConnectionError, requests.Timeout)) or (
                    isinstance(exc, docker.errors.APIError) and exc.status_code in {500, 502, 503, 504})
                if not transient or phase == "build" or attempt == 3:
                    raise
                log.write("Retrying transient Docker connection/service failure.\n")
                log.flush()
                time.sleep(attempt)


def put_files(container, destination, files):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(data))
    if not container.put_archive(destination, buffer.getvalue()):
        raise RuntimeError("Could not copy inputs into inference container.")


def execute(container, command, log_path, timeout, *, environment=None, workdir="/", append=False,
            stderr_path=None, on_cancel=None):
    """Bound Docker exec, whose streaming socket otherwise has no wall deadline."""
    execution = container.client.api.exec_create(
        container.id, command, stdout=True, stderr=True, stdin=False,
        environment=environment, workdir=workdir,
    )["Id"]
    errors = []

    def cancel():
        if on_cancel is not None:
            try:
                on_cancel()
                return
            except Exception:
                pass
        container.kill()

    def consume():
        try:
            from contextlib import ExitStack
            with ExitStack() as stack:
                log = stack.enter_context(log_path.open("ab" if append else "wb"))
                error_log = stack.enter_context(stderr_path.open("ab")) if stderr_path else log
                for stdout, stderr in container.client.api.exec_start(execution, stream=True, demux=True):
                    for chunk, target in ((stdout, log), (stderr, error_log)):
                        if chunk:
                            target.write(chunk)
                            target.flush()
        except Exception as exc:
            errors.append(exc)

    reader = threading.Thread(target=consume, daemon=True)
    reader.start()
    try:
        reader.join(timeout)
    except BaseException:
        cancel()
        reader.join(5)
        raise
    if reader.is_alive():
        cancel()
        reader.join(5)
        raise TimeoutError(f"Container command exceeded {timeout}s; see {log_path.name}.")
    if errors:
        raise RuntimeError(f"Container stream failed; see {log_path.name}.") from errors[0]
    return container.client.api.exec_inspect(execution)["ExitCode"]


def copy_outputs(container, destination):
    """Treat container output as untrusted; never extract links or escape paths."""
    import docker
    archive_path = destination / "agent-output.tar"
    try:
        try:
            chunks, _ = container.get_archive("/akicode-output")
        except docker.errors.NotFound:
            return
        size = 0
        with archive_path.open("wb") as stream:
            for chunk in chunks:
                size += len(chunk)
                if size > 512 * 1024 * 1024:
                    raise ValueError("Agent output archive exceeds 512 MiB.")
                stream.write(chunk)
        output = destination / "agent"
        output.mkdir(exist_ok=True)
        with tarfile.open(archive_path) as archive:
            for member in archive:
                path = PurePosixPath(member.name)
                if path.is_absolute() or ".." in path.parts or not path.parts:
                    raise ValueError("Unsafe path in agent output archive.")
                if path.parts[0] != "akicode-output":
                    raise ValueError("Unexpected root in agent output archive.")
                target = output.joinpath(*path.parts[1:])
                if not target.resolve().is_relative_to(output.resolve()):
                    raise ValueError("Agent output escapes its destination.")
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.extractfile(member) as source, target.open("wb") as sink:
                        while chunk := source.read(1024 * 1024):
                            sink.write(chunk)
                else:
                    raise ValueError("Links and special files are not accepted in agent output.")
    finally:
        archive_path.unlink(missing_ok=True)


def infer(client, image, row, manifest, directory, attempt, api_key):
    name = f"akicode-{manifest['run_id']}-{row['instance_id']}-{attempt.name}"
    # A previous host crash may leave this run's named inference container behind.
    import docker
    try:
        stale = client.containers.get(name)
    except docker.errors.NotFound:
        stale = None
    if stale is not None:
        if stale.labels.get(LABEL) != manifest["run_id"]:
            raise ValueError("A container with this name belongs to another run.")
        stale.remove(force=True)
    container = client.containers.create(
        image.id, name=name, command=["/bin/bash", "-c", "exec tail -f /dev/null"],
        platform=PLATFORM, labels={LABEL: manifest["run_id"]},
        working_dir="/testbed", user="root", detach=True,
    )
    started = time.monotonic()
    agent_started = False
    execution = {"exit_code": None, "status": "error", "phase": "prepare", "reason": "Agent did not start."}
    try:
        container.start()
        # Reset only the disposable container checkout, never a host Git workspace.
        base = shlex.quote(row["base_commit"])
        setup = (
            "set -eu\ncd /testbed\n"
            f"git reset --hard {base}\ngit clean -ffd\n"
            f'test "$(git rev-parse HEAD)" = {base}\n'
            'test -z "$(git status --porcelain --untracked-files=all)"\n'
            'test "$(uname -m)" = x86_64\n'
            "mkdir -p /opt/akicode /akicode-output /opt/akicode-runner /tmp/akicode-task\n"
            "/opt/miniconda3/bin/python -c 'import sys; "
            "assert sys.version_info[:2] == (3, 11), "
            '\"Expected Python 3.11 in the official image base\"' + "'\n"
            "/opt/miniconda3/bin/python -m venv /opt/akicode-venv\n"
        )
        setup_timeout = manifest["setup_timeout"]
        if execute(container, ["/bin/bash", "-c", setup], attempt / "prepare.log", setup_timeout):
            raise RuntimeError("Inference workspace preparation failed; see prepare.log.")
        if not container.put_archive("/opt/akicode", (directory / "agent-source.tar").read_bytes()):
            raise RuntimeError("Could not copy Agent source.")
        put_files(container, "/opt/akicode-runner", {
            "owner": manifest["run_id"].encode(),
            "collector.py": Path(__file__).with_name("collector.py").read_bytes(),
        })
        files = {"task.txt": task_text(row).encode("utf-8"),
                 "agent-requirements.txt": (directory / "agent-requirements.txt").read_bytes()}
        files.update({f"wheels/{path.name}": path.read_bytes()
                      for path in sorted((directory / "wheels").glob("*.whl"))})
        put_files(container, "/opt/akicode", files)
        install = ["/opt/akicode-venv/bin/python", "-m", "pip", "install", "--no-index",
                   "--find-links", "/opt/akicode/wheels", "-r", "/opt/akicode/agent-requirements.txt"]
        if execute(container, install, attempt / "prepare.log", setup_timeout, append=True):
            raise RuntimeError("Offline Agent installation failed; see prepare.log.")
        limits = manifest["limits"]
        command = ["/opt/akicode-venv/bin/python", "-m", "aki_code.main", "run",
                   "--task-file", "/opt/akicode/task.txt", "--workspace", "/testbed",
                   "--output-dir", "/akicode-output/run", "--permission-mode", "accept-edits",
                   "--execution-profile", "benchmark", "--scratch-dir", "/tmp/akicode-task",
                   "--patch-export", "runner",
                   "--max-turns", str(limits["max_turns"]), "--max-calls", str(limits["max_calls"]),
                   "--max-tokens", str(limits["max_tokens"]), "--timeout", str(limits["timeout"])]
        if manifest["goal"]:
            command += ["--goal", manifest["goal"]]
        env = {**manifest["environment"], "DEEPSEEK_API_KEY": api_key,
               "PYTHONPATH": "/opt/akicode", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1",
               "SKILLS_DIR": "/opt/akicode/skills",
               "TMPDIR": "/tmp/akicode-task", "TMP": "/tmp/akicode-task", "TEMP": "/tmp/akicode-task",
               "PATH": "/opt/miniconda3/envs/testbed/bin:/usr/local/sbin:/usr/local/bin:"
                       "/usr/sbin:/usr/bin:/sbin:/bin:/opt/miniconda3/bin"}
        agent_started = True
        execution["phase"] = "agent"
        exit_code = execute(container, command, attempt / "agent-console.log",
                            limits["timeout"] + 60, environment=env, workdir="/opt/akicode",
                            on_cancel=lambda: quiesce_agent(container, attempt))
        execution.update(exit_code=exit_code, status="exited", reason="")
    except BaseException as exc:
        execution.update(status="interrupted" if isinstance(exc, KeyboardInterrupt) else
                         "timeout" if isinstance(exc, TimeoutError) else "error", reason=f"{type(exc).__name__}: {exc}")
        (attempt / "inference-error.log").write_text(traceback.format_exc(), encoding="utf-8")
        raise
    finally:
        execution["elapsed_seconds"] = round(time.monotonic() - started, 3)
        execution["agent_started"] = agent_started
        write_json(attempt / "execution.json", execution)
        warnings = []
        try:
            if agent_started:
                collect_patch(container, row, attempt)
        finally:
            # A failed collection or stop must not suppress copying diagnostics
            # or removing this owned container, or mask the original exception.
            for operation in (lambda: container.stop(timeout=2),
                              lambda: copy_outputs(container, attempt),
                              lambda: container.remove(force=True)):
                try:
                    operation()
                except Exception as exc:
                    warnings.append(f"{type(exc).__name__}: {exc}")
            if warnings:
                execution["cleanup_warnings"] = warnings
                write_json(attempt / "execution.json", execution)


def quiesce_agent(container, attempt):
    command = ["/opt/miniconda3/bin/python", "/opt/akicode-runner/collector.py", "--quiesce-only"]
    if execute(container, command, attempt / "quiesce.log", 30):
        raise RuntimeError("Could not stop Agent processes; see quiesce.log.")


def collect_patch(container, row, attempt):
    """Independent of Agent result.json and local patch export; stdout is the patch."""
    patch = attempt / "patch.diff"
    metadata = {"collector": "runner", "base_commit": row["base_commit"], "status": "unavailable"}
    try:
        container.reload()
        if container.status != "running":
            raise RuntimeError("Inference container is not running; cannot collect Git changes.")
        command = ["/opt/miniconda3/bin/python", "/opt/akicode-runner/collector.py",
                   "--base-commit", row["base_commit"]]
        if execute(container, command, patch, 200, stderr_path=attempt / "patch-collection.log"):
            raise RuntimeError("Runner patch collection failed; see patch-collection.log.")
        from .common import digest
        size = patch.stat().st_size
        metadata.update(status="nonempty" if size else "empty", bytes=size, sha256=digest(patch))
    except Exception as exc:
        metadata["reason"] = f"{type(exc).__name__}: {exc}"
        if patch.exists():
            patch.replace(attempt / "patch.partial")
    finally:
        write_json(attempt / "patch.json", metadata)
    return metadata


def cleanup_run_containers(client, run_id, *, instance_id=None):
    """Clean this run, or only a crashed instance without disturbing its peers."""
    inference_pattern = (rf"akicode-{re.escape(run_id)}-{re.escape(instance_id)}-attempt-[0-9]{{4,}}"
                         if instance_id is not None else None)
    for container in client.containers.list(all=True, filters={"label": f"{LABEL}={run_id}"}):
        if inference_pattern is None or re.fullmatch(inference_pattern, container.name):
            container.remove(force=True)
    # The official CLI has no custom-label argument. Its unique run_id embeds
    # our ownership nonce; restrict cleanup to that exact generated name form.
    instance_pattern = re.escape(instance_id.lower()) if instance_id is not None else r"[a-z0-9_.-]+"
    pattern = rf"sweb\.eval\.{instance_pattern}\.akicode-{re.escape(run_id)}-[0-9a-f]{{10}}"
    for container in client.containers.list(all=True, filters={"name": f"akicode-{run_id}-"}):
        if re.fullmatch(pattern, container.name):
            container.remove(force=True)
