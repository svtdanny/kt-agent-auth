"""Local Vault setup and test administration; never connects to production Vault."""
import http.client
import json
import os
from pathlib import Path
import secrets
import ssl
import subprocess
import time

from database import ROOT,ENV,kubectl,apply

IMAGE='hashicorp/vault:1.21.4'
PREFIX='auth-demo/demo/'


class Admin:
    def __enter__(self):
        log=open(ROOT/'.cache/vault-portforward.log','w')
        self.process=subprocess.Popen(['kubectl','--context','auth-demo','-n','demo','port-forward',
                                      'service/auth-vault','18200:8200'],env=ENV,stdout=log,stderr=log)
        log.close()
        self.context=ssl.create_default_context(cafile=str(ROOT/'.cache/tls/ca.crt'))
        self.token=None
        path=ROOT/'.cache/vault-admin.json'
        if path.exists():self.token=json.loads(path.read_text())['root_token']
        for _ in range(60):
            if self.process.poll() is not None:raise RuntimeError('local Vault port-forward stopped')
            try:
                self.request('GET','sys/health')
                return self
            except OSError:time.sleep(1)
        self.__exit__(None,None,None)
        raise RuntimeError('local Vault did not become reachable')

    def __exit__(self,*_):
        self.process.terminate()
        try:self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:self.process.kill();self.process.wait()

    def request(self,method,path,body=None,token=None):
        headers={'Content-Type':'application/json'}
        selected=self.token if token is None else token
        if selected:headers['X-Vault-Token']=selected
        connection=http.client.HTTPSConnection('localhost',18200,timeout=10,context=self.context)
        try:
            connection.request(method,'/v1/'+path,body=None if body is None else json.dumps(body),headers=headers)
            response=connection.getresponse()
            raw=response.read()
            return response.status,json.loads(raw) if raw else {}
        finally:connection.close()

    def checked(self,method,path,body=None):
        status,data=self.request(method,path,body)
        if status>=300:raise RuntimeError('local Vault operation failed (HTTP '+str(status)+')')
        return data

    def read(self,name):
        return self.checked('GET','kv/data/'+PREFIX+name)['data']['data']['token']

    def write(self,name,value):
        self.checked('POST','kv/data/'+PREFIX+name,{'data':{'token':value}})


def client_env(role):
    return [{'name':'VAULT_ADDR','value':'https://auth-vault.demo.svc.cluster.local:8200'},
            {'name':'VAULT_CACERT','value':'/run/vault-ca/ca.crt'},
            {'name':'VAULT_ROLE','value':role},
            {'name':'VAULT_KV_MOUNT','value':'kv'},
            {'name':'VAULT_KV_PREFIX','value':'auth-demo'},
            {'name':'VAULT_JWT_PATH','value':'/var/run/vault-identity/token'}]


def configure_client(deployment):
    spec=deployment['spec']['template']['spec']
    spec['volumes'] += [
        {'name':'vault-ca','secret':{'secretName':'auth-control-plane-tls','items':[{'key':'ca.crt','path':'ca.crt'}]}},
        {'name':'vault-identity','projected':{'sources':[{'serviceAccountToken':{
            'audience':'vault','expirationSeconds':3600,'path':'token'}}]}}]
    spec['containers'][0]['volumeMounts'] += [
        {'name':'vault-ca','mountPath':'/run/vault-ca','readOnly':True},
        {'name':'vault-identity','mountPath':'/var/run/vault-identity','readOnly':True}]
    return deployment


