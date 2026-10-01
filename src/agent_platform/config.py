from __future__ import annotations
import tomllib
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

def load_config(root: Path = ROOT) -> dict:
    config = {}
    for name in ("providers", "agents", "policies", "skills", "memory", "mission_control"):
        with (root / "config" / f"{name}.toml").open("rb") as stream:
            config[name] = tomllib.load(stream)
    if os.environ.get("LMSTUDIO_BASE_URL"):
        config["providers"]["lmstudio"]["base_url"] = os.environ["LMSTUDIO_BASE_URL"]
    mc = config["mission_control"]["mission_control"]
    if os.environ.get("MC_URL"):
        mc["url"] = os.environ["MC_URL"]
    if os.environ.get("MC_API_KEY"):
        mc["api_key"] = os.environ["MC_API_KEY"]
    if os.environ.get("MC_ENABLED"):
        mc["enabled"] = os.environ["MC_ENABLED"].lower() in {"1", "true", "yes", "on"}
    validate_config(config, root)
    return config

def validate_config(config: dict, root: Path = ROOT) -> None:
    roles = config["agents"]["agents"]
    expected = {"orchestrator", "analyst", "architect", "backend", "frontend", "database", "qa", "devops", "reviewer"}
    if set(roles) != expected:
        raise ValueError(f"Agent registry mismatch: {sorted(expected ^ set(roles))}")
    for name, agent in roles.items():
        if agent["provider"] not in {"codex", "lmstudio"} or not (root / agent["prompt"]).is_file():
            raise ValueError(f"Invalid provider or prompt for {name}")
        fallback = agent.get("fallback_provider")
        if fallback is not None and fallback not in {"lmstudio"}:
            raise ValueError(f"Invalid fallback provider for {name}")
        if fallback == "lmstudio" and not agent.get("fallback_model"):
            raise ValueError(f"LM Studio fallback for {name} requires fallback_model")
    for provider in ("codex", "lmstudio"):
        if config["providers"][provider]["max_concurrency"] != 1:
            raise ValueError(f"{provider} concurrency must be 1")
    lmstudio = config["providers"]["lmstudio"]
    if lmstudio["context_target_tokens"] <= 0 or lmstudio["max_output_tokens"] <= 0:
        raise ValueError("LM Studio context and output limits must be positive")
    if lmstudio.get("known_context_length", 0) < 0:
        raise ValueError("known_context_length must be zero or a verified positive length")
    if not 0 < lmstudio.get("context_warning_ratio", 0.8) < 1:
        raise ValueError("context_warning_ratio must be between zero and one")
    memory = config["memory"]["engram"]
    if not isinstance(memory["enabled"], bool) or not isinstance(memory["auto_save_completed"], bool):
        raise ValueError("Engram flags must be booleans")
    if not 0 < memory["max_recall_chars"] <= 8000:
        raise ValueError("Engram recall limit must be 1..8000 characters")
    mc = config["mission_control"]["mission_control"]
    if not isinstance(mc["enabled"], bool):
        raise ValueError("Mission Control enabled flag must be boolean")
    if mc["enabled"] and (not mc["url"] or not mc["api_key"]):
        raise ValueError("Mission Control requires url and api_key when enabled")
    if mc["timeout_seconds"] <= 0:
        raise ValueError("Mission Control timeout must be positive")
