#!/usr/bin/env python3
"""Exercise additive permissions from a real kagent Session and captured command Pod."""
from datetime import datetime,timezone
import http.client
import json
import subprocess
import time
import uuid

import test_e2e as demo
from permissions_demo import CATALOG,MCP,permission_set,set_permissions
from database import apply


def main():
    results=[];handles=[];sid=None;token=None;started=datetime.now(timezone.utc).isoformat()
    def record(name,**evidence):
        results.append({'test':name,'passed':True,**evidence});print('PASS:',name,flush=True)
    try:
        pods=json.loads(demo.kubectl('-n','demo','get','pods','-l','app=command-runner','-o','json'))['items']
        assert len(pods)==1,'specific Pod is not deployed'
        pod=pods[0]['metadata'];uid=pod['uid']
        for namespace,target,ports in [('demo','service/auth-control-plane','18443:8443'),
                                       ('demo','service/catalog-api','18444:443'),
                                       ('test-services','service/mock-mcp','18445:8080'),
                                       ('demo','pod/'+pod['name'],'18446:3000')]:
            with open(demo.CACHE/('permissions-portforward-'+ports.split(':')[0]+'.log'),'w') as log:
                handles.append(subprocess.Popen(['kubectl','--context','auth-demo','-n',namespace,'port-forward',target,ports],env=demo.ENV,stdout=log,stderr=log))
        def plain(port,path,body=None):
            c=http.client.HTTPConnection('localhost',port,timeout=180)
            try:
                c.request('POST' if body is not None else 'GET',path,None if body is None else json.dumps(body),{'Content-Type':'application/json'})
                response=c.getresponse();data=response.read()
                try:value=json.loads(data)
                except json.JSONDecodeError:value=data.decode()
                return response.status,value
            finally:c.close()
        for _ in range(60):
            if any(p.poll() is not None for p in handles):raise AssertionError('test port-forward exited')
            try:
                if demo.request(18443,'/readyz')[0]==200 and plain(18446,'/readyz')[0]==200:break
            except OSError:pass
            time.sleep(1)
        else:raise AssertionError('test services did not become reachable')
        status,binding=demo.request(18443,'/internal/pods',{'namespace':'demo','name':pod['name'],'uid':uid},internal_client=True)
        assert status==200,(status,binding)
        record('trusted launcher registers immutable Pod UID and ServiceAccount UID binding',pod=pod['name'],podUid=uid)
        token=demo.kubectl('-n','demo','create','token','test-user','--audience=auth-demo','--duration=1h').strip()
        status,binding=demo.request(18443,'/v1/sessions',{'agent':{'namespace':'demo','name':'mastra-http'},
            'serviceAccount':'multi-reader','requestId':'permission-matrix-'+uuid.uuid4().hex},token=token)
        assert status==200,(status,binding);sid=binding['sessionId']
        record('one native Session and one specific Pod use the same multi-permission ServiceAccount',sessionId=sid)

        def context(identity):
            from urllib.parse import urlencode
            return demo.request(18443,'/internal/context?'+urlencode({'spiffeId':identity}),internal_client=True)
        identities=['spiffe://substrate-actor.local/worker-for-actor/demo/session-'+sid,
                    'spiffe://auth-demo.local/ns/demo/pod/'+uid]
        def select(names):
            set_permissions(names)
            expected=permission_set(names)
            def ready():
                for identity in identities:
                    status,value=context(identity)
                    if status!=200 or value['policy']['type']!='permission-set/v1' or value['policy']['config']!=expected:return False
                return True
            demo.wait_until(ready)
        def audit(origin):
            return (demo.audit() if origin==CATALOG else plain(18445,'/audit')[1]) or []
        def call(source,origin,method,path,headers=None):
            body={'origin':origin,'method':method,'path':path,'headers':headers or {}}
            if source=='pod':
                body['execution']='command'
                status,value=plain(18446,'/invoke',body)
            else:status,value=demo.request(18443,f'/v1/sessions/{sid}/invoke',body,token=token)
            assert status==200,('agent execution failed',source,status,value)
            calls=demo.calls(value);assert calls and len(calls)==1,('tool did not execute exactly once',source,value)
            return calls[0]

        for names in [[],['catalog'],['catalog','mcp'],['mcp']]:
            select(names)
            for source in ['session','pod']:
                for name,origin,method,path in [('catalog',CATALOG,'GET','/catalog'),('mcp',MCP,'POST','/mcp')]:
                    before=len(audit(origin));response=call(source,origin,method,path)
                    allowed=name in names
                    assert response['status']==(200 if allowed else 403),('matrix result',source,names,name,response)
                    assert len(audit(origin))==before+int(allowed),('unexpected upstream request',source,names,name)
                    if allowed:assert json.loads(response['body'])['credentialAccepted'],'upstream did not accept the rule-selected key'
                    record('permission matrix',source=source,permissions=names,destination=name,httpStatus=response['status'],upstreamRequests=int(allowed))

        select(['catalog','mcp'])
        for source in ['session','pod']:
            for origin,method,path in [(CATALOG,'POST','/mcp'),(MCP,'GET','/catalog'),(MCP,'POST','/private')]:
                before=len(audit(origin));response=call(source,origin,method,path)
                assert response['status']==403 and len(audit(origin))==before
            response=call(source,CATALOG,'GET','/catalog',{'Authorization':'Bearer forged','X-Service-Account':'catalog-writer','X-Agent-ID':'forged'})
            assert response['status']==200 and json.loads(response['body'])['credential']=='reader'
            record('permissions cannot be mixed and forged headers cannot select credentials',source=source)

        # No proxy environment settings participate in command execution. curl
        # itself also uses --noproxy '*'; interception is in this Pod's netns.
        for resource in [('service','mock-mcp'),('pod',None)]:
            if resource[0]=='service':
                ip=json.loads(demo.kubectl('-n','test-services','get','service','mock-mcp','-o','json'))['spec']['clusterIP']
            else:
                ip=json.loads(demo.kubectl('-n','test-services','get','pods','-l','app=mock-mcp','-o','json'))['items'][0]['status']['podIP']
            before=len(audit(MCP))
            probe=subprocess.run(['kubectl','--context','auth-demo','-n','demo','exec',pod['name'],'-c','mastra','--',
                'curl','--noproxy','*','--max-time','10','--silent','--output','/dev/null','--write-out','%{http_code}',
                'http://'+ip+':8080/mcp'],env=demo.ENV,text=True,capture_output=True)
            assert probe.stdout.strip()!='200' and len(audit(MCP))==before,'literal IP bypass reached upstream'
        record('captured Pod cannot bypass rules through Service or backing Pod IP')

        control={'apiVersion':'v1','kind':'Pod','metadata':{'name':'permissions-control','namespace':'demo'},'spec':{
            'automountServiceAccountToken':False,'restartPolicy':'Never','containers':[{'name':'control',
                'image':'localhost:5001/auth-demo/pod-runner:demo','imagePullPolicy':'Never',
                'command':['curl','--noproxy','*','--fail','--silent',MCP+'/public']}]}}
        demo.kubectl('-n','demo','delete','pod','permissions-control','--ignore-not-found=true','--wait=true')
        apply([control])
        for _ in range(60):
            phase=json.loads(demo.kubectl('-n','demo','get','pod','permissions-control','-o','json'))['status']['phase']
            if phase in ['Succeeded','Failed']:break
            time.sleep(1)
        assert phase=='Succeeded',demo.kubectl('-n','demo','logs','permissions-control')
        assert json.loads(demo.kubectl('-n','demo','logs','permissions-control'))['public']
        record('unrelated control Pod retains direct cross-namespace service access')

        # Exercise a real raw UDP socket from the unprivileged application.
        # Non-DNS UDP must be rejected by its own netns firewall, regardless of CNI.
        udp=demo.kubectl('-n','demo','exec',pod['name'],'-c','mastra','--','node','--input-type=module','-e',
            "import dgram from 'node:dgram'; const s=dgram.createSocket('udp4'); s.send('probe',8080,'"+ip+"',error=>{ console.log(error?.code || 'SENT'); s.close(); process.exit(error?0:1); });")
        assert 'EPERM' in udp or 'EACCES' in udp,('non-DNS UDP bypassed Pod firewall',udp)
        record('Pod netns firewall rejects non-DNS UDP without relying on CNI policy support')

        demo.kubectl('-n','demo','label','pod',pod['name'],'auth.demo.dev/egress-')
        try:
            demo.wait_until(lambda:context(identities[1])[0]==403)
            before=len(audit(MCP));response=call('pod',MCP,'POST','/mcp')
            assert response['status']==403 and len(audit(MCP))==before
            assert call('session',MCP,'POST','/mcp')['status']==200
            record('Pod enrollment label removal revokes only that Pod through its watch')
        finally:
            demo.kubectl('-n','demo','label','pod',pod['name'],'auth.demo.dev/egress=enforced','--overwrite')
        demo.wait_until(lambda:context(identities[1])[0]==200)

        before=len(audit(MCP))
        demo.kubectl('-n','demo','scale','deployment/pod-egress','--replicas=0')
        demo.kubectl('-n','demo','wait','--for=delete','pod','-l','app=pod-egress','--timeout=60s')
        try:
            probe=subprocess.run(['kubectl','--context','auth-demo','-n','demo','exec',pod['name'],'-c','mastra','--',
                'curl','--noproxy','*','--max-time','10','--silent',MCP+'/public'],env=demo.ENV,capture_output=True)
            assert probe.returncode!=0 and len(audit(MCP))==before,'Pod fell back to a direct connection'
            record('selected Pod has no direct fallback when its dedicated gateway stops')
        finally:
            demo.kubectl('-n','demo','scale','deployment/pod-egress','--replicas=1')
            demo.kubectl('-n','demo','rollout','status','deployment/pod-egress','--timeout=120s')
        # The relay can hold a connection to the removed gateway Pod. Probe
        # recovery with a safe GET; never replay a side-effecting POST here.
        recovered=False
        for _ in range(30):
            try:
                if call('pod',CATALOG,'GET','/catalog')['status']==200:
                    recovered=True;break
            except AssertionError:pass
            time.sleep(1)
        assert recovered,'relay did not reconnect after gateway restart'
        response=call('pod',MCP,'POST','/mcp');assert response['status']==200,response
        record('captured command traffic recovers after gateway restart')

        status,stats_before=demo.request(18443,'/internal/stats',internal_client=True);assert status==200
        for _ in range(4):
            assert call('pod',MCP,'POST','/mcp')['status']==200
            assert call('pod',CATALOG,'GET','/catalog')['status']==200
        status,stats_after=demo.request(18443,'/internal/stats',internal_client=True);assert status==200
        deltas={key:stats_after[key]-stats_before[key] for key in ['podGETs','accountGETs','vaultReads']}
        assert all(value==0 for value in deltas.values()),('warm authority reads',deltas)
        record('warm Pod requests perform zero Kubernetes object GETs or Vault reads',requests=8,counterDeltas=deltas)

        report={'cluster':'auth-demo','startedAtUTC':started,'finishedAtUTC':datetime.now(timezone.utc).isoformat(),
                'completed':True,'pod':pod['name'],'podUid':uid,'sessionId':sid,'tests':results}
        (demo.ROOT/'artifacts/permissions-results.json').write_text(json.dumps(report,indent=2)+'\n')
        print(f'{len(results)} permission and routing checks passed on local Minikube.',flush=True)
    finally:
        set_permissions(['catalog','mcp'])
        if sid is not None:
            try:demo.delete_session(token,sid)
            except Exception:print('native test Session cleanup failed',flush=True)
        demo.kubectl('-n','demo','delete','pod','permissions-control','--ignore-not-found=true','--wait=true')
        demo.kubectl('-n','demo','delete','pod','permissions-network-probe','--ignore-not-found=true','--wait=true')
        demo.close_connections()
        for handle in handles:
            handle.terminate()
            try:handle.wait(timeout=10)
            except subprocess.TimeoutExpired:handle.kill()


if __name__=='__main__':main()
