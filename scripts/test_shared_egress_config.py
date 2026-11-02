#!/usr/bin/env python3
"""Check the direct per-atespace processor rules preserve native route guards."""
import unittest
from shared_egress import AUTHORIZERS, FILTER, IDENTITY, bootstrap
from dedicated_gateway import bootstrap as dedicated_bootstrap


class SharedEgressConfigTests(unittest.TestCase):
    def test_each_native_route_keeps_its_guards_and_gets_one_project_processor(self):
        dedicated = dedicated_bootstrap()
        shared = bootstrap()
        def chains(config):
            listener = next(l for l in config['static_resources']['listeners'] if l['name'] == 'mitm_listener')
            return {c['name']: c['filters'][0]['typed_config'] for c in listener['filter_chains']}
        before, after = chains(dedicated), chains(shared)
        for name, hcm in before.items():
            hosts = {h['name']: h for h in after[name]['route_config']['virtual_hosts']}
            for host in hcm['route_config']['virtual_hosts']:
                if host['name'] == 'native_platform':
                    self.assertEqual(host, hosts[host['name']])
                    continue
                routes = hosts[host['name']]['routes']
                self.assertEqual(len(routes), len(host['routes']) * len(AUTHORIZERS))
                for namespace, start in zip(AUTHORIZERS, range(0, len(routes), len(host['routes']))):
                    for original, route in zip(host['routes'], routes[start:start+len(host['routes'])]):
                        self.assertEqual(route['route'], original['route'])
                        for key, value in original['match'].items():
                            self.assertEqual(route['match'][key], value)
                        self.assertEqual(route['match']['filter_state'], [{'key': IDENTITY, 'string_match': {
                            'prefix': 'spiffe://substrate-actor.local/worker-for-actor/' + namespace + '/'}}])
                        self.assertEqual(route['typed_per_filter_config'][FILTER]['overrides']['grpc_service']['envoy_grpc']['cluster_name'], 'authorizer_' + namespace)
    def test_clusters_use_fixed_project_dns_and_project_tls(self):
        clusters = {c['name']: c for c in bootstrap()['static_resources']['clusters']}
        for namespace, dns in AUTHORIZERS.items():
            c = clusters['authorizer_' + namespace]
            address = c['load_assignment']['endpoints'][0]['lb_endpoints'][0]['endpoint']['address']['socket_address']
            self.assertEqual(address, {'address': dns, 'port_value': 50052})
            tls = c['transport_socket']['typed_config']
            self.assertEqual(tls['sni'], 'authorizer.' + namespace + '.svc')
            self.assertEqual(tls['common_tls_context']['validation_context']['match_typed_subject_alt_names'][0]['matcher']['exact'], tls['sni'])
    def test_default_processor_cannot_fall_back_to_a_project(self):
        result = bootstrap()
        default = next(c for c in result['static_resources']['clusters'] if c['name'] == 'agent_authorizer')
        self.assertEqual(default['load_assignment']['endpoints'][0]['lb_endpoints'][0]['endpoint']['address']['socket_address'],
                         {'address': '127.0.0.1', 'port_value': 1})
        for listener in result['static_resources']['listeners']:
            if listener['name'] != 'mitm_listener': continue
            for chain in listener['filter_chains']:
                custom = next(f for f in chain['filters'][0]['typed_config']['http_filters'] if f['name'] == FILTER)['typed_config']
                self.assertFalse(custom['failure_mode_allow'])
                self.assertTrue(custom['disable_clear_route_cache'])


if __name__ == '__main__':
    unittest.main()
