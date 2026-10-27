#!/usr/bin/env python3
"""Verify two Agents share one project worker pool/proxy and preserve Session identity."""
import datetime,json,time,uuid
from local import ROOT,kubectl,apply
from dedicated_gateway import demo_config,resources
from deploy import SCOPES,AGENT,INTERNAL,EXTERNAL
from test_e2e import Forward,request,wait_for,invoke

def main():
    ns=SCOPES[0];second='mastra-second';sessions=[];forwards=[]
    first=json.loads(kubectl('-n',ns,'get','agent',AGENT,'-o','json'))
    image=first['status']['workloadImage']
    apply(resources('agent',demo_config(ns,second,AGENT_IMAGE=image)))
    def ready():
        status=json.loads(kubectl('-n',ns,'get','agent',second,'-o','json')).get('status',{})
        return any(c['type']=='Ready' and c['status']=='True' for c in status.get('conditions',[])) and status.get('desiredRevision')==status.get('latestSuccessfulRevision')
    wait_for(ready,seconds=120)
    for i,project in enumerate(SCOPES):forwards.append(Forward(project,'authorizer',18543+i))
    token=kubectl('-n',ns,'create','token','test-user','--audience=auth-demo','--duration=15m').strip()
    checks=[]
    try:
        wait_for(lambda:request(18543,'/readyz')[0]==200,seconds=15)
        for name in [AGENT,second]:
            body={'agent':{'namespace':ns,'name':name},'serviceAccount':'agent-caller','requestId':uuid.uuid4().hex}
            status,binding=request(18543,'/v1/sessions',body,token)
            assert status==200 and binding['agent']==body['agent'],(status,binding)
            sessions.append(binding['sessionId'])
            assert request(18543,'/v1/sessions',body,token)==(status,binding)
            for origin,path in [(INTERNAL,'/catalog'),(EXTERNAL,'/external')]:
                result=invoke(18543,token,binding['sessionId'],origin=origin,path=path)
                assert result['status']==200 and json.loads(result['body'])['credential']=='reader',result
            assert invoke(18543,token,binding['sessionId'],method='POST',path='/mcp')['status']==403
            checks.append({'agent':name,'bindingPreserved':True,'internal':200,'external':200,'deniedOperation':403})
        wait_for(lambda:request(18544,'/readyz')[0]==200,seconds=15)
        status,_=request(18544,f'/v1/sessions/{sessions[0]}/invoke',{'method':'GET','path':'/catalog'},token)
        assert status==403,status
        pools=json.loads(kubectl('-n',ns,'get','workerpools.ate.dev','-o','json'))['items']
        workers=json.loads(kubectl('-n',ns,'get','pods','-l','ate.dev/worker-pool=agent-workers','-o','json'))['items']
        gateways=[d for d in json.loads(kubectl('-n',ns,'get','deploy','-o','json'))['items'] if d['spec']['template']['metadata'].get('labels',{}).get('app')=='agent-egress']
        assert len(pools)==1 and len(workers)==1 and len(gateways)==1,(len(pools),len(workers),len(gateways))
        for name in [AGENT,second]:
            agent=json.loads(kubectl('-n',ns,'get','agent',name,'-o','json'))
            harness=json.loads(kubectl('-n',ns,'get','harness',agent['spec']['harnessRef']['name'],'-o','json'))
            assert harness['spec']['substrate']['workerPoolRef']['name']=='agent-workers'
        report={'timestamp':datetime.datetime.now(datetime.timezone.utc).isoformat(),'projectNamespace':ns,'workerPools':1,'workerPods':1,'proxyDeployments':len(gateways),'otherProjectRejected':True,'agents':checks}
        (ROOT/'artifacts/project-sharing.json').write_text(json.dumps(report,indent=2)+'\n')
        print('PASS: two Agents share one worker pool and project proxy; identity, HTTP grants and project isolation preserved',flush=True)
    finally:
        for sid in sessions:
            for _ in range(15):
                status,_=request(18543,f'/v1/sessions/{sid}',token=token,method='DELETE')
                if status==200:break
                if status!=409:raise RuntimeError('Session cleanup failed')
                time.sleep(1)
        for f in forwards:f.close()
if __name__=='__main__':main()
