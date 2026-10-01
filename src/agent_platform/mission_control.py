from __future__ import annotations
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .memory.working_state import WorkingState

ROLE_MAP = {
    "orchestrator": "agent",
    "analyst": "researcher",
    "architect": "assistant",
    "backend": "coder",
    "frontend": "coder",
    "database": "coder",
    "qa": "tester",
    "devops": "devops",
    "reviewer": "reviewer",
}


@dataclass
class MissionControlClient:
    enabled: bool
    url: str
    api_key: str
    tool_name: str = "agent-platform-v1"
    tool_version: str = "0.1.0"
    agent_prefix: str = "promoney"
    timeout_seconds: int = 5

    @classmethod
    def from_config(cls, config: dict) -> "MissionControlClient":
        value = config["mission_control"]["mission_control"]
        return cls(
            enabled=value["enabled"],
            url=value["url"].rstrip("/"),
            api_key=value["api_key"],
            tool_name=value.get("tool_name", "agent-platform-v1"),
            tool_version=value.get("tool_version", "0.1.0"),
            agent_prefix=value.get("agent_prefix", "promoney"),
            timeout_seconds=int(value.get("timeout_seconds", 5)),
        )

    def agent_name(self, role: str) -> str:
        return f"{self.agent_prefix}-{role}"

    def request(self, method: str, path: str, payload: dict | None = None) -> dict:
        if not self.enabled:
            return {"skipped": True, "reason": "disabled"}
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.url + path,
            data=body,
            method=method,
            headers={
                "Content-Type": "application/json",
                "x-api-key": self.api_key,
                "X-Agent-Name": self.tool_name,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw else {"ok": True}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"Mission Control HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Mission Control unavailable: {exc.reason}") from exc

    def connect(self, role: str) -> dict:
        return self.request("POST", "/api/connect", {
            "tool_name": self.tool_name,
            "tool_version": self.tool_version,
            "agent_name": self.agent_name(role),
            "agent_role": ROLE_MAP.get(role, "agent"),
        })

    def heartbeat(self, agent_id: int, connection_id: str, usage: dict | None = None) -> dict:
        payload = {"connection_id": connection_id}
        token_usage = token_payload(usage or {})
        if token_usage:
            payload["token_usage"] = token_usage
        return self.request("POST", f"/api/agents/{agent_id}/heartbeat", payload)

    def disconnect(self, connection_id: str) -> dict:
        return self.request("DELETE", "/api/connect", {"connection_id": connection_id})

    def emit_agent_run(self, role: str, usage: dict | None = None) -> dict:
        if not self.enabled:
            return {"skipped": True, "reason": "disabled"}
        connection = self.connect(role)
        agent_id = connection.get("agent_id")
        connection_id = connection.get("connection_id")
        if not agent_id or not connection_id:
            raise RuntimeError("Mission Control connect response did not include agent_id and connection_id")
        try:
            heartbeat = self.heartbeat(int(agent_id), str(connection_id), usage)
            return {"connected": connection, "heartbeat": heartbeat}
        finally:
            self.disconnect(str(connection_id))


def token_payload(usage: dict) -> dict:
    rounds = usage.get("rounds")
    if isinstance(rounds, list) and rounds:
        usage = rounds[-1] if isinstance(rounds[-1], dict) else usage
    input_tokens = usage.get("inputTokens", usage.get("prompt_tokens", usage.get("estimated_prompt_tokens", 0)))
    output_tokens = usage.get("outputTokens", usage.get("completion_tokens", 0))
    total_tokens = usage.get("total_tokens")
    if not input_tokens and total_tokens and not output_tokens:
        input_tokens = total_tokens
    if not input_tokens and not output_tokens:
        return {}
    payload = {"inputTokens": int(input_tokens or 0), "outputTokens": int(output_tokens or 0)}
    if usage.get("model"):
        payload["model"] = str(usage["model"])
    return payload


def write_mc_trace(workspace: Path, event: dict) -> None:
    from .tracing.tracer import Tracer
    Tracer(workspace).write({"event": "mission_control", **event})

