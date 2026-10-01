from pathlib import Path

FILES = ("PROJECT.md", "ARCHITECTURE.md", "STACK.md", "CONVENTIONS.md", "TESTING.md")

def load_project_knowledge(root: Path) -> dict[str, str]:
    return {f".ai/{name}": (root / ".ai" / name).read_text(encoding="utf-8")
            for name in FILES if (root / ".ai" / name).is_file()}
