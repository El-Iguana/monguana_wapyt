import sys
from pathlib import Path

APPCODE = Path(__file__).resolve().parents[1] / "appcode"
if str(APPCODE) not in sys.path:
    sys.path.insert(0, str(APPCODE))
