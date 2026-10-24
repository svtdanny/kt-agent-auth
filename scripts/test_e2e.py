#!/usr/bin/env python3
"""Exercise real Mastra sessions through the local Minikube egress gateway."""
import http.client
import json
import os
from pathlib import Path
import ssl
import secrets
import subprocess
import time
import uuid
from urllib.parse import urlencode
from vault_demo import Admin

ROOT=Path(__file__).resolve().parents[1]
CACHE=ROOT/'.cache'
ENV={**os.environ,'KUBECONFIG':str(CACHE/'kubeconfig')}
RESULTS=[]
CONNECTIONS={}

def kubectl(*args):
    return subprocess.check_output(['kubectl','--context','auth-demo',*args],env=ENV,text=True)

def record(name, **evidence):
    RESULTS.append({'test':name,'passed':True,**evidence})
    print('PASS:',name,flush=True)

def request(port,path,body=None,token=None,method=None,internal_client=False):
    headers={'Content-Type':'application/json'}
    if token: headers['Authorization']='Bearer '+token
    data=None if body is None else json.dumps(body).encode()
    key=(port,internal_client)
    if key not in CONNECTIONS:
        context=ssl.create_default_context(cafile=str(CACHE/'tls/ca.crt'))
        if internal_client:context.load_cert_chain(str(CACHE/'tls/ext-proc.crt'),str(CACHE/'tls/ext-proc.key'))
        CONNECTIONS[key]=http.client.HTTPSConnection('localhost',port,timeout=180,context=context)
    connection=CONNECTIONS[key]
    try:
        connection.request(method or ('POST' if body is not None else 'GET'),path,body=data,headers=headers)
        response=connection.getresponse()
        return response.status,json.loads(response.read())
    except Exception:
        CONNECTIONS.pop(key).close()
        raise

def close_connections():
    for connection in CONNECTIONS.values():connection.close()
    CONNECTIONS.clear()

def calls(value):
    if isinstance(value,dict):
        if 'calls' in value:return value['calls']
        for key,child in value.items():
            if key=='text' and isinstance(child,str):
                try:
                    result=calls(json.loads(child))
                    if result is not None:return result
                except json.JSONDecodeError:pass
            result=calls(child)
            if result is not None:return result
    if isinstance(value,list):
        for child in value:
            result=calls(child)
            if result is not None:return result
    return None

def invoke(token,sid,method,path,headers=None):
    status,result=request(18443,f'/v1/sessions/{sid}/invoke',{'method':method,'path':path,'headers':headers or {}},token)
    assert status==200,(status,result)
    result_calls=calls(result)
    assert result_calls and len(result_calls)==1,('Mastra did not execute exactly one HTTP tool call',result)
    return result_calls[0]

def audit():
    status,result=request(18444,'/audit');assert status==200;return result or []

def wait_until(check):
    for _ in range(100):
        if check():return
        time.sleep(.1)
    raise AssertionError('asynchronous state did not converge')

def context_for(sid):
    query=urlencode({'spiffeId':'spiffe://substrate-actor.local/worker-for-actor/demo/session-'+sid})
    return request(18443,'/internal/context?'+query,internal_client=True)

def policy_type(sid):
    status,value=context_for(sid)
    return value.get('policy',{}).get('type') if status==200 else None

def credential_is(sid,wanted):
    status,context=context_for(sid)
    if status!=200:return False
    grant={'spiffeId':'spiffe://substrate-actor.local/worker-for-actor/demo/session-'+sid,
           'policyVersion':context['policy']['version'],'name':'read-token',
           'request':{'scheme':'https','host':'catalog-api.demo.svc.cluster.local','port':443,'method':'GET','path':'/catalog','headers':{}}}
    status,response=request(18443,'/internal/credential',grant,internal_client=True)
    return status==200 and response.get('authorization')=='Bearer '+wanted

def invalidate_credential(name):
    status,_=request(18443,'/internal/credentials/invalidate',{'namespace':'demo','name':name},internal_client=True)
    assert status==200,'Vault credential invalidation failed'

