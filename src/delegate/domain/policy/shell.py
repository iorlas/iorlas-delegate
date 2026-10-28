"""What a worker's shell command may do (D3): git is the coordinator's, and a read-only step only reads. Pure functions of the command text."""
import shlex

from delegate.domain.policy.matchers import name_of, pieces, unwrap, writes_or_substitutes

# A read-only Claude step (reviewer, planner, light) gets Bash for reading only: every piece of the command line must start with one of
# these, with no output redirection or command substitution. Codex read steps get the same from their read-only sandbox, enforced by the OS;
# this is a filter on the command text, not a boundary.
READ_CMDS = {"cat", "head", "tail", "wc", "ls", "rg", "grep", "egrep", "fgrep", "find", "sed", "sort", "uniq", "cut", "tr", "diff", "nl",
             "file", "stat", "pwd", "echo", "jq", "tree", "du", "basename", "dirname", "realpath", "which", "true", "test", "cd", "git"}
READ_GIT = {"diff", "log", "show", "status", "blame", "ls-files", "ls-tree", "rev-parse", "grep", "merge-base", "cat-file", "describe",
            "shortlog", "rev-list", "branch"}
READ_ONLY_HELP = ("this step is read-only: Bash allows only reading commands (git diff/log/show/status/blame/grep/ls-files, rg, grep, cat, "
                  "head, tail, ls, find without -exec/-delete, sed without -i, wc, sort, diff, jq), no '>' and no $(...).")
GIT_LISTING = ("--list", "-l", "-a", "-r", "--show-current", "-v", "-vv", "--contains", "--merged", "--no-merged", "--points-at", "--format", "--sort")
GIT_CHANGING = ("commit", "push", "checkout", "switch", "merge", "rebase", "reset", "cherry-pick", "revert", "am", "pull")
FIND_RUNS = {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprintf", "-fls"}


def git_args(t: list[str]) -> list[str]:
    """Words after `git` and its -C/--no-pager/--git-dir options; the first is the subcommand."""
    args = t[1:]
    while (args and args[0] in ("-C", "--no-pager")) or (args and args[0].startswith("--git-dir")):
        args = args[2:] if args[0] == "-C" else args[1:]
    return args


def git_write(cmd: str) -> str | None:
    """The first git command in `cmd` that changes refs, the index's commits, worktrees or remotes (git is the coordinator's), else None.
    Listing forms pass: stash list/show, branch and tag listings, worktree list."""
    for t in pieces(cmd) or []:
        if name_of(t[0]) != "git":
            continue
        args = git_args(t)
        sub, rest = (args[0] if args else ""), args[1:]
        if sub in GIT_CHANGING \
                or (sub == "stash" and not (rest and rest[0] in ("list", "show"))) \
                or (sub == "worktree" and not (rest and rest[0] == "list")) \
                or (sub in ("branch", "tag") and ((rest and not rest[0].startswith("-")) or any(x.startswith("-") and not x.startswith(GIT_LISTING) for x in rest))):
            return " ".join(t[:3])
    return None


def piece_problem(t: list[str]) -> str | None:
    """Why one command line (as words) is not a reading command, else None."""
    name = name_of(t[0])
    if name not in READ_CMDS:
        return f"`{name}` is not a reading command"
    if name == "find" and set(t) & FIND_RUNS:
        return "find may not run or delete"
    if name == "sed" and any(x.startswith(("-i", "--in-place")) for x in t):
        return "sed -i writes"
    if name == "rg" and any(x.startswith("--pre") for x in t):
        return "rg --pre runs a program"
    if name == "git":
        args = git_args(t)
        sub = args[0] if args else ""
        if sub not in READ_GIT | {"stash", "tag", "worktree"}:         # also catches `git -c key=value ...`, which can run a program
            return f"git {sub} is not a reading command"
        if git_write(" ".join(shlex.quote(x) for x in t)):
            return f"git {sub} may only list"
        if any(x.startswith("--output") for x in args):
            return "--output writes a file"
    return None


def read_only_problem(cmd: str) -> str | None:
    """None when every piece of `cmd` only reads, else what is not allowed."""
    cmd = unwrap(cmd)
    if writes_or_substitutes(cmd):
        return "output redirection or command substitution"
    ps = pieces(cmd)
    if ps is None:
        return "unparsable command"
    for t in ps:
        why = piece_problem(t)
        if why:
            return why
    return None
