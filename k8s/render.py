#!/usr/bin/env python3
"""Render the three YAML bundles; never connects to Kubernetes or reads Secrets."""
import argparse,json,re
from pathlib import Path
import yaml
ROOT=Path(__file__).resolve().parent
TOKEN=re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")
FILES={
 'cluster':['reviewer-rbac.yaml','gateway-assignments.yaml'],
 'namespace':['namespace.yaml','authorizer-rbac.yaml','services.yaml','egress-deployment.yaml',
              'worker-pool.yaml','egress-policy.yaml','permissions.yaml','callers.yaml'],
 'agent':['harness.yaml','agent.yaml'],
}
def config(path=None,**overrides):
    values=yaml.safe_load((ROOT/'deployment.example.yaml').read_text())
    if path:values.update(yaml.safe_load(Path(path).read_text()))
    values.update(overrides)
    values['GATEWAYS_JSON']=json.dumps(values['GATEWAYS'],sort_keys=True)
    return values

def expand(value,values):
    if isinstance(value,dict):return {k:expand(v,values) for k,v in value.items()}
    if isinstance(value,list):return [expand(v,values) for v in value]
    if isinstance(value,str):
        def replace(match):
            key=match.group(1)
            if key not in values:raise ValueError('Missing bundle input: '+key)
            return str(values[key])
        return TOKEN.sub(replace,value)
    return value

def load(path,values):
    return [expand(doc,values) for doc in yaml.safe_load_all((ROOT/path).read_text()) if doc is not None]

def envoy(values=None):return load('namespace/envoy/envoy.yaml',values or config())[0]

def resources(bundle,values,trust_bundle=None):
    objects=[]
    for name in FILES[bundle]:objects.extend(load(bundle+'/'+name,values))
    if bundle=='namespace':
        sds=load('namespace/envoy/sds.yaml',values)[0]
        data={k:yaml.safe_dump(v,sort_keys=False) for k,v in sds.items()}
        data['envoy.yaml']=yaml.safe_dump(envoy(values),sort_keys=False)
        if trust_bundle is None:trust_bundle=Path(values['TRUST_BUNDLE_FILE']).read_text()
        for name,data in [('agent-envoy',data),('upstream-trust',{'upstream-ca.pem':trust_bundle})]:
            objects.append({'apiVersion':'v1','kind':'ConfigMap','metadata':{'name':name,'namespace':values['NAMESPACE']},'data':data})
    return objects

def api_patch(values):return load('cluster/substrate-api-patch.yaml',values)[0]

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True,help='Non-secret bundle inputs; see deployment.example.yaml')
    parser.add_argument('--bundle',choices=[*FILES,'all'],default='all')
    parser.add_argument('--output',required=True,help='Directory for rendered Kubernetes YAML')
    args=parser.parse_args();values=config(args.config)
    if not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?',values['NAMESPACE']):parser.error('Invalid project namespace')
    if args.bundle in ['agent','all'] and not re.fullmatch(r'[^\s@]+@sha256:[a-f0-9]{64}',values['AGENT_IMAGE']):
        parser.error('AGENT_IMAGE must be an immutable image reference ending in @sha256:<64 lowercase hex digits>')
    output=Path(args.output);output.mkdir(parents=True,exist_ok=True)
    for name in FILES if args.bundle=='all' else [args.bundle]:
        target=output/(name+'.yaml')
        target.write_text(yaml.safe_dump_all(resources(name,values),sort_keys=False))
        print(target)
        if name=='cluster':
            patch=output/'substrate-api-patch.yaml'
            patch.write_text(yaml.safe_dump(api_patch(values),sort_keys=False));print(patch)
if __name__=='__main__':main()
