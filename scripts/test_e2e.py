#!/usr/bin/env python3
"""Real kagent Sessions through two dedicated egress deployments."""
import base64,datetime,http.client,json,ssl,subprocess,time,uuid
from local import ROOT,CACHE,ENV,kubectl,apply
from deploy import SCOPES,AGENT,INTERNAL,EXTERNAL,policy
RESULTS=[]
def record(name,**evidence):
    RESULTS.append({'test':name,'passed':True,**evidence});print('PASS:',name,flush=True)
    (CACHE/'dedicated-test-progress.json').write_text(json.dumps(RESULTS,indent=2)+'\n')
def payload(value,key):
    if isinstance(value,dict):
        if key in value:return value[key]
        for name,child in value.items():
            if name=='text' and isinstance(child,str):
                try:
                    result=payload(json.loads(child),key)
                    if result is not None:return result
                except json.JSONDecodeError:pass
            result=payload(child,key)
            if result is not None:return result
    elif isinstance(value,list):
        for child in value:
            result=payload(child,key)
            if result is not None:return result
    return None
class Forward:
    def __init__(self,ns,service,port,target=8443):
        self.port=port;self.log=open(CACHE/(ns+'-'+service+'-forward.log'),'w')
        self.process=subprocess.Popen(['kubectl','--context','auth-demo','-n',ns,'port-forward','svc/'+service,f'{port}:{target}'],env=ENV,stdout=self.log,stderr=self.log)
    def close(self):self.process.terminate();self.process.wait(timeout=10);self.log.close()
def request(port,path,body=None,token=None,admin=False,method=None,client_scope=None):
    d=CACHE/'dedicated-tls'/SCOPES[port-18543];ctx=ssl.create_default_context(cafile=str(d/'ca.crt'))
    if admin:
        client=CACHE/'dedicated-tls'/(client_scope or SCOPES[port-18543])
        ctx.load_cert_chain(str(client/'ext-proc.crt'),str(client/'ext-proc.key'))
    conn=http.client.HTTPSConnection('localhost',port,context=ctx,timeout=180)
    headers={'Content-Type':'application/json'}
    if token:headers['Authorization']='Bearer '+token
    try:
        conn.request(method or ('POST' if body is not None else 'GET'),path,body=None if body is None else json.dumps(body),headers=headers)
        resp=conn.getresponse();data=resp.read();return resp.status,json.loads(data)
    finally:conn.close()
def wait_for(check,seconds=100):
    deadline=time.monotonic()+seconds;last=None
    while time.monotonic()<deadline:
        try:
            if check():return
        except Exception as error:last=error
        time.sleep(.5)
    raise AssertionError('condition did not converge: '+str(last))
def invoke(port,token,sid,method='GET',path='/catalog',origin=INTERNAL,headers=None,load=None,execution=None):
    body={'method':method,'path':path,'origin':origin,'headers':headers or {}}
    if load:body['load']=load
    if execution:body['execution']=execution
    # kagent may reject input while a new sandbox starts. Retry only its
    # explicit 'not accepted' response, never an operation that may have run.
    for attempt in range(60):
        status,value=request(port,f'/v1/sessions/{sid}/invoke',body,token)
        if status==502 and 'input was not accepted' in str(value):time.sleep(1);continue
        break
    assert status==200,(status,value)
    result=payload(value,'load' if load else 'calls')
    assert result is not None,('missing Mastra tool result',value)
    return result if load else result[0]
def set_policy(ns,config=None,typ='permission-set/v1'):
    kubectl('-n',ns,'patch','cm','agent-permissions','--type=merge','-p',json.dumps({'data':{'type':typ,'config':json.dumps(config if config is not None else policy(ns,ns==SCOPES[1]))}}))
def set_secret(ns,token):
    kubectl('-n',ns,'patch','secret','application-credentials','--type=merge','-p',json.dumps({'data':{'credentials.json':base64.b64encode(json.dumps({'api-token':{'type':'bearer','token':token}}).encode()).decode()}}))
