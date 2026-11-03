"""Lets `python -m unittest tests.<module>` find the sibling `helpers` module, as `discover -s tests` does."""
import sys
from pathlib import Path

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
