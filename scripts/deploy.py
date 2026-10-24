#!/usr/bin/env python3
"""Deploy the demo into auth-demo; only public APIs and runtime configuration are changed."""
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import yaml
from database import provision, migrate_legacy, ca_volume
from vault_demo import bootstrap,configure_client,client_env
from gateway_overlay import config_overlay,MCP_HOST

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / '.cache'
ENV = {**os.environ, 'KUBECONFIG': str(CACHE / 'kubeconfig')}
NS = 'demo'
ORIGIN = 'https://catalog-api.demo.svc.cluster.local'

def run(args, *, data=None, capture=False):
    return subprocess.run(args, cwd=ROOT, env=ENV, input=data, text=True,
                          stdout=subprocess.PIPE if capture else None, check=True).stdout

def kubectl(*args, **kwargs):
    return run(['kubectl', '--context', 'auth-demo', *args], **kwargs)

def apply(objects):
    kubectl('apply', '-f', '-', data=json.dumps({'apiVersion':'v1','kind':'List','items':objects}))

def obj(kind, name, **extra):
    version = 'rbac.authorization.k8s.io/v1' if kind in ('Role','RoleBinding','ClusterRole','ClusterRoleBinding') else 'v1'
    metadata = {'name': name}
    if kind not in ('Namespace','ClusterRole','ClusterRoleBinding'): metadata['namespace'] = NS
    return {'apiVersion':version,'kind':kind,'metadata':metadata,**extra}

def certificates():
    directory = CACHE / 'tls'
    directory.mkdir(parents=True, exist_ok=True)
    if not (directory/'ca.crt').exists():
        run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','7','-subj','/CN=auth-demo-ca',
             '-addext','basicConstraints=critical,CA:TRUE','-addext','keyUsage=critical,keyCertSign,cRLSign',
             '-keyout',str(directory/'ca.key'),'-out',str(directory/'ca.crt')])
    for name, dns in {'control-plane':['auth-control-plane.demo.svc.cluster.local','auth-control-plane.demo.svc','localhost'],
                      'ext-proc':['auth-ext-proc.demo.svc.cluster.local','auth-ext-proc.demo.svc'],
                      'catalog-api':['catalog-api.demo.svc.cluster.local','catalog-alias.demo.svc.cluster.local','localhost'],
                      'agentgateway':[], 'pod-egress':['pod-egress.demo.svc.cluster.local','pod-egress.demo.svc','localhost'],
                      'auth-vault':['auth-vault.demo.svc.cluster.local','auth-vault.demo.svc','localhost']}.items():
        if (directory/f'{name}.crt').exists(): continue
        ext = 'extendedKeyUsage=serverAuth,clientAuth\n'
        if dns: ext += 'subjectAltName='+','.join('DNS:'+x for x in dns)+'\n'
        (directory/f'{name}.ext').write_text(ext)
        run(['openssl','req','-newkey','rsa:2048','-nodes','-subj',f'/CN={name}',
             '-keyout',str(directory/f'{name}.key'),'-out',str(directory/f'{name}.csr')])
        run(['openssl','x509','-req','-in',str(directory/f'{name}.csr'),'-CA',str(directory/'ca.crt'),
             '-CAkey',str(directory/'ca.key'),'-CAcreateserial','-days','7','-extfile',str(directory/f'{name}.ext'),
             '-out',str(directory/f'{name}.crt')])
    for path in directory.glob('*.key'): path.chmod(0o600)
    return directory

def tls_secret(name, identity, directory, namespace=NS):
    files = {'ca.crt':'ca.crt','tls.crt':identity+'.crt','tls.key':identity+'.key'}
    return {'apiVersion':'v1','kind':'Secret','metadata':{'name':name,'namespace':namespace},
            'data':{key:base64.b64encode((directory/value).read_bytes()).decode() for key,value in files.items()}}

def service(name, port, target, selector=None):
    return obj('Service',name,spec={'selector':selector or {'app':name},'ports':[{'port':port,'targetPort':target}]})

def deployment(name, binary, cert, *, port, env=None, account=None, database=False):
    image='localhost:5001/auth-demo/'+binary+':demo'
    image_id=json.loads(run(['docker','inspect',image],capture=True))[0]['Id']
    volumes=[{'name':'tls','secret':{'secretName':cert}}] if cert else []
    mounts=[{'name':'tls','mountPath':'/tls','readOnly':True}] if cert else []
    if database:
        volumes.append(ca_volume())
        mounts.append({'name':'postgres-ca','mountPath':'/run/postgres-ca','readOnly':True})
    container={'name':name,'image':image,'imagePullPolicy':'IfNotPresent','ports':[{'containerPort':port}],
               'volumeMounts':mounts,'env':env or [],'resources':{'requests':{'cpu':'50m','memory':'64Mi'},'limits':{'cpu':'1','memory':'512Mi'}},
               'readinessProbe':{'httpGet':{'path':'/readyz','port':port,'scheme':'HTTPS' if port==8443 else 'HTTP'},'initialDelaySeconds':2}}
    pod={'serviceAccountName':account or 'default','automountServiceAccountToken':bool(account),
         'securityContext':{'runAsUser':65532,'runAsGroup':65532,'fsGroup':65532},'volumes':volumes,'containers':[container]}
    return {'apiVersion':'apps/v1','kind':'Deployment','metadata':{'namespace':NS,'name':name},
            'spec':{'replicas':1,'strategy':{'type':'Recreate'},'selector':{'matchLabels':{'app':name}},
                    'template':{'metadata':{'labels':{'app':name},'annotations':{'auth.demo.dev/image-id':image_id}},'spec':pod}}}