def bootstrap(directory):
    # Imported lazily to avoid a deployment/setup import cycle.
    from deploy import obj,tls_secret,service
    config={'disable_mlock':True,'ui':False,'api_addr':'https://auth-vault.demo.svc.cluster.local:8200',
            'storage':{'file':{'path':'/vault/data'}},
            'listener':{'tcp':{'address':'0.0.0.0:8200','tls_cert_file':'/vault/tls/tls.crt','tls_key_file':'/vault/tls/tls.key'}}}
    objects=[obj('ServiceAccount','auth-vault'),obj('ServiceAccount','auth-catalog-api'),
             obj('ClusterRole','auth-demo-vault-reviewer',rules=[{'apiGroups':['authentication.k8s.io'],'resources':['tokenreviews'],'verbs':['create']}]),
             obj('ClusterRoleBinding','auth-demo-vault-reviewer',subjects=[{'kind':'ServiceAccount','name':'auth-vault','namespace':'demo'}],roleRef={'apiGroup':'rbac.authorization.k8s.io','kind':'ClusterRole','name':'auth-demo-vault-reviewer'}),
             tls_secret('auth-vault-tls','auth-vault',directory),
             obj('ConfigMap','auth-vault-config',data={'vault.json':json.dumps(config)}),
             obj('PersistentVolumeClaim','auth-vault-data',spec={'accessModes':['ReadWriteOnce'],'resources':{'requests':{'storage':'1Gi'}}}),
             service('auth-vault',8200,8200),
             {'apiVersion':'apps/v1','kind':'Deployment','metadata':{'name':'auth-vault','namespace':'demo'},'spec':{
                 'replicas':1,'strategy':{'type':'Recreate'},'selector':{'matchLabels':{'app':'auth-vault'}},'template':{
                     'metadata':{'labels':{'app':'auth-vault'}},'spec':{'serviceAccountName':'auth-vault',
                         'securityContext':{'runAsUser':100,'runAsGroup':1000,'fsGroup':1000},
                         'containers':[{'name':'vault','image':IMAGE,'imagePullPolicy':'IfNotPresent','command':['vault'],
                             'args':['server','-config=/vault/config/vault.json'],
                             'resources':{'requests':{'cpu':'50m','memory':'128Mi'},'limits':{'cpu':'1','memory':'512Mi'}},
                             # Bootstrap must reach an initialized or uninitialized sealed server.
                             'readinessProbe':{'httpGet':{'path':'/v1/sys/health?sealedcode=200&uninitcode=200','port':8200,'scheme':'HTTPS'}},
                             'volumeMounts':[{'name':'tls','mountPath':'/vault/tls','readOnly':True},
                                             {'name':'config','mountPath':'/vault/config','readOnly':True},
                                             {'name':'data','mountPath':'/vault/data'}]}],
                         'volumes':[{'name':'tls','secret':{'secretName':'auth-vault-tls'}},
                                    {'name':'config','configMap':{'name':'auth-vault-config'}},
                                    {'name':'data','persistentVolumeClaim':{'claimName':'auth-vault-data'}}]}}}}]
    apply(objects)
    kubectl('-n','demo','rollout','status','deployment/auth-vault','--timeout=180s')
    with Admin() as admin:
        state=admin.checked('GET','sys/seal-status')
        path=ROOT/'.cache/vault-admin.json'
        if not state['initialized']:
            initialized=admin.checked('POST','sys/init',{'secret_shares':1,'secret_threshold':1})
            fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
            with os.fdopen(fd,'w') as handle:json.dump(initialized,handle)
            admin.token=initialized['root_token']
        if state['sealed']:
            if not path.exists():raise RuntimeError('local Vault unseal material is missing from .cache/vault-admin.json')
            material=json.loads(path.read_text())
            admin.checked('POST','sys/unseal',{'key':material['keys_base64'][0]})
        mounts=admin.checked('GET','sys/mounts')['data']
        if 'kv/' not in mounts:admin.checked('POST','sys/mounts/kv',{'type':'kv','options':{'version':'2'}})
        auth=admin.checked('GET','sys/auth')['data']
        if 'kubernetes/' not in auth:admin.checked('POST','sys/auth/kubernetes',{'type':'kubernetes'})
        admin.checked('POST','auth/kubernetes/config',{'kubernetes_host':'https://kubernetes.default.svc:443'})
        for role,account,names in [('auth-bridge','auth-control-plane',['read-token','write-token','mcp-token']),
                                   ('auth-catalog','auth-catalog-api',['mock/reader','mock/writer']),
                                   ('auth-mcp','mock-mcp',['mock/mcp-reader','mock/mcp-writer'])]:
            policy='\n'.join('path "kv/data/'+PREFIX+name+'" { capabilities = ["read"] }' for name in names)
            admin.checked('PUT','sys/policies/acl/'+role,{'policy':policy})
            admin.checked('POST','auth/kubernetes/role/'+role,{'bound_service_account_names':[account],
                'bound_service_account_namespaces':['test-services' if role=='auth-mcp' else 'demo'],'audience':'vault','token_policies':[role],
                'token_ttl':900,'token_max_ttl':3600})
        legacy=ROOT/'.cache/demo-credentials.json'
        old=json.loads(legacy.read_text()) if legacy.exists() else {'reader':secrets.token_urlsafe(24),'writer':secrets.token_urlsafe(24)}
        old.update({'mcp-reader':secrets.token_urlsafe(24),'mcp-writer':secrets.token_urlsafe(24)})
        for name,key in [('read-token','reader'),('write-token','writer'),('mock/reader','reader'),('mock/writer','writer'),
                         ('mcp-token','mcp-reader'),('mock/mcp-reader','mcp-reader'),('mock/mcp-writer','mcp-writer')]:
            status,_=admin.request('GET','kv/data/'+PREFIX+name)
            if status==404:admin.write(name,old[key])
            elif status!=200:raise RuntimeError('cannot seed local Vault credential')
    print('Local Vault initialized: TLS, persistent storage, Kubernetes auth, and separate read-only application roles.',flush=True)
