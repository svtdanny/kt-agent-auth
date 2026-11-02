#!/usr/bin/env python3
"""Compatibility checks: overlay must preserve every upstream policy and route."""
import copy
import unittest
import yaml
from namespace_egress_overlay import FILTER, additions, overlay, pinned_manifest, render


class OverlayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = pinned_manifest().read_text()
        cls.documents = list(yaml.safe_load_all(cls.source))
        cls.base = yaml.safe_load(next(d for d in cls.documents if d and
            d.get('kind') == 'ConfigMap' and 'envoy.yaml' in d.get('data', {}))['data']['envoy.yaml'])

    def test_removing_only_additions_restores_exact_upstream_config(self):
        original = copy.deepcopy(self.base)
        result = overlay(self.base)
        self.assertEqual(self.base, original)
        resources = result['static_resources']
        resources['listeners'] = [l for l in resources['listeners'] if l['name'] != 'project_authorizer_router']
        resources['clusters'] = [c for c in resources['clusters'] if c['name'] not in
                                {'project_authorizer_internal', 'project_authorizers_dns'}]
        for listener in resources['listeners']:
            if listener['name'] != 'mitm_listener':
                continue
            for chain in listener['filter_chains']:
                if chain['name'] == 'egress_passthrough':
                    continue
                filters = chain['filters'][0]['typed_config']['http_filters']
                self.assertEqual(sum(f['name'] == FILTER for f in filters), 1)
                filters[:] = [f for f in filters if f['name'] != FILTER]
        self.assertEqual(result, self.base)

    def test_manifest_changes_only_envoy_configmap_data(self):
        result = list(yaml.safe_load_all(render(self.source)))
        for before, after in zip(self.documents, result, strict=True):
            if before and before.get('kind') == 'ConfigMap' and 'envoy.yaml' in before.get('data', {}):
                after['data']['envoy.yaml'] = before['data']['envoy.yaml']
                for name in additions()['secrets']:
                    del after['data'][name]
            self.assertEqual(before, after)

    def test_internal_router_has_no_network_listener_or_project_registry(self):
        extra = additions()
        self.assertNotIn('address', extra['listener'])
        self.assertEqual(extra['listener']['internal_listener'], {})
        self.assertNotIn('auth-agent-a', yaml.safe_dump(extra))
        self.assertNotIn('auth-agent-b', yaml.safe_dump(extra))
        self.assertFalse(extra['processor']['typed_config']['failure_mode_allow'])
        self.assertTrue(extra['processor']['typed_config']['disable_clear_route_cache'])

    def test_repeated_overlay_and_upstream_drift_are_rejected(self):
        with self.assertRaises(ValueError):
            overlay(overlay(self.base))
        broken = copy.deepcopy(self.base)
        listener = next(l for l in broken['static_resources']['listeners'] if l['name'] == 'mitm_listener')
        listener['filter_chains'][0]['name'] = 'changed-upstream-contract'
        with self.assertRaises(ValueError):
            overlay(broken)

    def test_existing_additional_processor_is_not_silently_duplicated(self):
        base = copy.deepcopy(self.base)
        base['static_resources']['clusters'].append({'name': 'additional_egress_ext_proc'})
        with self.assertRaisesRegex(ValueError, 'disable additionalExtprocService'):
            overlay(base)


if __name__ == '__main__':
    unittest.main()
