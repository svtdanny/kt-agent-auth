#!/usr/bin/env python3
import unittest
from dedicated_gateway import bundle,bootstrap
class BundleTests(unittest.TestCase):
 def setUp(self):self.items=bundle('agent-a','mastra-http','authorizer:test','')
 def find(self,kind,name):return next(x for x in self.items if x['kind']==kind and x['metadata']['name']==name)
 def test_one_dedicated_deployment(self):
  deployments=[x for x in self.items if x['kind']=='Deployment'];self.assertEqual(len(deployments),1)
  pod=deployments[0]['spec']['template']['spec'];self.assertEqual({x['name'] for x in pod['containers']},{'envoy','native-egress','authorizer'})
 def test_only_authorizer_mounts_application_credentials(self):
  for c in self.find('Deployment','agent-egress')['spec']['template']['spec']['containers']:
   mounts={m['name'] for m in c.get('volumeMounts',[])};self.assertEqual('application-credentials' in mounts,c['name']=='authorizer')
 def test_no_secret_api_permissions(self):
  rules=self.find('Role','authorizer-watch')['rules'];self.assertNotIn('secrets',[resource for rule in rules for resource in rule['resources']]);self.assertIn('configmaps',rules[0]['resources'])
 def test_no_shared_gateway_or_pod_listener(self):
  config=bootstrap();self.assertEqual({x['name'] for x in config['static_resources']['listeners']},{'egress','mitm_listener','envoy_metrics'})
 def test_local_processor_fail_closed(self):
  config=bootstrap();cluster=next(x for x in config['static_resources']['clusters'] if x['name']=='agent_authorizer')
  address=cluster['load_assignment']['endpoints'][0]['lb_endpoints'][0]['endpoint']['address']['socket_address'];self.assertEqual(address,{'address':'127.0.0.1','port_value':50052})
 def test_scope_and_credentials_file_are_explicit(self):
  auth=next(x for x in self.find('Deployment','agent-egress')['spec']['template']['spec']['containers'] if x['name']=='authorizer');env={v['name']:v.get('value') for v in auth['env']}
  self.assertNotIn('AGENT_NAME',env);self.assertEqual(env['PROJECT_NAMESPACE'],'agent-a');self.assertEqual(env['CREDENTIALS_FILE'],'/credentials/credentials.json')
 def test_mtls_is_required_for_local_processor(self):
  cluster=next(x for x in bootstrap()['static_resources']['clusters'] if x['name']=='agent_authorizer');tls=cluster['transport_socket']['typed_config'];self.assertEqual(tls['sni'],'authorizer');self.assertIn('tls_certificates',tls['common_tls_context'])
 def test_platform_fqdn_bypasses_only_custom_processor(self):
  listener=next(x for x in bootstrap()['static_resources']['listeners'] if x['name']=='mitm_listener')
  chain=next(x for x in listener['filter_chains'] if x['name']=='egress_cleartext')
  hosts=chain['filters'][0]['typed_config']['route_config']['virtual_hosts']
  platform=next(x for x in hosts if x['name']=='native_platform')
  self.assertIn('kagent-controller.kagent.svc.cluster.local:*',platform['domains'])
  self.assertTrue(platform['typed_per_filter_config']['auth.agent.ext_proc']['disabled'])
  self.assertNotIn('*',platform['domains'])
 def test_processor_tls_protocol_range_is_valid(self):
  cluster=next(x for x in bootstrap()['static_resources']['clusters'] if x['name']=='agent_authorizer')
  params=cluster['transport_socket']['typed_config']['common_tls_context']['tls_params']
  self.assertEqual(params,{'tls_minimum_protocol_version':'TLSv1_3','tls_maximum_protocol_version':'TLSv1_3'})
 def test_gateway_reuses_existing_platform_api_access(self):
  ns=self.find('Namespace','agent-a')
  self.assertEqual(ns['metadata']['labels']['ate.dev/api-access'],'true')
if __name__=='__main__':unittest.main()
