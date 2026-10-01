from __future__ import annotations
import json
import re
from pathlib import Path

TOKEN = re.compile(r"[\w-]{3,}", re.UNICODE)

def frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---", 4)
    if end < 0:
        return {}
    result = {}
    folded = None
    for line in text[4:end].splitlines():
        if ":" in line and not line[0].isspace():
            key, value = line.split(":", 1)
            key, value = key.strip(), value.strip().strip("\"'")
            result[key] = "" if value in (">", "|") else value
            folded = key if value in (">", "|") else None
        elif folded and line.startswith(" "):
            result[folded] += " " + line.strip()
    return result

def build_index(root: Path, vendor: Path, output: Path) -> list[dict]:
    entries = []
    for path in sorted(vendor.rglob("SKILL.md")):
        if any(part.startswith(".") for part in path.relative_to(vendor).parts):
            continue  # Hidden adapter copies duplicate canonical skills.
        text = path.read_text(encoding="utf-8", errors="replace")
        meta = frontmatter(text)
        relative = path.relative_to(root).as_posix()
        domain = path.relative_to(vendor).parts[0]
        name = meta.get("name") or path.parent.name
        description = meta.get("description") or next((line.lstrip("# ") for line in text.splitlines() if line.startswith("# ")), "")
        keywords = sorted(set(TOKEN.findall(f"{name} {description} {domain}".lower())))
        entries.append({"name": name, "description": description[:600], "domain": domain,
                        "path": relative, "keywords": keywords})
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    return entries

def read_index(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))
