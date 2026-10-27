#!/usr/bin/env python3
"""Check bundle ownership and references across multiple project Agents."""
import json,unittest
from render import config,resources,api_patch,envoy
class RenderTests(unittest.TestCase):
 def setUp(self):self.values=config(NAMESPACE='project-test',AGENT='first',GATEWAYS={'project-test':'agent-egress.project-test.svc:443'})
 def test_namespace_owns_shared_runtime_and_permissions(self):
  objects=resources('namespace',self.values,'TEST PUBLIC CA')
  kinds=[o['kind'] for o in objects]
  for kind in ['WorkerPool','Deployment','EgressPolicy','ConfigMap','ServiceAccount']:self.assertIn(kind,kinds)
  self.assertNotIn('Agent',kinds);self.assertNotIn('Harness',kinds);self.assertNotIn('ClusterRole',kinds)
  self.assertEqual(kinds.count('Deployment'),1);self.assertEqual(kinds.count('WorkerPool'),1)
 def test_agents_reuse_project_infrastructure(self):
  for name in ['first','second']:
   values={**self.values,'AGENT':name};objects=resources('agent',values)
   self.assertEqual({o['kind'] for o in objects},{'Harness','Agent'})
   harness=next(o for o in objects if o['kind']=='Harness');agent=next(o for o in objects if o['kind']=='Agent')
   self.assertEqual(agent['spec']['harnessRef']['name'],harness['metadata']['name'])
   self.assertEqual(harness['spec']['substrate']['workerPoolRef']['name'],values['WORKER_POOL'])
   self.assertTrue(harness['spec']['substrate']['snapshotPolicy']['location'].endswith('/'+name+'/'))
 def test_every_resource_is_in_the_project_namespace(self):
  for bundle in ['namespace','agent']:
   for obj in resources(bundle,self.values,'CA'):
    if obj['kind'] not in ['Namespace','ClusterRoleBinding']:self.assertEqual(obj['metadata']['namespace'],'project-test')
 def test_gateway_assignments_are_cumulative_and_patch_is_separate(self):
  values=config(GATEWAYS={'a':'agent-egress.a.svc:443','b':'agent-egress.b.svc:443'})
  objects=resources('cluster',values)
  self.assertEqual({o['kind'] for o in objects},{'ClusterRole','ConfigMap'})
  cm=next(o for o in objects if o['kind']=='ConfigMap')
  self.assertEqual(json.loads(cm['data']['gateways.json']),values['GATEWAYS'])
  self.assertIn('containers',api_patch(values)['spec']['template']['spec'])
 def test_origins_match_between_transport_and_request_policies(self):
  values={**self.values,'INTERNAL_ORIGIN':'http://mcp.tools.svc:9090','EXTERNAL_ORIGIN':'https://example.org'}
  objects=resources('namespace',values,'CA')
  transport=next(o for o in objects if o['kind']=='EgressPolicy')
  cm=next(o for o in objects if o['kind']=='ConfigMap' and o['metadata']['name']=='agent-permissions')
  allowed={x['origin'] for x in transport['spec']['destinations']}
  for grant in json.loads(cm['data']['config'])['permissions']:self.assertIn(grant['config']['origin'],allowed)
 def test_platform_exemption_uses_configured_endpoints(self):
  text=json.dumps(envoy(config(SUBSTRATE_NAMESPACE='runtime',KAGENT_NAMESPACE='agents',CLUSTER_DOMAIN='corp.local')))
  self.assertIn('api.runtime.svc.corp.local',text);self.assertIn('kagent-controller.agents.svc.corp.local',text)
  self.assertNotIn('api.ate-system.svc',text)
if __name__=='__main__':unittest.main()
