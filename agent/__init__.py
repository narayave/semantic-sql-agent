# Load .env so `cp .env.example .env` works without exporting by hand.
# Variables already set in the shell win over the file.
import os
from pathlib import Path

_env = Path(__file__).parent.parent / ".env"
if _env.exists():
    for _line in _env.read_text().splitlines():
        _k, _sep, _v = _line.strip().removeprefix("export ").partition("=")
        if _sep and _k and not _k.startswith("#"):
            os.environ.setdefault(_k.strip(), _v.strip().strip("\"'"))
