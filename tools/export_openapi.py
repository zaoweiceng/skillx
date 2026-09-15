"""Export API contracts without starting the server or touching the database."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.main import app  # noqa: E402

if __name__ == "__main__":
    target = Path(sys.argv[1])
    target.write_text(json.dumps(app.openapi(), ensure_ascii=False, indent=2) + "\n")
