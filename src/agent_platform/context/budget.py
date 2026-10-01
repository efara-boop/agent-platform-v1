from __future__ import annotations

import json


def estimate_tokens(text: str) -> int:
    return (len(text) + 3) // 4


def estimate_request_tokens(payload: dict) -> int:
    """Heuristic for the preferred context target, calibrated after a response."""
    serialized = json.dumps(payload.get("messages", []), ensure_ascii=False, separators=(",", ":"))
    serialized += json.dumps(payload.get("tools", []), ensure_ascii=False, separators=(",", ":"))
    return estimate_tokens(serialized) + 64


def request_byte_bound(payload: dict) -> int:
    """Pessimistic bound using the same JSON serialization sent over HTTP."""
    serialized = json.dumps(payload).encode("utf-8")
    return len(serialized) + 512 + 64 * len(payload.get("messages", []))


def context_budget(prompt_tokens: int, context_length: int, output_reserve: int,
                   warning_ratio: float = 0.8) -> dict:
    input_limit = max(1, context_length - output_reserve)
    ratio = prompt_tokens / input_limit
    return {"prompt_tokens": prompt_tokens, "input_limit": input_limit,
            "remaining_input_tokens": max(0, input_limit - prompt_tokens),
            "utilization": round(ratio, 4),
            "level": "stop" if ratio >= 1 else "warning" if ratio >= warning_ratio else "ok"}

def fit_sections(sections: list[tuple[str, str]], target_tokens: int) -> tuple[str, list[str]]:
    remaining = target_tokens
    chosen = []
    names = []
    for name, content in sections:
        cost = estimate_tokens(content)
        if cost <= remaining:
            chosen.append(f"### {name}\n{content}")
            names.append(name)
            remaining -= cost
    return "\n\n".join(chosen), names
