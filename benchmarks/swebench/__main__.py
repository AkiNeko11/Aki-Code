"""Usage: python -m benchmarks.swebench --help"""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import sys
import tempfile

from .common import HARNESS_VERSION, ROOT, summarize


def positive(value):
    result = int(value)
    if result < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return result


def parser():
    cli = argparse.ArgumentParser(description="Run Aki-Code on SWE-bench Lite in isolated Linux containers.")
    commands = cli.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor", help="Check dependencies, Docker and amd64 compatibility; no model calls")
    doctor.add_argument("--skip-probe", action="store_true", help="Skip the small amd64 test container")
    doctor.add_argument("--env-file", type=Path, default=ROOT / ".env")
    for command, help_text in (("run", "Repair issues and grade exported patches"),
                               ("validate", "Check official reference patches only; no model calls")):
        run = commands.add_parser(command, help=help_text)
        run.add_argument("--output-dir", type=Path, required=True, help="New output directory; use --resume to reopen")
        run.add_argument("--env-file", type=Path, default=ROOT / ".env")
        run.add_argument("--resume", action="store_true", help="Use this run's frozen source, dataset and settings")
        run.add_argument("--retry-errors", action="store_true", help="On resume, retry errors/interrupted instances in new attempts")
        selection = run.add_mutually_exclusive_group()
        selection.add_argument("--instance-ids", nargs="+", help="Default: sympy__sympy-20590 only")
        selection.add_argument("--sample", type=positive, help="Deterministic pilot sample, ranked by seed+instance hash")
        selection.add_argument("--all", action="store_true", default=None, help="Explicitly select the entire dataset test split")
        run.add_argument("--seed", type=int, default=17)
        run.add_argument("--dataset", default="SWE-bench/SWE-bench_Lite")
        run.add_argument("--revision", help="Hugging Face revision; resolved to a commit and saved")
        run.add_argument("--dataset-file", type=Path, help="Offline official dataset JSON list; contents are snapshotted")
        run.add_argument("--build-images", action="store_true", default=None,
                         help="Build official x86_64 images locally instead of pulling swebench images")
        run.add_argument("--cache-level", choices=["env", "instance"],
                         help="Default env: remove instance images acquired by this run; preserve existing images")
        run.add_argument("--setup-timeout", type=positive, help="Seconds per setup step (default 1800)")
        run.add_argument("--eval-timeout", type=positive, help="Official test timeout per instance (default 1800)")
        run.add_argument("--max-turns", type=positive, help="Lead Agent turns (default 60)")
        run.add_argument("--max-calls", type=positive, help="All model request attempts (default 80)")
        run.add_argument("--max-tokens", type=positive, help="Aggregate token soft threshold (default 1000000)")
        run.add_argument("--timeout", type=positive, help="Agent wall-clock seconds, excluding setup (default 600)")
        run.add_argument("--goal", help="Optional Goal condition; disabled in the baseline")
        run.add_argument("--skip-gold", action="store_true", default=None,
                         help="Skip reference validation before inference (run only)")
    for command, help_text in (("grade", "Retry missing/failed official grading without calling the model"),
                               ("status", "Read saved summary without Docker, dependencies or model calls")):
        sub = commands.add_parser(command, help=help_text)
        sub.add_argument("--output-dir", type=Path, required=True)
    return cli


def doctor(args):
    from .containers import PLATFORM, docker_client, execute
    from .runner import require_harness
    require_harness()
    report = {"python": platform.python_version(), "host_system": platform.system(),
              "host_machine": platform.machine(), "target_platform": PLATFORM,
              "harness_version": importlib.metadata.version("swebench"),
              "api_key_present": bool(os.environ.get("DEEPSEEK_API_KEY")),
              "host_free_gib": round(shutil.disk_usage(ROOT).free / 1024**3, 1)}
    client = docker_client()
    try:
        info = client.info()
        report.update(docker_version=client.version()["Version"], docker_architecture=info.get("Architecture"),
                      docker_cpus=info.get("NCPU"),
                      docker_memory_gib=round(info.get("MemTotal", 0) / 1024**3, 1))
        report["emulation_required"] = info.get("Architecture") not in {"x86_64", "amd64"}
        if not args.skip_probe:
            image = client.images.pull("alpine:3.20", platform=PLATFORM)
            container = client.containers.create(image.id, command=["tail", "-f", "/dev/null"],
                                                 platform=PLATFORM, detach=True)
            try:
                container.start()
                with tempfile.TemporaryDirectory(prefix="akicode-probe-") as temporary:
                    log = Path(temporary) / "probe.log"
                    code = execute(container, ["/bin/sh", "-c", 'test "$(uname -m)" = x86_64 && uname -m'],
                                   log, 30)
                    if code:
                        raise RuntimeError("Docker amd64 compatibility probe failed.")
                    report["amd64_probe"] = log.read_text().strip()
            finally:
                container.remove(force=True)
        else:
            report["amd64_probe"] = "skipped"
    finally:
        client.close()
    return report


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "status":
            # Pure read: recompute from atomic per-instance checkpoints so an
            # active instance is visible before its final summary is written.
            result = summarize(args.output_dir.expanduser().resolve(), save=False)
        else:
            if sys.version_info < (3, 11):
                raise ValueError("Use Python 3.11+ for the optional SWE-bench host tools.")
            if hasattr(args, "env_file"):
                from dotenv import load_dotenv
                if args.env_file.is_file():
                    load_dotenv(args.env_file, override=False)
            if args.command == "doctor":
                result = doctor(args)
            else:
                from . import runner
                result = runner.grade(args) if args.command == "grade" else runner.run(args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.command in {"run", "validate", "grade"}:
            counts = result["counts"]
            if any(counts.get(key, 0) for key in ("error", "interrupted", "running", "pending")):
                return 1
        return 0
    except KeyboardInterrupt:
        print("Interrupted. Saved attempts are preserved; use --resume or grade to continue.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        if isinstance(exc, (ImportError, importlib.metadata.PackageNotFoundError)):
            print(f"Install benchmarks/swebench/requirements.txt (swebench=={HARNESS_VERSION}).", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
