"""Optional, project-local Engram CLI adapter. No server or cloud sync is started."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from ..config import ROOT


class EngramAdapter:
    SOURCE = "Gentleman-Programming/engram"

    def __init__(self, workspace: Path | None = None, data_dir: Path | None = None,
                 binary: str | Path | None = None):
        bundled = ROOT / "vendor" / "engram" / ("engram.exe" if os.name == "nt" else "engram")
        self.binary = str(binary or (bundled if bundled.is_file() else shutil.which("engram") or ""))
        self.workspace = (workspace or ROOT).resolve()
        self.data_dir = (data_dir or ROOT / ".state" / "engram").resolve()

    @property
    def available(self) -> bool:
        return bool(self.binary and (Path(self.binary).is_file() or shutil.which(self.binary)))

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        # Keep this adapter local even if another Engram installation uses cloud sync.
        for key in ("ENGRAM_CLOUD_TOKEN", "ENGRAM_CLOUD_SERVER", "ENGRAM_CLOUD_AUTOSYNC"):
            env.pop(key, None)
        env["ENGRAM_DATA_DIR"] = str(self.data_dir)
        env["ENGRAM_NO_UPDATE_CHECK"] = "1"
        return env

    def version(self) -> str | None:
        if not self.available:
            return None
        try:
            result = subprocess.run([self.binary, "--version"], cwd=self.workspace,
                                    env=self._env(), stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, encoding="utf-8",
                                    errors="replace", timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return result.stdout.strip() if result.returncode == 0 else None

    def _run(self, args: list[str]) -> str | None:
        if not self.available:
            return None
        result = subprocess.run([self.binary, *args], cwd=self.workspace, env=self._env(),
                                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=15)
        if result.returncode:
            raise RuntimeError(f"Engram {args[0]} failed: {result.stderr.strip()[:300]}")
        return result.stdout.strip()

    def search(self, query: str, max_chars: int = 3000) -> str | None:
        if not query.strip():
            return None
        output = self._run(["search", query[:300], "--match", "any"])
        if not output or output.startswith("No memories found"):
            return None
        return output[:max_chars]

    def context(self, max_chars: int = 3000) -> str | None:
        output = self._run(["context"])
        if not output or output.startswith("No previous session memories found"):
            return None
        return output[:max_chars]

    def remember(self, title: str, note: str) -> bool:
        if not title.strip() or len(title) > 120:
            raise ValueError("Engram title must be short and nonempty")
        if not note.strip() or len(note) > 2000:
            raise ValueError("Engram notes must be concise, not source code")
        if not self.available:
            return False
        self._run(["save", title, note])
        return True


def memory_safe_text(value: str, limit: int = 1000) -> str:
    """Keep durable notes short; redact common credential-bearing text."""
    value = re.sub(r"(?i)bearer\s+[A-Za-z0-9._-]+", "Bearer [REDACTED]", value)
    value = re.sub(r"\bsk-[A-Za-z0-9_-]{10,}\b", "[REDACTED]", value)
    value = re.sub(r"(?i)(api[_ -]?key|password|secret|credential|token)\s*[:=]\s*\S+",
                   r"\1=[REDACTED]", value)
    return value.strip()[:limit]


def may_contain_secret(value: str, strict: bool = False) -> bool:
    """Fail closed for automatic durable memory writes."""
    patterns = (
        r"(?i)-----BEGIN [A-Z ]*PRIVATE KEY-----",
        r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/-]{8,}",
        r"(?i)\b(?:password|passwd|secret|api[_ -]?key|access[_ -]?token|credential)\s*[:=]\s*\S+",
        r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b",
        r"\b(?:ghp_|gho_|github_pat_|xox[baprs]-|sk-)[A-Za-z0-9_-]{10,}\b",
        r"\b[A-Fa-f0-9]{32,}\b",
        r"\b[A-Za-z0-9+/]{48,}={0,2}\b",
    )
    if any(re.search(pattern, value) for pattern in patterns):
        return True
    return strict and bool(re.search(
        r"(?i)\b(?:password|secret|credential|token|private[_ -]?key|authorization)\b", value))
