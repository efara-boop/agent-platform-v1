from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_platform.config import ROOT, load_config
from agent_platform.skills.indexer import build_index

config = load_config()["skills"]["skills"]
entries = build_index(ROOT, ROOT / config["root"], ROOT / config["index"])
print(f"Indexed {len(entries)} skills")
