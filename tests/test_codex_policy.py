import hermetic  # noqa: F401  (a home of its own for every test)
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import routing_support as support
from test_route import route, CFG, lanes, T, argparse_ns


class CodexPolicy(unittest.TestCase):
    def setUp(self):
        self.cfg=copy.deepcopy(CFG)
        self.args={'authored_by':None,'failed':[],'premium':None,'allow_degraded':False,'unattended':True}

    def test_reviewer_runs_codex_at_medium_effort_planner_stays_high(self):
        """delegate-aam: medium on every Codex step of the reviewer role (and in the launched command); planner unchanged."""
        r=self.cfg['roles']['reviewer']
        steps=[s for v in r['by_author'].values() for s in v if s['on']=='openai']
        self.assertTrue(steps)
        self.assertEqual({s['effort'] for s in steps},{'medium'})
        ls={'OPENAI_SUBSCRIPTION':lanes()['OPENAI_SUBSCRIPTION']}
        d=support.decide('reviewer',self.cfg,ls,{},T,self.args)
        self.assertEqual(d['effort'],'medium')
        self.assertIn('model_reasoning_effort=medium',d['how']['cli'])
        p=support.decide('planner',self.cfg,ls,{},T,self.args)
        self.assertEqual(p['effort'],'high')
        self.assertIn('model_reasoning_effort=high',p['how']['cli'])
