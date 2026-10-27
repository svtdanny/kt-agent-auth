#!/usr/bin/env python3
"""Offline checks for the shared gateway bundle; no Kubernetes writes."""
from pathlib import Path
import unittest

import yaml

from permissions_demo import relay_config
from shared_gateway import bootstrap, gateway_ingress_policy

ROOT = Path(__file__).resolve().parents[1]


class DeployBundleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gateway = list(yaml.safe_load_all((ROOT/'deploy/shared-envoy.yaml').read_text()))
        cls.pods = list(yaml.safe_load_all((ROOT/'deploy/pod-egress-demo.yaml').read_text()))
        cls.deployment = cls.find(cls.gateway, 'Deployment', 'atenet-egress')
        cls.service = cls.find(cls.gateway, 'Service', 'atenet-egress')
        cls.policy = cls.find(cls.gateway, 'NetworkPolicy', 'auth-managed-pod-gateway')
        cls.config = yaml.safe_load(cls.find(cls.gateway, 'ConfigMap', 'auth-shared-envoy')['data']['envoy.yaml'])

    @staticmethod
    def find(resources, kind, name):
        return next(item for item in resources if item['kind'] == kind and item['metadata']['name'] == name)

    def test_gateway_config_matches_generator(self):
        self.assertEqual(self.config, bootstrap())

    def test_network_policy_matches_generator(self):
        self.assertEqual(self.policy, gateway_ingress_policy())

    def test_relay_uses_shared_gateway(self):
        config = yaml.safe_load(self.find(self.pods, 'ConfigMap', 'command-runner-relay')['data']['envoy.yaml'])
        self.assertEqual(config, relay_config())
        upstream = config['static_resources']['clusters'][0]['load_assignment']['endpoints'][0]['lb_endpoints'][0]['endpoint']['address']['socket_address']
        self.assertEqual(upstream, {'address': 'atenet-egress.ate-system.svc.cluster.local', 'port_value': 8443})
        self.assertFalse(any(item['kind'] in ('Deployment', 'Service') and item['metadata']['name'] == 'pod-egress' for item in self.pods))

    def test_service_ports_match_listeners(self):
        listeners = {item['name']: item['address']['socket_address']['port_value']
                     for item in self.config['static_resources']['listeners'] if 'address' in item}
        ports = {item['port']: item['targetPort'] for item in self.service['spec']['ports']}
        self.assertEqual(ports[443], listeners['egress'])
        self.assertEqual(ports[8443], listeners['managed_pods'])
        self.assertNotEqual(ports[443], ports[8443])

    def test_network_policy_preserves_native_and_readiness_ingress(self):
        # A policy selects the whole Pod, not just the managed listener. Native
        # traffic must explicitly remain admitted on the target/container port.
        native = next(port['targetPort'] for port in self.service['spec']['ports'] if port['port'] == 443)
        envoy = next(c for c in self.deployment['spec']['template']['spec']['containers'] if c['name'] == 'envoy')
        readiness = envoy['readinessProbe']['httpGet']['port']
        unrestricted_ports = {port['port'] for rule in self.policy['spec']['ingress'] if not rule.get('from')
                              for port in rule.get('ports', []) if port.get('protocol', 'TCP') == 'TCP'}
        for port in (native, readiness):
            with self.subTest(port=port):
                self.assertIn(port, unrestricted_ports, 'NetworkPolicy would isolate an existing gateway listener')

    def test_managed_listener_keeps_combined_namespace_and_pod_selector(self):
        target = next(port['targetPort'] for port in self.service['spec']['ports'] if port['port'] == 8443)
        rules = [rule for rule in self.policy['spec']['ingress']
                 if not rule.get('ports') or any(port['port'] == target for port in rule['ports'])]
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]['from'], [{
            'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'demo'}},
            'podSelector': {'matchLabels': {'auth.demo.dev/egress': 'enforced'}},
        }])

    def test_processor_failure_is_closed(self):
        filters = [f['typed_config'] for listener in self.config['static_resources']['listeners']
                   for chain in listener.get('filter_chains', []) for network in chain['filters']
                   for f in network.get('typed_config', {}).get('http_filters', []) if f['name'] == 'auth.demo.ext_proc']
        self.assertEqual(len(filters), 5)
        self.assertTrue(all(f['failure_mode_allow'] is False for f in filters))

    def test_public_bundles_contain_no_private_material(self):
        for filename in ('shared-envoy.yaml', 'pod-egress-demo.yaml', 'command-runner.yaml'):
            with self.subTest(filename=filename):
                raw = (ROOT/'deploy'/filename).read_text()
                self.assertNotIn('PRIVATE KEY-----', raw)
                self.assertFalse(any(item['kind'] == 'Secret' for item in yaml.safe_load_all(raw)))


if __name__ == '__main__':
    unittest.main()
