import hermetic  # noqa: F401  (a home of its own for every test)
import copy
import json
import unittest
from unittest.mock import patch
from test_route import route, CFG, lanes, T


class RoutingSnapshot(unittest.TestCase):
    def snapshot(self, cfg=None, ls=None):
        return route.routing_snapshot(cfg or copy.deepcopy(CFG), ls if ls is not None else lanes(), {}, T)

    def test_inventory_includes_disabled_lanes_and_distinguishes_free_and_credit(self):
        cfg = copy.deepcopy(CFG)
        cfg['settings']['private_key'] = 'do-not-export'
        result = self.snapshot(cfg)
        inventory = {l['id']: l for l in result['lanes']}
        self.assertEqual(set(inventory), set(cfg['lanes']))
        self.assertFalse(inventory['DEEPSEEK_FREE']['enabled'])
        self.assertEqual(inventory['DEEPSEEK_FREE']['billing'], 'free')
        self.assertFalse(inventory['CLAUDE_CREDIT']['unattended'])
        self.assertNotIn('do-not-export', json.dumps(result))
        self.assertEqual(result['caller'], 'unattended')

    def test_selected_matches_real_decision_and_keeps_credit_out(self):
        cfg = copy.deepcopy(CFG)
        ls = lanes()
        ls['OPENAI_SUBSCRIPTION'].out = True
        ls['CLAUDE_SUBSCRIPTION'].out = True
        result = self.snapshot(cfg, ls)
        roles = {r['id']: r for r in result['roles']}
        self.assertEqual(roles['worker']['selected']['action'], 'stop')
        self.assertTrue(all('CLAUDE_CREDIT' not in s['lanes'] for r in roles.values() for s in r['chain']))
        normal = {r['id']: r for r in self.snapshot()['roles']}
        self.assertEqual(normal['worker']['selected']['lane'], 'OPENAI_SUBSCRIPTION')

    def test_deepseek_precedence_and_review_by_author(self):
        cfg = copy.deepcopy(CFG);cfg['settings']['deepseek_free_enabled'] = True
        ls = lanes();ls['DEEPSEEK_FREE'] = route.Lane('DEEPSEEK_FREE', 'deepseek', 'subscription')
        roles = {r['id']: r for r in self.snapshot(cfg, ls)['roles']}
        self.assertEqual(roles['worker']['selected']['lane'], 'DEEPSEEK_FREE')
        self.assertEqual(roles['light']['selected']['lane'], 'DEEPSEEK_FREE')
        self.assertEqual(roles['reviewer']['by_author']['deepseek']['selected']['lane'], 'OPENAI_SUBSCRIPTION')
        self.assertEqual(roles['worker']['chain'][0]['lanes'], ['DEEPSEEK_FREE'])
        self.assertEqual(roles['hard']['advance'], 'on_failure')

    def test_busy_choice_is_waiting_and_snapshot_does_not_mutate_lanes(self):
        ls=lanes()
        def busy(current, *args):
            lane=current['OPENAI_SUBSCRIPTION'];lane.busy=True;lane.out=True;lane.out_until=T+60;lane.notes=['busy']
        with patch.object(route, 'mark_busy', side_effect=busy):
            result=self.snapshot(ls=ls)
        worker=next(r for r in result['roles'] if r['id']=='worker')
        self.assertEqual(worker['selected']['reason'], 'busy')
        self.assertFalse(ls['OPENAI_SUBSCRIPTION'].out)
