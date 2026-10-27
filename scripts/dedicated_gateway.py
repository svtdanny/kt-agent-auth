"""Local demo adapter for the canonical project YAML bundles in k8s/."""
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from k8s.render import config,resources,envoy,api_patch
ENVOY_IMAGE='envoyproxy/envoy:v1.38.6'
ATENET_IMAGE='localhost:5001/auth-demo/substrate-atenet:dedicated'
def obj(kind,name,ns,**extra):
    api='rbac.authorization.k8s.io/v1' if kind in ('Role','RoleBinding','ClusterRole','ClusterRoleBinding') else 'apps/v1' if kind=='Deployment' else 'v1'
    return {'apiVersion':api,'kind':kind,'metadata':{'name':name,**({'namespace':ns} if ns else {})},**extra}
def demo_config(ns,agent='mastra-http',**overrides):
    return config(NAMESPACE=ns,AGENT=agent,CALLER='test-user',WORKER_POOL='agent-workers',
       AUTHORIZER_IMAGE='localhost:5001/auth-demo/authorizer:dedicated',ATENET_IMAGE=ATENET_IMAGE,
       INTERNAL_ORIGIN='http://internal-mcp.test-services.svc.cluster.local:8080',
       EXTERNAL_ORIGIN='https://dedicated-api.auth-demo.test:8443',**overrides)
def bootstrap():return envoy(config())
def bundle(ns,agent,authorizer_image,ca_pem):
    values=demo_config(ns,agent);values['AUTHORIZER_IMAGE']=authorizer_image
    return resources('namespace',values,ca_pem+Path('/etc/ssl/certs/ca-certificates.crt').read_text())