def delete_native(sid):
    name='auth-test-native-delete'
    kubectl('-n','demo','delete','pod',name,'--ignore-not-found=true','--wait=true')
    pod={'apiVersion':'v1','kind':'Pod','metadata':{'name':name,'namespace':'demo'},'spec':{
        'serviceAccountName':'auth-control-plane','restartPolicy':'Never','activeDeadlineSeconds':90,
        'containers':[{'name':'delete','image':'localhost:5001/auth-demo/benchmark:demo','imagePullPolicy':'Never',
                       'args':['-delete-native-session',sid]}]}}
    subprocess.run(['kubectl','--context','auth-demo','apply','-f','-'],env=ENV,input=json.dumps(pod),text=True,check=True,stdout=subprocess.DEVNULL)
    try:
        for _ in range(90):
            state=json.loads(kubectl('-n','demo','get','pod',name,'-o','json'))['status']['phase']
            if state in ('Succeeded','Failed'):break
            time.sleep(1)
        assert state=='Succeeded',kubectl('-n','demo','logs',name)
    finally:kubectl('-n','demo','delete','pod',name,'--wait=true','--ignore-not-found=true')

def scale(name,count):
    kubectl('-n','demo','scale','deployment/'+name,'--replicas='+str(count))
    if count:kubectl('-n','demo','rollout','status','deployment/'+name,'--timeout=120s')
    else:
        for _ in range(30):
            pod=json.loads(kubectl('-n','demo','get','pods','-l','app='+name,'-o','json'))['items']
            if not pod:return
            time.sleep(1)
        raise AssertionError('deployment did not stop: '+name)

def delete_session(token,sid):
    for _ in range(30):
        status,result=request(18443,f'/v1/sessions/{sid}',token=token,method='DELETE')
        if status==200:return
        if status!=409:raise AssertionError(('session cleanup failed',status,result))
        time.sleep(1)
    raise AssertionError(('session remained busy during cleanup',sid,result))

