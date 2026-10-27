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

def provision():
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

if __name__=='__main__':provision()
