"""Keep the bootstrap digest synchronized with the reviewed installer."""
import hashlib
from pathlib import Path
import re
import sys

root = Path(__file__).resolve().parents[1]
path = root / "install.sh"
digest = hashlib.sha256((root / "setup.py").read_bytes()).hexdigest()
source = path.read_text()
current = re.search(r"script_sha256='([^']+)'", source).group(1)
if "--update" in sys.argv:
    path.write_text(source.replace("script_sha256='" + current + "'", "script_sha256='" + digest + "'"))
elif current != digest:
    raise SystemExit("setup.py changed: review it, then run python3 tests/check_bootstrap.py --update")
print("Bootstrap SHA-256 OK")
