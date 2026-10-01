from __future__ import annotations
import json
from dataclasses import dataclass, field
from pathlib import Path

from ..config import ROOT, load_config
from ..providers.base import process_lock
from ..providers.codex_cli import CodexCliProvider
from ..providers.lmstudio import LMStudioProvider

@dataclass
class ChatDecision:
    kind: str
    response: str = ""
    task: str = ""
    questions: list[str] = field(default_factory=list)

def parse_chat_decision(content: str, original: str) -> ChatDecision:
    cleaned = content.strip()
    if cleaned.startswith("```") and cleaned.endswith("```"):
        cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        return ChatDecision("task", task=original)
    if not isinstance(value, dict) or value.get("kind") not in {"chat", "task"}:
        return ChatDecision("task", task=original)
    questions = value.get("questions") or []
    if not isinstance(questions, list):
        questions = []
    return ChatDecision(
        value["kind"],
        str(value.get("response") or "")[:6000],
        str(value.get("task") or original)[:12000],
        [str(item)[:500] for item in questions if isinstance(item, str)][:3],
    )

def decide_message(message: str, workspace: Path, history: list[dict] | None = None) -> ChatDecision:
    config = load_config(ROOT)
    agent = config["agents"]["agents"]["orchestrator"]
    roles = ", ".join(config["agents"]["agents"])
    system = (
        "You are the local chat front door for a software agent platform. "
        "Classify the latest Russian or English message as chat or task. "
        "An informational question is chat. Only classify as task when the user asks to run, change, "
        "implement, test, review, or otherwise perform work on the project. "
        f"Configured agent roles are: {roles}. "
        "For chat, answer briefly in the user's language. For a task, rewrite it into an actionable "
        "task and ask at most three questions only when missing information would materially change implementation. "
        "Never ask about details that can be discovered from the repository. Return JSON only with keys: "
        "kind ('chat' or 'task'), response, task, questions (array of strings)."
    )
    messages = [{"role": "system", "content": system}, *(history or [])[-6:],
                {"role": "user", "content": f"Workspace: {workspace}\nLatest message: {message}"}]
    if agent["provider"] == "codex":
        prompt = (system + "\n\nConversation:\n" +
                  "\n".join(f"{item['role']}: {item['content']}" for item in messages[1:]) +
                  "\n\nReturn only the requested JSON object.")
        try:
            result = CodexCliProvider(config["providers"]["codex"]).run(
                prompt, workspace, structured=False)
            return parse_chat_decision(result.summary, message)
        except Exception:
            if agent.get("fallback_provider") != "lmstudio":
                raise
    provider = LMStudioProvider(dict(config["providers"]["lmstudio"]))
    models = provider.models()
    model = agent.get("model") or agent.get("fallback_model") or provider.select_model(models)
    if model not in models:
        raise RuntimeError(f"Orchestrator fallback model is unavailable in LM Studio: {model}")
    payload = {
        "model": model, "messages": messages, "temperature": agent["temperature"],
        "max_tokens": 1200, "stream": False,
        "response_format": {"type": "json_schema", "json_schema": {
            "name": "chat_decision", "strict": True, "schema": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["chat", "task"]},
                    "response": {"type": "string"},
                    "task": {"type": "string"},
                    "questions": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
                },
                "required": ["kind", "response", "task", "questions"],
                "additionalProperties": False,
            },
        }},
    }
    lock_path = Path(provider.config.get("lock_path", ROOT / ".state" / "lmstudio.lock"))
    with process_lock(lock_path, timeout=180):
        response = provider._request("chat/completions", payload)
    content = response.get("choices", [{}])[0].get("message", {}).get("content", "")
    return parse_chat_decision(content, message)
