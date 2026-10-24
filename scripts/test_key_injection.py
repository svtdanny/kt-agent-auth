#!/usr/bin/env python3
"""Test real gateway credential insertion against a mock bearer-authenticated API."""
from datetime import datetime, timezone
import json
import secrets
import subprocess
import time
import uuid

import test_e2e as demo
from vault_demo import Admin


def patch_secret(value):
    with Admin() as vault:vault.write('read-token',value)
    demo.invalidate_credential('read-token')


def main():
    assert demo.kubectl('config','current-context').strip()=='auth-demo'
    started_at=datetime.now(timezone.utc).isoformat()
    handles=[]
    sessions=[]
    results=[]
    observed=[]
    token=None
    original=None
    altered=False

    def record(name,**evidence):
        results.append({'test':name,'passed':True,**evidence})
        print('PASS:',name,flush=True)

    def tool(session,method='GET',path='/catalog',headers=None):
        result=demo.invoke(token,session,method,path,headers)
        observed.append(result)
        return result

    def accepted(result,identity):
        assert result['status']==200,'upstream rejected an injected key'
        event=json.loads(result['body'])
        assert event['credentialAccepted'] and event['credential']==identity,'wrong key identity reached upstream'
        assert event['authorizationHeaderCount']==1,'gateway did not replace Authorization with exactly one value'

    try:
        for service,ports in [('auth-control-plane','18443:8443'),('catalog-api','18444:443')]:
            log=open(demo.CACHE/('key-test-portforward-'+service+'.log'),'w')
            handles.append(subprocess.Popen(['kubectl','--context','auth-demo','-n','demo','port-forward',
                                             'service/'+service,ports],env=demo.ENV,stdout=log,stderr=log))
        for _ in range(60):
            if any(p.poll() is not None for p in handles):raise AssertionError('port-forward stopped')
            try:
                if demo.request(18443,'/readyz')[0]==200 and demo.request(18444,'/audit')[0]==200:break
            except (OSError,ConnectionError):pass
            time.sleep(1)
        else:raise AssertionError('demo services did not become reachable')

        with Admin() as vault:
            original=vault.read('read-token')
            writer_key=vault.read('write-token')
        assert original and writer_key and original!=writer_key,'mock requires two distinct configured keys'
        wrong_key='invalid-test-key-'+secrets.token_urlsafe(24)

        status,event=demo.request(18444,'/catalog')
        assert status==401 and not event['credentialAccepted'],'mock accepted an absent key'
        record('mock API rejects a missing key',httpStatus=status)
        status,event=demo.request(18444,'/catalog',token=wrong_key)
        assert status==401 and not event['credentialAccepted'],'mock accepted an invalid key'
        record('mock API rejects an incorrect key',httpStatus=status)
        for key,identity in [(original,'reader'),(writer_key,'writer')]:
            status,event=demo.request(18444,'/catalog',token=key)
            assert status==200 and event['credential']==identity,'mock key and Kubernetes Secret differ'
        record('mock accepts the exact reader and writer keys stored in Vault')

        # Exercise the real Vault role, rather than only a mock ACL response.
        jwt=demo.kubectl('-n','demo','create','token','auth-control-plane','--audience=vault','--duration=10m').strip()
        with Admin() as vault:
            status,login=vault.request('POST','auth/kubernetes/login',{'role':'auth-bridge','jwt':jwt},token='')
            assert status==200,'bridge Kubernetes identity could not authenticate to Vault'
            scoped=login['auth']['client_token']
            status,_=vault.request('GET','kv/data/auth-demo/demo/mock/reader',token=scoped)
            assert status==403,'bridge Vault role read mock service private credentials'
            status,_=vault.request('POST','kv/data/auth-demo/demo/read-token',{'data':{'token':'must-not-be-written'}},token=scoped)
            assert status==403,'bridge Vault role can write application credentials'
        record('Vault Kubernetes role cannot read mock-private keys or write application keys')

        token=demo.kubectl('-n','demo','create','token','test-user','--audience=auth-demo','--duration=1h').strip()
        run=uuid.uuid4().hex
        for account in ['catalog-reader','catalog-writer']:
            status,binding=demo.request(18443,'/v1/sessions',{
                'agent':{'namespace':'demo','name':'mastra-http'},'serviceAccount':account,
                'requestId':'key-injection-'+run+'-'+account},token=token)
            assert status==200,'could not create a native test Session'
            sessions.append(binding['sessionId'])
        reader,writer=sessions
        accepted(tool(reader),'reader')
        record('gateway replaces the Mastra placeholder with the reader key',authorizationHeaderCount=1)
        accepted(tool(reader,headers={'Authorization':'Bearer '+wrong_key}),'reader')
        record('gateway overwrites an agent-supplied incorrect Authorization header',authorizationHeaderCount=1)
        accepted(tool(writer,'POST','/orders'),'writer')
        record('same Mastra Agent receives the writer key under the writer ServiceAccount')

        before=len(demo.audit())
        denied=tool(reader,'POST','/orders',{'Authorization':'Bearer '+wrong_key,'X-Service-Account':'catalog-writer'})
        assert denied['status']==403 and len(demo.audit())==before,'denied request reached upstream'
        record('forged account and Authorization headers cannot obtain writer access',upstreamRequests=0)

        # The mock keeps its own valid key. Changing only the bridge Vault path must
        # produce an upstream 401, proving the actual header value was replaced.
        altered=True
        patch_secret(wrong_key)
        demo.wait_until(lambda:demo.credential_is(reader,wrong_key))
        before=len(demo.audit())
        rejected=tool(reader)
        event=json.loads(rejected['body'])
        assert rejected['status']==401 and not event['credentialAccepted'],'new Secret was not used on the real HTTP path'
        assert event['authorizationHeaderCount']==1 and len(demo.audit())==before+1
        record('Vault update and cache invalidation change the injected key end to end',httpStatus=401,upstreamRequests=1)

        patch_secret(original)
        altered=False
        demo.wait_until(lambda:demo.credential_is(reader,original))
        accepted(tool(reader),'reader')
        record('restoring the valid Vault key restores HTTP success without restarting bridge or processor',httpStatus=200)

        harness=json.loads(demo.kubectl('-n','demo','get','harness.api.kagent.dev','mastra','-o','json'))
        public=json.dumps({'responses':observed,'audit':demo.audit(),'harness':harness,'evidence':results})
        for key in [original,writer_key,wrong_key]:
            assert key not in public,'key appeared in a response, audit, Harness configuration, or evidence'
        assert not any('valueFrom' in entry for entry in harness['spec'].get('env',[])), 'Harness unexpectedly receives secret environment references'
        record('service keys are absent from agent responses, mock audit, and Harness configuration')

        for session in sessions:demo.delete_session(token,session)
        sessions=[]
        report={'cluster':'auth-demo','startedAtUTC':started_at,'finishedAtUTC':datetime.now(timezone.utc).isoformat(),
                'path':'real Mastra → atunnel → agentgateway → ext_proc → bridge → mock HTTPS API',
                'authentication':'Vault KV v2 + Kubernetes auth; Authorization: Bearer <synthetic opaque key>',
                'tests':results,'completed':True,'credentialsRestored':True}
        (demo.ROOT/'artifacts/key-injection-results.json').write_text(json.dumps(report,indent=2)+'\n')
        print(f'{len(results)} key-injection checks passed on local Minikube.',flush=True)
    finally:
        if altered and original is not None:
            patch_secret(original)
        for session in sessions:
            try:demo.delete_session(token,session)
            except Exception:print('Native test Session cleanup failed',flush=True)
        demo.close_connections()
        for handle in handles:
            handle.terminate()
            try:handle.wait(timeout=10)
            except subprocess.TimeoutExpired:handle.kill()


if __name__=='__main__':main()
