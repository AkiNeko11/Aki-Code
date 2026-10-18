"""Durable workflow checkpoints, protected by a process-owned run lock."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import re
import threading
import time
from datetime import datetime, timezone
from uuid import uuid4

from aki_code.workflow.model import WorkflowBusy, WorkflowInputError


_RUN_ID = re.compile(r"[0-9a-f]{32}\Z")
_IDENTITY_FIELDS = frozenset({"format_version", "run_id", "identity"})
_FORMAT_VERSION = 1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _check_keys(value) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise WorkflowInputError("JSON object keys must be strings")
            _check_keys(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _check_keys(child)


def _encode(value) -> bytes:
    try:
        _check_keys(value)
        return json.dumps(value, ensure_ascii=False, allow_nan=False,
                          sort_keys=True, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise WorkflowInputError(f"Value is not finite JSON: {exc}") from exc


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _decode(raw: bytes):
    def reject_constant(value):
        raise ValueError(f"Non-finite JSON constant: {value}")

    return json.loads(raw.decode("utf-8"), parse_constant=reject_constant,
                      object_pairs_hook=_unique_object)


def _normalized(value):
    return _decode(_encode(value))


def _validate_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or _RUN_ID.fullmatch(run_id) is None:
        raise WorkflowInputError("run_id must be 32 lowercase hexadecimal characters")
    return run_id


def _atomic_json(path: Path, value) -> None:
    data = _encode(value) + b"\n"
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        # Windows may briefly deny replacement while another reader (or an
        # indexer) holds the destination without FILE_SHARE_DELETE. Keep the
        # old snapshot intact, and bound retries to those sharing failures.
        for attempt in range(6):
            try:
                os.replace(temporary, path)
                break
            except PermissionError as exc:
                if (os.name != "nt" or getattr(exc, "winerror", None) not in (None, 5, 32, 33)
                        or attempt == 5):
                    raise
                time.sleep(0.01 * (2 ** attempt))
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            # Cleanup must not hide the failure that left the old snapshot in
            # place. A leftover uniquely named .tmp is never used for resume.
            pass


class _RunLock:
    """An advisory OS lock; process exit releases it without stale-lock cleanup."""

    def __init__(self, path: Path):
        self.path = path
        self.stream = None

    def acquire(self) -> None:
        stream = self.path.open("a+b")
        try:
            if stream.seek(0, os.SEEK_END) == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            stream.close()
            raise WorkflowBusy(f"Workflow run is already in use: {self.path.stem}") from exc
        self.stream = stream

    def release(self) -> None:
        if self.stream is None:
            return
        stream, self.stream = self.stream, None
        try:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()


def _read_snapshot(path: Path, run_id: str) -> dict:
    try:
        snapshot = _decode(path.read_bytes())
    except (OSError, ValueError, UnicodeError) as exc:
        raise WorkflowInputError(f"Cannot read workflow snapshot: {exc}") from exc
    if (not isinstance(snapshot, dict)
            or type(snapshot.get("format_version")) is not int
            or snapshot["format_version"] != _FORMAT_VERSION
            or snapshot.get("run_id") != run_id
            or not isinstance(snapshot.get("identity"), dict)
            or not isinstance(snapshot.get("status"), str)):
        raise WorkflowInputError("Invalid workflow snapshot")
    # Detect numeric overflow (e.g. 1e999), which the decoder represents as inf.
    _encode(snapshot)
    return snapshot


def _record_key(record: dict) -> tuple[str, str]:
    if (not isinstance(record, dict)
            or set(record) != {"step_id", "fingerprint", "value"}
            or not isinstance(record["step_id"], str) or not record["step_id"]
            or not isinstance(record["fingerprint"], str) or not record["fingerprint"]):
        raise WorkflowInputError("Invalid workflow journal record")
    _encode(record)
    return record["step_id"], record["fingerprint"]


def _read_journal(path: Path):
    """Validate everything before returning a possible final-line repair."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise WorkflowInputError(f"Cannot read workflow journal: {exc}") from exc
    cache = {}
    offset = 0
    truncate_to = None
    add_newline = False
    lines = raw.splitlines(keepends=True)
    for index, line in enumerate(lines):
        final_unterminated = index == len(lines) - 1 and not line.endswith(b"\n")
        try:
            record = _decode(line)
        except (json.JSONDecodeError, UnicodeError) as exc:
            if final_unterminated:
                truncate_to = offset
                break
            raise WorkflowInputError(f"Corrupt workflow journal at byte {offset}") from exc
        except ValueError as exc:
            raise WorkflowInputError(f"Invalid workflow journal at byte {offset}: {exc}") from exc
        key = _record_key(record)
        if key in cache and _encode(cache[key]) != _encode(record["value"]):
            raise WorkflowInputError(f"Conflicting workflow journal results for {key[0]}")
        cache[key] = record["value"]
        add_newline = final_unterminated
        offset += len(line)
    return cache, truncate_to, add_newline


