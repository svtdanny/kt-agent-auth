#!/usr/bin/env python3
"""Retire this repository's previous local pilot after dedicated E2E validation."""
import base64,gzip,json,yaml
from local import kubectl,apply

def main():
    # Restore the platform gateway from its own installed Helm release. Other
    # namespaces keep their original default egress; only our old demo is retired.
    releases=json.loads(kubectl('-n','ate-system','get','secrets','-l','owner=helm,name=substrate,status=deployed','-o','json'))['items']
    if len(releases)!=1:raise RuntimeError('Expected one deployed Substrate Helm release')
    release=json.loads(gzip.decompress(base64.b64decode(base64.b64decode(releases[0]['data']['release']))))
    objects=[o for o in yaml.safe_load_all(release['manifest']) if o and o.get('metadata',{}).get('name','').startswith('atenet-egress') and o['kind'] in ['Deployment','Service','ConfigMap']]
    deployment=next(o for o in objects if o['kind']=='Deployment')
    if [c['name'] for c in deployment['spec']['template']['spec']['containers']]!=['agentgateway']:
        raise RuntimeError('Installed platform release is not the expected stock gateway')
    apply([o for o in objects if o['kind']=='ConfigMap'])
    # Replace exact Pod spec/Service ports: a three-way apply would retain
    # sidecars and ports introduced before a last-applied annotation existed.
    kubectl('-n','ate-system','patch','deploy','atenet-egress','--type=json','-p',json.dumps([{'op':'replace','path':'/spec','value':deployment['spec']}]))
    service=next(o for o in objects if o['kind']=='Service')
    kubectl('-n','ate-system','patch','svc','atenet-egress','--type=json','-p',json.dumps([{'op':'replace','path':'/spec/ports','value':service['spec']['ports']}]))
    kubectl('-n','ate-system','rollout','status','deploy/atenet-egress','--timeout=180s')
    # Remove only the superseded resources owned by this demo. Keep PostgreSQL
    # and its retained troubleshooting rows; do not touch unrelated workloads.
    kubectl('-n','demo','delete','agent.api.kagent.dev','mastra-http','--ignore-not-found=true','--wait=false')
    kubectl('-n','demo','delete','harness.api.kagent.dev','mastra','--ignore-not-found=true','--wait=false')
    kubectl('-n','demo','delete','workerpool.ate.dev','auth-demo','--ignore-not-found=true','--wait=false')
    kubectl('-n','demo','delete','deploy','auth-control-plane','auth-ext-proc','command-runner','--ignore-not-found=true')
    kubectl('-n','demo','delete','svc','auth-control-plane','auth-ext-proc','--ignore-not-found=true')
    kubectl('-n','ate-system','delete','cm','auth-shared-envoy','auth-upstream-trust','--ignore-not-found=true')
    kubectl('-n','ate-system','delete','networkpolicy','auth-managed-pod-gateway','--ignore-not-found=true')
    print('Previous shared demo retired; stock platform default gateway restored.')
if __name__=='__main__':main()
