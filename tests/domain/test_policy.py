"""domain/policy: the verdict on a worker's tool call, and the typed refusal record (D3)."""
import unittest

import hermetic  # noqa: F401  (a home of its own for every test)

from delegate.contracts import events as ev
from delegate.domain.policy.matchers import heredoc_only_hit
from delegate.domain.policy.shell import git_write, read_only_problem
from delegate.domain.policy.worker import denial, denial_item, is_policy_denial, judge


class Policy(unittest.TestCase):
    def test_a_secret_path_is_blocked_but_an_excluded_one_and_an_example_pass(self):
        self.assertTrue(judge("Bash", {"command": "cat ~/.ssh/id_rsa"}, False).blocked)
        self.assertTrue(judge("Read", {"file_path": "/x/.env"}, False).blocked)
        for ok in ("rg foo --glob '!**/.env*'", "cat .env.example", "grep -r x --exclude=.env ."):
            self.assertFalse(judge("Bash", {"command": ok}, False).blocked, ok)

    def test_a_secret_only_inside_a_heredoc_body_gets_the_apply_patch_hint(self):
        hd = "cat > a.py <<'EOF'\nopen('.env')\nEOF"
        self.assertTrue(heredoc_only_hit(hd))
        self.assertIn("apply_patch", judge("Bash", {"command": hd}, False).reason)
        self.assertFalse(heredoc_only_hit("cat .env"))

    def test_git_writes_are_blocked_and_listings_pass(self):
        for c in ("git commit -m x", "git -C . push", "bash -lc 'git checkout -b y'", "git stash", "git branch new"):
            self.assertTrue(git_write(c), c)
            self.assertTrue(judge("Bash", {"command": c}, False).blocked, c)
        for c in ("git status", "git stash list", "git branch --list", "git worktree list", "git log -1"):
            self.assertIsNone(git_write(c), c)

    def test_a_read_only_step_allows_only_reading(self):
        self.assertIsNone(read_only_problem("git diff | head -5 && rg x"))
        self.assertEqual(read_only_problem("rm x"), "`rm` is not a reading command")
        self.assertIn("redirection", read_only_problem("echo a > b"))
        self.assertTrue(judge("Bash", {"command": "touch x"}, True).blocked)
        self.assertFalse(judge("Bash", {"command": "touch x"}, False).blocked)

    def test_the_denial_is_a_typed_record_with_the_matched_path(self):
        d = denial("secret", "Bash", {"command": "cat .env"})
        self.assertIsInstance(d, ev.Denied)
        self.assertEqual(denial_item(d), {"kind": "secret", "tool": "Bash", "path": ".env", "input": {"command": "cat .env"}})
        self.assertEqual(denial_item(denial("policy", "Read", {"file_path": "/a"}))["path"], "/a")
        self.assertEqual(denial("x", "Bash", None).input, {})

    def test_a_git_write_or_a_read_only_violation_is_policy_not_the_owners_call(self):
        self.assertTrue(is_policy_denial("Bash", {"command": "git push"}))
        self.assertTrue(is_policy_denial("Bash", {"command": "touch x"}, True))
        self.assertFalse(is_policy_denial("Bash", {"command": "cat .env > x"}, True))     # a secret stays the owner's
        self.assertFalse(is_policy_denial("Read", {"command": "git push"}))


if __name__ == "__main__":
    unittest.main()
