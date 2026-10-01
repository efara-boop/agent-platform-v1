from __future__ import annotations
import json
import socket
import threading
import time
from collections.abc import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from .base import AgentResult, process_lock
from ..config import ROOT
from .workspace_tools import TOOL_SCHEMAS, WorkspaceTools
from ..context.budget import context_budget, estimate_request_tokens, request_byte_bound
from pathlib import Path

_LOCK = threading.Semaphore(1)

def parse_chat_response(value: dict) -> AgentResult:
    try:
        message = value["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("Invalid LM Studio chat response") from exc
    if isinstance(message, list):
        message = "\n".join(part.get("text", "") for part in message if isinstance(part, dict))
    if not isinstance(message, str):
        raise ValueError("Invalid LM Studio content")
    try:
        parsed = json.loads(message)
    except json.JSONDecodeError:
        result = AgentResult("completed", message.strip())
    else:
        result = AgentResult.from_mapping(parsed) if isinstance(parsed, dict) else AgentResult("completed", message.strip())
    result.usage = value.get("usage") or {}
    result.model = value.get("model") or ""
    return result

class LMStudioProvider:
    def __init__(self, config: dict):
        self.config = config
        self.base_url = config["base_url"].rstrip("/")

    def _request(self, path: str, payload: dict | None = None, timeout: float | None = None) -> dict:
        data = json.dumps(payload).encode() if payload is not None else None
        request = Request(f"{self.base_url}/v1/{path}", data=data,
            headers={"Content-Type": "application/json"} if data else {}, method="POST" if data else "GET")
        retries = self.config.get("retries", 2)
        for attempt in range(retries + 1):
            try:
                with urlopen(request, timeout=timeout or self.config.get("timeout_seconds", 120)) as response:
                    return json.load(response)
            except HTTPError:
                raise
            except (URLError, TimeoutError, socket.timeout, ConnectionError):
                if attempt == retries:
                    raise
                time.sleep(0.5 * (attempt + 1))
        raise AssertionError("unreachable")

    def models(self) -> list[str]:
        value = self._request("models", timeout=self.config.get("connect_timeout_seconds", 5))
        return [item["id"] for item in value.get("data", []) if isinstance(item, dict) and isinstance(item.get("id"), str)]

    def loaded_context_length(self, model: str) -> int | None:
        """Read native LM Studio metadata when supported; do not change model settings."""
        try:
            request = Request(f"{self.base_url}/api/v1/models", method="GET")
            with urlopen(request, timeout=self.config.get("connect_timeout_seconds", 5)) as response:
                items = json.load(response).get("models", [])
        except (HTTPError, URLError, TimeoutError, socket.timeout, OSError, ValueError, KeyError):
            return None
        for item in items:
            for instance in item.get("loaded_instances", []):
                if instance.get("id") == model:
                    length = instance.get("config", {}).get("context_length")
                    return length if isinstance(length, int) and length > 0 else None
        return None

    @staticmethod
    def select_model(models: list[str]) -> str:
        qwen = [m for m in models if "qwen" in m.lower() and ("coder" in m.lower() or "code" in m.lower())]
        if not qwen:
            raise RuntimeError(f"No Qwen Coder model available; models={models}")
        def score(model: str) -> int:
            name = model.lower()
            return sum(weight for term, weight in (("huihui", 3), ("30b", 4), ("a3b", 4),
                ("instruct", 1), ("abliterated", 3), ("i1", 1), ("q4_k_m", 3)) if term in name)
        return max(qwen, key=score)

    def run(self, system_prompt: str, task: str, context: str, skills: str, model: str, temperature: float,
            workspace=None, on_round: Callable[[dict], None] | None = None,
            on_warning: Callable[[dict], None] | None = None) -> AgentResult:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"TASK\n{task}\n\nSELECTED PROJECT CONTEXT\n{context}\n\nSELECTED SKILLS\n{skills}\n\nUse workspace tools to inspect and edit files. Read before editing. Stop within 12 tool rounds. Return one JSON object: status (completed, success, incomplete, failed, or needs_revision), summary (string), changed_files (array of strings), test_results (array of strings), issues (array of strings). Use empty arrays when none."}]
        tools = WorkspaceTools(workspace) if workspace is not None else None
        lock_path = Path(self.config.get("lock_path", ROOT / ".state" / "lmstudio.lock"))
        loaded_limit = self.config.get("loaded_context_length")
        if not isinstance(loaded_limit, int) or loaded_limit <= 0:
            loaded_limit = self.loaded_context_length(model)
        if not isinstance(loaded_limit, int) or loaded_limit <= 0:
            loaded_limit = self.config.get("known_context_length")
        if not isinstance(loaded_limit, int) or loaded_limit <= 0:
            return AgentResult("incomplete", "Loaded model context length is unknown",
                issues=["Check LM Studio loaded model metadata or set known_context_length to the verified loaded window"],
                model=model)
        target = self.config.get("context_target_tokens", 32768)
        context_limit = min(target, loaded_limit)
        output_reserve = self.config.get("max_output_tokens", 4096)
        warning_ratio = self.config.get("context_warning_ratio", 0.8)
        hard_limit = loaded_limit
        usage_rounds: list[dict] = []
        warnings: list[dict] = []
        calibration = 1.0
        format_retries = 0
        with _LOCK, process_lock(lock_path):
            for round_number in range(1, 13):
                if tools:
                    compact_tool_history(messages, tools.changed_files)
                while True:
                    payload = {"model": model, "messages": messages, "temperature": temperature,
                               "max_tokens": output_reserve, "stream": False}
                    if tools:
                        payload["tools"] = TOOL_SCHEMAS
                        payload["tool_choice"] = "auto"
                    raw_estimate = estimate_request_tokens(payload)
                    predicted = max(raw_estimate, int(raw_estimate * calibration))
                    budget = context_budget(predicted, context_limit, output_reserve, warning_ratio)
                    hard_bound = request_byte_bound(payload)
                    hard_stop = hard_bound >= hard_limit - output_reserve
                    if (budget["level"] != "stop" and not hard_stop) or not tools or len(messages) <= 4:
                        break
                    before = len(messages)
                    compact_tool_history(messages, tools.changed_files,
                                         max_chars=max(1, sum(len(json.dumps(m)) for m in messages) - 1))
                    if len(messages) >= before:
                        break
                if budget["level"] != "ok" or hard_stop:
                    level = "stop" if hard_stop else budget["level"]
                    warning = {"round": round_number, "level": level,
                               "estimated_prompt_tokens": predicted, "context_limit": context_limit,
                               "output_reserve": output_reserve,
                               "hard_bound_tokens": hard_bound, "loaded_context_limit": hard_limit}
                    warnings.append(warning)
                    if on_warning:
                        on_warning(warning)
                if budget["level"] == "stop" or hard_stop:
                    return AgentResult("incomplete", "Context budget reached before request",
                                       tools.changed_files if tools else [], tools.test_results if tools else [],
                                       ["Context budget reached; select less context before retrying"],
                                       usage={"rounds": usage_rounds, "warnings": warnings,
                                              "context_limit": context_limit}, model=model)
                started = time.monotonic()
                response = self._request("chat/completions", payload)
                server_usage = response.get("usage") or {}
                actual = server_usage.get("prompt_tokens")
                if isinstance(actual, int) and actual > 0:
                    calibration = max(1.0, actual / max(1, raw_estimate))
                round_usage = {"round": round_number, "estimated_prompt_tokens": predicted,
                               "prompt_tokens": actual if isinstance(actual, int) else None,
                               "completion_tokens": server_usage.get("completion_tokens"),
                               "total_tokens": server_usage.get("total_tokens"),
                               "duration_seconds": round(time.monotonic() - started, 3),
                               "context_limit": context_limit}
                usage_rounds.append(round_usage)
                if on_round:
                    on_round(round_usage)
                message = response["choices"][0]["message"]
                calls = message.get("tool_calls") or []
                if not calls:
                    try:
                        result = parse_chat_response(response)
                    except ValueError as exc:
                        if format_retries == 0:
                            format_retries += 1
                            messages.append(message)
                            messages.append({"role": "user", "content":
                                f"Invalid AgentResult: {str(exc)[:160]}. Return only a JSON object with status and summary strings, and changed_files, test_results, issues as arrays of strings. Do not call tools again."})
                            continue
                        result = AgentResult("incomplete", "Model returned invalid AgentResult JSON",
                            issues=[str(exc)[:160]], model=model)
                    if tools:
                        result.changed_files = tools.changed_files
                        result.test_results = tools.test_results
                    result.usage = {**result.usage, "rounds": usage_rounds,
                                    "warnings": warnings, "context_limit": context_limit}
                    return result
                if not tools:
                    raise RuntimeError("Unexpected LM Studio tool calls")
                messages.append(message)
                for call in calls:
                    try:
                        args = json.loads(call["function"].get("arguments") or "{}")
                        output = tools.dispatch(call["function"]["name"], args)
                    except (KeyError, ValueError, PermissionError, OSError) as exc:
                        output = {"error": str(exc)}
                    messages.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(output, ensure_ascii=False)})
            return AgentResult("incomplete", "Tool round limit reached", tools.changed_files, tools.test_results,
                               ["LM Studio agent exceeded 12 tool rounds"],
                               usage={"rounds": usage_rounds, "warnings": warnings,
                                      "context_limit": context_limit}, model=model)

def compact_tool_history(messages: list[dict], changed_files: list[str], max_chars: int = 120000) -> None:
    """Remove oldest complete tool rounds before context overflow; keep recent evidence."""
    while sum(len(json.dumps(message)) for message in messages) > max_chars and len(messages) > 4:
        start = 2
        if messages[start].get("role") == "user" and messages[start].get("content", "").startswith("Earlier tool rounds"):
            start += 1
        if start >= len(messages) or messages[start].get("role") != "assistant":
            break
        end = start + 1
        while end < len(messages) and messages[end].get("role") == "tool":
            end += 1
        del messages[start:end]
        summary = "Earlier tool rounds compacted. Changed files: " + ", ".join(changed_files)
        if len(messages) > 2 and messages[2].get("role") == "user" and messages[2].get("content", "").startswith("Earlier tool rounds"):
            messages[2]["content"] = summary
        else:
            messages.insert(2, {"role": "user", "content": summary})
