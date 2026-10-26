# The gate: ruff + pyrefly + the whole unittest suite (it includes tests/test_rules.py: caps, layers, ratchets).
check: lint types test

lint:
    #!/usr/bin/env bash
    set -euo pipefail
    uv tool run "ruff@$(just pin ruff)" check

types:
    #!/usr/bin/env bash
    set -euo pipefail
    uv tool run "pyrefly@$(just pin pipx:pyrefly)" check

# The suite runs against a throwaway config dir, never the real ~/.config/delegate.
test:
    #!/usr/bin/env bash
    set -euo pipefail
    DELEGATE_CONFIG_DIR="$(mktemp -d)"
    export DELEGATE_CONFIG_DIR
    trap 'rm -rf "$DELEGATE_CONFIG_DIR"' EXIT
    python3 -m unittest discover -s tests

# The version mise.toml pins for a tool (one source for the pins).
[private]
pin tool:
    @python3 -c "import sys, tomllib; print(tomllib.load(open('mise.toml', 'rb'))['tools'][sys.argv[1]])" {{tool}}
