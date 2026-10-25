import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from test_route import route, CFG, lanes, T
import onboard


class ZaiTests(unittest.TestCase):
    def choose(self, role, ls, **kwargs):
        cfg=copy.deepcopy(CFG)
        cfg['settings'].update(deepseek_free_enabled=True, zai_coding_enabled=True)
        return route.decide(role,cfg,ls,{},T,dict(authored_by=None, failed=[], premium=None, allow_degraded=False, **kwargs))

    def test_worker_priority_and_backoff(self):
        ls=lanes()
        ls['DEEPSEEK_FREE']=route.Lane('DEEPSEEK_FREE','deepseek','subscription')
        ls['ZAI_CODING_SUBSCRIPTION']=route.Lane('ZAI_CODING_SUBSCRIPTION','zai','subscription')
        self.assertEqual(self.choose('worker',ls)['lane'],'DEEPSEEK_FREE')
        ls['DEEPSEEK_FREE'].out=True
        d=self.choose('worker',ls)
        self.assertEqual(d['lane'],'ZAI_CODING_SUBSCRIPTION')
        self.assertEqual(d['how']['model'],'zai-coding-plan/glm-5.3')
        self.assertEqual(self.choose('light',ls)['model'],'glm-5.3-flash')
        ls['ZAI_CODING_SUBSCRIPTION'].out=True
        self.assertEqual(self.choose('worker',ls)['lane'],'OPENAI_SUBSCRIPTION')

    def test_zai_author_gets_other_family(self):
        cfg=copy.deepcopy(CFG);cfg['settings'].update(deepseek_free_enabled=True,zai_coding_enabled=True)
        args=dict(authored_by='zai',failed=[],premium=None,allow_degraded=False)
        d=route.decide('reviewer',cfg,lanes(),{},T,args)
        self.assertEqual(d['lane'],'OPENAI_SUBSCRIPTION')
        ls={'ZAI_CODING_SUBSCRIPTION':route.Lane('ZAI_CODING_SUBSCRIPTION','zai','subscription')}
        d=route.decide('reviewer',cfg,ls,{},T,args)
        self.assertEqual(d['flag'],'same_family')

    def test_disabled_does_not_change_original_rungs(self):
        cfg=copy.deepcopy(CFG)
        steps=route.build_tiers(cfg['roles']['worker'],cfg,{})[0]
        self.assertEqual(steps[0]['on'],'openai')
        self.assertEqual(steps[0]['idx'],0)

    def test_readiness_needs_subscription_credential(self):
        cfg=copy.deepcopy(CFG);cfg['settings']['zai_coding_enabled']=True
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,{'XDG_DATA_HOME':tmp}), patch.object(route,'opencode_exe',return_value='opencode'), patch.object(route,'load_routing',return_value=cfg), patch.object(onboard.sys,'platform','darwin'):
            p=Path(tmp)/'opencode/auth.json';p.parent.mkdir()
            for auth,expected in [({'zai':{'type':'api','key':'test'}},False),({'zai-coding-plan':{}},False),({'zai-coding-plan':{'type':'api','key':'test'}},True)]:
                p.write_text(json.dumps(auth))
                self.assertEqual(onboard.check_opencode_zai_coding(route),expected)

    def test_launch_pins_provider_and_removes_override(self):
        d=dict(lane='ZAI_CODING_SUBSCRIPTION',model='glm-5.3',effort='high',how=dict(model='zai-coding-plan/glm-5.3',cwd='/tmp',write=False))
        a=SimpleNamespace(resume=None,schema=None,timeout=10)
        with tempfile.TemporaryDirectory() as tmp, patch.object(route,'config_dir',return_value=Path(tmp)), patch.object(route,'opencode_exe',return_value='opencode'),patch.object(route.sys,'platform','darwin'),patch.object(route,'opencode_shell'),patch.object(route,'run_proc',return_value=(0,'','',False)) as run,patch.object(route,'agent_runs',None),patch.object(route,'child_env',return_value={'OPENCODE_AUTH_JSON':'override','OPENCODE_CONFIG':'override','ZHIPU_API_KEY':'override'}):
            self.assertEqual(route.launch_opencode(d,'OK',a,'test1234',1,{})['status'],'done')
            # Inspect the actual child process environment.
            env=run.call_args.args[3]
            self.assertEqual(json.loads(env['OPENCODE_CONFIG_CONTENT'])['enabled_providers'],['zai-coding-plan'])
            self.assertNotIn('ZHIPU_API_KEY',env)
            self.assertNotIn('OPENCODE_AUTH_JSON',env)
            d['how']['model']='zai/glm-5.3'
            run.reset_mock()
            self.assertEqual(route.launch_opencode(d,'OK',a,'test1234',1,{})['reason'],'setup')
            run.assert_not_called()

    def test_dashboard_inventory_exposes_subscription(self):
        cfg=copy.deepcopy(CFG);cfg['settings']['zai_coding_enabled']=True
        ls=lanes();ls['ZAI_CODING_SUBSCRIPTION']=route.Lane('ZAI_CODING_SUBSCRIPTION','zai','subscription')
        shot=route.routing_snapshot(cfg,ls,{},T)
        row=next(r for r in shot['lanes'] if r['id']=='ZAI_CODING_SUBSCRIPTION')
        self.assertEqual(row['billing'],'subscription')
        self.assertEqual(row['label'],'Z.ai GLM Coding Plan')

    def test_both_ported_lanes_run_under_the_opencode_watchdog(self):
        a=SimpleNamespace(resume=None,schema=None,timeout=10)
        for lane,model in (('ZAI_CODING_SUBSCRIPTION','zai-coding-plan/glm-5.3'),('DEEPSEEK_FREE','opencode/deepseek-v4.1-flash-free')):
            d=dict(lane=lane,model='m',effort='high',how=dict(model=model,cwd='/tmp',write=False))
            with self.subTest(lane=lane), tempfile.TemporaryDirectory() as tmp, patch.object(route,'config_dir',return_value=Path(tmp)), patch.object(route,'opencode_exe',return_value='opencode'),patch.object(route.sys,'platform','darwin'),patch.object(route,'opencode_shell'),patch.object(route,'opencode_free_model',return_value=True),patch.object(route,'run_proc',return_value=(0,'','',False)) as run,patch.object(route,'agent_runs',None),patch.object(route,'child_env',return_value={}):
                route.launch_opencode(d,'OK',a,'test1234',1,{})
                self.assertTrue(callable(run.call_args.args[6]), 'run_proc got no watchdog poll')

if __name__=='__main__':unittest.main()
