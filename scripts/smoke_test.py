from pathlib import Path
import json
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_platform.config import ROOT, load_config
from agent_platform.providers.codex_cli import CodexCliProvider
from agent_platform.providers.lmstudio import LMStudioProvider
from agent_platform.skills.indexer import read_index
from agent_platform.skills.router import route_skills

config = load_config()
report = {}
lm = LMStudioProvider(config["providers"]["lmstudio"])
try:
    model = lm.select_model(lm.models())
    response = lm._request("chat/completions", {"model": model, "messages": [
        {"role": "system", "content": "Follow the user's exact output instruction."},
        {"role": "user", "content": "Reply exactly:\nQWEN_OK"}],
        "temperature": 0, "max_tokens": 30, "stream": False})
    actual = response["choices"][0]["message"]["content"].strip()
    report["qwen"] = {"model": model, "actual": actual, "passed": actual == "QWEN_OK"}
except Exception as exc:
    report["qwen"] = {"passed": False, "error": str(exc)}

try:
    actual = CodexCliProvider(config["providers"]["codex"]).run("Reply exactly: CODEX_OK", ROOT, structured=False).summary.strip()
    report["codex"] = {"actual": actual, "passed": actual == "CODEX_OK"}
except Exception as exc:
    report["codex"] = {"passed": False, "error": str(exc)}

index = read_index(ROOT / config["skills"]["skills"]["index"])
report["skills"] = [entry["name"] for entry in route_skills("Symfony REST Kafka endpoint", index)]
print(json.dumps(report, ensure_ascii=False, indent=2))
raise SystemExit(0 if report["qwen"]["passed"] and report["codex"]["passed"] else 1)
