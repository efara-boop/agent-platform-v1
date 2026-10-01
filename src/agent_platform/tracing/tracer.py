from __future__ import annotations
import json
import re
from datetime import datetime, timezone
from pathlib import Path

REDACT_KEYS = {"secret", "token", "password", "credential", "api_key", "authorization"}
NUMERIC_TOKEN_KEYS = {"prompt_tokens", "completion_tokens", "total_tokens", "estimated_prompt_tokens",
                      "hard_bound_tokens", "context_target_tokens", "max_output_tokens"}

def scrub(value):
    if isinstance(value, dict):
        return {key: item if key.lower() in NUMERIC_TOKEN_KEYS and isinstance(item, (int, float))
                else "[REDACTED]" if any(term in key.lower() for term in REDACT_KEYS) else scrub(item)
                for key, item in value.items()}
    if isinstance(value, list):
        return [scrub(item) for item in value]
    if isinstance(value, str):
        value = re.sub(r"(?i)bearer\s+[A-Za-z0-9._-]+", "Bearer [REDACTED]", value)
        return re.sub(r"\bsk-[A-Za-z0-9_-]{10,}\b", "[REDACTED]", value)
    return value

class Tracer:
    def __init__(self, root: Path):
        self.path = root / "traces" / "runs.jsonl"

    def write(self, event: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        value = {"timestamp": datetime.now(timezone.utc).isoformat(), **scrub(event)}
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(value, ensure_ascii=False) + "\n")
