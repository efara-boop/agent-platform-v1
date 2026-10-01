from __future__ import annotations
import re

ALIASES = {"rest": {"api", "endpoint"}, "symfony": {"php", "backend"},
           "kafka": {"events", "messaging", "streaming"}, "security": {"audit", "secure"}}
STOPWORDS = {"return", "summary", "make", "file", "files", "changes", "change", "current",
             "please", "exactly", "analyze", "review", "implement", "project", "repository",
             "with", "from", "into", "this", "that", "task", "code", "test", "tests"}

def route_skills(task: str, index: list[dict], limit: int = 4) -> list[dict]:
    words = set(re.findall(r"[\w]{3,}", task.lower())) - STOPWORDS
    expanded = words | {term for word in words for term in ALIASES.get(word, set())}
    dev_task = bool(words & {"symfony", "kafka", "rest", "api", "endpoint", "backend", "frontend", "database"})
    ranked = []
    for item in index:
        name = set(re.findall(r"[\w]{3,}", item["name"].lower()))
        description = item["description"].lower()
        keys = set(item["keywords"])
        name_hits = len(expanded & name)
        description_hits = sum(1 for word in words if word in description)
        score = 6 * name_hits + len(expanded & keys) + description_hits
        if dev_task and item["domain"] in {"engineering", "engineering-team"}:
            score += 3
        if dev_task and item["domain"] in {"product-team", "marketing", "business-growth"}:
            score -= 3
        if score >= 7 and (name_hits or description_hits >= 2):
            ranked.append((score, item))
    ranked.sort(key=lambda pair: (-pair[0], pair[1]["name"], pair[1]["path"]))
    return [item for _, item in ranked[:max(1, min(limit, 4))]]
