"""Check the controller and feature scripts with the installed Node runtime."""

from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parent.parent
for path in sorted((ROOT / "web").rglob("*.js")):
    subprocess.run(["node", "--check", str(path)], check=True)
    print(f"Checked {path.relative_to(ROOT)}")
