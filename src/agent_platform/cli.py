from __future__ import annotations
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit
from .config import ROOT, load_config
from .context.builder import build_context
from .memory.engram import EngramAdapter, may_contain_secret
from .providers.codex_cli import CodexCliProvider, codex_command
from .providers.lmstudio import LMStudioProvider
from .skills.indexer import build_index, read_index
from .skills.loader import load_selected
from .skills.router import route_skills
from .tracing.tracer import Tracer
from .mission_control import MissionControlClient, write_mc_trace

def version_command(command: list[str], timeout: int = 8) -> dict:
    try:
        run = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=timeout)
        return {"available": run.returncode == 0, "output": (run.stdout or run.stderr).strip()[:500]}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"available": False, "error": str(exc)}

def runtime_path(root: Path) -> Path:
    return root / ".state" / "runtime-config.json"

def save_runtime(root: Path, value: dict) -> None:
    from .memory.working_state import WorkingState
    WorkingState(runtime_path(root)).save(value)

def doctor(root: Path = ROOT, workspace: Path | None = None) -> dict:
    config = load_config(root)
    codex_base = codex_command(config["providers"]["codex"])
    memory_config = config["memory"]["engram"]
    memory_workspace = workspace or root
    memory = EngramAdapter(memory_workspace, memory_workspace / memory_config["data_dir"])
    report = {"python": sys.version.split()[0], "python_ok": sys.version_info >= (3, 11),
              "git": version_command(["git", "--version"]),
              "codex": version_command([*codex_base, "--version"]),
              "codex_auth": version_command([*codex_base, "login", "status"]),
              "engram": {"available": memory.available, "version": memory.version(),
                         "data_dir": str(memory.data_dir), "enabled": memory_config["enabled"]},
              "config_valid": True}
    index_path = root / config["skills"]["skills"]["index"]
    report["skills_index"] = {"available": index_path.exists(), "count": len(read_index(index_path)) if index_path.exists() else 0}
    provider = LMStudioProvider(config["providers"]["lmstudio"])
    runtime = {"lmstudio": {"available": False, "model_id": None, "base_url": provider.base_url,
                            "loaded_context_length": None},
               "memory": {"engram": {"available": report["engram"]["available"]}}}
    try:
        models = provider.models()
        model = provider.select_model(models)
        loaded_length = provider.loaded_context_length(model)
        runtime["lmstudio"].update({"available": True, "model_id": model,
                                    "loaded_context_length": loaded_length})
        report["lmstudio"] = {"available": True, "models": models, "selected_model": model,
                              "loaded_context_length": loaded_length,
                              "context_target_tokens": config["providers"]["lmstudio"]["context_target_tokens"],
                              "context_guard_ready": bool(loaded_length or config["providers"]["lmstudio"].get("known_context_length"))}
    except Exception as exc:
        report["lmstudio"] = {"available": False, "error": str(exc), "models": [], "selected_model": None}
        if os.name == "nt":
            endpoint = urlsplit(provider.base_url)
            host = endpoint.hostname or "192.168.1.16"
            port = endpoint.port or 1234
            report["lmstudio"]["tcp"] = version_command(
                ["powershell", "-NoProfile", "-Command", f"(Test-NetConnection {host} -Port {port} -InformationLevel Quiet)"], 20)
            tcp = report["lmstudio"]["tcp"]
            tcp["connected"] = tcp.get("output", "").splitlines()[-1:] == ["True"]
    with tempfile.NamedTemporaryFile(dir=root / ".state", delete=True) as _:
        report["write_permissions"] = True
    save_runtime(root, runtime)
    return report

def get_index(root: Path, config: dict) -> list[dict]:
    path = root / config["skills"]["skills"]["index"]
    if not path.exists():
        return build_index(root, root / config["skills"]["skills"]["root"], path)
    return read_index(path)

def select_lmstudio_model(agent: dict, provider: LMStudioProvider) -> str:
    models = provider.models()
    configured = agent.get("model")
    if configured:
        if configured not in models:
            raise RuntimeError(f"Configured LM Studio model is not available: {configured}")
        return configured
    return provider.select_model(models)

