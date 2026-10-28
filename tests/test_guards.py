"""The secret guard and git ownership. Run: python -m unittest discover -s tests"""
import hermetic  # noqa: F401  (a home of its own for every test)
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from test_route import route, ROOT, _call
from delegate.domain.policy.matchers import heredoc_only_hit, secret_hit
from delegate.domain.policy.shell import git_write, read_only_problem
from delegate.domain.policy.worker import is_policy_denial
from delegate.app import launch
from delegate.domain.policy import worker
from delegate.adapters import git


class SecretGuard(unittest.TestCase):
    def guard(self, tool_input, *flags, tool="X"):
        import io
        real = sys.stdin, sys.stderr
        sys.stdin, sys.stderr = io.StringIO(json.dumps({"tool_name": tool, "tool_input": tool_input})), io.StringIO()
        try:
            return route.main(["guard"] + list(flags))
        finally:
            sys.stdin, sys.stderr = real

    def test_blocks_secret_paths_in_what_is_run_or_opened(self):
        for ti in ({"command": "cat ~/.ssh/id_ed25519"}, {"file_path": "/r/.env"}, {"command": "cp app/.env.local /tmp"},
                   {"path": "/Users/x/.aws"}, {"pattern": "**/.env"}, {"command": "cat ~/.config/gh/hosts.yml"},
                   {"command": "rg -g '!.git' token .env"}, {"command": "rg --glob '**/.env' token"}):
            self.assertEqual(self.guard(ti), 2, ti)

    def test_read_only_steps_may_run_reading_commands_only(self):
        for c in ("cd /s && git diff 53cc344 -- client; git log --oneline -5", "git diff origin/main...HEAD | head -200", "rg -n Foo src 2>/dev/null",
                  "find . -name '*.cs'", "sed -n '1,40p' a.cs", "git branch --show-current"):
            self.assertEqual(self.guard({"command": c}, "--read-only", tool="Bash"), 0, c)
        for c in ("echo x > f", "git checkout x", "rm -rf a", "find . -delete", "sed -i s/a/b/ f", "git -c core.pager=sh log", "cat $(ls)",
                  "git diff --output=f", "dotnet test", "git branch new", "rg --pre sh x"):
            self.assertEqual(self.guard({"command": c}, "--read-only", tool="Bash"), 2, c)
        self.assertEqual(self.guard({"command": "dotnet test"}, tool="Bash"), 0)       # write steps: not limited

    def test_git_writes_are_blocked_for_every_step_and_listings_pass(self):
        for c in ("grep -n '' justfile | head; git stash list >/dev/null; git diff HEAD --stat -- justfile",       # lifesim-q21f
                  "git ls-files | grep -i x; git log -1 --format=%h -- a | head -1; git branch --contains 0dedd418 2>&1 | head -3",
                  "git branch -a", "git tag -l 'v*'", "git worktree list", "git stash show -p"):
            self.assertEqual(self.guard({"command": c}, tool="Bash"), 0, c)
            self.assertEqual(self.guard({"command": c}, "--read-only", tool="Bash"), 0, c)
        for c in ("git stash", "git branch new", "git tag v1", "git checkout -b x", "cd a && git commit -m x", "ls; git push", "git -C /x reset --hard"):
            self.assertEqual(self.guard({"command": c}, tool="Bash"), 2, c)
            self.assertTrue(is_policy_denial(*_call({"tool_name": "Bash", "tool_input": {"command": c}})), c)

    def test_excluding_secret_paths_in_quoted_and_wrapped_searches_is_not_touching_them(self):
        real = ("/bin/zsh -lc \"rg --files --hidden -g '*SKILL.md' -g '\"'!'\"\\\\.git' -g '\"'!'\"\\\\.beads/**' -g '\"'!'\"\\\\.task/**' "
                "-g '\"'!'\"\\\\.env*' -g '\"'!**/node_modules/**'\"' -g '\"'!**/.venv/**'\"'\"")
        for c in (real, "rg -n --hidden -g '!config' -g '!.git/**' -g '!.beads/**' -g '!.env*' foo",
                  "rg --files --hidden -g AGENTS.md -g '!.git' -g '!.beads' -g '!.env*'", "bash -c \"zsh -c 'rg x --glob=!.env'\"",
                  "find . -not -path './.env*'", "find . ! -path '*/.ssh/*'"):
            self.assertIsNone(secret_hit(c), c)
            self.assertIsNone(read_only_problem(c), c)
            self.assertEqual(self.guard({"command": c}, "--read-only", tool="Bash"), 0, c)
        for c in ("cat .env", "rg foo .env", "rg -g '.env*' foo", "cat ~/.ssh/id_rsa", "bash -lc \"cat .env\"", "rg -g '!x' foo .env"):
            self.assertTrue(secret_hit(c), c)
            self.assertEqual(self.guard({"command": c}, tool="Bash"), 2, c)
        self.assertEqual(read_only_problem("bash -lc 'rm x'"), "`rm` is not a reading command")

    def test_heredoc_body_secret_name_gets_an_apply_patch_hint_and_real_reads_do_not(self):
        hd = "zsh -lc \"python - <<'EOF'\nif path.name == '.env':\n    pass\nEOF\""
        self.assertTrue(heredoc_only_hit(hd))
        self.assertTrue(secret_hit(hd))                       # still blocked
        for c in ("python -c \"open('.env')\"", "cat .env", "cat .env <<'EOF'\nx\nEOF"):
            self.assertFalse(heredoc_only_hit(c), c)
            self.assertTrue(secret_hit(c), c)
        r = subprocess.run([sys.executable, str(ROOT / "skills" / "delegate" / "route.py"), "guard"], capture_output=True, text=True,
                           input=json.dumps({"tool_name": "Bash", "tool_input": {"command": hd}}))
        self.assertEqual(r.returncode, 2)
        self.assertIn("apply_patch", r.stderr)
        r = subprocess.run([sys.executable, str(ROOT / "skills" / "delegate" / "route.py"), "guard"], capture_output=True, text=True,
                           input=json.dumps({"tool_name": "Bash", "tool_input": {"command": "cat .env"}}))
        self.assertNotIn("apply_patch", r.stderr)

    def test_writer_briefs_say_to_edit_with_apply_patch(self):
        self.assertIn("apply_patch", worker.EDIT_RULE)
        self.assertIn(worker.EDIT_RULE, launch.PREAMBLE)

    def test_lets_through_lookalikes_and_written_content(self):
        for ti in ({"command": "cat .env.example"}, {"command": "node -e 'process.env.HOME'"}, {"command": "dotnet test"},
                   {"file_path": "src/environment.ts"}, {"file_path": ".gitignore", "content": ".env\n.ssh/"}, {"command": "ls .envrc"},
                   {"command": "/bin/zsh -lc \"head -45 client/Assets/Tests/Editor/LiveViewTests.cs; rg -n --hidden --glob '!**/.git/**' "
                               "--glob '!**/.env*' --glob '!**/.aws/**' '^(<<<<<<<|>>>>>>>)( |$)' .\""},      # lifesim-6dq0: excluding is avoiding
                   {"command": "grep -r --exclude=.env --exclude-dir=.ssh foo ."}, {"command": "find . -not -path '*/.ssh/*'"}, {"command": "git ls-files ':!.env'"}):
            self.assertEqual(self.guard(ti), 0, ti)


