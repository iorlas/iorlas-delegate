"""One Codex home's lifetime: generated rules plus an auth symlink, never credential contents or copied user configuration."""
import json
import tempfile
from pathlib import Path


class CodexOverlay:
    def __init__(self, state: Path, real_home: Path, shim: list[str], rules: list[list[str]]) -> None:
        self._temp = tempfile.TemporaryDirectory(prefix="codex-exec-", dir=state)
        self.root = Path(self._temp.name)
        try:
            policy = self.root / "exec-allow.json"
            policy.write_text(json.dumps(rules), encoding="utf-8")
            self.prefix = [*shim, "--exec-policy", str(policy), "--exec-allow"]
            folder = self.root / "rules"
            folder.mkdir()
            text = "".join('prefix_rule(pattern=' + json.dumps([*self.prefix, *rule]) + ', decision="allow")\n' for rule in rules)
            (folder / "default.rules").write_text(text, encoding="utf-8")
            # This creates only a link. Do not stat, open, resolve, or copy its credential target.
            (self.root / "auth.json").symlink_to(real_home / "auth.json")
            # Keep transcripts/resume storage in its original location; deleting this home removes only these links.
            for name in ("sessions", "archived_sessions"):
                target = real_home / name
                target.mkdir(parents=True, exist_ok=True)
                (self.root / name).symlink_to(target, target_is_directory=True)
        except BaseException:
            self.cleanup()
            raise

    def cleanup(self) -> None:
        self._temp.cleanup()
