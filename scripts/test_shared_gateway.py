#!/usr/bin/env python3
"""Real native Session and captured Pod against one Envoy and two locations."""
from datetime import datetime,timezone
from concurrent.futures import ThreadPoolExecutor
import http.client
import itertools
import json
import socket
import ssl
import subprocess
import time
import uuid
from urllib.parse import urlencode

import test_e2e as demo
from permissions_demo import CATALOG,MCP,permission_set,set_permissions
from shared_gateway import EXTERNAL_ORIGIN,GATEWAY_HOST


def main():
    results=[];handles=[];sid=None;writer_sid=None;token=None
    started=datetime.now(timezone.utc).isoformat()
    def record(name,**evidence):
        results.append({'test':name,'passed':True,**evidence});print('PASS:',name,flush=True)
    try:
        gateway=json.loads(demo.kubectl('-n','ate-system','get','pods','-l','app=atenet-egress','-o','json'))['items']
        assert len(gateway)==1 and {c['name'] for c in gateway[0]['spec']['containers']}=={'envoy','native-egress'}
        assert any(c['name']=='sdsmint' for c in gateway[0]['spec']['initContainers'])
        gateway_uid=gateway[0]['metadata']['uid']
        pod=json.loads(demo.kubectl('-n','demo','get','pods','-l','app=command-runner','-o','json'))['items'][0]['metadata']
        fixture=json.loads((demo.CACHE/'external-fixture.json').read_text())
        assert not demo.kubectl('-n','demo','get','deployment','pod-egress','--ignore-not-found=true').strip(),'old dedicated Pod gateway still exists'
        record('one shared Envoy gateway with native authorization and SDS issuer',gatewayPodUid=gateway_uid,externalFixture=fixture)
        for namespace,target,ports in [('demo','service/auth-control-plane','18443:8443'),('demo','service/catalog-api','18444:443'),
                ('test-services','service/mock-mcp','18445:8080'),('demo','pod/'+pod['name'],'18446:3000'),
                ('ate-system','service/atenet-egress','18543:443'),('ate-system','service/atenet-egress','18544:8443')]:
            with open(demo.CACHE/('shared-forward-'+ports[:5]+'.log'),'w') as log:
                handles.append(subprocess.Popen(['kubectl','--context','auth-demo','-n',namespace,'port-forward',target,ports],env=demo.ENV,stdout=log,stderr=log))
        def request(port,path,body=None,tls=False,token=None):
            c=(http.client.HTTPSConnection('localhost',port,context=ssl.create_default_context(cafile=str(demo.CACHE/'tls/ca.crt')),timeout=120)
               if tls else http.client.HTTPConnection('localhost',port,timeout=120))
            try:
                headers={'Content-Type':'application/json'}
                if token:headers['Authorization']='Bearer '+token
                c.request('GET' if body is None else 'POST',path,None if body is None else json.dumps(body),headers)
                response=c.getresponse();raw=response.read()
                try:value=json.loads(raw)
                except json.JSONDecodeError:value=raw.decode()
                return response.status,value
            finally:c.close()
        for _ in range(60):
            try:
                if demo.request(18443,'/readyz')[0]==200 and request(18446,'/readyz')[0]==200 and request(18448,'/readyz',tls=True)[0]==200:break
            except OSError:pass
            time.sleep(1)
        else:raise AssertionError('shared gateway fixtures unreachable')
        token=demo.kubectl('-n','demo','create','token','test-user','--audience=auth-demo','--duration=1h').strip()
        status,binding=demo.request(18443,'/v1/sessions',{'agent':{'namespace':'demo','name':'mastra-http'},
            'serviceAccount':'multi-reader','requestId':'shared-'+uuid.uuid4().hex},token=token)
        assert status==200,(status,binding);sid=binding['sessionId']
        identities=['spiffe://substrate-actor.local/worker-for-actor/demo/session-'+sid,'spiffe://auth-demo.local/ns/demo/pod/'+pod['uid']]
        def context(identity):return demo.request(18443,'/internal/context?'+urlencode({'spiffeId':identity}),internal_client=True)
        def select(names):
            set_permissions(names);expected=permission_set(names)
            def ready():
                return all((lambda result:result[0]==200 and result[1]['policy']['config']==expected)(context(identity)) for identity in identities)
            demo.wait_until(ready)
        def audit(name):
            return (demo.audit() if name=='catalog' else request(18445,'/audit')[1] if name=='mcp' else request(18448,'/audit',tls=True)[1]) or []
        def invoke(source,origin,method,path,headers=None):
            body={'origin':origin,'method':method,'path':path,'headers':headers or {}}
            if source=='pod':body['execution']='command';status,value=request(18446,'/invoke',body)
            else:status,value=request(18443,f'/v1/sessions/{writer_sid if source=="writer" else sid}/invoke',body,tls=True,token=token)
            assert status==200,('agent invocation',source,status,value)
            calls=demo.calls(value)
            assert calls and len(calls)==1,('tool execution',source,value)
            return calls[0]
        destinations=[('catalog',CATALOG,'GET','/catalog'),('mcp',MCP,'POST','/mcp'),('external',EXTERNAL_ORIGIN,'GET','/external')]
        # Keep the same Session ID and Pod UID through all eight permission sets.
        for bits in itertools.product([False,True],repeat=3):
            names=[name for bit,(name,*_) in zip(bits,destinations) if bit]
            select(names)
            for source in ['session','pod']:
                for name,origin,method,path in destinations:
                    before=len(audit(name));response=invoke(source,origin,method,path)
                    allowed=name in names
                    assert response['status']==(200 if allowed else 403),('matrix',source,names,name,response)
                    assert len(audit(name))==before+int(allowed),('upstream count',source,names,name)
                    if allowed:assert json.loads(response['body'])['credentialAccepted'],'wrong injected key'
                    record('permission matrix',source=source,destination=name,grants=names,status=response['status'],upstreamRequests=int(allowed))
        select(['catalog','mcp','external'])
        for source in ['session','pod']:
            for name,origin,_,_ in destinations:
                before=len(audit(name));response=invoke(source,origin,'DELETE','/private')
                assert response['status']==403 and len(audit(name))==before
            response=invoke(source,EXTERNAL_ORIGIN,'GET','/external',{
                'Authorization':'Bearer forged','X-Service-Account':'catalog-writer','X-Agent-ID':'forged',
                'X-Forwarded-Client-Cert':'URI=spiffe://attacker/forged'})
            assert response['status']==200 and json.loads(response['body'])['credential']=='reader'
            record('wrong operations denied and forged headers cannot choose identity or keys',source=source)

        # The main logs must show both authenticated callers on this SAME Envoy.
        logs=demo.kubectl('-n','ate-system','logs','deployment/atenet-egress','-c','envoy','--tail=5000')
        for identity in identities:assert identity in logs,('identity absent from shared gateway',identity)
        assert fixture['ip'] in logs and 'mock-mcp.test-services.svc.cluster.local' in logs
        record('same gateway handles both verified identities and internal/external destinations',gatewayPodUid=gateway_uid)
        issuer=demo.kubectl('-n','ate-system','logs','deployment/atenet-egress','-c','sdsmint','--tail=2000')
        assert fixture['hostname'] in issuer and 'certificate issued' in issuer
        record('SDS dynamically issues external destination certificate',hostname=fixture['hostname'])

        status,before=demo.request(18443,'/internal/stats',internal_client=True);assert status==200
        for source in ['session','pod']:
            for name,origin,method,path in destinations:assert invoke(source,origin,method,path)['status']==200
        status,after=demo.request(18443,'/internal/stats',internal_client=True);assert status==200
        deltas={key:after[key]-before[key] for key in ['podGETs','accountGETs','vaultReads']}
        assert all(v==0 for v in deltas.values()),deltas
        record('warm native and Pod authorization makes zero Kubernetes object GETs or Vault reads',counterDeltas=deltas)

        # A CA-trusted infrastructure client without a registered Pod URI is
        # rejected on the managed listener, before opening a destination.
        ctx=ssl.create_default_context(cafile=str(demo.CACHE/'tls/ca.crt'))
        ctx.load_cert_chain(str(demo.CACHE/'tls/agentgateway.crt'),str(demo.CACHE/'tls/agentgateway.key'))
        with socket.create_connection(('localhost',18544),timeout=10) as raw:
            with ctx.wrap_socket(raw,server_hostname=GATEWAY_HOST) as stream:
                stream.sendall(b'CONNECT 192.0.2.1:443 HTTP/1.1\r\nHost: 192.0.2.1:443\r\n\r\n')
                response=stream.recv(4096)
        assert b'403' in response,response
        record('managed listener rejects a trusted certificate without a Pod identity')
        # The native listener trusts Actor certificates, never our Pod issuer.
        native_ca=demo.CACHE/'shared-native-server-ca.crt'
        native_ca.write_text(demo.kubectl('-n','ate-system','exec','deployment/atenet-egress','-c','envoy','--','cat','/run/servicedns.podcert.ate.dev/trust-bundle.pem'))
        ctx=ssl.create_default_context(cafile=str(native_ca))
        ctx.load_cert_chain(str(demo.CACHE/'tls/command-runner.crt'),str(demo.CACHE/'tls/command-runner.key'))
        rejected=False
        try:
            with socket.create_connection(('localhost',18543),timeout=10) as raw:
                with ctx.wrap_socket(raw,server_hostname=GATEWAY_HOST) as stream:
                    stream.sendall(b'CONNECT 192.0.2.1:443 HTTP/1.1\r\nHost: 192.0.2.1:443\r\n\r\n')
                    answer=stream.recv(4096);rejected=b'200' not in answer
        except (ssl.SSLError,ConnectionResetError):rejected=True
        assert rejected,'Pod identity accepted on native Actor listener'
        record('native listener rejects the managed Pod certificate issuer')

        # Interleave two native Sessions with different ServiceAccounts and a
        # Pod. Each native Session has one request in flight at a time; all
        # three callers run concurrently through the same Envoy process.
        status,writer=demo.request(18443,'/v1/sessions',{'agent':{'namespace':'demo','name':'mastra-http'},
            'serviceAccount':'catalog-writer','requestId':'shared-writer-'+uuid.uuid4().hex},token=token)
        assert status==200,(status,writer);writer_sid=writer['sessionId']
        assert invoke('writer',CATALOG,'POST','/orders')['status']==200
        select(['mcp','external'])
        counts_before={name:len(audit(name)) for name,_,_,_ in destinations}
        def workload(source):
            count=0
            for _ in range(3):
                for name,origin,method,path in destinations:
                    if source=='writer' and name=='catalog':method,path='POST','/orders'
                    expected=200 if (name=='catalog')==(source=='writer') else 403
                    response=invoke(source,origin,method,path)
                    assert response['status']==expected,('identity isolation',source,name,response)
                    if expected==200:
                        value=json.loads(response['body'])
                        assert value['credentialAccepted'] and value['credential']==('writer' if source=='writer' else 'reader')
                    count+=1
            return count
        with ThreadPoolExecutor(max_workers=3) as pool:
            completed=sum(pool.map(workload,['session','writer','pod']))
        deltas={name:len(audit(name))-counts_before[name] for name,_,_,_ in destinations}
        assert deltas=={'catalog':3,'mcp':6,'external':6},deltas
        record('concurrent native Sessions and Pod preserve different ServiceAccount permissions and keys',requests=completed,upstreamDeltas=deltas)
        select(['catalog','mcp','external'])

        # Internet smoke: no credential is configured for this public origin.
        select(['catalog','mcp','external','public'])
        for source in ['session','pod']:
            response=invoke(source,'https://example.com','GET','/')
            assert response['status']==200 and 'Example Domain' in response['body'],response
            record('real public Internet HTTPS through shared gateway',source=source,status=200,origin='https://example.com')
        select(['catalog','mcp','external'])

        report={'completed':True,'cluster':'auth-demo','startedAtUTC':started,'finishedAtUTC':datetime.now(timezone.utc).isoformat(),
            'gatewayPodUid':gateway_uid,'sessionId':sid,'writerSessionId':writer_sid,'podUid':pod['uid'],'externalFixture':fixture,
            'gatewayImages':gateway[0]['status'].get('containerStatuses',[]),'tests':results}
        (demo.ROOT/'artifacts/shared-gateway-results.json').write_text(json.dumps(report,indent=2)+'\n')
        print(f'{len(results)} shared-gateway checks passed.',flush=True)
    finally:
        set_permissions(['catalog','mcp','external'])
        for session_id in [sid,writer_sid]:
            if session_id:
                try:demo.delete_session(token,session_id)
                except Exception:print('Session cleanup failed',flush=True)
        demo.close_connections()
        for handle in handles:
            handle.terminate()
            try:handle.wait(timeout=10)
            except subprocess.TimeoutExpired:handle.kill();handle.wait()


if __name__=='__main__':main()
