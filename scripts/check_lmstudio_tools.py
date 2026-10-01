"""Read-only live check of LM Studio's OpenAI-compatible tool calls."""
from pathlib import Path
import json
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_platform.config import ROOT, load_config
from agent_platform.providers import lmstudio


class ReadOnlyWorkspaceTools(lmstudio.WorkspaceTools):
    calls: list[str] = []

    def dispatch(self, name: str, args: dict) -> dict:
        if name not in {"list_files", "read_file"}:
            raise PermissionError("Diagnostic run permits read-only tools only")
        self.calls.append(name)
        return super().dispatch(name, args)


config = load_config()["providers"]["lmstudio"]
provider = lmstudio.LMStudioProvider(config)
model = provider.select_model(provider.models())
task = ("Call list_files with contains=AGENTS.md, then call read_file with path=AGENTS.md. "
        "Do not write files or run tests. Return AgentResult JSON with status completed, "
        "a brief summary of the first heading, and empty changed_files, test_results, issues.")
with patch.object(lmstudio, "WorkspaceTools", ReadOnlyWorkspaceTools):
    result = provider.run("Follow the task and use tools for factual file reads.", task, "", "", model, 0.1, ROOT)
report = {"model": model, "status": result.status, "summary": result.summary,
          "observed_tool_calls": ReadOnlyWorkspaceTools.calls, "changed_files": result.changed_files,
          "usage_rounds": result.usage.get("rounds", []),
          "context_warnings": result.usage.get("warnings", [])}
print(json.dumps(report, ensure_ascii=False, indent=2))
raise SystemExit(0 if {"list_files", "read_file"}.issubset(ReadOnlyWorkspaceTools.calls)
                 and not result.changed_files else 1)