def main():
    assert kubectl('config','current-context').strip()=='auth-demo'
    from benchmark import build_runner
    build_runner()
    handles=[]
    sessions=[]
    token=None
    try:
        for service,ports in [('auth-control-plane','18443:8443'),('catalog-api','18444:443')]:
            log=open(CACHE/('portforward-'+service+'.log'),'w')
            handles.append(subprocess.Popen(['kubectl','--context','auth-demo','-n','demo','port-forward','service/'+service,ports],env=ENV,stdout=log,stderr=log))
        last_error=None
        for _ in range(60):
            if any(p.poll() is not None for p in handles):raise AssertionError('port-forward stopped')
            try:
                if request(18443,'/readyz')[0]==200 and request(18444,'/audit')[0]==200:break
            except Exception as error:last_error=error
            time.sleep(1)
        else:raise AssertionError(f'services did not become reachable: {last_error}')
        token=kubectl('-n','demo','create','token','test-user','--audience=auth-demo','--duration=1h').strip()
        unprivileged=kubectl('-n','demo','create','token','unprivileged-user','--audience=auth-demo','--duration=1h').strip()
        run_id=uuid.uuid4().hex
        def create(account,key,caller=token):
            return request(18443,'/v1/sessions',{'agent':{'namespace':'demo','name':'mastra-http'},'serviceAccount':account,'requestId':key},caller)
        status,result=create('catalog-reader',run_id+'-unauthorized',unprivileged)
        assert status==403,(status,result);record('unauthorized service-account selection rejected')
        status,result=create('catalog-reader',run_id+'-invalid','not-a-valid-token')
        assert status==401,(status,result);record('invalid caller credential rejected')
        status,reader=create('catalog-reader',run_id+'-reader');assert status==200,(status,reader)
        sessions.append(reader['sessionId'])
        status,writer=create('catalog-writer',run_id+'-writer');assert status==200,(status,writer)
        sessions.append(writer['sessionId'])
        assert reader['sessionId']!=writer['sessionId']
        record('two native kagent sessions bound to different service accounts',reader=reader['sessionId'],writer=writer['sessionId'])
        status,retry=create('catalog-reader',run_id+'-reader');assert status==200 and retry==reader
        status,_=create('catalog-writer',run_id+'-reader');assert status==409
        record('creation retries preserve an immutable service-account binding')
        status,_=request(18443,f'/v1/sessions/{reader["sessionId"]}/invoke',{'method':'GET','path':'/catalog'},unprivileged)
        assert status==403;record('session invocation requires its authenticated owner')
        result=invoke(token,reader['sessionId'],'GET','/catalog');assert result['status']==200,result
        upstream=json.loads(result['body']);assert upstream['credential']=='reader' and upstream['credentialAccepted']
        record('Mastra GET reaches HTTPS API with gateway-injected reader credential')
        before=len(audit());result=invoke(token,reader['sessionId'],'POST','/orders');assert result['status']==403,result
        assert len(audit())==before;record('reader POST denied before upstream receives it')
        result=invoke(token,writer['sessionId'],'POST','/orders');assert result['status']==200,result
        assert json.loads(result['body'])['credential']=='writer';record('same Agent can write under writer service account')
        logs=kubectl('-n','demo','logs','deployment/auth-ext-proc')
        assert reader['sessionId'] in logs and writer['sessionId'] in logs
        record('gateway audit distinguishes both verified session identities')
        before=len(audit());result=invoke(token,reader['sessionId'],'POST','/orders',{'X-Service-Account':'catalog-writer','X-Agent-ID':writer['sessionId']})
        assert result['status']==403 and len(audit())==before;record('forged HTTP identity headers cannot change permissions')
        for path in ('/private','/%63atalog','/catalog/../orders'):
            before=len(audit());result=invoke(token,reader['sessionId'],'GET',path);assert result['status']==403 and len(audit())==before,(path,result)
        record('ungranted and encoded paths denied')
        # Watch-based policy and Secret changes are intentionally eventual.
        kubectl('-n','demo','annotate','serviceaccount','catalog-reader','auth.demo.dev/policy-type=deny-all/v1','--overwrite')
        try:
            wait_until(lambda:policy_type(reader['sessionId'])=='deny-all/v1')
            result=invoke(token,reader['sessionId'],'GET','/catalog');assert result['status']==403,result
            record('ServiceAccount watch propagates policy revocation')
        finally:
            kubectl('-n','demo','annotate','serviceaccount','catalog-reader','auth.demo.dev/policy-type=catalog-access/v1','--overwrite')
            wait_until(lambda:policy_type(reader['sessionId'])=='catalog-access/v1')
        kubectl('-n','demo','label','serviceaccount','catalog-reader','auth.demo.dev/managed-')
        try:
            wait_until(lambda:context_for(reader['sessionId'])[0]==403)
            status,_=create('catalog-reader',run_id+'-unmanaged');assert status==403
            result=invoke(token,reader['sessionId'],'GET','/catalog');assert result['status']==403
            record('removing managed label revokes egress and prevents new bindings')
        finally:
            kubectl('-n','demo','label','serviceaccount','catalog-reader','auth.demo.dev/managed=true','--overwrite')
            wait_until(lambda:context_for(reader['sessionId'])[0]==200)
        with Admin() as vault:
            original_text=vault.read('read-token')
            rotated=secrets.token_urlsafe(24)
            try:
                vault.write('read-token',rotated)
                assert credential_is(reader['sessionId'],original_text),'cached Vault value refreshed without invalidation'
                invalidate_credential('read-token')
                wait_until(lambda:credential_is(reader['sessionId'],rotated))
                record('Vault invalidation refreshes rotated credentials without polling')
            finally:
                vault.write('read-token',original_text)
                invalidate_credential('read-token')
                wait_until(lambda:credential_is(reader['sessionId'],original_text))
        status,stats_before=request(18443,'/internal/stats',internal_client=True);assert status==200
        for _ in range(20):assert credential_is(reader['sessionId'],original_text)
        status,stats_after=request(18443,'/internal/stats',internal_client=True);assert status==200
        assert stats_before['accountGETs']==stats_after['accountGETs'] and stats_before['vaultReads']==stats_after['vaultReads']
        record('warm authorization performs zero Kubernetes object GETs or Vault reads',grantRequests=20)
        before=len(audit());scale('auth-ext-proc',0)
        try:
            result=invoke(token,reader['sessionId'],'GET','/catalog');assert result['status']>=400 and len(audit())==before,result
            record('unavailable ext_proc fails closed',httpStatus=result['status'])
        finally:scale('auth-ext-proc',1)
        # A stopped control plane also stops its port-forward; test its outage through native kagent later.
        kubectl('-n','demo','rollout','restart','deployment/auth-control-plane')
        kubectl('-n','demo','rollout','status','deployment/auth-control-plane','--timeout=120s')
        close_connections()
        for p in handles:p.terminate();p.wait(timeout=10)
        handles=[]
        log=open(CACHE/'portforward-auth-control-plane.log','w')
        handles.append(subprocess.Popen(['kubectl','--context','auth-demo','-n','demo','port-forward','service/auth-control-plane','18443:8443'],env=ENV,stdout=log,stderr=log))
        for _ in range(30):
            try:
                if request(18443,'/readyz')[0]==200:break
            except Exception:pass
            time.sleep(1)
        status,retry=create('catalog-reader',run_id+'-reader');assert status==200 and retry['sessionId']==reader['sessionId']
        result=invoke(token,reader['sessionId'],'GET','/catalog');assert result['status']==200,result
        record('bridge restart preserves PostgreSQL bindings and request idempotency')
        old=json.loads(kubectl('-n','demo','get','serviceaccount','catalog-reader','-o','json'))
        kubectl('-n','demo','delete','serviceaccount','catalog-reader')
        replacement={'apiVersion':'v1','kind':'ServiceAccount','metadata':{
            'name':'catalog-reader','namespace':'demo','labels':old['metadata'].get('labels',{}),'annotations':old['metadata'].get('annotations',{})},
            'automountServiceAccountToken':False}
        subprocess.run(['kubectl','--context','auth-demo','apply','-f','-'],env=ENV,input=json.dumps(replacement),text=True,check=True,stdout=subprocess.DEVNULL)
        wait_until(lambda:context_for(reader['sessionId'])[0]==403)
        result=invoke(token,reader['sessionId'],'GET','/catalog');assert result['status']==403
        record('recreated ServiceAccount UID cannot inherit an existing binding')
        delete_native(writer['sessionId'])
        status,before_gc=context_for(writer['sessionId']);assert status==200,(status,before_gc)
        status,gc=request(18443,'/internal/reconcile',{},internal_client=True);assert status==200,(status,gc)
        assert gc['ConfirmedDeleted']>=1,gc
        assert context_for(writer['sessionId'])[0]==403
        status,_=create('catalog-writer',run_id+'-writer');assert status==409
        record('asynchronous reconciliation revokes externally deleted native Sessions',cleanup=gc)
        for sid in sessions:delete_session(token,sid)
        sessions=[]
        status,_=request(18443,f'/v1/sessions/{reader["sessionId"]}/invoke',{'method':'GET','path':'/catalog'},token)
        assert status==403;record('deleted session bindings cannot invoke the Agent')
        (ROOT/'artifacts').mkdir(exist_ok=True)
        report={'cluster':'auth-demo','driver':'minikube/docker','kagent':'1.0.0-alpha17','substrate':'0.6.0-alpha3','tests':RESULTS}
        (ROOT/'artifacts/e2e-results.json').write_text(json.dumps(report,indent=2)+'\n')
        print(f'{len(RESULTS)} integration checks passed on local Minikube.',flush=True)
    finally:
        for sid in sessions:
            try:
                delete_session(token,sid)
            except Exception as error:print(f'Session cleanup failed: {error}',flush=True)
        close_connections()
        for p in handles:
            p.terminate()
            try:p.wait(timeout=10)
            except subprocess.TimeoutExpired:p.kill()

if __name__=='__main__':main()
