from __future__ import annotations
from dataclasses import dataclass, field
from contextlib import contextmanager
import os
import time
from pathlib import Path

@contextmanager
def process_lock(path: Path, timeout: float = 30):
    """One inference process per provider across independent CLI invocations."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    with path.open("r+b") as stream:
        if path.stat().st_size == 0:
            stream.write(b"\0")
            stream.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Provider lock busy: {path.name}")
                time.sleep(0.1)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

@dataclass
class AgentResult:
    status: str
    summary: str
    changed_files: list[str] = field(default_factory=list)
    test_results: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    model: str = ""

    @classmethod
    def from_mapping(cls, value: dict) -> "AgentResult":
        if value.get("status") not in {"completed", "success", "incomplete", "failed", "needs_revision"}:
            raise ValueError("AgentResult requires a recognized status")
        if not isinstance(value.get("summary"), str):
            raise ValueError("AgentResult requires a summary string")
        for key in ("changed_files", "test_results", "issues"):
            if key in value and (not isinstance(value[key], list) or not all(isinstance(item, str) for item in value[key])):
                raise ValueError(f"AgentResult {key} must be a list of strings")
        if "usage" in value and not isinstance(value["usage"], dict):
            raise ValueError("AgentResult usage must be an object")
        return cls(value["status"], value["summary"],
                   list(value.get("changed_files") or []), list(value.get("test_results") or []),
                   list(value.get("issues") or []), dict(value.get("usage") or {}), str(value.get("model", "")))
