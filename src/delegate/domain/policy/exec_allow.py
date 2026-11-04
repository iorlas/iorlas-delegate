"""The owner's whole-argv allow: only decided build/test forms and exact just recipes."""
import posixpath
import shlex

from delegate.domain.policy.worker import judge

BROAD = {("dotnet", "build"), ("dotnet", "test"), ("dotnet", "restore"),
         ("python", "-m", "unittest"), ("python", "-m", "pytest")}
EXACT = {("dotnet", "--version")}


def words(value: object) -> list[str]:
    if not isinstance(value, list) or not value or not all(isinstance(x, str) and x and "\0" not in x for x in value):
        raise ValueError("expected a nonempty argv of nonempty strings")
    return value


def valid_rule(rule: list[str]) -> bool:
    return tuple(rule) in BROAD | EXACT or (len(rule) == 2 and rule[0] == "just" and
                                           all(c.isascii() and (c.isalnum() or c in "_-") for c in rule[1]) and
                                           not rule[1].startswith("-"))


def host_policy(doc: object) -> tuple[dict[str, str], list[list[str]]]:
    """Validate every rule, even unused ones. A bad host policy never grants a partial allow."""
    if not isinstance(doc, dict) or not isinstance(doc.get("binaries"), dict):
        raise ValueError("host.json needs binaries and writer_exec_allow")
    binaries, rules = doc["binaries"], doc.get("writer_exec_allow")
    if not isinstance(rules, list):
        raise ValueError("writer_exec_allow must be an argv list")
    for name, path in binaries.items():
        if name not in {"dotnet", "python", "just"} or not isinstance(path, str) or not path.startswith("/") or "\0" in path:
            raise ValueError("binaries must pin dotnet/python/just to absolute paths")
        if posixpath.normpath(path) != path:
            raise ValueError("binary paths must be normalized")
    if len(set(binaries.values())) != len(binaries):
        raise ValueError("each binary must have its own path")
    for rule in rules:
        if not valid_rule(words(rule)) or rule[0] not in binaries:
            raise ValueError("unsafe or unpinned writer_exec_allow rule")
    return binaries, rules


def allowed_argv(argv: list[str], binaries: dict[str, str], rules: list[list[str]], read_only: bool,
                 engine_only: tuple[str, ...]) -> list[str]:
    """Resolve a logical name or an exact pinned path; only decided forms accept arbitrary trailing arguments."""
    words(argv)
    if read_only:
        raise ValueError("exec-allow is writer-only")
    name = next((name for name, path in binaries.items() if argv[0] in (name, path)), None)
    if name is None:
        raise ValueError("executable is not pinned in host.json")
    logical = [name, *argv[1:]]
    verdict = judge("Bash", {"command": shlex.join(logical)}, False, engine_only)
    if verdict.blocked:
        raise ValueError(verdict.reason)
    for rule in rules:
        if logical == rule or (tuple(rule) in BROAD and logical[:len(rule)] == rule):
            return [binaries[name], *argv[1:]]
    raise ValueError("whole argv is not allowed by host.json")


def call_rules(value: object, binaries: dict[str, str], host_rules: list[list[str]], engine_only: tuple[str, ...]) -> list[list[str]]:
    """A call may narrow the host allow, never add an executable, wrapper, or engine-only recipe."""
    if not isinstance(value, list):
        raise ValueError("writer_exec_allow must be a list of argv lists")
    out = []
    for entry in value:
        resolved = allowed_argv(words(entry), binaries, host_rules, False, engine_only)
        name = next(name for name, path in binaries.items() if path == resolved[0])
        out.append([name, *resolved[1:]])
    return out
