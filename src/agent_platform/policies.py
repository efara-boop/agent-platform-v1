from __future__ import annotations
from pathlib import Path
import re

FORBIDDEN = {"git_push", "force_push", "production_credentials", "openai_api_key"}
APPROVAL = {"git_commit", "deploy", "production_ssh", "destructive_db", "production_db_write"}
ALLOWED = {"git_read", "git_diff", "git_branch", "tests", "lint", "build", "workspace_read", "workspace_write", "local_db_read", "local_db_write", "staging_db_read", "production_db_read"}

def action_policy(action: str) -> str:
    action = re.sub(r"[^a-z0-9]+", "_", action.lower()).strip("_")
    if action in FORBIDDEN:
        return "forbid"
    if action in APPROVAL:
        return "approval"
    if action in ALLOWED:
        return "allow"
    return "approval"

def workspace_path(workspace: Path, requested: str, *, write: bool = False) -> Path:
    if not requested or Path(requested).is_absolute():
        raise PermissionError("Use a relative workspace path")
    root = workspace.resolve()
    requested_parts = {part.lower() for part in Path(requested).parts}
    path = (root / requested).resolve()
    if not path.is_relative_to(root):
        raise PermissionError("Path escapes workspace")
    parts = {part.lower() for part in path.relative_to(root).parts} | requested_parts
    if parts & {".git", "vendor", ".venv", ".state", "traces"}:
        raise PermissionError("Protected directory")
    if parts & {".ssh", ".aws", ".codex"} or any(
        part.startswith(".env") or any(word in part for word in ("secret", "credential", "token", "private_key"))
        for part in parts):
        raise PermissionError("Protected credential-like file")
    return path