class WorkflowStore:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()

    def _path(self, run_id: str, suffix: str) -> Path:
        path = self.root / f"{_validate_run_id(run_id)}{suffix}"
        if path.is_symlink() or path.resolve().parent != self.root:
            raise WorkflowInputError("Workflow artifact must stay inside its store")
        return path

    def open_run(self, identity: dict, resume_from_run_id: str | None = None):
        if not isinstance(identity, dict):
            raise WorkflowInputError("Workflow identity must be a JSON object")
        identity = _normalized(identity)
        if resume_from_run_id is not None:
            _validate_run_id(resume_from_run_id)
        return RunJournal(self, identity, resume_from_run_id)

    def get_run(self, run_id: str) -> dict:
        snapshot = _read_snapshot(self._path(run_id, ".json"), run_id)
        _read_journal(self._path(run_id, ".journal.jsonl"))
        return snapshot

    def list_runs(self) -> list[dict]:
        if not self.root.exists():
            return []
        results = []
        for path in sorted(self.root.glob("*.json")):
            if _RUN_ID.fullmatch(path.stem) is None:
                continue
            try:
                snapshot = self.get_run(path.stem)
                results.append({key: snapshot[key] for key in
                                ("run_id", "status", "identity", "created_at", "updated_at", "error")
                                if key in snapshot})
            except WorkflowInputError as exc:
                results.append({"run_id": path.stem, "status": "invalid", "error": str(exc)})
        return results


class RunJournal:
    def __init__(self, store: WorkflowStore, identity: dict, resume_id: str | None):
        self.store = store
        self.run_id = resume_id or uuid4().hex
        self._identity = identity
        self._resume = resume_id is not None
        self.snapshot_path = store._path(self.run_id, ".json")
        self.output_path = store._path(self.run_id, ".output.json")
        self.journal_path = store._path(self.run_id, ".journal.jsonl")
        self._run_lock = _RunLock(store._path(self.run_id, ".lock"))
        self._mutex = threading.RLock()
        self._snapshot: dict = {}
        self._cache: dict = {}
        self._stream = None
        self._entered = False

    @property
    def snapshot(self) -> dict:
        with self._mutex:
            return copy.deepcopy(self._snapshot)

    def _load_resume(self):
        snapshot = _read_snapshot(self.snapshot_path, self.run_id)
        if _encode(snapshot["identity"]) != _encode(self._identity):
            raise WorkflowInputError("Workflow identity changed; start a new run")
        cache, truncate_to, add_newline = _read_journal(self.journal_path)
        return snapshot, cache, truncate_to, add_newline

    def __enter__(self):
        if self._entered:
            raise WorkflowInputError("A run journal cannot be entered twice")
        if self._resume:
            # Failed identity/corruption checks must not create or repair files.
            self._load_resume()
        else:
            self.store.root.mkdir(parents=True, exist_ok=True)
            if any(path.exists() for path in
                   (self.snapshot_path, self.output_path, self.journal_path, self._run_lock.path)):
                raise WorkflowInputError("Workflow run_id already exists")
        self._run_lock.acquire()
        try:
            if self._resume:
                self._snapshot, self._cache, truncate_to, add_newline = self._load_resume()
                if truncate_to is not None:
                    with self.journal_path.open("r+b") as stream:
                        stream.truncate(truncate_to)
                        stream.flush()
                        os.fsync(stream.fileno())
                elif add_newline:
                    with self.journal_path.open("ab") as stream:
                        stream.write(b"\n")
                        stream.flush()
                        os.fsync(stream.fileno())
                self._stream = self.journal_path.open("ab")
            else:
                created = _now()
                self._snapshot = {"format_version": _FORMAT_VERSION, "run_id": self.run_id,
                                  "identity": copy.deepcopy(self._identity), "status": "running",
                                  "created_at": created, "updated_at": created}
                self._stream = self.journal_path.open("xb")
                _atomic_json(self.snapshot_path, self._snapshot)
            self._entered = True
            return self
        except BaseException:
            if self._stream is not None:
                self._stream.close()
                self._stream = None
            self._run_lock.release()
            raise

    def __exit__(self, exc_type, exc, tb):
        with self._mutex:
            if self._stream is not None:
                self._stream.close()
                self._stream = None
            self._run_lock.release()
            self._entered = False

    def _require_open(self):
        if not self._entered or self._stream is None:
            raise WorkflowInputError("Workflow journal is not open")

    def cached(self, step_id: str, fingerprint: str) -> tuple[bool, object]:
        with self._mutex:
            self._require_open()
            key = (step_id, fingerprint)
            return (True, copy.deepcopy(self._cache[key])) if key in self._cache else (False, None)

    def record(self, step_id: str, fingerprint: str, value) -> None:
        record = {"step_id": step_id, "fingerprint": fingerprint, "value": value}
        key = _record_key(record)
        data = _encode(record) + b"\n"
        stored_value = _normalized(value)
        with self._mutex:
            self._require_open()
            if key in self._cache:
                if _encode(self._cache[key]) != _encode(stored_value):
                    raise WorkflowInputError(f"A completed step cannot change: {step_id}")
                return
            self._stream.write(data)
            self._stream.flush()
            os.fsync(self._stream.fileno())
            self._cache[key] = stored_value

    def save(self, state: dict) -> None:
        if not isinstance(state, dict):
            raise WorkflowInputError("Workflow state must be a JSON object")
        if _IDENTITY_FIELDS.intersection(state):
            raise WorkflowInputError("Workflow state cannot overwrite identity fields")
        state = _normalized(state)
        if "status" in state and not isinstance(state["status"], str):
            raise WorkflowInputError("Workflow status must be a string")
        with self._mutex:
            self._require_open()
            snapshot = {**self._snapshot, **state, "updated_at": _now()}
            _atomic_json(self.snapshot_path, snapshot)
            self._snapshot = snapshot

    def write_output(self, result) -> None:
        with self._mutex:
            self._require_open()
            _atomic_json(self.output_path, result)
