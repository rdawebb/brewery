"""Fix brewery entry point for Python 3.14 + uv editable install issue."""

from pathlib import Path

# Find project root (where .venv exists)
PROJECT_ROOT = Path(__file__).resolve().parent
while not (PROJECT_ROOT / ".venv").exists() and PROJECT_ROOT != PROJECT_ROOT.parent:
    PROJECT_ROOT = PROJECT_ROOT.parent

if not (PROJECT_ROOT / ".venv").exists():
    raise FileNotFoundError(
        "Could not find .venv directory. Run this from your project."
    )

VENV_PYTHON = PROJECT_ROOT / ".venv" / "bin" / "python3"
ENTRY_POINT = PROJECT_ROOT / ".venv" / "bin" / "brewery"
SRC_PATH = PROJECT_ROOT / "src"

print("Fixing brewery entry point...")

entry_point_content = f"""#!{VENV_PYTHON}
import sys
sys.path.insert(0, '{SRC_PATH}')
from brewery.cli.main import app
sys.exit(app())
"""

ENTRY_POINT.write_text(entry_point_content)
ENTRY_POINT.chmod(0o755)

print(f"✓ Entry point fixed at: {ENTRY_POINT}")
