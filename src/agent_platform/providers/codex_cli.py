from __future__ import annotations
import json
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from .base import AgentResult, process_lock
from ..config import ROOT

_LOCK = threading.Semaphore(1)
SCHEMA = {"type": "object", "properties": {
    "status": {"type": "string", "enum": ["completed", "success", "incomplete", "failed", "needs_revision"]}, "summary": {"type": "string"},
    "changed_files": {"type": "array", "items": {"type": "string"}},
    "test_results": {"type": "array", "items": {"type": "string"}},
    "issues": {"type": "array", "items": {"type": "string"}}},
    "required": ["status", "summary", "changed_files", "test_results", "issues"], "additionalProperties": False}

def parse_codex_response(output: str) -> AgentResult:
    try:
        value = json.loads(output.strip())
    except json.JSONDecodeError:
        return AgentResult("completed", output.strip())
    if not isinstance(value, dict):
        raise ValueError("Codex output must be an object")
    return AgentResult.from_mapping(value)

def find_codex_binary(config: dict | None = None) -> str | None:
    config = config or {}
    configured = config.get("binary", "codex")
    path = Path(configured).expanduser()
    if path.is_file():
        return str(path)
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            bin_root = Path(local_app_data) / "OpenAI" / "Codex" / "bin"
            if bin_root.is_dir():
                candidates = sorted(bin_root.glob("*/codex.exe"), key=lambda item: item.stat().st_mtime, reverse=True)
                if candidates:
                    return str(candidates[0])
    binary = shutil.which(configured)
    if binary:
        return binary
    return None

def codex_command(config: dict | None = None) -> list[str]:
    config = config or {}
    binary = find_codex_binary(config)
    if not binary:
        raise RuntimeError("codex executable not found")
    return [binary, *config.get("args", [])]

class CodexCliProvider:
    def __init__(self, config: dict):
        self.config = config
        self.last_events: list[dict] = []

    def run(self, prompt: str, workspace: Path, *, structured: bool = True, capture_events: bool = False) -> AgentResult:
        self.last_events = []
        if len(prompt) > 24000:
            raise ValueError("Codex prompt exceeds safe Windows command-line budget")
        base_command = codex_command(self.config)
        workspace = workspace.resolve()
        lock_path = Path(self.config.get("lock_path", ROOT / ".state" / "codex.lock"))
        with _LOCK, process_lock(lock_path), tempfile.TemporaryDirectory(prefix="agent-codex-") as temp:
            last = Path(temp) / "last.txt"
            command = [*base_command, "exec", "--sandbox", "read-only", "--cd", str(workspace), "--output-last-message", str(last)]
            if self.config.get("ephemeral", True):
                command.append("--ephemeral")
            if capture_events:
                command.append("--json")
            if structured and self.config.get("structured_output", True):
                schema = Path(temp) / "result.schema.json"
                schema.write_text(json.dumps(SCHEMA), encoding="utf-8")
                command += ["--output-schema", str(schema)]
            if not (workspace / ".git").exists():
                command.append("--skip-git-repo-check")
            command.append(prompt)
            env = os.environ.copy()
            env.pop("OPENAI_API_KEY", None)
            try:
                completed = subprocess.run(command, cwd=workspace, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                    timeout=self.config.get("timeout_seconds", 180), env=env)
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(f"codex exec timed out after {exc.timeout}s") from None
            if completed.returncode:
                raise RuntimeError(f"codex exec failed ({completed.returncode}): {(completed.stderr or completed.stdout)[-1800:].strip()}")
            if capture_events:
                for line in completed.stdout.splitlines():
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(event, dict):
                        self.last_events.append(event)
            output = last.read_text(encoding="utf-8") if last.exists() else completed.stdout
            result = parse_codex_response(output)
            result.model = "codex-cli-default"
            return result
