from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_platform.cli import doctor
import json
print(json.dumps(doctor(), ensure_ascii=False, indent=2))
