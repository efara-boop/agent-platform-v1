import hashlib
import json
import tempfile
import unittest
import subprocess
from pathlib import Path
from unittest.mock import patch

from agent_platform.config import ROOT, load_config
from agent_platform.memory.working_state import WorkingState
from agent_platform.memory.engram import EngramAdapter, may_contain_secret, memory_safe_text
from agent_platform.context.budget import context_budget, request_byte_bound
from agent_platform.policies import action_policy, workspace_path
from agent_platform.providers.codex_cli import CodexCliProvider, codex_command, parse_codex_response
from agent_platform.providers.lmstudio import LMStudioProvider, parse_chat_response
from agent_platform.providers.lmstudio import compact_tool_history
from agent_platform.providers.base import AgentResult
from agent_platform.providers.workspace_tools import WorkspaceTools
from agent_platform.skills.indexer import build_index, frontmatter
from agent_platform.skills.router import route_skills
from agent_platform.tracing.tracer import Tracer
from agent_platform.mission_control import MissionControlClient, token_payload
from agent_platform.orchestration.workflow import execute_workflow, WorkflowControl
from agent_platform.orchestration.chat_orchestrator import parse_chat_decision
from agent_platform.orchestration.planner import build_plan
from agent_platform.cli import run_agent, select_lmstudio_model


