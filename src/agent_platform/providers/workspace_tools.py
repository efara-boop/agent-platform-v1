from __future__ import annotations
import hashlib
import os
import re
import tempfile
from pathlib import Path
from ..policies import workspace_path

SKIP = {".git", "vendor", ".venv", ".state", "traces", "__pycache__"}

TOOL_SCHEMAS = [
    {"type": "function", "function": {"name": "list_files", "description": "List workspace files matching a substring. No vendor or secret files.",
      "parameters": {"type": "object", "properties": {"contains": {"type": "string"}}, "required": ["contains"]}}},
    {"type": "function", "function": {"name": "read_file", "description": "Read an existing workspace file, returning text and SHA256. Read before editing.",
      "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "write_file", "description": "Write a workspace file atomically. For existing files supply the SHA256 returned by read_file. Never write secrets or destructive commands.",
      "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}, "expected_sha256": {"type": "string"}}, "required": ["path", "content"]}}},
    {"type": "function", "function": {"name": "run_tests", "description": "Run the project's Python unittest suite only.",
      "parameters": {"type": "object", "properties": {}, "required": []}}},
]

class WorkspaceTools:
    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve()
        self.changed_files: list[str] = []
        self.test_results: list[str] = []

    def dispatch(self, name: str, args: dict) -> dict:
        if name == "list_files":
            needle = str(args.get("contains", "")).lower()
            files = [path.relative_to(self.workspace).as_posix() for path in self.workspace.rglob("*")
                     if path.is_file() and not any(part in SKIP for part in path.relative_to(self.workspace).parts)
                     and is_visible_workspace_file(self.workspace, path)
                     and needle in path.relative_to(self.workspace).as_posix().lower()]
            return {"files": files[:200]}
        if name == "read_file":
            path = workspace_path(self.workspace, str(args["path"]))
            if path.stat().st_size > 100_000:
                raise ValueError("File too large for context")
            raw = path.read_bytes()
            return {"content": raw.decode("utf-8"), "sha256": hashlib.sha256(raw).hexdigest()}
        if name == "write_file":
            path = workspace_path(self.workspace, str(args["path"]), write=True)
            content = str(args["content"])
            if len(content.encode("utf-8")) > 200_000:
                raise ValueError("Write exceeds limit")
            if path.exists():
                current = hashlib.sha256(path.read_bytes()).hexdigest()
                if args.get("expected_sha256") != current:
                    raise PermissionError("Existing file requires matching expected_sha256")
            elif args.get("expected_sha256"):
                raise ValueError("Expected hash supplied for a new file")
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temp = tempfile.mkstemp(prefix="agent-write-", dir=path.parent)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    stream.write(content)
                os.replace(temp, path)
            finally:
                if os.path.exists(temp):
                    os.unlink(temp)
            relative = path.relative_to(self.workspace).as_posix()
            if relative not in self.changed_files:
                self.changed_files.append(relative)
            return {"written": relative}
        if name == "run_tests":
            from ..config import load_config
            from .codex_cli import CodexCliProvider
            prompt = ("Run only `python -B -m unittest discover -s tests -v` in this workspace. "
                "Do not use network, edit files, launch other agents, or run any other command. "
                "Return AgentResult JSON with the observed test exit status and a concise test_results entry.")
            provider = CodexCliProvider(load_config()["providers"]["codex"])
            provider.run(prompt, self.workspace, capture_events=True)
            summary = verify_unittest_events(provider.last_events)
            self.test_results.append(summary)
            return {"status": "success", "output": summary}
        raise PermissionError(f"Unknown tool {name}")

def is_visible_workspace_file(workspace: Path, path: Path) -> bool:
    try:
        workspace_path(workspace, path.relative_to(workspace).as_posix())
        return True
    except (PermissionError, ValueError):
        return False

def verify_unittest_events(events: list[dict]) -> str:
    command_items = [event["item"] for event in events if event.get("type") in {"item.started", "item.completed"}
                     and isinstance(event.get("item"), dict) and event["item"].get("type") == "command_execution"]
    completed = [event["item"] for event in events if event.get("type") == "item.completed"
                 and isinstance(event.get("item"), dict) and event["item"].get("type") == "command_execution"]
    if len(completed) != 1 or any(item.get("command") != completed[0].get("command") for item in command_items):
        raise RuntimeError("Expected exactly one observed test command in Codex JSONL events")
    command = str(completed[0].get("command", "")).strip()
    exact = "python -B -m unittest discover -s tests -v"
    wrapped = re.fullmatch(r'"[^"\n]*pwsh\.exe"\s+-Command\s+\'python -B -m unittest discover -s tests -v\'', command, re.I)
    if command != exact and not wrapped:
        raise RuntimeError("Codex executed an unexpected command")
    exit_code = completed[0].get("exit_code")
    if type(exit_code) is not int:
        raise RuntimeError("Test command has no observed integer exit code")
    if exit_code:
        raise RuntimeError(f"Observed unittest exit code {exit_code}")
    return "Observed unittest command exited 0 in Codex JSONL events"
