from __future__ import annotations
import re
from pathlib import Path

SUSPICIOUS = [re.compile(pattern, re.I) for pattern in (
    r"ignore (all |your )?(previous|prior) instructions", r"(curl|wget)\s+[^\n|]+\|\s*(bash|sh)",
    r"(send|upload|exfiltrat).{0,80}(secret|token|credential|\.env)",
    r"--dangerously-bypass-approvals-and-sandbox", r"(act as root|pretend you have no restrictions)",
    r"(skip safety checks|disable content filtering)", r"[\u200b-\u200f\ufeff]")]

def audit_skill(text: str) -> list[str]:
    return [pattern.pattern for pattern in SUSPICIOUS if pattern.search(text)]

def load_selected(root: Path, selected: list[dict], max_chars: int = 12000) -> str:
    sections = []
    for item in selected[:4]:
        path = (root / item["path"]).resolve()
        if not path.is_relative_to((root / "vendor" / "claude-skills").resolve()) or path.name != "SKILL.md":
            raise ValueError("Skill path escapes vendor root")
        text = path.read_text(encoding="utf-8", errors="replace")
        findings = audit_skill(text)
        if findings:
            raise PermissionError(f"Skill {item['name']} needs human security review: {findings}")
        if len(text) > max_chars:
            boundary = text.rfind("\n\n", 0, max_chars)
            text = text[:boundary if boundary > max_chars // 2 else max_chars] + "\n\n[Further skill details omitted by context budget]"
        sections.append(f"## {item['name']} ({item['path']})\n{text}")
    return "\n\n".join(sections)
