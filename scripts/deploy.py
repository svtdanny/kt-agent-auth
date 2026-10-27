#!/usr/bin/env python3
"""Local dedicated-agent pilot on an already installed pinned kagent/Substrate cluster."""
import base64,json,os,secrets,subprocess,time
from pathlib import Path
from urllib.parse import quote
from local import ROOT,CACHE,ENV,kubectl,apply
from dedicated_gateway import bundle,obj
SCOPES=['auth-agent-a','auth-agent-b']
AGENT='mastra-http'
INTERNAL='http://internal-mcp.test-services.svc.cluster.local:8080'
EXTERNAL='https://dedicated-api.auth-demo.test:8443'

def run(args,**kw):
    return subprocess.run(args,cwd=ROOT,check=True,text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,**kw).stdout

def certificates(scope=None):
    d=CACHE/'dedicated-tls';
    if scope:d=d/scope
    d.mkdir(parents=True,exist_ok=True,mode=0o700)
    if not (d/'ca.crt').exists():
        run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','30','-subj','/CN=dedicated-pilot-ca',
             '-addext','basicConstraints=critical,CA:TRUE','-addext','keyUsage=critical,keyCertSign,cRLSign','-keyout',str(d/'ca.key'),'-out',str(d/'ca.crt')])
    for name,dns in {'authorizer':['authorizer','localhost',*[f'authorizer.{ns}.svc' for ns in SCOPES]],
                     'auth-egress':[],'ext-proc':[],'fixture':['dedicated-api.auth-demo.test','localhost']}.items():
        if (d/(name+'.crt')).exists():continue
        (d/(name+'.ext')).write_text('extendedKeyUsage=serverAuth,clientAuth\n'+('subjectAltName='+','.join('DNS:'+x for x in dns)+'\n' if dns else ''))
        run(['openssl','req','-newkey','rsa:2048','-nodes','-subj','/CN='+name,'-keyout',str(d/(name+'.key')),'-out',str(d/(name+'.csr'))])
        run(['openssl','x509','-req','-in',str(d/(name+'.csr')),'-CA',str(d/'ca.crt'),'-CAkey',str(d/'ca.key'),'-CAcreateserial',
             '-days','30','-extfile',str(d/(name+'.ext')),'-out',str(d/(name+'.crt'))])
    for p in d.glob('*.key'):p.chmod(0o600)
    return d

def tls_secret(ns,name,identity,d):
    return obj('Secret',name,ns,data={k:base64.b64encode((d/f).read_bytes()).decode() for k,f in
        {'ca.crt':'ca.crt','tls.crt':identity+'.crt','tls.key':identity+'.key'}.items()})

def keys():
    p=CACHE/'dedicated-keys.json'
    if not p.exists():
        p.write_text(json.dumps({'reader':secrets.token_urlsafe(32),'writer':secrets.token_urlsafe(32)}));p.chmod(0o600)
    return json.loads(p.read_text())

def sql(text,database='kagent'):
    return kubectl('-n','kagent','exec','-i','deployment/kagent-postgresql','-c','postgresql','--','psql','-Xq','-v','ON_ERROR_STOP=1','-U','postgres','-d',database,'-At',input=text)

def database(ns):
    # Separate login/database per authorizer, on the platform's existing PostgreSQL server.
    name=ns.replace('-','_');p=CACHE/(name+'-postgres.json')
    if not p.exists():p.write_text(json.dumps({'password':secrets.token_urlsafe(32)}));p.chmod(0o600)
    password=json.loads(p.read_text())['password'];escaped=password.replace("'","''")
    sql(f"DO $$ BEGIN IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='{name}') THEN CREATE ROLE {name} LOGIN; END IF; END $$; SET password_encryption='scram-sha-256'; ALTER ROLE {name} PASSWORD '{escaped}';")
    if sql(f"SELECT count(*) FROM pg_database WHERE datname='{name}';").strip()=='0':sql(f'CREATE DATABASE {name} OWNER {name};','postgres')
    sql(f'CREATE SCHEMA IF NOT EXISTS auth_bridge AUTHORIZATION {name}; REVOKE ALL ON SCHEMA auth_bridge FROM PUBLIC;',name)
    config=json.loads(kubectl('-n','kagent','get','cm','kagent-postgresql','-o','json'))
    rule=f'hostssl {name} {name} all scram-sha-256'
    hba=config['data']['pg_hba.conf']
    if rule not in hba:
        kubectl('-n','kagent','patch','cm','kagent-postgresql','--type=merge','-p',json.dumps({'data':{'pg_hba.conf':hba.rstrip()+'\n'+rule+'\n'}}))
        pods=json.loads(kubectl('-n','kagent','get','pods','-l','app.kubernetes.io/component=database','-o','json'))['items']
        for pod in pods:kubectl('-n','kagent','annotate','pod',pod['metadata']['name'],'auth.demo.dev/hba-refresh='+str(time.time_ns()),'--overwrite')
        for _ in range(90):
            if rule in kubectl('-n','kagent','exec','deploy/kagent-postgresql','-c','postgresql','--','cat','/etc/postgresql/pg_hba.conf'):break
            time.sleep(1)
        else:raise RuntimeError('PostgreSQL HBA projection did not update')
        sql('SELECT pg_reload_conf();')
    url=f'postgresql://{name}:{quote(password,safe="")}@kagent-postgresql.kagent.svc:5432/{name}?sslmode=verify-full&channel_binding=disable&sslrootcert=/run/postgres-ca/trust-bundle.pem'
    apply([obj('Secret','authorizer-db',ns,stringData={'DATABASE_URL':url})])

