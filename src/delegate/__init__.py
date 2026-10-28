"""delegate. Importing the package puts the vendored shelf packages (skills/delegate/_vendor, dev/vendor.py) on sys.path, once, so src/ imports
them by absolute name (process_lock, stream_proc, ...). This is the only bootstrap; every entry point reaches the package first."""
import sys
from pathlib import Path

VENDOR_DIR = str(Path(__file__).resolve().parents[2] / "skills" / "delegate" / "_vendor")

if VENDOR_DIR not in sys.path:
    sys.path.append(VENDOR_DIR)
