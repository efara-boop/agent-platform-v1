"""Source-tree shim for running without installation."""
from pathlib import Path
__path__.append(str(Path(__file__).resolve().parent.parent / "src" / "agent_platform"))
