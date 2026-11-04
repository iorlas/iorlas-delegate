# Host command guard (R9-04)

`route.py guard --exec-allow ARGV...` executes one argv without a shell. Guard options go before
`--exec-allow`; everything after it, including `--`, belongs to the command. The trusted caller passes
`--read-only` for reading steps and `--engine-only=RECIPE` for reserved recipes. Reading steps cannot
use this execution path. The caller must determine these values, not the worker's command text.

The policy lives in `host.json` inside delegate's state folder (`DELEGATE_CONFIG_DIR`, or the installed
plugin's data folder). `--config-dir DIR` pins that folder; generated Claude hooks pin it explicitly
because worker environments remove delegate variables. No policy is loaded from the working repository.
Missing, unreadable, malformed or invalid policy refuses every hook/execution call. An owner who only
needs the existing Claude hook can provision `{"binaries":{},"writer_exec_allow":[]}`.

Example owner policy (paths must be absolute, normalized, and provisioned for this host):

```json
{
  "binaries": {
    "dotnet": "/opt/dotnet/dotnet",
    "python": "/opt/venv/bin/python3",
    "just": "/usr/local/bin/just"
  },
  "writer_exec_allow": [
    ["dotnet", "build"],
    ["dotnet", "test"],
    ["dotnet", "restore"],
    ["dotnet", "--version"],
    ["python", "-m", "unittest"],
    ["python", "-m", "pytest"],
    ["just", "check"]
  ]
}
```

The three dotnet build/test/restore forms and the two Python modules accept arbitrary trailing argv,
as Denis decided. `dotnet --version` and each just recipe match the entire argv exactly. Only these
rule shapes are valid; configuring a wrapper or an unpinned rule invalidates the policy. Commands may
name the logical binary or its exact pinned absolute path. No runtime PATH lookup or basename/prefix
match occurs. Engine-only recipes and credential-path checks still apply before dispatch.

`guard.jsonl` in the same state folder receives one authorization row for each hook or execution call:
`v`, `mode`, `argv`, `allowed`, `reason`, `ts`, `cwd`, plus `resolved_argv` for authorized execution or
`tool`/`command` for a valid hook. Tool contents being edited are not logged. The row is persisted before
execution; an audit failure refuses without starting a child. If the state folder cannot be opened,
the guard refuses on stderr and cannot write a row there. A later child launch failure returns 2;
its already-written row records authorization, not successful execution. Child exit status otherwise
passes through. No command is retried or rerouted.

The shared reader classifier also refuses the escape corpus: sed execution/write/external scripts
(only inline print scripts qualify), sort compression/output options, git pager/external-diff/textconv/
output options, rg preprocessors/hostname executables, find execution/output actions, jq script options,
and background commands containing writes. Tests: `tests/test_exec_allow.py`.

## Worker-run contract 4 (UNITYCODEX)

The optional request field is `writer_exec_allow: list[list[str]]`, default `[]`.
It is serialized as `--writer-exec-allow '[["dotnet","test"],["python","-m","unittest"]]'`.
`route.py contract --json` advertises `contract: 4`, `capabilities: ["writer_exec_allow"]`, and the flag
in `run_args`. `contract.json` also publishes the generated `request_schema`. The caller must check
both the exact contract version and the capability before sending a nonempty grant. Python callers
use `RunRequest.to_argv(route, contract=handshake)`; an old/missing handshake or capability raises
`ValueError` before serializing. An old CLI also rejects the unknown flag, rather than ignoring it.

At admission delegate validates the entire list against owner `host.json`; malformed requests,
unpinned binaries, forbidden wrappers and engine-only recipes refuse as `bad_request`, without
launching a worker. Only Codex write steps can honor a nonempty list. A different runner or a reading
step refuses; a schema reroute cannot silently discard the grant by switching to another runner.

Each nonempty launch (fresh or resume) gets a private temporary `CODEX_HOME` in delegate's state folder.
`rules/default.rules` contains one allow `prefix_rule` per entry. Every prefix names the absolute
Python interpreter, installed `route.py guard`, pinned state folder and per-call policy file, then the
allowed argv. The worker prompt gives these exact guard invocations. No raw dotnet/python/just prefix
is granted. The guard checks both the call's grant and current host policy, audits, and dispatches the
pinned executable. Concrete entries with extra arguments match exactly; only the decided bare
test/build forms accept trailing arguments.

`--ignore-rules` is removed and `approval_policy=never` prevents other escalations. The sandbox and
network restrictions remain. CLI trust overrides mark cwd and every ancestor untrusted so project
config, hooks and rule files cannot add grants. User configuration remains ignored. This follows
Codex's [rule evaluation](https://github.com/openai/codex/blob/main/codex-rs/core/src/exec_policy.rs)
and [project trust loading](https://github.com/openai/codex/blob/main/codex-rs/config/src/loader/mod.rs).

`auth.json` is a symlink to the original `CODEX_HOME/auth.json`; delegate never reads or copies its
contents. Nothing from user configuration is copied. Session directories are symlinked to their
original locations and `sqlite_home` stays there, preserving transcripts, indexing and resume after
the overlay is removed. Cleanup removes the overlay's links, rules and per-call policy, including on
launch/publication/supervision errors; it never follows the links to delete original data.

An empty/absent list preserves the existing argv, environment, prompt and `--ignore-rules` behavior
and creates no overlay. Agent-runs v1 is unchanged; the request flag, handshake capability, generated
request schema and answer's contract number are the worker-run changes. There is no fallback host
execution path or compatibility reader. Tests: `tests/test_codex_overlay*.py`.

The broader forms run writer-written tests, project targets and conftest on the host. R9-02's scrubbed
environment and network isolation must ship in the same release (review synthesis D-03/D-17 and its
closing note); this command guard does not supply that isolation.
