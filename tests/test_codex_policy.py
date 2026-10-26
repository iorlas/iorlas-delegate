import copy
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from test_route import route, CFG, lanes, T, argparse_ns


class CodexPolicy(unittest.TestCase):
    def setUp(self):
        self.cfg=copy.deepcopy(CFG)
        self.cfg['settings']['codex_plan_review_only']=True
        self.args={'authored_by':None,'failed':[],'premium':None,'allow_degraded':False,'unattended':True}

    def test_worker_hard_and_light_do_not_use_codex_even_if_only_codex_is_available(self):
        ls={'OPENAI_SUBSCRIPTION':lanes()['OPENAI_SUBSCRIPTION']}
        for role in ('worker','hard','light'):
            with self.subTest(role=role):
                decision=route.decide(role,self.cfg,ls,{},T,self.args)
                self.assertEqual(decision['action'],'stop')
                self.assertFalse(any(s['on']=='openai' for tier in route.build_tiers(self.cfg['roles'][role],self.cfg,self.args,role) for s in tier))

    def test_planner_and_reviewer_retain_read_only_codex(self):
        ls={'OPENAI_SUBSCRIPTION':lanes()['OPENAI_SUBSCRIPTION']}
        for role in ('planner','reviewer'):
            d=route.decide(role,self.cfg,ls,{},T,self.args)
            self.assertEqual(d['lane'],'OPENAI_SUBSCRIPTION')
            self.assertEqual(d['how']['sandbox'],'read-only')

    def test_reviewer_runs_codex_at_medium_effort_planner_stays_high(self):
        """delegate-aam: medium on every Codex step of the reviewer role (and in the launched command); planner unchanged."""
        r=self.cfg['roles']['reviewer']
        steps=[s for v in r['by_author'].values() for s in v if s['on']=='openai']
        self.assertTrue(steps)
        self.assertEqual({s['effort'] for s in steps},{'medium'})
        ls={'OPENAI_SUBSCRIPTION':lanes()['OPENAI_SUBSCRIPTION']}
        d=route.decide('reviewer',self.cfg,ls,{},T,self.args)
        self.assertEqual(d['effort'],'medium')
        self.assertIn('model_reasoning_effort=medium',d['how']['cli'])
        p=route.decide('planner',self.cfg,ls,{},T,self.args)
        self.assertEqual(p['effort'],'high')
        self.assertIn('model_reasoning_effort=high',p['how']['cli'])

    def test_fresh_worker_uses_deepseek_and_falls_back_to_other_non_codex_lanes(self):
        self.cfg['settings']['deepseek_free_enabled']=True
        ls=lanes();ls['DEEPSEEK_FREE']=route.Lane('DEEPSEEK_FREE','deepseek','subscription')
        self.assertEqual(route.decide('worker',self.cfg,ls,{},T,self.args)['lane'],'DEEPSEEK_FREE')
        ls['DEEPSEEK_FREE'].out=True
        self.assertNotEqual(route.decide('worker',self.cfg,ls,{},T,self.args).get('lane'),'OPENAI_SUBSCRIPTION')

    def test_saved_codex_writer_is_refused_without_disabling_the_review_lane(self):
        a=argparse_ns('worker',resume='old-writer')
        st={'refs':{'old-writer':{'lane':'OPENAI_SUBSCRIPTION','model':'gpt-6.1-sol','mode':'write','effort':'medium','host':False}}}
        with patch.object(route,'load_state',return_value=st), patch.object(route,'gather',return_value=lanes()), patch.object(route,'publish_lanes'), patch.object(route,'mark_busy'), patch.object(route,'mark_out') as mark:
            d,ls,_,_=route.decision(a,self.cfg)
        self.assertEqual(d['action'],'lane_out')
        self.assertIn('paused',d['say'])
        self.assertFalse(ls['OPENAI_SUBSCRIPTION'].out)
        mark.assert_not_called()

    def test_launch_guard_rejects_stale_writable_decisions_before_starting_a_process(self):
        for role,sandbox in [('worker','workspace-write'),('worker','read-only'),('planner','workspace-write')]:
            d={'role':role,'how':{'sandbox':sandbox}}
            with patch.object(route,'load_routing',return_value=self.cfg),patch.object(route,'run_proc') as launch:
                result=route.launch_codex(d,'brief',SimpleNamespace(role=role),'rid',T,{})
                self.assertEqual(result['status'],'lane_out')
                launch.assert_not_called()

    def test_snapshot_matches_the_pause_and_identifies_codex_restriction(self):
        with patch.object(route,'mark_busy'):
            snap=route.routing_snapshot(self.cfg,lanes(),{},T)
        codex=next(l for l in snap['lanes'] if l['id']=='OPENAI_SUBSCRIPTION')
        self.assertIn('planning/review only',codex['label'])
        for role in snap['roles']:
            if role['id'] in ('worker','hard','light'):
                self.assertTrue(all(s['family']!='openai' for s in role['chain']))
