# The gate: ruff + pyrefly + the structural rules test (caps, signals, ratchets). Seconds.
check: lint types rules

# check plus the whole unittest suite (what CI runs). `just release-check` is this, run before every tag.
check-all: check
    python3 -m unittest discover -s tests

# Run before bumping .claude-plugin/plugin.json and tagging: red here means no release.
release-check: check-all

lint:
    #!/usr/bin/env bash
    set -euo pipefail
    uv tool run "ruff@$(just pin ruff)" check

types:
    #!/usr/bin/env bash
    set -euo pipefail
    uv tool run "pyrefly@$(just pin pipx:pyrefly)" check

rules:
    python3 -m unittest discover -s tests -p test_rules.py

# The version mise.toml pins for a tool (one source for the pins).
[private]
pin tool:
    @python3 -c "import sys, tomllib; print(tomllib.load(open('mise.toml', 'rb'))['tools'][sys.argv[1]])" {{tool}}