def policy(ns,write=False):
    return {'permissions':[
        {'id':'internal','type':'http-request/v1','config':{'origin':INTERNAL,'methods':['GET','POST'] if write else ['GET'],'paths':['/catalog','/mcp'],'credential':'api-token'}},
        {'id':'external','type':'http-request/v1','config':{'origin':EXTERNAL,'methods':['GET'],'paths':['/external'],'credential':'api-token'}}]}

def install_fixture(d,k):
    apply([obj('Namespace','test-services',None),obj('Secret','dedicated-fixture-keys','test-services',stringData={'keys.json':json.dumps(k)}),
       obj('Service','internal-mcp','test-services',spec={'selector':{'app':'dedicated-fixture'},'ports':[{'port':8080,'targetPort':8080}]}),
       obj('Deployment','dedicated-fixture','test-services',spec={'replicas':1,'strategy':{'type':'Recreate'},'selector':{'matchLabels':{'app':'dedicated-fixture'}},'template':{
          'metadata':{'labels':{'app':'dedicated-fixture'}},'spec':{'automountServiceAccountToken':False,'containers':[{
             'name':'api','image':'localhost:5001/auth-demo/test-api:dedicated','env':[{'name':'MOCK_KEYS_FILE','value':'/keys/keys.json'},{'name':'MOCK_PLAINTEXT','value':'true'}],
             'volumeMounts':[{'name':'keys','mountPath':'/keys','readOnly':True}]}],'volumes':[{'name':'keys','secret':{'secretName':'dedicated-fixture-keys'}}]}}})])
    target=CACHE/'dedicated-external';target.mkdir(exist_ok=True,mode=0o700)
    import shutil
    for dst,src in [('tls.crt','fixture.crt'),('tls.key','fixture.key')]:shutil.copyfile(d/src,target/dst)
    (target/'keys.json').write_text(json.dumps(k))
    for p in target.iterdir():p.chmod(0o600)
    node=json.loads(run(['docker','inspect','auth-demo']))[0];network=next(iter(node['NetworkSettings']['Networks']))
    subprocess.run(['docker','rm','-f','auth-dedicated-external'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    run(['docker','run','-d','--name','auth-dedicated-external','--network',network,'--user',f'{os.getuid()}:{os.getgid()}',
        '-p','127.0.0.1:18458:8443','--mount','type=bind,src='+str(target)+',dst=/tls,readonly','-e','MOCK_KEYS_FILE=/tls/keys.json','localhost:5001/auth-demo/test-api:dedicated'])
    ip=json.loads(run(['docker','inspect','auth-dedicated-external']))[0]['NetworkSettings']['Networks'][network]['IPAddress']
    cm=json.loads(kubectl('-n','kube-system','get','cm','coredns','-o','json'));core=cm['data']['Corefile']
    import re
    core=re.sub(r'\n\s*# dedicated fixture BEGIN.*?# dedicated fixture END','',core,flags=re.S)
    record=f'\n       # dedicated fixture BEGIN\n       {ip} dedicated-api.auth-demo.test\n       # dedicated fixture END'
    if 'hosts {' in core:core=core.replace('hosts {','hosts {'+record,1)
    else:core=core.replace('{','{\n    hosts {'+record+'\n       fallthrough\n    }',1)
    kubectl('-n','kube-system','patch','cm','coredns','--type=merge','-p',json.dumps({'data':{'Corefile':core}}))
    kubectl('-n','kube-system','rollout','restart','deploy/coredns');kubectl('-n','kube-system','rollout','status','deploy/coredns','--timeout=120s')

def install_routing():
    assignments={ns:f'agent-egress.{ns}.svc:443' for ns in SCOPES}
    apply([obj('ConfigMap','agent-egress-gateways','ate-system',data={'gateways.json':json.dumps(assignments)})])
    digest=next(x for x in json.loads(run(['docker','inspect','localhost:5001/auth-demo/substrate-api:dedicated']))[0]['RepoDigests'] if x.startswith('localhost:5001/'))
    patch={'spec':{'template':{'metadata':{'annotations':{'auth.demo.dev/routing':json.dumps(assignments,sort_keys=True)}},'spec':{'volumes':[{'name':'agent-egress-gateways','configMap':{'name':'agent-egress-gateways'}}],
      'containers':[{'name':'ate-api-server','image':digest,'securityContext':{'runAsUser':0},
        'env':[{'name':'AGENT_EGRESS_GATEWAYS_FILE','value':'/etc/agent-egress/gateways.json'}],
        'volumeMounts':[{'name':'agent-egress-gateways','mountPath':'/etc/agent-egress','readOnly':True}]}]}}}}
    kubectl('-n','ate-system','patch','deploy','ate-api-server','--type=strategic','-p',json.dumps(patch))
    kubectl('-n','ate-system','rollout','status','deploy/ate-api-server','--timeout=240s')

def main():
    if not Path(ENV['KUBECONFIG']).exists():raise SystemExit('Install the pinned platform first; a local kubeconfig is required')
    # The sandbox image cache accesses the existing loopback registry through a Service.
    apply([obj('Namespace','demo',None)])
    node=json.loads(run(['docker','inspect','auth-demo']))[0]
    network=next(iter(node['NetworkSettings']['Networks']))
    registry=json.loads(run(['docker','inspect','kind-registry']))[0]
    if network not in registry['NetworkSettings']['Networks']:
        run(['docker','network','connect',network,'kind-registry'])
    ip=json.loads(run(['docker','inspect','kind-registry']))[0]['NetworkSettings']['Networks'][network]['IPAddress']
    apply([obj('Service','auth-demo-registry','demo',spec={'ports':[{'port':5000,'targetPort':5000}]}),
        obj('Endpoints','auth-demo-registry','demo',subsets=[{'addresses':[{'ip':ip}],'ports':[{'port':5000}]}])])
    # Minikube imports tags without RepoDigests. Let the node resolve immutable
    # references through the local-only registry mirror instead of node loopback.
    run(['docker','exec','auth-demo','mkdir','-p','/etc/containerd/certs.d/localhost:5001'])
    mirror=f'server = "http://{ip}:5000"\n[host."http://{ip}:5000"]\n  capabilities = ["pull", "resolve"]\n'
    run(['docker','exec','-i','auth-demo','tee','/etc/containerd/certs.d/localhost:5001/hosts.toml'],input=mirror)
    minikube=CACHE/'bin/minikube'
    subprocess.run([str(minikube) if minikube.exists() else 'minikube','-p','auth-demo','image','load','--daemon',
        *['localhost:5001/auth-demo/'+name+':dedicated' for name in ['authorizer','test-api','substrate-api','substrate-atenet']]],check=True)
    d=certificates();k=keys()
    apply([obj('Namespace',ns,None) for ns in SCOPES])
    for ns in SCOPES:
        database(ns)
        copies=[]
        for name in ['actor-id-ca-certs','egress-mitm-ca-pool']:
            source=json.loads(kubectl('-n','ate-system','get','secret',name,'-o','json'));copies.append(obj('Secret',name,ns,data=source['data']))
        copies.extend([tls_secret(ns,'authorizer-tls','authorizer',certificates(ns)),tls_secret(ns,'auth-client','auth-egress',certificates(ns)),
           obj('Secret','application-credentials',ns,stringData={'credentials.json':json.dumps({'api-token':{'type':'bearer','token':k['reader' if ns==SCOPES[0] else 'writer']}})})])
        apply(copies)
        resources=bundle(ns,AGENT,'localhost:5001/auth-demo/authorizer:dedicated',(d/'ca.crt').read_text())
        resources += [obj('ConfigMap','agent-permissions',ns,data={'type':'permission-set/v1','config':json.dumps(policy(ns,ns==SCOPES[1]))}),
          obj('ServiceAccount','agent-caller',ns,automountServiceAccountToken=False),obj('ServiceAccount','test-user',ns),
          obj('Role','select-agent-account',ns,rules=[{'apiGroups':[''],'resources':['serviceaccounts'],'resourceNames':['agent-caller'],'verbs':['use']}]),
          obj('RoleBinding','select-agent-account',ns,subjects=[{'kind':'ServiceAccount','name':'test-user','namespace':ns}],roleRef={'apiGroup':'rbac.authorization.k8s.io','kind':'Role','name':'select-agent-account'})]
        for x in resources:
            if x['kind']=='ConfigMap' and x['metadata']['name']=='agent-permissions':x['metadata']['labels']={'auth.demo.dev/managed':'true'}
            if x['kind']=='ServiceAccount' and x['metadata']['name']=='agent-caller':
                x['metadata'].update(labels={'auth.demo.dev/managed':'true'},annotations={'auth.demo.dev/policy-ref':'agent-permissions'})
        # Immutable image digests and config hashes make repeated deploys deterministic.
        import hashlib
        for resource in resources:
            if resource['kind']=='Deployment':
                spec=resource['spec']['template']['spec']
                for container in spec['containers']+spec.get('initContainers',[]):
                    if container['image'].startswith('localhost:5001/'):
                        container['image']=next(x for x in json.loads(run(['docker','inspect',container['image']]))[0]['RepoDigests'] if x.startswith('localhost:5001/'))
                resource['spec']['template']['metadata']['annotations']={'auth.demo.dev/config-hash':hashlib.sha256(json.dumps([x for x in resources if x['kind']=='ConfigMap'],sort_keys=True).encode()).hexdigest(), 'auth.demo.dev/tls-hash':hashlib.sha256((certificates(ns)/'authorizer.crt').read_bytes()+(certificates(ns)/'auth-egress.crt').read_bytes()).hexdigest()}
        apply(resources)
    install_routing();install_fixture(d,k)
    # Reuse the already built, immutable Mastra BYO image, or the image built by build.py.
    image=os.environ.get('MASTRA_IMAGE')
    if not image:
        digests=json.loads(run(['docker','inspect','localhost:5001/auth-demo/mastra:dedicated']))[0]['RepoDigests']
        image=next(x for x in digests if x.startswith('localhost:5001/'))
    for ns in SCOPES:
        resources=[{'apiVersion':'ate.dev/v1alpha1','kind':'WorkerPool','metadata':{'name':'agent-workers','namespace':ns,'labels':{'kagent.dev/worker-pool-name':'agent-workers','kagent.dev/worker-pool-namespace':ns}},'spec':{
          'replicas':1,'sandboxClasses':[{'name':'gvisor','configRef':{'name':'gvisor-default'}}],'workerImage':'ghcr.io/kagent-dev/substrate/ateworker-gvisor:v0.6.0-alpha3',
          'template':{'resources':{'requests':{'cpu':'100m','memory':'256Mi'},'limits':{'cpu':'2','memory':'2Gi'}}}}},
          {'apiVersion':'api.kagent.dev/v1alpha3','kind':'Harness','metadata':{'namespace':ns,'name':'mastra'},'spec':{'byo':{},'workload':{'image':image,'command':['/app/adapter']},
              'env':[{'name':'DEMO_API_ORIGIN','value':INTERNAL}],'substrate':{'workerPoolRef':{'name':'agent-workers'},'snapshotPolicy':{'location':'s3://ate-snapshots/'+ns+'/'}}}},
          {'apiVersion':'api.kagent.dev/v1alpha3','kind':'EgressPolicy','metadata':{'namespace':ns,'name':'transport'},'spec':{'destinations':[{'origin':INTERNAL},{'origin':EXTERNAL}]}},
          {'apiVersion':'api.kagent.dev/v1alpha3','kind':'Agent','metadata':{'namespace':ns,'name':AGENT},'spec':{'template':{'inline':{'description':'Dedicated authorization pilot'}},'harnessRef':{'name':'mastra'},'egressPolicyRefs':[{'name':'transport'}]}}]
        apply(resources)
        kubectl('-n',ns,'rollout','status','deploy/agent-egress','--timeout=240s')
        for _ in range(150):
            status=json.loads(kubectl('-n',ns,'get','agent',AGENT,'-o','json')).get('status',{})
            if any(c['type']=='Ready' and c['status']=='True' for c in status.get('conditions',[])) and status.get('workloadImage')==image and status.get('desiredRevision')==status.get('latestSuccessfulRevision'):break
            time.sleep(2)
        else:raise RuntimeError('Agent preparation did not finish: '+ns)
    print('Dedicated agent deployments ready: '+', '.join(SCOPES))
if __name__=='__main__':main()
