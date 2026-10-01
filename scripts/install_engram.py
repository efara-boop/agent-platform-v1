"""Install the reviewed Engram Windows release into this workspace only."""
from __future__ import annotations

import hashlib
import io
import sys
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_platform.config import ROOT

VERSION = "2.2.1"
SHA256 = "c66947b2468d27104296fedb1bb8416f45fd880262606759217193a4bab9be0f"
URL = ("https://github.com/Gentleman-Programming/engram/releases/download/"
       f"v{VERSION}/engram_{VERSION}_windows_amd64.zip")
DEST = ROOT / "vendor" / "engram" / "engram.exe"


def main() -> int:
    if sys.platform != "win32":
        raise RuntimeError("This installer is for Windows amd64 only")
    if DEST.exists():
        print(f"Existing binary preserved: {DEST}")
        return 0
    with urllib.request.urlopen(URL, timeout=120) as response:
        archive = response.read()
    actual = hashlib.sha256(archive).hexdigest()
    if actual != SHA256:
        raise RuntimeError(f"Engram archive SHA256 mismatch: {actual}")
    with zipfile.ZipFile(io.BytesIO(archive)) as package:
        binary = package.read("engram.exe")
    DEST.parent.mkdir(parents=True, exist_ok=True)
    with DEST.open("xb") as output:
        output.write(binary)
    print(f"Installed Engram {VERSION}: {DEST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
