#!/usr/bin/env python3
"""Regenerate skills/delegate/contract.json's `answer_schema` (from wire.RunResult) and `status_schema` (from status.Status).

The rest of the file (reasons, scopes, run_args, usage notes) is hand-edited with the classifier and is kept as it is.
`python3 dev/gen_contract.py` writes; `--check` exits 1 when the committed file differs from what would be written.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from delegate.contracts import schema, status, wire  # noqa: E402

PATH = ROOT / "skills" / "delegate" / "contract.json"


def render() -> str:
    doc = json.loads(PATH.read_text(encoding="utf-8"))
    doc["answer_schema"] = schema.of(wire.RunResult)
    doc["status_schema"] = schema.of(status.Status)
    return json.dumps(doc, indent=1, ensure_ascii=False) + "\n"


def main(argv: list[str]) -> int:
    want = render()
    if "--check" in argv:
        if PATH.read_text(encoding="utf-8") != want:
            print("contract.json is stale: run python3 dev/gen_contract.py", file=sys.stderr)
            return 1
        return 0
    PATH.write_text(want, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
