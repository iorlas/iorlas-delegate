#!/usr/bin/env python3
"""delegate router: deterministic lane and step selection for an LLM orchestrator. A launcher: the code is src/delegate (next to skills/ in the plugin
tree); `route.py --help` lists the verbs, docs/dev/integrations.md describes what they print."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from delegate.cli.main import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
