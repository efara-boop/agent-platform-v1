from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_platform.config import load_config
from agent_platform.providers.lmstudio import LMStudioProvider

provider = LMStudioProvider(load_config()["providers"]["lmstudio"])
try:
    models = provider.models()
    print({"available": True, "models": models, "selected_model": provider.select_model(models)})
except Exception as exc:
    print({"available": False, "error": str(exc)})
    raise SystemExit(1)
