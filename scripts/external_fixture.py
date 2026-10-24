"""A real TLS/authentication mock in Docker, outside the Kubernetes cluster."""
import json
import os
import subprocess

from database import ROOT,kubectl
from shared_gateway import EXTERNAL_HOST

NAME='auth-demo-external-api'


def install():
    from deploy import certificates
    from vault_demo import Admin
    directory=certificates()
    target=ROOT/'.cache/external-api';target.mkdir(exist_ok=True,mode=0o700)
    if not (target/'tls.crt').exists():
        (target/'cert.ext').write_text('extendedKeyUsage=serverAuth\nsubjectAltName=DNS:'+EXTERNAL_HOST+',DNS:localhost\n')
        subprocess.run(['openssl','req','-newkey','rsa:2048','-nodes','-subj','/CN='+EXTERNAL_HOST,
            '-keyout',str(target/'tls.key'),'-out',str(target/'cert.csr')],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        subprocess.run(['openssl','x509','-req','-in',str(target/'cert.csr'),'-CA',str(directory/'ca.crt'),
            '-CAkey',str(directory/'ca.key'),'-CAcreateserial','-days','7','-extfile',str(target/'cert.ext'),'-out',str(target/'tls.crt')],
            check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        (target/'tls.key').chmod(0o600)
    with Admin() as admin:
        keys={'reader':admin.read('mock/external-reader'),'writer':admin.read('mock/external-writer')}
    fd=os.open(target/'keys.json',os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w') as handle:json.dump(keys,handle)
    node=json.loads(subprocess.check_output(['docker','inspect','auth-demo'],text=True))[0]
    network=next(iter(node['NetworkSettings']['Networks']))
    subprocess.run(['docker','rm','-f',NAME],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    subprocess.run(['docker','run','-d','--name',NAME,'--network',network,'--user',str(os.getuid())+':'+str(os.getgid()),
        '-p','127.0.0.1:18448:8443','--mount','type=bind,src='+str(target)+',dst=/tls,readonly',
        '-e','MOCK_KEYS_FILE=/tls/keys.json','localhost:5001/auth-demo/test-api:demo'],check=True,stdout=subprocess.DEVNULL)
    container=json.loads(subprocess.check_output(['docker','inspect',NAME],text=True))[0]
    ip=container['NetworkSettings']['Networks'][network]['IPAddress']
    dns=json.loads(kubectl('-n','kube-system','get','configmap','coredns','-o','json'))
    core=dns['data']['Corefile']
    begin='# auth-demo external fixture BEGIN';end='# auth-demo external fixture END'
    if begin in core:
        start=core.index(begin);finish=core.index(end,start)+len(end)
        core=core[:start]+core[finish:]
    record=begin+'\n       '+ip+' '+EXTERNAL_HOST+'\n       '+end+'\n'
    if 'hosts {' in core:
        index=core.index('hosts {')+len('hosts {')
        core=core[:index]+'\n       '+record+core[index:]
    else:
        index=core.index('{')+1
        core=core[:index]+'\n    hosts {\n       '+record+'       fallthrough\n    }\n'+core[index:]
    kubectl('-n','kube-system','patch','configmap','coredns','--type=merge','-p',json.dumps({'data':{'Corefile':core}}))
    kubectl('-n','kube-system','rollout','restart','deployment/coredns')
    kubectl('-n','kube-system','rollout','status','deployment/coredns','--timeout=120s')
    metadata={'hostname':EXTERNAL_HOST,'ip':ip,'dockerContainer':NAME,'dockerNetwork':network,
        'location':'Docker container outside Kubernetes; no Kubernetes Service/Endpoint'}
    (ROOT/'.cache/external-fixture.json').write_text(json.dumps(metadata,indent=2)+'\n')
    print('External HTTPS mock:',EXTERNAL_HOST,ip,flush=True)
    return metadata


if __name__=='__main__':install()
