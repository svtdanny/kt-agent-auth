#!/usr/bin/env python3
"""Run SQL race tests in-cluster, using an isolated database and verified TLS."""
import base64
import json
import os
import shutil
import subprocess
import time
from database import ROOT,ENV,sql,provision,kubectl,apply,ca_volume

def main():
    os.chdir(ROOT);provision()
    sql('DROP DATABASE IF EXISTS auth_bridge_test WITH (FORCE);','postgres')
    sql('CREATE DATABASE auth_bridge_test OWNER auth_bridge_user;','postgres')
    sql('CREATE SCHEMA auth_bridge AUTHORIZATION auth_bridge_user;','auth_bridge_test')
    for package,binary in [('store','store-tests'),('legacy','legacy-tests')]:
        subprocess.run(['go','test','-c','-race','-o','.cache/build/'+binary,'./internal/'+package],env={**ENV,'CGO_ENABLED':'1'},check=True)
    subprocess.run(['docker','build','-f','deploy/Dockerfile.sql-tests','-t','localhost:5001/auth-demo/sql-tests:demo','.'],check=True)
    minikube=shutil.which('minikube') or str(ROOT/'.cache/bin/minikube')
    subprocess.run([minikube,'-p','auth-demo','image','load','--daemon','localhost:5001/auth-demo/sql-tests:demo'],check=True)
    db=json.loads(kubectl('-n','demo','get','secret','auth-bridge-db','-o','json'))
    url=base64.b64decode(db['data']['DATABASE_URL']).decode().replace('/kagent?','/auth_bridge_test?')
    secret={'apiVersion':'v1','kind':'Secret','metadata':{'name':'auth-sql-tests','namespace':'demo'},'stringData':{'URL':url}}
    pod={'apiVersion':'v1','kind':'Pod','metadata':{'name':'auth-sql-tests','namespace':'demo'},'spec':{
        'restartPolicy':'Never','activeDeadlineSeconds':120,'automountServiceAccountToken':False,
        'containers':[{'name':'tests','image':'localhost:5001/auth-demo/sql-tests:demo','imagePullPolicy':'Never',
            'env':[{'name':'AUTH_DEMO_TEST_DATABASE_URL','valueFrom':{'secretKeyRef':{'name':'auth-sql-tests','key':'URL'}}}],
            'volumeMounts':[{'name':'postgres-ca','mountPath':'/run/postgres-ca','readOnly':True}]}],
        'volumes':[ca_volume()]}}
    try:
        kubectl('-n','demo','delete','pod','auth-sql-tests','--ignore-not-found=true','--wait=true')
        apply([secret,pod])
        for _ in range(90):
            state=json.loads(kubectl('-n','demo','get','pod','auth-sql-tests','-o','json'))['status']['phase']
            if state in ('Succeeded','Failed'):break
            time.sleep(1)
        logs=kubectl('-n','demo','logs','auth-sql-tests')
        print(logs,flush=True)
        if state!='Succeeded':raise AssertionError('SQL integration tests failed')
        (ROOT/'artifacts').mkdir(exist_ok=True)
        (ROOT/'artifacts/postgres-tests.json').write_text(json.dumps({'database':'isolated auth_bridge_test on existing PostgreSQL server','tls':'verify-full','raceDetector':True,'podPhase':state,'log':logs},indent=2)+'\n')
    finally:
        kubectl('-n','demo','delete','pod','auth-sql-tests','--ignore-not-found=true','--wait=true')
        kubectl('-n','demo','delete','secret','auth-sql-tests','--ignore-not-found=true')
        sql('DROP DATABASE IF EXISTS auth_bridge_test WITH (FORCE);','postgres')

if __name__=='__main__':main()
