"""Offline integration checks through the real SDK and supervised CLI.

All workspaces are disposable Git repositories. The local HTTP fixture returns
scripted Anthropic messages; no API key, external network or model spend needed.
"""
from __future__ import annotations

from contextlib import contextmanager
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from anthropic.types import Message
from rich.console import Console

from aki_code.artifacts import export_patch, git, validate_workspace, json_default
from aki_code.headless import EXIT_CODES, execute_task
from aki_code.artifacts import RunRecorder
from aki_code.runtime import AgentRuntime


ROOT = Path(__file__).resolve().parent.parent


def text(value="Done"):
    return {"type": "text", "text": value}


def tool(name, **inputs):
    return {"type": "tool_use", "id": "tool_" + name, "name": name, "input": inputs}


def response(*blocks):
    return {"id": "msg_fixture", "type": "message", "role": "assistant",
            "model": "test-model", "content": list(blocks),
            "stop_reason": "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn",
            "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 5}}


class ScriptedClient:
    def __init__(self, script):
        self.script = list(script)
        self.requests = []
        self.messages = self

    def with_options(self, **options):
        return self

    def create(self, **request):
        self.requests.append(json.loads(json.dumps(request, default=json_default)))
        value = self.script.pop(0)
        if isinstance(value, BaseException):
            raise value
        return Message.model_validate(value)

    @contextmanager
    def stream(self, **request):
        result = self.create(**request)
        class Stream:
            text_stream = [b.text for b in result.content if b.type == "text"]
            def get_final_message(self):
                return result
        yield Stream()


class APIFixture:
    def __init__(self, script):
        self.script = list(script)
        self.requests = []
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.requests.append(request)
                try:
                    item = owner.script.pop(0)
                except IndexError:
                    item = {"http_status": 400}
                if "delay" in item:
                    time.sleep(item["delay"])
                    item = response(text())
                if "http_status" in item:
                    body = json.dumps({"type": "error", "error": {"type": "api_error", "message": "fixture error"}}).encode()
                    self.send_response(item["http_status"])
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if not request.get("stream"):
                    body = json.dumps(item).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                start = {**item, "content": [], "stop_reason": None}
                start["usage"] = {"input_tokens": 10, "output_tokens": 0}
                events = [("message_start", {"type": "message_start", "message": start})]
                for index, block in enumerate(item["content"]):
                    if block["type"] == "text":
                        initial = {"type": "text", "text": ""}
                        delta = {"type": "text_delta", "text": block["text"]}
                    else:
                        initial = {**block, "input": {}}
                        delta = {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
                    events.extend([
                        ("content_block_start", {"type": "content_block_start", "index": index, "content_block": initial}),
                        ("content_block_delta", {"type": "content_block_delta", "index": index, "delta": delta}),
                        ("content_block_stop", {"type": "content_block_stop", "index": index}),
                    ])
                events.extend([
                    ("message_delta", {"type": "message_delta", "delta": {"stop_reason": item["stop_reason"], "stop_sequence": None},
                                       "usage": {"output_tokens": 5}}),
                    ("message_stop", {"type": "message_stop"}),
                ])
                body = "".join(f"event: {kind}\ndata: {json.dumps(event)}\n\n" for kind, event in events).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass  # Expected when the supervisor ends a timed-out call.
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class HeadlessTests(unittest.TestCase):
    def setUp(self):
        root = ROOT / ".task_outputs"
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="headless-test-", dir=root)
        self.root = Path(self.temp.name).resolve()
        self.workspace = self.root / "repository"
        self.workspace.mkdir()
        self.output = self.root / "run"
        git(self.workspace, "init", "--quiet")
        (self.workspace / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
        git(self.workspace, "add", ".")
        git(self.workspace, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
            "commit", "--quiet", "-m", "baseline")
        self.base = validate_workspace(self.workspace)

    def tearDown(self):
        # The resolved target is a directory created solely by this fixture.
        self.assertTrue(self.root.is_relative_to((ROOT / ".task_outputs").resolve()))
        self.temp.cleanup()

    def direct(self, script, *, goal=None, **limits):
        self.output.mkdir()
        request = {"workspace": str(self.workspace), "task": "/literal task", "goal": goal,
                   "permission_mode": "accept-edits", "limits": {"max_turns": 10,
                   "max_calls": 20, "max_tokens": 10000, **limits}}
        client = ScriptedClient(script)
        result = execute_task(request, RunRecorder(self.output), client=client)
        return result, client

    def cli(self, script, *options, task_file=False):
        with APIFixture(script) as api:
            env = dict(os.environ, DEEPSEEK_API_KEY="fixture-key",
                       DEEPSEEK_BASE_URL=f"http://127.0.0.1:{api.server.server_port}",
                       MODEL_NAME="test-model", GOAL_EVALUATOR_MODEL="test-model",
                       PYTHONIOENCODING="utf-8", MAX_TURNS="0")
            task_args = ["--task", "修复加法 /goal is literal"]
            if task_file:
                path = self.root / "task.txt"
                path.write_text("修复加法 /goal is literal", encoding="utf-8-sig")
                task_args = ["--task-file", str(path)]
            proc = subprocess.run([sys.executable, "-m", "aki_code.main", "run",
                                   "--workspace", str(self.workspace), "--output-dir", str(self.output),
                                   *task_args, "--permission-mode", "accept-edits",
                                   "--timeout", "15", *options], cwd=ROOT, env=env,
                                  stdin=subprocess.DEVNULL, capture_output=True, timeout=35)
            result = json.loads(proc.stdout.decode("utf-8"))
            return result, proc, api.requests

    def test_cli_edits_and_patch_apply_without_real_index_changes(self):
        index_path = Path(os.fsdecode(git(self.workspace, "rev-parse", "--absolute-git-dir")).strip()) / "index"
        before = index_path.read_bytes()
        result, proc, requests = self.cli([
            response(tool("edit_file", path="calc.py", old_string="a - b", new_string="a + b")),
            response(tool("write_file", path="added.txt", content="new file\n")),
            response(tool("bash", command=f'"{sys.executable}" -c "from calc import add; assert add(2,3)==5; print(123)"')),
            response(text("Fixed and verified.")),
        ])
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(result["request_attempts"], 4)
        self.assertEqual(result["usage"]["total_tokens"], 60)
        self.assertEqual(before, index_path.read_bytes())
        self.assertIn(b"added.txt", (self.output / "patch.diff").read_bytes())
        self.assertEqual(json.loads((self.output / "result.json").read_text(encoding="utf-8")), result)
        self.assertEqual(len(proc.stdout.decode("utf-8").splitlines()), 1)
        self.assertTrue(all(str(self.workspace) in req["system"] for req in requests))
        self.assertIn("修复加法 /goal is literal", requests[0]["messages"][0]["content"])
        self.assertFalse((self.workspace / ".memory").exists())
        clone = self.root / "clone"
        subprocess.run(["git", "clone", "--quiet", str(self.workspace), str(clone)], check=True, capture_output=True)
        git(clone, "apply", "--check", str(self.output / "patch.diff"))
        git(clone, "apply", str(self.output / "patch.diff"))
        self.assertIn("a + b", (clone / "calc.py").read_text())
        self.assertEqual((clone / "added.txt").read_text(), "new file\n")

    def test_pending_approval_exits_instead_of_reading_stdin(self):
        result, proc, _ = self.cli([response(tool("bash", command="pip install fake-package"))])
        self.assertEqual(result["status"], "blocked", result)
        self.assertEqual(proc.returncode, EXIT_CODES["blocked"])
        self.assertEqual(result["request_attempts"], 1)

    def test_default_file_permission_is_blocked(self):
        result, _, _ = self.cli([response(tool("write_file", path="blocked.txt", content="no"))],
                                "--permission-mode", "default")
        self.assertEqual(result["status"], "blocked")
        self.assertFalse((self.workspace / "blocked.txt").exists())

    def test_goal_rejection_then_verification_then_achievement(self):
        result, client = self.direct([
            response(text("Probably done")),
            response(text('{"ok":false,"reason":"Need test exit code","impossible":false}')),
            response(tool("bash", command=f'"{sys.executable}" -c "print(456)"')),
            response(text("Command exit code 0")),
            response(text('{"ok":true,"reason":"Verified exit 0","impossible":false}')),
        ], goal="Validation command exits 0")
        self.assertEqual(result["status"], "achieved", result)
        self.assertEqual(result["goal"]["checks"], 2)
        self.assertEqual(result["request_attempts"], 5)
        self.assertEqual(result["lead_turns"], 3)
        self.assertIn("Need test exit code", json.dumps(client.requests[2]["messages"]))

    def test_goal_impossible_and_evaluator_error_stay_distinct(self):
        result, _ = self.direct([response(text()), response(text('{"ok":false,"reason":"Cannot satisfy constraint","impossible":true}'))], goal="Impossible")
        self.assertEqual(result["status"], "failed")

    def test_invalid_evaluator_reply(self):
        result, _ = self.direct([response(text()), response(text("invalid JSON"))], goal="Verify")
        self.assertEqual(result["status"], "goal_error")
        self.assertTrue(result["goal"]["active"])

    def test_subagent_uses_workspace_profile_and_shared_call_budget(self):
        result, client = self.direct([
            response(tool("task", prompt="Inspect calc.py")),
            response(tool("read_file", path="calc.py")),
            response(text("a minus b")),
        ], max_calls=2)
        self.assertEqual(result["status"], "limit", result)
        self.assertEqual(result["request_attempts"], 2)
        self.assertEqual(len(client.requests), 2)
        self.assertIn(str(self.workspace), client.requests[1]["system"])
        self.assertNotIn("task", {s["name"] for s in client.requests[1]["tools"]})
        self.assertNotIn("workflow", {s["name"] for s in client.requests[0]["tools"]})

    def test_subagent_permission_requirement_propagates_to_host(self):
        result, _ = self.direct([response(tool("task", prompt="Install dependencies")),
                                 response(tool("bash", command="pip install fake-package"))])
        self.assertEqual(result["status"], "blocked", result)

    def test_background_wait_and_continuation_share_turn_limit(self):
        result, _ = self.direct([
            response(tool("bash", command=f'"{sys.executable}" -c "import time; time.sleep(0.4); print(789)"', run_in_background=True)),
            response(text("Waiting")),
            response(text("Finished")),
        ], max_turns=2)
        self.assertEqual(result["status"], "limit", result)
        self.assertEqual(result["lead_turns"], 2)
        messages = (self.output / "messages.json").read_text(encoding="utf-8")
        self.assertIn("789", messages)
        self.assertIn("[exit code: 0]", messages)

    def test_background_completion_without_goal(self):
        result, _ = self.direct([
            response(tool("bash", command=f'"{sys.executable}" -c "import time; time.sleep(0.2); print(789)"', run_in_background=True)),
            response(text("Waiting")), response(text("Finished")),
        ])
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["text"], "Finished")
        self.assertEqual(result["lead_turns"], 3)

    def test_goal_waits_for_background_evidence_before_judging(self):
        result, client = self.direct([
            response(tool("bash", command=f'"{sys.executable}" -c "import time; time.sleep(0.2); print(789)"', run_in_background=True)),
            response(text("Waiting")), response(text("Finished")),
            response(text('{"ok":true,"reason":"Background exit 0","impossible":false}')),
        ], goal="Background command exits 0")
        self.assertEqual(result["status"], "achieved", result)
        self.assertEqual(result["goal"]["checks"], 1)
        self.assertIn("789", json.dumps(client.requests[-1]["messages"]))
        self.assertEqual(result["request_attempts"], 4)

    def test_goal_status_is_paused_by_host_budget(self):
        result, _ = self.direct([response(text("Unverified"))], goal="Verify result", max_calls=1)
        self.assertEqual(result["status"], "limit")
        self.assertEqual(result["goal"]["status"], "limit")
        self.assertFalse(result["goal"]["automatic"])
        self.assertTrue(result["goal"]["active"])

    def test_final_judge_token_overshoot_keeps_achieved_goal(self):
        result, _ = self.direct([response(text("Verified")),
                                 response(text('{"ok":true,"reason":"Verified","impossible":false}'))],
                                goal="Verify result", max_tokens=20)
        self.assertEqual(result["status"], "achieved", result)
        self.assertEqual(result["goal"]["status"], "achieved")
        self.assertEqual(result["usage"]["total_tokens"], 30)

    def test_compaction_is_isolated_and_uses_shared_call_budget(self):
        result, _ = self.direct([response(tool("compact")), response(text("Facts-only summary")),
                                 response(text("Done"))], max_calls=2)
        self.assertEqual(result["status"], "limit", result)
        self.assertEqual(result["request_attempts"], 2)
        self.assertTrue(list((self.output / "state" / "transcripts").glob("*.json")))
        self.assertFalse((self.workspace / ".transcripts").exists())

    def test_token_limit_prevents_next_tool(self):
        result, _ = self.direct([response(tool("write_file", path="not-written.txt", content="no"))], max_tokens=10)
        self.assertEqual(result["status"], "limit")
        self.assertFalse((self.workspace / "not-written.txt").exists())

    def test_retries_count_before_attempt(self):
        result, _, requests = self.cli([{"http_status": 429}, response(text())], "--max-calls", "1")
        self.assertEqual(result["status"], "limit", result)
        self.assertEqual(result["request_attempts"], 1)
        self.assertEqual(len(requests), 1)
        self.assertEqual(result["usage"]["calls"], 0)

    def test_api_failure_retains_patch(self):
        result, _, _ = self.cli([response(tool("write_file", path="partial.txt", content="work")), {"http_status": 400}])
        self.assertEqual(result["status"], "error", result)
        self.assertTrue(result["patch_exported"])
        self.assertIn(b"partial.txt", (self.output / "patch.diff").read_bytes())

    def test_timeout_during_api_call(self):
        before = time.monotonic()
        result, proc, _ = self.cli([{"delay": 5}], "--timeout", "3.5")
        self.assertEqual(result["status"], "timeout", result)
        self.assertEqual(proc.returncode, 124)
        self.assertLess(time.monotonic() - before, 9)
        self.assertTrue(result["patch_exported"])

    def test_timeout_kills_shell_before_export(self):
        command = f'"{sys.executable}" -c "import time; from pathlib import Path; Path(\'started.txt\').write_text(str(time.time())); time.sleep(5); Path(\'late.txt\').write_text(\'orphan\')"'
        result, proc, _ = self.cli([response(tool("bash", command=command))], "--timeout", "3.5")
        self.assertEqual(result["status"], "timeout", result)
        self.assertEqual(proc.returncode, 124)
        started = float((self.workspace / "started.txt").read_text())
        time.sleep(max(0, started + 5.2 - time.time()))
        self.assertFalse((self.workspace / "late.txt").exists(), "Shell child outlived the worker")

    def test_timeout_cleans_background_process(self):
        command = f'"{sys.executable}" -c "import time; from pathlib import Path; Path(\'started.txt\').write_text(str(time.time())); time.sleep(5); Path(\'late.txt\').write_text(\'orphan\')"'
        result, _, _ = self.cli([response(tool("bash", command=command, run_in_background=True)),
                                response(text("Waiting"))], "--timeout", "3.5")
        self.assertEqual(result["status"], "timeout", result)
        started = float((self.workspace / "started.txt").read_text())
        time.sleep(max(0, started + 5.2 - time.time()))
        self.assertFalse((self.workspace / "late.txt").exists(), "Background child outlived the worker")

    def test_dirty_workspace_rejected_without_modification(self):
        (self.workspace / "untouched.txt").write_text("user change")
        result, proc, requests = self.cli([])
        self.assertEqual(result["status"], "error")
        self.assertIn("clean", result["reason"])
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(requests, [])
        self.assertEqual((self.workspace / "untouched.txt").read_text(), "user change")
        self.assertFalse(self.output.exists())

    def test_task_file_bom_and_unicode(self):
        result, _, requests = self.cli([response(text("完成 ✅"))], task_file=True)
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["text"], "完成 ✅")
        self.assertEqual(requests[0]["messages"][0]["content"], "修复加法 /goal is literal")

    def test_output_must_be_new_and_outside_workspace(self):
        self.output.mkdir()
        marker = self.output / "keep.txt"
        marker.write_text("existing output")
        result, _, _ = self.cli([])
        self.assertEqual(result["status"], "error")
        self.assertEqual(marker.read_text(), "existing output")
        result, _, _ = self.cli([], "--output-dir", str(self.workspace / "run"))
        self.assertEqual(result["status"], "error")
        self.assertIn("outside", result["reason"])
        self.assertFalse((self.workspace / "run").exists())

    def test_coding_profile_blocks_dispatch_of_hidden_tools(self):
        runtime = AgentRuntime(workspace=self.workspace, state_dir=self.root / "state",
                               profile="coding", client=ScriptedClient([]),
                               console=Console(file=io.StringIO()))
        try:
            self.assertIn("unknown tool", runtime.registry.execute("connect_mcp", server="docs"))
            self.assertIn("unknown tool", runtime.registry.execute("schedule_cron", prompt="no"))
            self.assertEqual(runtime.mcp_manager.clients, {})
        finally:
            runtime.close()

    def test_interruption_preserves_task_changes_and_reports_reason(self):
        result, _ = self.direct([response(tool("write_file", path="partial.txt", content="work")), KeyboardInterrupt()])
        self.assertEqual(result["status"], "interrupted", result)
        self.assertIn("Interrupted", result["reason"])
        self.assertEqual((self.workspace / "partial.txt").read_text(), "work")

    def test_patch_preserves_additions_deletions_and_binary(self):
        (self.workspace / "calc.py").unlink()
        binary = bytes(range(256))
        (self.workspace / "binary.bin").write_bytes(binary)
        self.output.mkdir()
        export_patch(self.workspace, self.base, self.output)
        clone = self.root / "clone"
        subprocess.run(["git", "clone", "--quiet", str(self.workspace), str(clone)], check=True, capture_output=True)
        git(clone, "apply", str(self.output / "patch.diff"))
        self.assertFalse((clone / "calc.py").exists())
        self.assertEqual((clone / "binary.bin").read_bytes(), binary)
        self.assertEqual(git(self.workspace, "diff", "--cached"), b"")

    def test_interactive_runtime_keeps_full_tools_and_permission_callback(self):
        asked = []
        client = ScriptedClient([response(tool("write_file", path="interactive.txt", content="yes")), response(text())])
        runtime = AgentRuntime(workspace=self.workspace, state_dir=self.root / "interactive",
                               client=client, console=Console(file=io.StringIO()),
                               ask_permission=lambda *args: asked.append(args) or True)
        try:
            names = {s["name"] for s in runtime.registry.get_schemas()}
            self.assertTrue({"workflow", "spawn_teammate", "schedule_cron", "connect_mcp"} <= names)
            runtime.settings.show_hooks = False
            runtime.append_user("Write a file")
            with patch.object(runtime.memory_store, "extract_memories", return_value=False):
                result = runtime.run_turn("Write a file", interactive=True)
            self.assertTrue(result.ok, result)
            self.assertEqual(len(asked), 1)
            self.assertEqual((self.workspace / "interactive.txt").read_text(), "yes")
        finally:
            runtime.close()

    def test_interactive_cli_commands_use_shared_session(self):
        from aki_code import main as interactive
        stream = io.StringIO()
        inputs = iter(["/help", "/mode accept-edits", "/goal", "/workflows list", "/exit"])
        def factory(**kwargs):
            return AgentRuntime(workspace=self.workspace, state_dir=self.root / "interactive-cli",
                                client=ScriptedClient([]), **kwargs)
        with patch.object(interactive, "console", Console(file=stream)), \
             patch.object(interactive, "AgentRuntime", factory), \
             patch.object(interactive, "_read_input", lambda: next(inputs)), \
             patch.object(interactive.config, "API_KEY", "fixture-key"):
            interactive.run()
        self.assertIn("accept-edits", stream.getvalue())
        self.assertIn("review-changes", stream.getvalue())


if __name__ == "__main__":
    unittest.main()
