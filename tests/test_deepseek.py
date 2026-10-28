"""Free lane must fail closed and keep the actual author family."""
import hermetic  # noqa: F401  (a home of its own for every test)
import copy
import json
import subprocess
import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from test_route import route, CFG, lanes, decide
from delegate.adapters.harness.opencode import OpenCodeHarness
from delegate.app.supervisor import RunHooks
from fakes.fake_run import replay


class DeepSeekTests(unittest.TestCase):
    def catalog(self, **changes):
        obj = dict(id='deepseek-v4.1-flash-free', providerID='opencode', status='active',
                   cost={'input': 0, 'output': 0, 'cache': {'read': 0, 'write': 0}})
        obj.update(changes)
        return 'opencode/deepseek-v4.1-flash-free\n' + json.dumps(obj)

    @patch.dict(CFG["settings"], {"deepseek_free_enabled": True})
    def test_free_first_light_and_worker_then_existing_fallback(self):
        ls = lanes()
        ls['DEEPSEEK_FREE'] = route.Lane('DEEPSEEK_FREE', 'deepseek', 'subscription')
        self.assertEqual(decide('light', ls)['lane'], 'DEEPSEEK_FREE')
        self.assertEqual(decide('worker', ls)['lane'], 'DEEPSEEK_FREE')
        ls['OPENAI_SUBSCRIPTION'].out = True
        self.assertEqual(decide('worker', ls)['lane'], 'DEEPSEEK_FREE')
        ls['DEEPSEEK_FREE'].out = True
        self.assertNotEqual(decide('light', ls)['lane'], 'DEEPSEEK_FREE')

    def test_deepseek_author_gets_other_family_review(self):
        self.assertEqual(decide('reviewer', lanes(), authored_by='deepseek')['lane'], 'OPENAI_SUBSCRIPTION')
        steps = CFG['roles']['reviewer']['by_author']['deepseek']
        self.assertEqual(steps[-1]['flag'], 'same_family')
        self.assertTrue(all(s['on'] != 'deepseek' for s in steps[:-1]))

    def test_disabled_is_skipped_even_with_enabled_profile(self):
        cfg = copy.deepcopy(CFG)
        with patch.object(route, 'lane_mode', return_value='off'), patch.dict(route.READERS, {k: lambda *a: None for k in route.READERS}):
            self.assertNotIn('DEEPSEEK_FREE', route.gather(cfg, {}, 1, check_config=False))

    def test_launch_isolates_credentials_and_refuses_unverified_model(self):
        d = {'lane': 'DEEPSEEK_FREE', 'model': 'deepseek-v4.1-flash-free', 'effort': 'low', 'role': 'worker',
             'how': {'kind': 'opencode', 'model': 'opencode/deepseek-v4.1-flash-free', 'cwd': '/tmp', 'write': False}}
        a = SimpleNamespace(resume=None, schema=None, timeout=10)
        for valid in (False, True):
            with tempfile.TemporaryDirectory() as tmp, patch.object(route, 'config_dir', return_value=Path(tmp)), \
                 patch.object(route, 'opencode_exe', return_value='opencode'), patch.object(route.sys, 'platform', 'darwin'), \
                 patch.object(OpenCodeHarness, 'write_shell'), patch.object(OpenCodeHarness, 'free_model_ok', return_value=valid) as guard, \
                 replay(['{}']), patch.object(route, 'mark_out'), patch.object(route, 'update_state'), patch.object(route, 'agent_runs', None), \
                 patch.object(route, 'child_env', return_value={'OPENCODE_CONFIG': '/private/key-config', 'OPENCODE_AUTH_JSON': 'secret'}):
                result = route.launch_worker(d, 'Say OK', a, 'test1234', 1, route.load_routing(), {}, RunHooks())
                if valid:
                    env = guard.call_args.args[1]
                    self.assertNotIn('OPENCODE_CONFIG', env)
                    self.assertNotIn('OPENCODE_AUTH_JSON', env)
                    self.assertEqual(env['XDG_DATA_HOME'], str(Path(tmp) / 'opencode-free-data'))
                    self.assertIsNone(result.get('outcome'))
                else:
                    self.assertEqual(result['outcome'], 'rate_limited')


