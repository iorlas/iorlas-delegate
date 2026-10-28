"""The text matchers of the worker policy: the one place of the domain that reads shell text with a regex (D3). Pure functions of a command line."""
import posixpath
import re
import shlex

# Paths a worker must never touch: credentials outside the repo and .env files in it (.env.example and kin are fine). A tripwire for
# accidents and lazy injections, not a boundary: a determined worker can spell a path so it does not match.
SECRET_RE = re.compile(r"\.ssh\b|\.gnupg\b|\.aws\b|\.azure\b|\.config/gh\b|\.docker/config\.json|\.netrc\b|\.git-credentials\b|"
                       r"\.codex/auth\.json|Library/Keychains|(?<![\w.-])\.env(?:\.(?!(?:example|sample|template|dist)\b)[\w-]+)?(?![\w.-])")
# Arguments that keep a path OUT of a search or listing (rg --glob '!**/.env*', grep --exclude, find -not -path, git ':!x'): a worker
# avoiding a secret must not trip the guard.
EXCLUDED_RE = re.compile(r"""(?:--i?glob|-g)[=\s]+(?:(['"])!.*?\1|!\S+)|--exclude(?:-dir)?[=\s]+(?:(['"]).*?\2|\S+)"""
                         r"""|(?:-not|!)\s+-i?(?:path|name|wholename|regex)\s+(?:(['"]).*?\3|\S+)|(['"]):(?:!|\(exclude\))[^'"]*\4|(?<!\S):(?:!|\(exclude\))\S+""")
SHELLS = {"sh", "bash", "zsh", "dash"}
EXCL_OPTS = {"-g", "--glob", "--iglob", "--exclude", "--exclude-dir", "--exclude-from"}
EXCL_PATH_OPTS = {"-path", "-ipath", "-name", "-iname", "-wholename", "-iwholename", "-regex", "-iregex"}
HEREDOC_RE = re.compile(r"<<-?[ \t]*(['\"]?)(\w+)\1[^\n]*\n.*?(?:\n[ \t]*\2\b|\Z)", re.S)
QUIET_REDIRECT_RE = re.compile(r"\d?>&\d|&?\d?>\s*/dev/null")
WRITES_RE = re.compile(r"`|\$\(|[<>]\(|>")
LOOP_HEADS = {"for", "while", "until"}
SHELL_C_RE = re.compile(r"-[a-z]*c[a-z]*")
EXCLUDE_WORD_RE = re.compile(r"--exclude(?:-dir|-from)?=|(?:--i?glob=|-g)!")
EXCLUDE_PATHSPEC_RE = re.compile(r":(?:!|\(exclude\))")


def unwrap(cmd: str, depth: int = 3) -> str:
    """The script inside `sh|bash|zsh -c/-lc "<script>"` (nested up to `depth` times), else `cmd` unchanged."""
    for _ in range(depth):
        try:
            t = shlex.split(cmd)
        except ValueError:
            return cmd
        i = next((k for k, x in enumerate(t[1:], 1) if SHELL_C_RE.fullmatch(x)), 0)
        if not (t and posixpath.basename(t[0]) in SHELLS and i and all(x.startswith("-") for x in t[1:i]) and i + 1 < len(t)):
            return cmd
        cmd = t[i + 1]
    return cmd


def without_excluded(text: str) -> str | None:
    """`text` as shell words with the arguments that keep a path OUT of a search removed (quote splicing collapsed); None if unparsable."""
    try:
        t = shlex.split(unwrap(text))
    except ValueError:
        return None
    out: list[str] = []
    prev, prev2 = "", ""
    for x in t:
        glob_excl = prev in EXCL_OPTS and (x.startswith("!") or prev.startswith("--exclude"))
        path_excl = prev in EXCL_PATH_OPTS and prev2 in ("-not", "!")
        inline = EXCLUDE_WORD_RE.match(x)
        if not (glob_excl or path_excl or inline or x in EXCL_OPTS or EXCLUDE_PATHSPEC_RE.match(x) or x in ("-not", "!")
                or (x in EXCL_PATH_OPTS and prev in ("-not", "!"))):
            out.append(x)
        prev2, prev = prev, x
    return " ".join(out)


def secret_hit(text: str | None) -> str | None:
    """The secret path `text` names, ignoring paths it only excludes; None when there is none."""
    text = text or ""
    stripped = without_excluded(text)
    m = SECRET_RE.search(stripped if stripped is not None else EXCLUDED_RE.sub(" ", text))
    return m.group() if m else None


def heredoc_only_hit(cmd: str | None) -> bool:
    """True when `cmd` names a secret path only inside a heredoc body (code being written), not in the command itself."""
    cmd = unwrap(cmd or "")
    return bool(secret_hit(cmd) and not secret_hit(HEREDOC_RE.sub(lambda m: m.group().split("\n", 1)[0] + "\n", cmd)))


def split_unquoted(text: str) -> list[str]:
    """`text` cut at | || && ; and newlines that are not inside quotes or escaped: a separator in a quoted word stays in the word."""
    out: list[str] = []
    cur: list[str] = []
    quote = ""
    i = 0
    while i < len(text):
        c = text[i]
        if quote:
            if c == "\\" and quote == '"' and i + 1 < len(text):
                cur.append(c)
                i += 1
                c = text[i]
            elif c == quote:
                quote = ""
        elif c == "\\" and i + 1 < len(text):
            cur.append(c)
            i += 1
            c = text[i]
        elif c in "'\"":
            quote = c
        elif c in "|;&\n" and (c != "&" or text[i + 1:i + 2] == "&"):
            out.append("".join(cur))
            cur = []
            i += 2 if text[i + 1:i + 2] == c and c in "|&" else 1
            continue
        cur.append(c)
        i += 1
    out.append("".join(cur))
    return out


def loop_words(t: list[str]) -> list[str]:
    """The command part of one line of a for/while/until loop (header word, `do` and a lone `done` removed); a for header is no command."""
    if t[0] == "for" or t == ["done"]:
        return []
    if t[0] in LOOP_HEADS or t[0] == "do":
        return t[1:]
    return t


def pieces(cmd: str) -> list[list[str]] | None:
    """The command lines in `cmd` (split on unquoted | || && ; and newlines; `for/while ... do ... done` reduced to its commands), each as
    words; None when one cannot be parsed."""
    out = []
    for seg in split_unquoted(QUIET_REDIRECT_RE.sub(" ", unwrap(cmd))):
        try:
            t = shlex.split(seg)
        except ValueError:
            return None
        t = loop_words(t) if t else t
        if t:
            out.append(t)
    return out


def writes_or_substitutes(cmd: str) -> bool:
    """True when `cmd` redirects output to a file or substitutes a command (redirects to /dev/null and between descriptors do not count)."""
    return bool(WRITES_RE.search(QUIET_REDIRECT_RE.sub(" ", cmd)))


def name_of(word: str) -> str:
    return posixpath.basename(word)