class GitOwnership(unittest.TestCase):
    """Workers must not change git state; the coordinator owns branches, commits and worktrees."""

    def setUp(self):
        import subprocess
        self.sp = subprocess
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        run = lambda *a: subprocess.run(["git", "-C", self.dir] + list(a), capture_output=True, text=True, check=True)
        self.git = run
        run("init", "-q")
        run("config", "user.email", "t@example.com")
        run("config", "user.name", "t")
        Path(self.dir, "f.txt").write_text("1")
        run("add", ".")
        run("commit", "-qm", "init")

    def tearDown(self):
        self.tmp.cleanup()

    def test_editing_files_is_clean(self):
        before = git.state(self.dir)
        Path(self.dir, "f.txt").write_text("2")
        Path(self.dir, "new.txt").write_text("x")
        self.assertEqual(git.changes(before, git.state(self.dir)), [])

    def test_branch_and_head_changes_are_reported_other_refs_and_worktrees_are_not(self):
        before = git.state(self.dir)
        wt = tempfile.mkdtemp()
        self.git("worktree", "add", "-q", os.path.join(wt, "w"), "-b", "other")         # parallel slots do this all the time
        self.assertEqual(git.changes(before, git.state(self.dir)), [])
        self.git("checkout", "-qb", "feature")
        Path(self.dir, "f.txt").write_text("3")
        self.git("commit", "-qam", "x")
        text = " ".join(git.changes(before, git.state(self.dir)))
        self.assertIn("branch", text)
        self.assertIn("HEAD", text)

    def test_not_a_repo_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(git.state(d))
            self.assertEqual(git.changes(None, None), [])

    def test_the_guard_blocks_every_git_write(self):
        for c in ("git commit -m x", "git push", "git checkout x", "git switch x", "git branch x", "git worktree add y", "git merge x",
                  "git rebase x", "git reset --hard", "git stash", "git cherry-pick a", "git tag v", "git revert a", "git am p", "git pull"):
            self.assertTrue(git_write(c), c)


if __name__ == "__main__":
    unittest.main()
