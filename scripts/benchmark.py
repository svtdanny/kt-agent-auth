#!/usr/bin/env python3
"""Run bounded in-cluster load and collect resource samples on auth-demo only."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

ROOT=Path(__file__).resolve().parents[1]
ENV={**os.environ,'KUBECONFIG':str(ROOT/'.cache/kubeconfig')}
NAME='auth-benchmark'

def kubectl(*args, input=None):
    return subprocess.check_output(['kubectl','--context','auth-demo',*args],env=ENV,input=input,text=True)

def apply(items):
    kubectl('apply','-f','-',input=json.dumps({'apiVersion':'v1','kind':'List','items':items}))

def reported_env(entries):
    return [{**entry,'value':'[redacted]'} if 'value' in entry and any(
        word in entry['name'].upper() for word in ('SECRET','PASSWORD','TOKEN','API_KEY','CREDENTIAL'))
        else entry for entry in entries]

def pods():
    data=json.loads(kubectl('get','pods','-A','-o','json'))
    return [{'namespace':p['metadata']['namespace'],'name':p['metadata']['name'],
             'containers':[{'name':c['name'],'imageID':c.get('imageID'),'restarts':c['restartCount'],'state':c['state']} for c in p.get('status',{}).get('containerStatuses',[])]}
            for p in data['items'] if p['metadata']['namespace'] in ('demo','kagent','ate-system')]

def resources():
    data=json.loads(kubectl('get','--raw','/api/v1/nodes/auth-demo/proxy/stats/summary'))
    return {'atUTC':datetime.now(timezone.utc).isoformat(),
            'node':{'cpu':data['node'].get('cpu'),'memory':data['node'].get('memory')},
            'pods':[{'namespace':p['podRef']['namespace'],'name':p['podRef']['name'],
                     'cpu':p.get('cpu'),'memory':p.get('memory'),
                     'containers':[{'name':c['name'],'cpu':c.get('cpu'),'memory':c.get('memory')} for c in p.get('containers',[])]}
                    for p in data.get('pods',[]) if p['podRef']['namespace'] in ('demo','kagent','ate-system')]}

def build_runner():
    minikube=shutil.which('minikube') or str(ROOT/'.cache/bin/minikube')
    subprocess.run(['go','build','-trimpath','-ldflags=-s -w','-o','.cache/build/benchmark','./cmd/benchmark'],env={**ENV,'CGO_ENABLED':'0'},check=True)
    subprocess.run(['docker','build','-f','deploy/Dockerfile.services','--build-arg','BINARY=benchmark','-t','localhost:5001/auth-demo/benchmark:demo','.'],check=True)
    subprocess.run([minikube,'-p','auth-demo','image','load','--daemon','localhost:5001/auth-demo/benchmark:demo'],check=True)

def main():
    os.chdir(ROOT)
    assert kubectl('config','current-context').strip()=='auth-demo'
    build_runner()
    kubectl('-n','demo','delete','pod',NAME,'--ignore-not-found=true','--wait=true')
    token=kubectl('-n','demo','create','token','test-user','--audience=auth-demo','--duration=1h').strip()
    before=pods()
    images=json.loads(kubectl('get','deploy','-A','-o','json'))
    environment=[{'namespace':d['metadata']['namespace'],'name':d['metadata']['name'],
                  'replicas':d['spec'].get('replicas'),
                  'containers':[{'name':c['name'],'image':c['image'],'args':c.get('args',[]),'resources':c.get('resources',{}),'env':reported_env(c.get('env',[]))} for c in d['spec']['template']['spec']['containers']]}
                 for d in images['items'] if d['metadata']['namespace'] in ('demo','kagent','ate-system')]
    from database import sql
    import yaml
    controller=json.loads(kubectl('-n','kagent','get','configmap','kagent-controller','-o','json'))
    substrate_args=next(c['args'] for d in environment if d['namespace']=='ate-system' and d['name']=='ate-api-server' for c in d['containers'])
    substrate_pool=int(next(a.split('=',1)[1] for a in substrate_args if a.startswith('--postgres-pool-max-conns=')))
    database_budget={'serverMaxConnections':int(sql('SHOW max_connections;').strip()),
                     'bridgeMaxConnections':4,
                     'kagentPool':yaml.safe_load(controller['data']['controller.yaml'])['database']['pool'],
                     'substratePoolMaxConnectionsPerReplica':substrate_pool}
    secret={'apiVersion':'v1','kind':'Secret','metadata':{'name':NAME+'-token','namespace':'demo'},'stringData':{'token':token}}
    pod={'apiVersion':'v1','kind':'Pod','metadata':{'name':NAME,'namespace':'demo','labels':{'app':NAME}},'spec':{
        'restartPolicy':'Never','activeDeadlineSeconds':600,'automountServiceAccountToken':False,
        'securityContext':{'runAsUser':65532,'runAsGroup':65532},
        'containers':[{'name':'benchmark','image':'localhost:5001/auth-demo/benchmark:demo','imagePullPolicy':'Never',
                       'resources':{'requests':{'cpu':'100m','memory':'64Mi'},'limits':{'cpu':'2','memory':'512Mi'}},
                       'volumeMounts':[{'name':'tls','mountPath':'/tls','readOnly':True},{'name':'token','mountPath':'/token','readOnly':True},{'name':'control-tls','mountPath':'/control-tls','readOnly':True}]}],
        'volumes':[{'name':'tls','secret':{'secretName':'auth-gateway-client-benchmark'}},{'name':'token','secret':{'secretName':NAME+'-token'}},{'name':'control-tls','secret':{'secretName':'auth-ext-proc-tls'}}]}}
    # Use the same trust and client identity as the gateway only for the explicitly
    # labelled direct ext_proc measurement; no credentials appear in the report.
    gateway=json.loads(kubectl('-n','ate-system','get','secret','auth-gateway-client','-o','json'))
    client_secret={'apiVersion':'v1','kind':'Secret','metadata':{'name':'auth-gateway-client-benchmark','namespace':'demo'},'data':gateway['data']}
    samples=[]
    try:
        apply([secret,client_secret,pod])
        kubectl('-n','demo','wait','--for=condition=Ready','pod/'+NAME,'--timeout=90s')
        seen=set()
        for _ in range(120):
            try:samples.append(resources())
            except subprocess.CalledProcessError as error:print('Resource sampling failed:',error,flush=True)
            logs=kubectl('-n','demo','logs',NAME)
            for line in logs.splitlines():
                if line.startswith('STAGE ') and line not in seen:print(line,flush=True);seen.add(line)
            state=json.loads(kubectl('-n','demo','get','pod',NAME,'-o','json'))
            if state['status']['phase'] in ('Succeeded','Failed'):break
            time.sleep(5)
        else:raise AssertionError('benchmark exceeded bounded runtime')
        logs=kubectl('-n','demo','logs',NAME)
        (ROOT/'.cache/benchmark.log').write_text(logs)
        lines=[line for line in logs.splitlines() if line.startswith('BENCHMARK_RESULT ')]
        if not lines:raise AssertionError('benchmark did not report a result: '+logs[-3000:])
        report=json.loads(lines[-1].removeprefix('BENCHMARK_RESULT '))
        report.update({'cluster':'auth-demo','node':json.loads(kubectl('get','node','auth-demo','-o','json'))['status']['nodeInfo'],
                       'environment':environment,'databaseBudget':database_budget,'podStateBefore':before,'podStateAfter':pods(),'resourceSamples':samples,
                       'limitations':['Single local Minikube node; one bridge and one processor replica.',
                                      'Closed-loop concurrency, not an open-loop arrival rate or an overnight soak.',
                                      'Direct ext_proc stage uses the gateway client certificate and an asserted real session SPIFFE ID; it excludes the actual gateway and upstream.',
                                      'Full path runs real Mastra tools and transparent gateway interception in two native sessions.',
                                      'Client-go QPS/burst and service resource limits are unchanged; policies use Kubernetes watches and Vault credentials use cache-on-first-read with explicit invalidation.']})
        full=[s for s in report['stages'] if s.get('statuses')]
        direct=[s for s in report['stages'] if s.get('counts')]
        report['assessment']={
            'authorizationBypasses':sum(s['unexpectedAllows'] for s in full)+sum(s['counts']['unexpectedAllows'] for s in direct),
            'wrongCredentialResponses':sum(s['wrongCredentialCount'] for s in full),
            'upstreamAuditMatchesSuccessfulRequests':all(s['upstreamCountMatchesSuccesses'] for s in full),
            'allowedWorkloadFailures':sum(s['completed']-s['statuses'].get('200',0) for s in full if not s['forbiddenWorkload']),
            'directUnexpectedDenials':sum(s['counts']['unexpectedDenials'] for s in direct),
            'directTransportErrors':sum(s['counts']['transportErrors'] for s in direct),
            'recoveredAfterOverload':next((s['recovered'] for s in report['stages'] if s['name']=='recovery/three-successes'),False),
        }
        delta=report.get('authorityTraffic',{}).get('delta',{})
        report['assessment']['warmPolicyGETs']=delta.get('accountGETs',0)
        report['assessment']['warmVaultReads']=delta.get('vaultReads',0)
        (ROOT/'artifacts').mkdir(exist_ok=True)
        destination=ROOT/'artifacts/benchmark-results.json'
        destination.write_text(json.dumps(report,indent=2)+'\n')
        print('Report:',destination,flush=True)
        if state['status']['phase']!='Succeeded' or not report['completed']:raise AssertionError(report.get('error','benchmark pod failed'))
    finally:
        kubectl('-n','demo','delete','pod',NAME,'--ignore-not-found=true','--wait=true')
        kubectl('-n','demo','delete','secret',NAME+'-token','auth-gateway-client-benchmark','--ignore-not-found=true')

if __name__=='__main__':main()
