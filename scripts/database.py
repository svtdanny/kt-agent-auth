"""Provision only our schema/role on the existing local PostgreSQL server."""
import json
import os
from pathlib import Path
import secrets
import subprocess
import time
from urllib.parse import quote
import yaml

ROOT=Path(__file__).resolve().parents[1]
ENV={**os.environ,'KUBECONFIG':str(ROOT/'.cache/kubeconfig')}

def kubectl(*args,input=None):
    return subprocess.check_output(['kubectl','--context','auth-demo',*args],env=ENV,input=input,text=True)

def sql(statement,database='kagent'):
    return kubectl('-n','kagent','exec','-i','deployment/kagent-postgresql','-c','postgresql','--',
                   'psql','-Xq','-v','ON_ERROR_STOP=1','-U','postgres','-d',database,'-At',input=statement)

def apply(items):
    kubectl('apply','-f','-',input=json.dumps({'apiVersion':'v1','kind':'List','items':items}))

def ca_volume():
    return {'name':'postgres-ca','projected':{'sources':[{'clusterTrustBundle':{
        'signerName':'servicedns.podcert.ate.dev/identity','labelSelector':{'matchLabels':{'podcert.ate.dev/canarying':'live'}},'path':'trust-bundle.pem'}}]}}

def pool_budget():
    # Keep the existing 100-connection server. Explicit client budgets avoid
    # defaults derived from the host CPU count exhausting shared connections.
    cm=json.loads(kubectl('-n','kagent','get','configmap','kagent-controller','-o','json'))
    config=yaml.safe_load(cm['data']['controller.yaml'])
    pool=config.setdefault('database',{}).setdefault('pool',{})
    if pool.get('maxConns')!=8:
        pool['maxConns']=8
        kubectl('-n','kagent','patch','configmap','kagent-controller','--type=merge','-p',json.dumps({'data':{'controller.yaml':yaml.safe_dump(config,sort_keys=False)}}))
        kubectl('-n','kagent','rollout','restart','deployment/kagent-controller')
        kubectl('-n','kagent','rollout','status','deployment/kagent-controller','--timeout=180s')
    deployment=json.loads(kubectl('-n','ate-system','get','deployment','ate-api-server','-o','json'))
    container=deployment['spec']['template']['spec']['containers'][0]
    args=container.get('args',[])
    if '--postgres-pool-max-conns=8' not in args:
        args=[a for a in args if not a.startswith('--postgres-pool-max-conns=')]+['--postgres-pool-max-conns=8']
        kubectl('-n','ate-system','patch','deployment','ate-api-server','--type=strategic','-p',json.dumps({'spec':{'template':{'spec':{'containers':[{'name':container['name'],'args':args}]}}}}))
        kubectl('-n','ate-system','rollout','status','deployment/ate-api-server','--timeout=180s')

def provision():
    pool_budget()
    path=ROOT/'.cache/bridge-postgres.json'
    if not path.exists():path.write_text(json.dumps({'password':secrets.token_urlsafe(32)}));path.chmod(0o600)
    password=json.loads(path.read_text())['password']
    escaped=password.replace("'","''")
    sql("DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='auth_bridge_user') THEN CREATE ROLE auth_bridge_user LOGIN; END IF; END $$;\n"
        "SET password_encryption='scram-sha-256';\nALTER ROLE auth_bridge_user PASSWORD '"+escaped+"';\n"
        "CREATE SCHEMA IF NOT EXISTS auth_bridge AUTHORIZATION auth_bridge_user;\n"
        "REVOKE ALL ON SCHEMA auth_bridge FROM PUBLIC;\nGRANT CONNECT ON DATABASE kagent TO auth_bridge_user;\n")
    cm=json.loads(kubectl('-n','kagent','get','configmap','kagent-postgresql','-o','json'))
    rule='hostssl kagent,auth_bridge_test auth_bridge_user all scram-sha-256'
    hba=cm['data']['pg_hba.conf']
    if rule not in hba:
        hba=hba.rstrip()+'\n'+rule+'\n'
        kubectl('-n','kagent','patch','configmap','kagent-postgresql','--type=merge','-p',json.dumps({'data':{'pg_hba.conf':hba}}))
        pods=json.loads(kubectl('-n','kagent','get','pods','-l','app.kubernetes.io/component=database','-o','json'))['items']
        for pod in pods:kubectl('-n','kagent','annotate','pod',pod['metadata']['name'],'auth.demo.dev/hba-refresh='+str(time.time_ns()),'--overwrite')
    for _ in range(90):
        live=kubectl('-n','kagent','exec','deployment/kagent-postgresql','-c','postgresql','--','cat','/etc/postgresql/pg_hba.conf')
        if rule in live:break
        time.sleep(1)
    else:raise RuntimeError('PostgreSQL did not receive the bridge authentication rule')
    sql('SELECT pg_reload_conf();')
    # The bundled service certificate can use Ed25519, for which pgx does not
    # define tls-server-end-point hashing. Use SCRAM over verified TLS without
    # SCRAM-PLUS; certificate chain and hostname verification remain enabled.
    url='postgresql://auth_bridge_user:'+quote(password,safe='')+'@kagent-postgresql.kagent.svc:5432/kagent?sslmode=verify-full&channel_binding=disable&sslrootcert=/run/postgres-ca/trust-bundle.pem'
    apply([{'apiVersion':'v1','kind':'Secret','metadata':{'name':'auth-bridge-db','namespace':'demo'},'stringData':{'DATABASE_URL':url}}])
    return password

def migrate_legacy():
    found=subprocess.run(['kubectl','--context','auth-demo','-n','demo','get','pvc','auth-bindings'],env=ENV,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    if found.returncode:return
    exists=sql("SELECT to_regclass('auth_bridge.metadata') IS NOT NULL;").strip()=='t'
    if exists and sql("SELECT EXISTS(SELECT 1 FROM auth_bridge.metadata WHERE key='legacy_import');").strip()=='t':return
    kubectl('-n','demo','scale','deployment/auth-control-plane','--replicas=0')
    kubectl('-n','demo','wait','--for=delete','pod','-l','app=auth-control-plane','--timeout=90s')
    kubectl('-n','demo','delete','job','auth-import-bbolt','--ignore-not-found=true')
    job={'apiVersion':'batch/v1','kind':'Job','metadata':{'name':'auth-import-bbolt','namespace':'demo'},'spec':{'backoffLimit':0,'activeDeadlineSeconds':120,'template':{'spec':{
        'restartPolicy':'Never','automountServiceAccountToken':False,'securityContext':{'runAsUser':65532,'runAsGroup':65532,'fsGroup':65532},
        'containers':[{'name':'import','image':'localhost:5001/auth-demo/import-bbolt:demo','imagePullPolicy':'IfNotPresent',
            'env':[{'name':'DATABASE_URL','valueFrom':{'secretKeyRef':{'name':'auth-bridge-db','key':'DATABASE_URL'}}}],
            'volumeMounts':[{'name':'legacy','mountPath':'/legacy','readOnly':True},{'name':'postgres-ca','mountPath':'/run/postgres-ca','readOnly':True}]}],
        'volumes':[{'name':'legacy','persistentVolumeClaim':{'claimName':'auth-bindings','readOnly':True}},ca_volume()]}}}}
    apply([job])
    kubectl('-n','demo','wait','--for=condition=complete','job/auth-import-bbolt','--timeout=120s')
    print(kubectl('-n','demo','logs','job/auth-import-bbolt').strip(),flush=True)
    kubectl('-n','demo','delete','job','auth-import-bbolt','--wait=true')

if __name__=='__main__':provision()
