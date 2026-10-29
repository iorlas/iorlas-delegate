"""Free lane must fail closed and keep the actual author family."""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import hermetic  # noqa: F401  (a home of its own for every test)
from fakes.fake_run import replay
from test_route import CFG, decide, lanes

from delegate.adapters import agent_runs
from delegate.adapters.harness.opencode import OpenCodeHarness
from delegate.app import decide as dec
from delegate.app import env, launch, usage
from delegate.app.settle import Settler
from delegate.app.supervisor import RunHooks
from delegate.domain.lanes.lane import Lane


class DeepSeekTests(unittest.TestCase):
    def catalog(self, **changes):
        obj = dict(id='deepseek-v4.1-flash-free', providerID='opencode', status='active',
                   cost={'input': 0, 'output': 0, 'cache': {'read': 0, 'write': 0}})
        obj.update(changes)
        return 'opencode/deepseek-v4.1-flash-free\n' + json.dumps(obj)

    def test_free_first_light_and_worker_then_existing_fallback(self):
        ls = lanes()
        ls['DEEPSEEK_FREE'] = Lane('DEEPSEEK_FREE', 'deepseek', 'subscription')
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
        env.config_store().set_lane('DEEPSEEK_FREE', 'declined')
        with patch.dict(usage.READERS, {k: lambda *a: None for k in usage.READERS}):
            self.assertNotIn('DEEPSEEK_FREE', dec.fleet_builder(cfg).build(env.load_state(), 1, check_config=False).lanes)

    def test_launch_isolates_credentials_and_refuses_unverified_model(self):
        d = {'lane': 'DEEPSEEK_FREE', 'model': 'deepseek-v4.1-flash-free', 'effort': 'low', 'role': 'worker',
             'how': {'kind': 'opencode', 'model': 'opencode/deepseek-v4.1-flash-free', 'cwd': '/tmp', 'write': False}}
        a = SimpleNamespace(resume=None, schema=None, timeout=10)
        for valid in (False, True):
            with tempfile.TemporaryDirectory() as tmp, patch.object(env, 'config_dir', return_value=Path(tmp)), \
                 patch.object(OpenCodeHarness, 'find_exe', return_value='opencode'), patch.object(sys, 'platform', 'darwin'), \
                 patch.object(OpenCodeHarness, 'write_shell'), patch.object(OpenCodeHarness, 'free_model_ok', return_value=valid) as guard, \
                 replay(['{}']), patch.object(Settler, 'mark_out'), patch.object(env, 'update_state'), patch.object(agent_runs, 'run'), patch.object(agent_runs, 'lanes'), \
                 patch.object(env, 'child_env', return_value={'OPENCODE_CONFIG': '/private/key-config', 'OPENCODE_AUTH_JSON': 'secret'}):
                result = launch.launch_worker(d, 'Say OK', a, 'test1234', 1, env.load_routing(), {}, RunHooks())
                if valid:
                    wenv = guard.call_args.args[1]
                    self.assertNotIn('OPENCODE_CONFIG', wenv)
                    self.assertNotIn('OPENCODE_AUTH_JSON', wenv)
                    self.assertEqual(wenv['XDG_DATA_HOME'], str(Path(tmp) / 'opencode-free-data'))
                    self.assertIsNone(result.get('outcome'))
                else:
                    self.assertEqual(result['outcome'], 'rate_limited')