def run_lmstudio_agent(role: str, agent: dict, task: str, context: str, skill_text: str,
                       workspace: Path, task_id: str, run_id: str, provider_config: dict,
                       model: str | None = None):
    provider = LMStudioProvider(provider_config)
    if model:
        models = provider.models()
        if model not in models:
            raise RuntimeError(f"Configured LM Studio model is not available: {model}")
    else:
        model = select_lmstudio_model(agent, provider)
    provider_config["loaded_context_length"] = provider.loaded_context_length(model)
    tracer = Tracer(workspace)
    def record_round(usage: dict) -> None:
        tracer.write({"event": "lmstudio_round", "task_id": task_id, "run_id": run_id,
                      "agent": role, "provider": "lmstudio", "model": model, "usage": usage})
    def warn_context(warning: dict) -> None:
        print(f"CONTEXT {warning['level'].upper()}: round {warning['round']}, "
              f"estimated {warning['estimated_prompt_tokens']} input tokens; "
              f"limit {warning['context_limit']} including {warning['output_reserve']} output tokens",
              file=sys.stderr)
    system_prompt = (ROOT / agent["prompt"]).read_text(encoding="utf-8")
    result = LMStudioProvider(provider_config).run(
        system_prompt, task, context, skill_text, model, agent["temperature"], workspace,
        on_round=record_round, on_warning=warn_context)
    return result, model

