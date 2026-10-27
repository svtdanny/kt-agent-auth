#!/usr/bin/env python3
"""Quick fresh-Session check, including after retiring the old shared demo."""
import datetime,json,uuid,time
from test_e2e import Forward,request,wait_for,invoke
from local import ROOT,kubectl
from deploy import SCOPES,AGENT,INTERNAL,EXTERNAL

def main():
    checks=[]
    for i,ns in enumerate(SCOPES):
        f=Forward(ns,'authorizer',18543+i);sid=None
        token=kubectl('-n',ns,'create','token','test-user','--audience=auth-demo','--duration=15m').strip()
        try:
            wait_for(lambda:request(f.port,'/readyz')[0]==200,seconds=15)
            status,b=request(f.port,'/v1/sessions',{'agent':{'namespace':ns,'name':AGENT},'serviceAccount':'agent-caller','requestId':uuid.uuid4().hex},token)
            assert status==200,(status,b);sid=b['sessionId']
            for origin,path in [(INTERNAL,'/catalog'),(EXTERNAL,'/external')]:
                result=invoke(f.port,token,sid,origin=origin,path=path)
                assert result['status']==200 and json.loads(result['body'])['credential']==('reader' if i==0 else 'writer'),result
                checks.append({'namespace':ns,'origin':origin,'passed':True})
            assert invoke(f.port,token,sid,path='/private')['status']==403
            checks.append({'namespace':ns,'deniedPath':'/private','passed':True})
        finally:
            if sid:
                for _ in range(15):
                    status,_=request(f.port,f'/v1/sessions/{sid}',token=token,method='DELETE')
                    if status==200:break
                    if status!=409:raise RuntimeError('Session cleanup failed')
                    time.sleep(1)
            f.close()
    report={'timestamp':datetime.datetime.now(datetime.timezone.utc).isoformat(),'cluster':'auth-demo','tests':checks}
    (ROOT/'artifacts/dedicated-smoke.json').write_text(json.dumps(report,indent=2)+'\n')
    print('PASS: fresh native Sessions use dedicated gateways for internal/external traffic; ungranted paths denied',flush=True)
if __name__=='__main__':main()