def main():
    forwards=[];sessions=[];tokens=[];run_id=uuid.uuid4().hex
    try:
        for i,ns in enumerate(SCOPES):
            f=Forward(ns,'authorizer',18543+i);forwards.append(f)
            wait_for(lambda:request(f.port,'/readyz')[0]==200)
            token=kubectl('-n',ns,'create','token','test-user','--audience=auth-demo','--duration=1h').strip();tokens.append(token)
            body={'agent':{'namespace':ns,'name':AGENT},'serviceAccount':'agent-caller','requestId':run_id}
            status,binding=request(f.port,'/v1/sessions',body,token);assert status==200,(status,binding)
            sessions.append(binding['sessionId'])
            status,retry=request(f.port,'/v1/sessions',body,token);assert status==200 and retry==binding,(status,retry)
        a,b=[x.port for x in forwards];ta,tb=tokens;sa,sb=sessions
        record('two native Sessions created in separate agent scopes; retries preserve bindings')
        for port,token,sid,identity in [(a,ta,sa,'reader'),(b,tb,sb,'writer')]:
            for origin,path in [(INTERNAL,'/catalog'),(EXTERNAL,'/external')]:
                result=invoke(port,token,sid,path=path,origin=origin);assert result['status']==200,result
                event=json.loads(result['body']);assert event['credential']==identity and event['credentialAccepted'] and event['authorizationHeaderCount']==1,event
        record('internal HTTP and external HTTPS inject each agent own mounted key')
        assert invoke(a,ta,sa,method='POST',path='/mcp')['status']==403
        result=invoke(b,tb,sb,method='POST',path='/mcp');assert result['status']==200 and json.loads(result['body'])['credential']=='writer',result
        assert invoke(a,ta,sa,path='/private')['status']==403
        assert invoke(a,ta,sa,method='POST',path='/mcp',headers={'X-Agent-ID':sb,'X-Service-Account':'writer','Authorization':'Bearer forged'})['status']==403
        record('operation grants and spoofed identity headers enforced')
        status,_=request(b,f'/v1/sessions/{sa}/invoke',{'method':'GET','path':'/catalog'},ta);assert status==403,status
        record('other agent authorizer rejects Session from wrong scope')
        try:
            request(a,'/internal/stats',admin=True,client_scope=SCOPES[1])
        except (ssl.SSLError,ConnectionError):pass
        else:raise AssertionError('Other bundle client certificate accepted')
        record('per-bundle mTLS prevents cross-authorizer internal access')
        # A deliberate TLS reset can terminate kubectl's port-forward stream.
        # Reopen the test transport; the gateway Pod itself remains healthy.
        forwards[0].close();forwards[0]=Forward(SCOPES[0],'authorizer',a)
        wait_for(lambda:request(a,'/readyz')[0]==200,seconds=15)
        for ns in SCOPES:
            pod=json.loads(kubectl('-n',ns,'get','deploy','agent-egress','-o','json'))['spec']['template']['spec']
            holders=[c['name'] for c in pod['containers']+pod.get('initContainers',[]) if any(m['name']=='application-credentials' for m in c.get('volumeMounts',[]))]
            assert holders==['authorizer'],holders
        record('application credentials mounted only in dedicated authorizer')
        set_policy(SCOPES[0],typ='deny-all/v1')
        try:
            wait_for(lambda:invoke(a,ta,sa)['status']==403)
            assert invoke(b,tb,sb)['status']==200
            record('ConfigMap watch revokes one agent while other remains authorized')
        finally:
            set_policy(SCOPES[0]);wait_for(lambda:invoke(a,ta,sa)['status']==200)
        reduced=policy(SCOPES[0]);reduced['permissions']=[reduced['permissions'][0]]
        set_policy(SCOPES[0],reduced)
        try:
            wait_for(lambda:invoke(a,ta,sa,origin=EXTERNAL,path='/external')['status']==403)
            assert invoke(a,ta,sa)['status']==200
            record('one Session holds multiple grants; removing external grant preserves internal access')
        finally:
            set_policy(SCOPES[0]);wait_for(lambda:invoke(a,ta,sa,origin=EXTERNAL,path='/external')['status']==200)
        original=json.loads((CACHE/'dedicated-keys.json').read_text())['reader']
        set_secret(SCOPES[0],'deliberately-invalid-rotated-key')
        try:
            wait_for(lambda:invoke(a,ta,sa)['status']==401,seconds=120)
            assert invoke(b,tb,sb)['status']==200
            record('projected Secret rotation reaches upstream without restart and stays isolated')
        finally:
            set_secret(SCOPES[0],original);wait_for(lambda:invoke(a,ta,sa)['status']==200,seconds=120)
        before=request(a,'/internal/stats',admin=True)[1]
        # kagent serializes Session messages. Generate concurrent application
        # requests inside one invocation, so this measures egress authorization
        # rather than racing the native Session state machine.
        warm=invoke(a,ta,sa,load={'concurrency':4,'durationSeconds':2})
        assert warm['completed']>0 and warm['statuses']=={'200':warm['completed']} and warm['credentialFailures']==0,warm
        after=request(a,'/internal/stats',admin=True)[1]
        for key in ['accountGETs','policyGETs','listCalls','credentialFileLoads','oauthExchanges']:assert after[key]==before[key],(key,before,after)
        record('warm concurrent authorization makes no Kubernetes GETs or credential reloads',requests=warm['completed'])
        # A separate benchmark command records bounded load, without repeated session creation.
        if __import__('os').environ.get('AUTH_BENCHMARK')=='1':
            loads=[]
            for n in [1,8,32]:
                result=invoke(a,ta,sa,load={'concurrency':n,'durationSeconds':5})
                assert result['completed']>0 and result['statuses']=={'200':result['completed']} and result['credentialFailures']==0,result
                loads.append(result);print('LOAD',n,result['requestsPerSecond'],flush=True)
            record('bounded HTTP load succeeds through dedicated gateway',loads=loads)
        kubectl('-n',SCOPES[0],'rollout','restart','deploy/agent-egress')
        kubectl('-n',SCOPES[0],'rollout','status','deploy/agent-egress','--timeout=180s')
        forwards[0].close();forwards[0]=Forward(SCOPES[0],'authorizer',a)
        wait_for(lambda:request(a,'/readyz')[0]==200)
        assert invoke(a,ta,sa)['status']==200
        body={'agent':{'namespace':SCOPES[0],'name':AGENT},'serviceAccount':'agent-caller','requestId':run_id}
        status,retry=request(a,'/v1/sessions',body,ta);assert status==200 and retry['sessionId']==sa,(status,retry)
        record('bundle restart preserves PostgreSQL bindings and idempotency')
        # Verify a replaced identity cannot inherit permissions, even with the same name.
        account=json.loads(kubectl('-n',SCOPES[0],'get','sa','agent-caller','-o','json'))
        kubectl('-n',SCOPES[0],'delete','sa','agent-caller')
        apply([{'apiVersion':'v1','kind':'ServiceAccount','metadata':{'name':'agent-caller','namespace':SCOPES[0],'labels':account['metadata']['labels'],'annotations':account['metadata']['annotations']},'automountServiceAccountToken':False}])
        wait_for(lambda:invoke(a,ta,sa)['status']==403)
        record('ServiceAccount UID replacement revokes existing Session')
        report={'timestamp':datetime.datetime.now(datetime.timezone.utc).isoformat(),'cluster':'auth-demo','architecture':'dedicated Envoy + in-process authorizer per agent namespace','tests':RESULTS}
        (ROOT/'artifacts/dedicated-e2e.json').write_text(json.dumps(report,indent=2)+'\n')
    finally:
        for i,sid in enumerate(sessions):
            for _ in range(15):
                try:
                    status,_=request(18543+i,f'/v1/sessions/{sid}',token=tokens[i],method='DELETE')
                    if status==200:break
                    if status!=409:break
                except Exception:break
                time.sleep(1)
        for f in forwards:f.close()
if __name__=='__main__':main()