class PlatformTests(unittest.TestCase):
    def test_config_and_registry(self):
        config = load_config()
        self.assertEqual(len(config["agents"]["agents"]), 9)
        self.assertEqual(config["agents"]["agents"]["reviewer"]["provider"], "codex")
        self.assertEqual(config["agents"]["agents"]["reviewer"]["fallback_provider"], "lmstudio")
        self.assertIn("fable", config["agents"]["agents"]["reviewer"]["fallback_model"])
        self.assertIn("coder", config["agents"]["agents"]["backend"]["model"])
        self.assertEqual(config["agents"]["agents"]["orchestrator"]["provider"], "codex")
        self.assertEqual(config["agents"]["agents"]["orchestrator"]["fallback_provider"], "lmstudio")
        self.assertIn("fable", config["agents"]["agents"]["orchestrator"]["fallback_model"])
        self.assertEqual(config["providers"]["lmstudio"]["max_concurrency"], 1)
        self.assertTrue(config["memory"]["engram"]["enabled"])
        self.assertFalse(config["mission_control"]["mission_control"]["enabled"])

    def test_mission_control_token_payload(self):
        self.assertEqual(token_payload({"prompt_tokens": 100, "completion_tokens": 25, "model": "qwen"}),
                         {"inputTokens": 100, "outputTokens": 25, "model": "qwen"})
        self.assertEqual(token_payload({"rounds": [{"prompt_tokens": 10}, {"prompt_tokens": 20, "completion_tokens": 5}]}),
                         {"inputTokens": 20, "outputTokens": 5})
        self.assertEqual(token_payload({}), {})

    def test_mission_control_disabled_skips_network(self):
        client = MissionControlClient(False, "http://localhost:3000", "")
        self.assertEqual(client.emit_agent_run("backend")["reason"], "disabled")

    def test_mission_control_emit_agent_run_lifecycle(self):
        calls = []
        client = MissionControlClient(True, "http://localhost:3000", "key")
        def fake_request(method, path, payload=None):
            calls.append((method, path, payload))
            if path == "/api/connect":
                return {"agent_id": 42, "connection_id": "conn-1"}
            return {"ok": True}
        client.request = fake_request
        response = client.emit_agent_run("backend", {"prompt_tokens": 100, "completion_tokens": 20})
        self.assertEqual(response["connected"]["agent_id"], 42)
        self.assertEqual(calls[0][1], "/api/connect")
        self.assertEqual(calls[0][2]["agent_name"], "promoney-backend")
        self.assertEqual(calls[0][2]["agent_role"], "coder")
        self.assertEqual(calls[1][1], "/api/agents/42/heartbeat")
        self.assertEqual(calls[1][2]["token_usage"]["inputTokens"], 100)
        self.assertEqual(calls[2][0], "DELETE")

    def test_context_budget_levels(self):
        self.assertEqual(context_budget(1000, 32768, 4096)["level"], "ok")
        self.assertEqual(context_budget(24000, 32768, 4096)["level"], "warning")
        self.assertEqual(context_budget(28672, 32768, 4096)["level"], "stop")

    def test_engram_adapter_is_project_local_and_uses_closed_stdin(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            binary = root / "engram.exe"
            binary.write_bytes(b"fake")
            data_dir = root / "memory"
            adapter = EngramAdapter(root, data_dir, binary)
            def fake_run(command, **kwargs):
                self.assertEqual(command[:2], [str(binary), "search"])
                self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
                self.assertEqual(kwargs["env"]["ENGRAM_DATA_DIR"], str(data_dir))
                self.assertEqual(kwargs["env"]["ENGRAM_NO_UPDATE_CHECK"], "1")
                self.assertNotIn("ENGRAM_CLOUD_TOKEN", kwargs["env"])
                return type("Completed", (), {"returncode": 0, "stdout": "Relevant memory", "stderr": ""})()
            with patch.dict("os.environ", {"ENGRAM_CLOUD_TOKEN": "private"}), \
                 patch("agent_platform.memory.engram.subprocess.run", side_effect=fake_run):
                self.assertEqual(adapter.search("architecture"), "Relevant memory")
            self.assertEqual(memory_safe_text("password=private"), "password=[REDACTED]")
            self.assertTrue(may_contain_secret("AKIA1234567890ABCDEF"))
            self.assertTrue(may_contain_secret("Implement token refresh", strict=True))

    def test_engram_workspaces_use_separate_data_directories(self):
        with tempfile.TemporaryDirectory() as temp:
            first = Path(temp) / "first"
            second = Path(temp) / "second"
            first.mkdir(); second.mkdir()
            config = load_config()["memory"]["engram"]
            a = EngramAdapter(first, first / config["data_dir"])
            b = EngramAdapter(second, second / config["data_dir"])
            self.assertNotEqual(a.data_dir, b.data_dir)
            self.assertTrue(a.data_dir.is_relative_to(first))
            self.assertTrue(b.data_dir.is_relative_to(second))

    def test_skill_index_and_routing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            skill = root / "vendor" / "engineering" / "symfony-rest-kafka" / "SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("---\nname: symfony-rest-kafka\ndescription: >\n  Symfony REST Kafka endpoint design\n---\n# Skill\n", encoding="utf-8")
            index = build_index(root, root / "vendor", root / ".state" / "index.json")
            self.assertEqual(len(index), 1)
            self.assertIn("Symfony", index[0]["description"])
            self.assertEqual(route_skills("Symfony REST Kafka endpoint", index)[0]["name"], "symfony-rest-kafka")
            self.assertEqual(route_skills("Return summary CODEX_STRUCTURED_OK", index), [])

    def test_frontmatter_missing(self):
        self.assertEqual(frontmatter("# Hello"), {})

    def test_lmstudio_response_parsing(self):
        response = {"model": "qwen-model", "usage": {"total_tokens": 10}, "choices": [{"message": {"content": '{"status":"completed","summary":"ok","changed_files":[],"test_results":[],"issues":[]}'}}]}
        result = parse_chat_response(response)
        self.assertEqual(result.summary, "ok")
        self.assertEqual(result.usage["total_tokens"], 10)
        self.assertEqual(result.model, "qwen-model")

    def test_lmstudio_model_discovery_mocked(self):
        provider = LMStudioProvider({"base_url": "http://localhost:1234", "retries": 0})
        with patch.object(provider, "_request", return_value={"data": [{"id": "Huihui-Qwen3-Coder-30B-A3B-Q4_K_M"}]}):
            models = provider.models()
        self.assertEqual(provider.select_model(models), models[0])

    def test_agent_configured_lmstudio_model_wins(self):
        provider = LMStudioProvider({"base_url": "http://localhost:1234", "retries": 0})
        with patch.object(provider, "_request", return_value={"data": [
            {"id": "huihui-qwen3-coder-30b-a3b-instruct-abliterated-i1"},
            {"id": "qwen3.6-27b-fable-fusion-711-uncensored-heretic-nm-dau-neo-max-mtp"},
        ]}):
            model = select_lmstudio_model({"model": "qwen3.6-27b-fable-fusion-711-uncensored-heretic-nm-dau-neo-max-mtp"}, provider)
        self.assertEqual(model, "qwen3.6-27b-fable-fusion-711-uncensored-heretic-nm-dau-neo-max-mtp")

    def test_codex_agent_falls_back_to_lmstudio_model(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            with patch("agent_platform.cli.build_context", return_value=("context", ["file.py"])), \
                 patch("agent_platform.cli.CodexCliProvider") as codex_provider, \
                 patch("agent_platform.cli.LMStudioProvider") as lm_provider:
                codex_provider.return_value.run.side_effect = RuntimeError("limit reached")
                lm_provider.return_value.models.return_value = [
                    "huihui-qwen3-coder-30b-a3b-instruct-abliterated-i1",
                    "qwen3.6-27b-fable-fusion-711-uncensored-heretic-nm-dau-neo-max-mtp",
                ]
                lm_provider.return_value.loaded_context_length.return_value = 88832
                lm_provider.return_value.base_url = "http://localhost:1234"
                lm_provider.return_value.run.return_value = AgentResult("completed", "local review")
                result, _ = run_agent("reviewer", "Review current diff", workspace, "task-1")
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.model, "")
        self.assertIn("fallback", result.issues[0].lower())
        args = lm_provider.return_value.run.call_args.args
        self.assertEqual(args[4], "qwen3.6-27b-fable-fusion-711-uncensored-heretic-nm-dau-neo-max-mtp")

    def test_lmstudio_records_usage_for_each_tool_round(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            provider = LMStudioProvider({"base_url": "http://localhost:1234", "retries": 0,
                "lock_path": str(root / "lmstudio.lock"), "context_target_tokens": 32768,
                "max_output_tokens": 4096, "loaded_context_length": 88832})
            responses = [
                {"usage": {"prompt_tokens": 500, "completion_tokens": 15, "total_tokens": 515},
                 "choices": [{"message": {"role": "assistant", "tool_calls": [{"id": "call1",
                    "function": {"name": "list_files", "arguments": "{}"}}]}}]},
                {"model": "qwen", "usage": {"prompt_tokens": 560, "completion_tokens": 20,
                    "total_tokens": 580}, "choices": [{"message": {"role": "assistant", "content":
                    '{"status":"completed","summary":"ok"}'}}]},
            ]
            observed = []
            with patch.object(provider, "_request", side_effect=responses):
                result = provider.run("role", "task", "", "", "qwen", 0.2, root,
                                      on_round=observed.append)
            self.assertEqual(result.status, "completed")
            self.assertEqual([item["prompt_tokens"] for item in observed], [500, 560])
            self.assertEqual(result.usage["rounds"], observed)

    def test_lmstudio_stops_before_context_overflow(self):
        with tempfile.TemporaryDirectory() as temp:
            provider = LMStudioProvider({"base_url": "http://localhost:1234", "retries": 0,
                "lock_path": str(Path(temp) / "lmstudio.lock"),
                "context_target_tokens": 600, "max_output_tokens": 200,
                "loaded_context_length": 10000})
            with patch.object(provider, "_request") as request:
                result = provider.run("x" * 3000, "task", "", "", "qwen", 0.2)
            self.assertEqual(result.status, "incomplete")
            request.assert_not_called()
            self.assertEqual(result.usage["warnings"][0]["level"], "stop")

    def test_serialized_request_hard_bound_covers_unicode_payload(self):
        payload = {"model": "qwen", "messages": [{"role": "user", "content": "Привет" * 1000}],
                   "temperature": 0.2, "max_tokens": 100}
        self.assertGreater(request_byte_bound(payload), len(json.dumps(payload).encode("utf-8")))

    def test_lmstudio_requires_verified_loaded_window(self):
        provider = LMStudioProvider({"base_url": "http://localhost:1234", "retries": 0,
            "context_target_tokens": 32768, "max_output_tokens": 4096})
        with patch.object(provider, "loaded_context_length", return_value=None), \
             patch.object(provider, "_request") as request:
            result = provider.run("role", "task", "", "", "qwen", 0.2)
        self.assertEqual(result.status, "incomplete")
        self.assertIn("unknown", result.summary)
        request.assert_not_called()

    def test_lmstudio_rechecks_byte_bound_after_response_replacement(self):
        with tempfile.TemporaryDirectory() as temp:
            provider = LMStudioProvider({"base_url": "http://localhost:1234", "retries": 0,
                "lock_path": str(Path(temp) / "lmstudio.lock"),
                "context_target_tokens": 3000, "max_output_tokens": 200,
                "loaded_context_length": 3000})
            malformed = '{"status":"completed","summary":"' + "я" * 1500 + '","test_results":{}}'
            with patch.object(provider, "_request", return_value={"choices": [{"message": {
                "role": "assistant", "content": malformed}}]}) as request:
                result = provider.run("role", "task", "", "", "qwen", 0.2)
            self.assertEqual(result.status, "incomplete")
            self.assertEqual(request.call_count, 1)
            self.assertEqual(result.usage["warnings"][0]["level"], "stop")

    def test_lmstudio_warns_before_next_request_using_actual_usage(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            provider = LMStudioProvider({"base_url": "http://localhost:1234", "retries": 0,
                "lock_path": str(root / "lmstudio.lock"), "context_target_tokens": 2000,
                "max_output_tokens": 500, "context_warning_ratio": 0.8,
                "loaded_context_length": 100000})
            warnings = []
            calls = []
            def reply(path, payload):
                calls.append(path)
                if len(calls) == 1:
                    return {"usage": {"prompt_tokens": 1200}, "choices": [{"message": {
                        "role": "assistant", "tool_calls": [{"id": "call1", "function": {
                            "name": "list_files", "arguments": "{}"}}]}}]}
                self.assertEqual(warnings[0]["round"], 2)
                return {"usage": {"prompt_tokens": 1250}, "choices": [{"message": {
                    "role": "assistant", "content": '{"status":"completed","summary":"ok"}'}}]}
            with patch.object(provider, "_request", side_effect=reply), \
                 patch("agent_platform.providers.lmstudio.estimate_request_tokens", return_value=100):
                result = provider.run("role", "task", "", "", "qwen", 0.2, root,
                                      on_warning=warnings.append)
            self.assertEqual(result.status, "completed")
            self.assertEqual(len(calls), 2)
            self.assertEqual(warnings[0]["level"], "warning")

    def test_lmstudio_repairs_one_invalid_result(self):
        with tempfile.TemporaryDirectory() as temp:
            provider = LMStudioProvider({"base_url": "http://localhost:1234", "retries": 0,
                "lock_path": str(Path(temp) / "lmstudio.lock"),
                "context_target_tokens": 32768, "max_output_tokens": 4096,
                "loaded_context_length": 88832})
            responses = [
                {"choices": [{"message": {"role": "assistant", "content":
                    '{"status":"completed","summary":"ok","test_results":{}}'}}]},
                {"choices": [{"message": {"role": "assistant", "content":
                    '{"status":"completed","summary":"ok","test_results":[]}'}}]},
            ]
            with patch.object(provider, "_request", side_effect=responses) as request:
                result = provider.run("role", "task", "", "", "qwen", 0.2)
            self.assertEqual(result.status, "completed")
            self.assertEqual(request.call_count, 2)
            self.assertEqual(len(result.usage["rounds"]), 2)

    def test_codex_response_parsing(self):
        self.assertEqual(parse_codex_response('{"status":"completed","summary":"ok"}').summary, "ok")
        self.assertEqual(parse_codex_response("CODEX_OK").summary, "CODEX_OK")
        with self.assertRaises(ValueError):
            parse_codex_response("{}")
        with self.assertRaises(ValueError):
            parse_codex_response('{"status":"success","summary":"ok","issues":"bad"}')

    def test_codex_subprocess_devnull_mocked(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            def fake_run(command, **kwargs):
                self.assertEqual(kwargs["stdin"], __import__("subprocess").DEVNULL)
                self.assertIn("read-only", command)
                last = Path(command[command.index("--output-last-message") + 1])
                last.write_text("CODEX_OK", encoding="utf-8")
                return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()
            with patch("agent_platform.providers.codex_cli.shutil.which", return_value="codex"), patch("agent_platform.providers.codex_cli.subprocess.run", side_effect=fake_run):
                result = CodexCliProvider({"timeout_seconds": 10, "lock_path": str(workspace / "codex.lock")}).run("Reply exactly CODEX_OK", workspace, structured=False)
            self.assertEqual(result.summary, "CODEX_OK")

    def test_codex_command_includes_configured_args(self):
        with patch("agent_platform.providers.codex_cli.find_codex_binary", return_value="codex"):
            self.assertEqual(codex_command({"args": ["--no-daemon"]}), ["codex", "--no-daemon"])

    def test_working_state(self):
        with tempfile.TemporaryDirectory() as temp:
            storage = WorkingState(Path(temp) / ".state" / "current-task.json")
            state = {"task_id": "1", "execution_plan": []}
            storage.save(state)
            self.assertEqual(storage.load(), state)

    def test_trace_scrubs_secrets(self):
        with tempfile.TemporaryDirectory() as temp:
            tracer = Tracer(Path(temp))
            tracer.write({"task_id": "1", "api_key": "do-not-log", "summary": "ok",
                          "usage": {"prompt_tokens": 123, "completion_tokens": 20}})
            event = json.loads(tracer.path.read_text(encoding="utf-8"))
            self.assertEqual(event["api_key"], "[REDACTED]")
            self.assertEqual(event["usage"]["prompt_tokens"], 123)
            self.assertIn("timestamp", event)

    def test_policies_and_workspace_boundaries(self):
        self.assertEqual(action_policy("git_push"), "forbid")
        self.assertEqual(action_policy("git push"), "forbid")
        self.assertEqual(action_policy("git.push"), "forbid")
        self.assertEqual(action_policy("git/push"), "forbid")
        self.assertEqual(action_policy("unrecognized shell action"), "approval")
        self.assertEqual(action_policy("deploy"), "approval")
        self.assertEqual(action_policy("git_diff"), "allow")
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(PermissionError):
                workspace_path(Path(temp), "../escape")
            with self.assertRaises(PermissionError):
                workspace_path(Path(temp), ".env", write=True)
            with self.assertRaises(PermissionError):
                workspace_path(Path(temp), "secrets/config.json")

    def test_write_requires_hash(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "file.txt"
            path.write_text("old", encoding="utf-8")
            tools = WorkspaceTools(root)
            with self.assertRaises(PermissionError):
                tools.dispatch("write_file", {"path": "file.txt", "content": "new"})
            digest = hashlib.sha256(b"old").hexdigest()
            tools.dispatch("write_file", {"path": "file.txt", "content": "new", "expected_sha256": digest})
            self.assertEqual(path.read_text(encoding="utf-8"), "new")

    def test_list_files_hides_credentials(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".env").write_text("SECRET", encoding="utf-8")
            (root / "credentials.json").write_text("{}", encoding="utf-8")
            (root / "safe.py").write_text("pass", encoding="utf-8")
            files = WorkspaceTools(root).dispatch("list_files", {"contains": ""})["files"]
            self.assertEqual(files, ["safe.py"])

    def test_run_tests_uses_sandboxed_codex_mock(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch("agent_platform.providers.codex_cli.CodexCliProvider") as provider_class:
                provider_class.return_value.run.return_value = AgentResult("completed", "2 tests passed")
                provider_class.return_value.last_events = [{"type": "item.completed", "item": {
                    "type": "command_execution", "command": "python -B -m unittest discover -s tests -v", "exit_code": 0}}]
                output = WorkspaceTools(Path(temp)).dispatch("run_tests", {})
            self.assertEqual(output["status"], "success")
            self.assertIn("unittest", provider_class.return_value.run.call_args.args[0])
            self.assertTrue(provider_class.return_value.run.call_args.kwargs["capture_events"])

    def test_context_compaction_keeps_recent_round(self):
        messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "task"},
            {"role": "assistant", "tool_calls": [{"id": "1"}]}, {"role": "tool", "content": "x" * 100},
            {"role": "assistant", "tool_calls": [{"id": "2"}]}, {"role": "tool", "content": "recent"}]
        compact_tool_history(messages, ["file.py"], max_chars=300)
        self.assertIn("recent", str(messages))
        self.assertNotIn("x" * 100, str(messages))
        self.assertIn("file.py", str(messages))

    def test_workflow_includes_orchestrator_and_review(self):
        with tempfile.TemporaryDirectory() as temp:
            calls = []
            def runner(role, task, workspace, task_id):
                calls.append(role)
                return AgentResult("completed", "ok"), []
            state = execute_workflow("Implement backend API", Path(temp), runner)
            self.assertEqual(state["status"], "completed")
            self.assertEqual(calls[0], "orchestrator")
            self.assertEqual(calls[-1], "reviewer")

    def test_self_improvement_plan_preserves_diagnosis_before_implementation(self):
        task = Path(ROOT / "task.txt").read_text(encoding="utf-8")
        roles = [stage["agent"] for stage in build_plan(task)]
        self.assertEqual(roles[:4], ["orchestrator", "analyst", "architect", "backend"])
        self.assertNotIn("database", roles[:4])
        self.assertLess(roles.index("architect"), roles.index("backend"))

    def test_workflow_passes_bounded_stage_handoff(self):
        with tempfile.TemporaryDirectory() as temp:
            seen = {}
            def runner(role, task, workspace, task_id):
                seen[role] = task
                summary = "x" * 2000 if role == "analyst" else "ok"
                return AgentResult("completed", summary), []
            execute_workflow("Analyze architecture and implement quick wins", Path(temp), runner)
            self.assertIn("PRIOR STAGE HANDOFF", seen["architect"])
            self.assertIn("analyst: completed", seen["architect"])
            self.assertLess(len(seen["architect"]), 5000)

    def test_chat_decision_parsing(self):
        value = parse_chat_decision('{"kind":"task","task":"Implement API","questions":["Auth?"]}', "raw")
        self.assertEqual(value.kind, "task")
        self.assertEqual(value.questions, ["Auth?"])
        fallback = parse_chat_decision("not json", "raw task")
        self.assertEqual(fallback.task, "raw task")
        fenced = parse_chat_decision('```json\n{"kind":"chat","response":"ok","task":"","questions":[]}\n```', "raw")
        self.assertEqual(fenced.response, "ok")

    def test_workflow_events_and_guidance(self):
        with tempfile.TemporaryDirectory() as temp:
            events = []
            seen = []
            control = WorkflowControl()
            control.add_guidance("Prefer a small patch")
            def runner(role, task, workspace, task_id):
                seen.append(task)
                return AgentResult("completed", "ok"), []
            state = execute_workflow("Implement backend API", Path(temp), runner,
                                     on_event=events.append, control=control)
            self.assertEqual(state["status"], "completed")
            self.assertIn("Prefer a small patch", seen[0])
            self.assertEqual(events[0]["type"], "workflow_started")
            self.assertEqual(events[-1]["type"], "workflow_finished")

    def test_completed_workflow_saves_reviewed_memory(self):
        with tempfile.TemporaryDirectory() as temp:
            class Memory:
                available = True
                saved = []
                def remember(self, title, note):
                    self.saved.append((title, note))
                    return True
            memory = Memory()
            def runner(role, task, workspace, task_id):
                return AgentResult("completed", "Reviewed implementation"), []
            state = execute_workflow("Implement backend API", Path(temp), runner, memory=memory)
            self.assertTrue(state["memory"]["saved"])
            self.assertEqual(len(memory.saved), 1)
            self.assertIn("Reviewer completed successfully", memory.saved[0][1])

    def test_workflow_skips_autosave_when_task_or_review_mentions_secrets(self):
        with tempfile.TemporaryDirectory() as temp:
            class Memory:
                available = True
                saved = []
                def remember(self, title, note):
                    self.saved.append((title, note))
                    return True
            memory = Memory()
            def runner(role, task, workspace, task_id):
                summary = "Reviewer saw AKIA1234567890ABCDEF" if role == "reviewer" else "ok"
                return AgentResult("completed", summary), []
            state = execute_workflow("Implement backend API", Path(temp), runner, memory=memory)
            self.assertFalse(state["memory"]["saved"])
            self.assertEqual(state["memory"]["skipped_reason"], "sensitive_content")
            self.assertEqual(memory.saved, [])
            def clean_runner(role, task, workspace, task_id):
                return AgentResult("completed", "Reviewed"), []
            state = execute_workflow("Implement API with password=private", Path(temp),
                                     clean_runner, memory=memory)
            self.assertEqual(state["memory"]["skipped_reason"], "sensitive_content")
            self.assertEqual(memory.saved, [])

    def test_review_findings_trigger_bounded_rework(self):
        with tempfile.TemporaryDirectory() as temp:
            calls = []
            def runner(role, task, workspace, task_id):
                calls.append(role)
                issues = ["bug"] if role == "reviewer" and calls.count("reviewer") == 1 else []
                return AgentResult("completed", "ok", issues=issues), []
            state = execute_workflow("Implement backend API", Path(temp), runner)
            self.assertEqual(state["status"], "completed")
            self.assertEqual([item["agent"] for item in state["rework"]], ["backend", "qa", "reviewer"])


if __name__ == "__main__":
    unittest.main()