def configure_gateway(directory):
    maps=json.loads(kubectl('-n','ate-system','get','configmap','-o','json',capture=True))['items']
    cm=next(x for x in maps if x['metadata']['name'].endswith('atenet-egress-agentgateway-config'))
    raw=config_overlay(yaml.safe_load(cm['data']['config.yaml']))
    patch={'data':{'config.yaml':raw}}
    kubectl('-n','ate-system','patch','configmap',cm['metadata']['name'],'--type=merge','-p',json.dumps(patch))
    patch={'spec':{'template':{'metadata':{'annotations':{'auth.demo.dev/config-hash':hashlib.sha256(raw.encode()).hexdigest()}},'spec':{
        'volumes':[{'name':'auth-extproc','secret':{'secretName':'auth-gateway-client'}}],
        'containers':[{'name':'agentgateway','volumeMounts':[{'name':'auth-extproc','mountPath':'/run/auth-extproc','readOnly':True}]}]}}}}
    kubectl('-n','ate-system','patch','deployment','atenet-egress','--type=strategic','-p',json.dumps(patch))
    kubectl('-n','ate-system','rollout','status','deployment/atenet-egress','--timeout=180s')

def main():
    if kubectl('config','current-context',capture=True).strip()!='auth-demo': raise SystemExit('auth-demo kubeconfig required')
    apply([obj('Namespace',NS)])
    provision()
    migrate_legacy()
    # Expose the existing loopback registry to Pods without changing its host port binding.
    node=json.loads(run(['docker','inspect','auth-demo'],capture=True))[0]
    network=next(iter(node['NetworkSettings']['Networks']))
    registry=json.loads(run(['docker','inspect','kind-registry'],capture=True))[0]
    if network not in registry['NetworkSettings']['Networks']:
        run(['docker','network','connect',network,'kind-registry'])
    registry=json.loads(run(['docker','inspect','kind-registry'],capture=True))[0]
    ip=registry['NetworkSettings']['Networks'][network]['IPAddress']
    registry_service=obj('Service','auth-demo-registry',spec={'ports':[{'port':5000,'targetPort':5000}]})
    apply([registry_service,obj('Endpoints','auth-demo-registry',subsets=[{'addresses':[{'ip':ip}],'ports':[{'port':5000}]}])])

    directory=certificates()
    bootstrap(directory)
    objects=[tls_secret('auth-control-plane-tls','control-plane',directory),tls_secret('auth-ext-proc-tls','ext-proc',directory),
             tls_secret('catalog-api-tls','catalog-api',directory),tls_secret('auth-gateway-client','agentgateway',directory,'ate-system')]
    for name,access in [('catalog-reader','read'),('catalog-writer','write')]:
        sa=obj('ServiceAccount',name,automountServiceAccountToken=False)
        sa['metadata']['labels']={'auth.demo.dev/managed':'true'}
        sa['metadata']['annotations']={'auth.demo.dev/policy-type':'catalog-access/v1','auth.demo.dev/policy-config':json.dumps({
            'agent':{'namespace':NS,'name':'mastra-http'},'origin':ORIGIN,'access':access,'credential':access+'-token'})}
        objects.append(sa)
    objects.extend([obj('ServiceAccount','auth-control-plane'),obj('ServiceAccount','test-user'),obj('ServiceAccount','unprivileged-user'),
        obj('Role','select-demo-accounts',rules=[{'apiGroups':[''],'resources':['serviceaccounts'],'resourceNames':['catalog-reader','catalog-writer','multi-reader'],'verbs':['use']}]),
        obj('RoleBinding','test-user-accounts',subjects=[{'kind':'ServiceAccount','name':'test-user','namespace':NS}],roleRef={'apiGroup':'rbac.authorization.k8s.io','kind':'Role','name':'select-demo-accounts'}),
        obj('Role','read-demo-policy',rules=[{'apiGroups':[''],'resources':['serviceaccounts','pods'],'verbs':['get','list','watch']}]),
        obj('RoleBinding','control-plane-policy',subjects=[{'kind':'ServiceAccount','name':'auth-control-plane','namespace':NS}],roleRef={'apiGroup':'rbac.authorization.k8s.io','kind':'Role','name':'read-demo-policy'}),
        obj('ClusterRole','auth-demo-reviewer',rules=[{'apiGroups':['authentication.k8s.io'],'resources':['tokenreviews'],'verbs':['create']},{'apiGroups':['authorization.k8s.io'],'resources':['subjectaccessreviews'],'verbs':['create']}]),
        obj('ClusterRoleBinding','auth-demo-reviewer',subjects=[{'kind':'ServiceAccount','name':'auth-control-plane','namespace':NS}],roleRef={'apiGroup':'rbac.authorization.k8s.io','kind':'ClusterRole','name':'auth-demo-reviewer'})])
    objects.extend([
        configure_client(deployment('auth-control-plane','control-plane','auth-control-plane-tls',port=8443,account='auth-control-plane',database=True,
                   env=[{'name':'DATABASE_URL','valueFrom':{'secretKeyRef':{'name':'auth-bridge-db','key':'DATABASE_URL'}}}]+client_env('auth-bridge'))),
        service('auth-control-plane',8443,8443),
        deployment('auth-ext-proc','ext-proc','auth-ext-proc-tls',port=8080,env=[{'name':'CONTROL_PLANE_URL','value':'https://auth-control-plane.demo.svc.cluster.local:8443'}]),
        service('auth-ext-proc',50051,50051),
        configure_client(deployment('catalog-api','test-api','catalog-api-tls',port=8443,account='auth-catalog-api',env=client_env('auth-catalog'))),
        service('catalog-api',443,8443),service('catalog-alias',443,8443,{'app':'catalog-api'})])
    apply(objects)
    for name in ('auth-control-plane','auth-ext-proc','catalog-api'):
        kubectl('-n',NS,'rollout','status','deployment/'+name,'--timeout=180s')
    kubectl('-n',NS,'delete','secret','read-token','write-token','--ignore-not-found=true')
    (CACHE/'demo-credentials.json').unlink(missing_ok=True)
    configure_gateway(directory)

    image=json.loads(run(['docker','inspect','localhost:5001/auth-demo/mastra:demo'],capture=True))[0]['RepoDigests']
    image=next(x for x in image if x.startswith('localhost:5001/'))
    objects=[{'apiVersion':'ate.dev/v1alpha1','kind':'WorkerPool','metadata':{'name':'auth-demo','namespace':NS,'labels':{'kagent.dev/worker-pool-name':'auth-demo','kagent.dev/worker-pool-namespace':NS}},'spec':{
        'replicas':1,'sandboxClasses':[{'name':'gvisor','configRef':{'name':'gvisor-default'}}],'workerImage':'ghcr.io/kagent-dev/substrate/ateworker-gvisor:v0.6.0-alpha3',
        'template':{'resources':{'requests':{'cpu':'100m','memory':'256Mi'},'limits':{'cpu':'4','memory':'3Gi'}}}}},
        {'apiVersion':'api.kagent.dev/v1alpha3','kind':'Harness','metadata':{'namespace':NS,'name':'mastra'},'spec':{
            'byo':{},'workload':{'image':image,'command':['/app/adapter']},
            'env':[{'name':'DEMO_API_ORIGIN','value':ORIGIN}],
            'substrate':{'workerPoolRef':{'name':'auth-demo'},'snapshotPolicy':{'location':'s3://ate-snapshots/auth-demo/'}}}},
        {'apiVersion':'api.kagent.dev/v1alpha3','kind':'EgressPolicy','metadata':{'namespace':NS,'name':'catalog-transport'},'spec':{'destinations':[{'origin':ORIGIN},{'origin':'http://'+MCP_HOST+':8080'}]}},
        {'apiVersion':'api.kagent.dev/v1alpha3','kind':'Agent','metadata':{'namespace':NS,'name':'mastra-http'},'spec':{
            'template':{'inline':{'description':'Mastra HTTP authorization demo'}},'harnessRef':{'name':'mastra'},'egressPolicyRefs':[{'name':'catalog-transport'}]}}]
    apply(objects)
    # An older revision can still be Ready immediately after a Harness update.
    # Wait until kagent has prepared this exact immutable image.
    for _ in range(150):
        status=json.loads(kubectl('-n',NS,'get','agent.api.kagent.dev/mastra-http','-o','json',capture=True)).get('status',{})
        ready=any(c['type']=='Ready' and c['status']=='True' for c in status.get('conditions',[]))
        if (ready and status.get('workloadImage')==image and status.get('desiredRevision')
                and status['desiredRevision']==status.get('latestSuccessfulRevision')):break
        time.sleep(2)
    else:raise SystemExit('Mastra did not prepare the requested image within 300 seconds: '+json.dumps(status))
    print('Demo deployed on local Minikube context auth-demo.')

if __name__=='__main__': main()