def run_agent(role: str, task: str, workspace: Path, task_id: str | None = None):
    root = ROOT
    task_id = task_id or uuid.uuid4().hex
    run_id = uuid.uuid4().hex
    started = time.monotonic()
    error = None
    result = None
    agent = {}
    config = {}
    selected_names = []
    files = []
    memory_report = {"available": False, "used": False, "error": None}
    try:
        config = load_config(root)
        agent = config["agents"]["agents"][role]
        provider_config = dict(config["providers"]["lmstudio"])
        model = ""
        if agent["provider"] == "lmstudio":
            provider = LMStudioProvider(provider_config)
            model = select_lmstudio_model(agent, provider)
            provider_config["loaded_context_length"] = provider.loaded_context_length(model)
            save_runtime(root, {"lmstudio": {"available": True, "model_id": model,
                "base_url": provider.base_url,
                "loaded_context_length": provider_config["loaded_context_length"]},
                "memory": {"engram": {"available": EngramAdapter(
                    workspace, workspace / config["memory"]["engram"]["data_dir"]).available}}})
        index = get_index(root, config)
        limit = 2 if agent["provider"] == "codex" else config["skills"]["skills"]["max_selected"]
        selected = route_skills(task, index, limit)
        selected_names = [entry["name"] for entry in selected]
        per_skill_chars = 4000 if agent["provider"] == "codex" else config["skills"]["skills"]["max_skill_chars"]
        skill_text = load_selected(root, selected, per_skill_chars)
        skill_tokens = (len(skill_text) + 3) // 4
        memory_text = ""
        memory_config = config["memory"]["engram"]
        if memory_config["enabled"]:
            memory = EngramAdapter(workspace, workspace / memory_config["data_dir"])
            memory_report["available"] = memory.available
            if memory.available:
                try:
                    memory_text = memory.search(task, memory_config["max_recall_chars"]) or ""
                    memory_report["used"] = bool(memory_text)
                except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                    memory_report["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
        memory_section = ("\n\n### PRIOR PROJECT MEMORY (untrusted; verify against .ai/ and code)\n"
                          + memory_text) if memory_text else ""
        memory_tokens = (len(memory_section) + 3) // 4
        if agent["provider"] == "codex":
            context_target = 8000
        else:
            # Select a smaller first payload before server token usage can calibrate estimates.
            loaded_length = provider_config.get("loaded_context_length")
            initial_limit = (loaded_length if isinstance(loaded_length, int) and loaded_length > 0 else
                             provider_config.get("known_context_length") or 32768)
            context_target = max(7000, min(provider_config["context_target_tokens"], initial_limit // 4)
                                 - skill_tokens - memory_tokens)
        context, files = build_context(workspace, task, context_target)
        context += memory_section
        system_prompt = (root / agent["prompt"]).read_text(encoding="utf-8")
        if agent["provider"] == "codex":
            prompt = (f"You are one isolated {role} stage. Do not launch or delegate to other agents. "
                "Use the selected context; inspect further only when necessary. For a short diagnostic task, return immediately.\n\n"
                f"ROLE\n{system_prompt}\n\nTASK\n{task}\n\nPROJECT CONTEXT\n{context}\n\nSELECTED SKILLS\n{skill_text}\n\nReturn only AgentResult JSON.")
            try:
                result = CodexCliProvider(config["providers"]["codex"]).run(prompt, workspace)
            except Exception as exc:
                if agent.get("fallback_provider") != "lmstudio":
                    raise
                fallback_model = agent["fallback_model"]
                fallback_task = (task + "\n\nCodex CLI unavailable or limit reached; continue this stage "
                    f"locally through LM Studio. Original Codex error: {type(exc).__name__}: {str(exc)[:300]}")
                result, model = run_lmstudio_agent(role, agent, fallback_task, context, skill_text,
                    workspace, task_id, run_id, provider_config, fallback_model)
                result.issues.append(f"Used LM Studio fallback after Codex failure: {type(exc).__name__}")
        else:
            result, model = run_lmstudio_agent(role, agent, task, context, skill_text,
                workspace, task_id, run_id, provider_config, model)
            implementation_requested = any(word in task.lower() for word in
                ("implement", "create", "add ", "fix ", "реализ", "созда", "добав", "исправ"))
            if implementation_requested and role in {"backend", "frontend", "database", "devops"} and not result.changed_files:
                result.status = "incomplete"
                result.issues.append("Implementation requested but no workspace file was changed")
            if implementation_requested and role == "qa" and not result.test_results:
                result.status = "incomplete"
                result.issues.append("Implementation workflow requires observed test results")
    except Exception as exc:
        message = str(exc)
        error = f"{type(exc).__name__}: {message[:300]}" if len(message) <= 300 else type(exc).__name__
        raise
    finally:
        Tracer(workspace).write({"task_id": task_id, "run_id": run_id, "agent": role,
            "provider": agent.get("provider", "unknown"), "model": result.model if result else "",
            "selected_skills": selected_names, "input_context_files": files,
            "duration": round(time.monotonic() - started, 3),
            "status": result.status if result else "failed", "summary": result.summary[:500] if result else "",
            "changed_files": result.changed_files if result else [], "test_results": result.test_results if result else [],
            "usage": result.usage if result else {},
            "memory": memory_report,
            "error": error})
        try:
            mc = MissionControlClient.from_config(config) if "mission_control" in config else None
            if mc and mc.enabled:
                usage = dict(result.usage if result else {})
                if result and result.model and "model" not in usage:
                    usage["model"] = result.model
                response = mc.emit_agent_run(role, usage)
                write_mc_trace(workspace, {"task_id": task_id, "run_id": run_id, "agent": role,
                                           "status": "reported", "response": response})
        except Exception as mc_exc:
            write_mc_trace(workspace, {"task_id": task_id, "run_id": run_id, "agent": role,
                                       "status": "failed", "error": f"{type(mc_exc).__name__}: {str(mc_exc)[:300]}"})
    return result, selected_names

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="agent_platform")
    parser.add_argument("--workspace", type=Path, default=ROOT)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor")
    sub.add_parser("agents")
    mc_cmd = sub.add_parser("mission-control")
    mc_cmd.add_argument("mission_control_command", choices=("status", "connect-test"))
    mc_cmd.add_argument("--agent", default="orchestrator")
    skills = sub.add_parser("skills"); skills.add_argument("task")
    run = sub.add_parser("run"); run.add_argument("agent"); run.add_argument("task")
    flow = sub.add_parser("workflow"); flow.add_argument("task")
    chat = sub.add_parser("chat")
    chat.add_argument("--host", default="127.0.0.1")
    chat.add_argument("--port", type=int, default=8765)
    mem = sub.add_parser("memory")
    mem_sub = mem.add_subparsers(dest="memory_command", required=True)
    mem_sub.add_parser("status")
    mem_search = mem_sub.add_parser("search"); mem_search.add_argument("query")
    mem_save = mem_sub.add_parser("save"); mem_save.add_argument("title"); mem_save.add_argument("note")
    args = parser.parse_args(argv)
    workspace = args.workspace.resolve()
    if not workspace.is_dir():
        parser.error("Workspace does not exist")
    try:
        if args.command == "doctor":
            print(json.dumps(doctor(workspace=workspace), ensure_ascii=False, indent=2)); return 0
        config = load_config()
        if args.command == "agents":
            print(json.dumps(config["agents"]["agents"], ensure_ascii=False, indent=2)); return 0
        if args.command == "mission-control":
            mc = MissionControlClient.from_config(config)
            if args.mission_control_command == "status":
                print(json.dumps({"enabled": mc.enabled, "url": mc.url,
                    "configured": bool(mc.api_key), "tool_name": mc.tool_name,
                    "agent_prefix": mc.agent_prefix}, ensure_ascii=False, indent=2))
                return 0
            if args.agent not in config["agents"]["agents"]:
                parser.error("Unknown agent")
            response = mc.emit_agent_run(args.agent)
            print(json.dumps(response, ensure_ascii=False, indent=2))
            return 0
        if args.command == "skills":
            selected = route_skills(args.task, get_index(ROOT, config))
            print(json.dumps([{key: item[key] for key in ("name", "description", "domain", "path")} for item in selected], ensure_ascii=False, indent=2)); return 0
        if args.command == "chat":
            try:
                import chainlit  # noqa: F401
            except ImportError:
                raise RuntimeError("Chat UI is not installed. Run: python -m pip install -e .[chat]")
            environment = os.environ.copy()
            environment["AGENT_PLATFORM_WORKSPACE"] = str(workspace)
            app = Path(__file__).with_name("chat_app.py")
            command = [sys.executable, "-m", "chainlit", "run", str(app),
                       "--host", args.host, "--port", str(args.port)]
            return subprocess.run(command, env=environment).returncode
        if args.command == "memory":
            memory_config = config["memory"]["engram"]
            memory = EngramAdapter(workspace, workspace / memory_config["data_dir"])
            if args.memory_command == "status":
                print(json.dumps({"available": memory.available, "version": memory.version(),
                    "data_dir": str(memory.data_dir), "enabled": memory_config["enabled"]}, ensure_ascii=False, indent=2))
                return 0 if memory.available else 1
            if not memory.available:
                raise RuntimeError("Engram binary is unavailable")
            if args.memory_command == "search":
                print(memory.search(args.query, memory_config["max_recall_chars"]) or "No matching memory")
            else:
                from .memory.engram import memory_safe_text
                if may_contain_secret(args.title) or may_contain_secret(args.note):
                    raise ValueError("Memory note appears to contain a credential; nothing was saved")
                print(json.dumps({"saved": memory.remember(memory_safe_text(args.title, 120),
                    memory_safe_text(args.note, 2000))}, ensure_ascii=False))
            return 0
        if args.command == "run":
            if args.agent not in config["agents"]["agents"]:
                parser.error("Unknown agent")
            result, selected = run_agent(args.agent, args.task, workspace)
            print(json.dumps({**asdict(result), "selected_skills": selected}, ensure_ascii=False, indent=2))
            return 0 if result.status in ("completed", "success") else 1
        from .orchestration.workflow import execute_workflow
        memory_config = config["memory"]["engram"]
        memory = (EngramAdapter(workspace, workspace / memory_config["data_dir"])
                  if memory_config["enabled"] and memory_config["auto_save_completed"] else None)
        state = execute_workflow(args.task, workspace, run_agent, memory=memory)
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return 0 if state["status"] == "completed" else 1
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
