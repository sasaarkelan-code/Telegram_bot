"""Run with `python -m unittest discover -s tests -t .` in either layout."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import main  # Registers the smm package for a flattened mobile upload.
