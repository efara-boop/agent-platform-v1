from __future__ import annotations
import re
from pathlib import Path
from .budget import fit_sections
from ..memory.project_memory import load_project_knowledge

IGNORED = {".git", ".state", "traces", "vendor", "__pycache__", ".venv"}

def build_context(root: Path, task: str, target_tokens: int = 32768) -> tuple[str, list[str]]:
    docs = list(load_project_knowledge(root).items())
    terms = set(re.findall(r"[\w.-]{4,}", task.lower()))
    files = []
    for path in root.rglob("*"):
        if not path.is_file() or any(part in IGNORED for part in path.relative_to(root).parts):
            continue
        if not path.resolve().is_relative_to(root.resolve()):
            continue
        if path.suffix.lower() not in {".py", ".php", ".js", ".ts", ".twig", ".toml", ".md"}:
            continue
        if path.name.lower().startswith(".env") or any(word in path.name.lower() for word in ("secret", "credential", "token")):
            continue
        relative = path.relative_to(root).as_posix()
        match = len(terms & set(re.findall(r"[\w.-]{4,}", relative.lower())))
        if match and path.stat().st_size <= 30000:
            files.append((match, relative, path))
    files.sort(key=lambda item: (-item[0], item[1]))
    sections = docs + [(relative, path.read_text(encoding="utf-8", errors="replace")) for _, relative, path in files[:12]]
    return fit_sections(sections, min(target_tokens, 32768) - 6000)
