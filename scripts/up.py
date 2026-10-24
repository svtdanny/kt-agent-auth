#!/usr/bin/env python3
"""Install released upstream binaries and deploy the demo on local Minikube."""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import yaml

ROOT=Path(__file__).resolve().parents[1]
CACHE=ROOT/'.cache'
VERSION='1.0.0-alpha17'
BASE='docker.io/kicbase/stable:v0.0.51@sha256:4a1c825b61479e6c898851ea66f13c620aaeab6002746e95067fc2c4b38a0b24'
os.chdir(ROOT)
CACHE.mkdir(exist_ok=True)
(CACHE/'bin').mkdir(exist_ok=True)
os.environ['PATH']=str(CACHE/'bin')+':'+os.environ['PATH']
os.environ['KUBECONFIG']=str(CACHE/'kubeconfig')

def run(*args,**kwargs):return subprocess.run(args,check=True,**kwargs)

def main():
    for name in ('docker','minikube','helm','kubectl','go','npm','openssl'):
        if not shutil.which(name):raise SystemExit('Required executable missing: '+name)
    real_helm=str(CACHE/'bin/helm3')
    if not Path(real_helm).exists():
        archive=CACHE/'helm3.tar.gz'
        run('curl','-fLsS','--retry','3','https://get.helm.sh/helm-v3.22.1-linux-amd64.tar.gz','-o',str(archive))
        with tarfile.open(archive) as package:
            Path(real_helm).write_bytes(package.extractfile('linux-amd64/helm').read())
        Path(real_helm).chmod(0o755)
    # systemd in a second local Kubernetes node needs more than the default 128 inotify instances.
    if int(Path('/proc/sys/fs/inotify/max_user_instances').read_text())<1024:
        run('docker','run','--rm','--privileged','--entrypoint','sh',BASE,'-c','echo 8192 > /proc/sys/fs/inotify/max_user_instances')
    run('minikube','start','-p','auth-demo','--driver=docker','--base-image='+BASE,'--container-runtime=containerd',
        '--kubernetes-version=v1.37.0','--cpus=8','--memory=16384','--keep-context','--cache-images=false',
        '--feature-gates=ClusterTrustBundle=true,ClusterTrustBundleProjection=true,PodCertificateRequest=true',
        '--extra-config=apiserver.runtime-config=certificates.k8s.io/v1beta1=true')
    run('kubectl','config','use-context','auth-demo')
    # A registry is needed only for Substrate's sandbox image cache.
    existing=subprocess.run(['docker','inspect','kind-registry'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    if existing.returncode:run('docker','run','-d','--name','kind-registry','-p','127.0.0.1:5001:5000','registry:3')
    from deploy import certificates,tls_secret,apply,obj
    apply([obj('Namespace','demo'),{'apiVersion':'v1','kind':'Namespace','metadata':{'name':'ate-system'}}])
    directory=certificates()
    apply([tls_secret('auth-gateway-client','agentgateway',directory,'ate-system')])
    (CACHE/'bin/helm').unlink(missing_ok=True)
    (CACHE/'bin/helm').symlink_to(ROOT/'scripts/helm-wrapper.py')
    (ROOT/'scripts/helm-wrapper.py').chmod(0o755)
    (ROOT/'scripts/helm-post-renderer.py').chmod(0o755)
    os.environ['AUTH_DEMO_REAL_HELM']=real_helm
    binary=CACHE/'bin/kagent'
    url=f'https://github.com/kagent-dev/kagent/releases/download/v{VERSION}/kagent-linux-amd64'
    if not binary.exists():run('curl','-fLsS','--retry','3',url,'-o',str(binary));binary.chmod(0o755)
    checksum=CACHE/'bin/kagent.sha256'
    if not checksum.exists():run('curl','-fLsS','--retry','3',url+'.sha256','-o',str(checksum))
    if hashlib.sha256(binary.read_bytes()).hexdigest()!=checksum.read_text().split()[0]:raise SystemExit('kagent binary checksum mismatch')
    config={'install':{'defaultModelProvider':'Ollama',
        'kagent':{'version':VERSION,'valuesFiles':[str(ROOT/'deploy/kagent-values.yaml')]},
        'substrate':{'version':'0.6.0-alpha3','valuesFiles':[str(ROOT/'deploy/substrate-values.yaml')]}}}
    (CACHE/'install.yaml').write_text(yaml.safe_dump(config))
    for attempt in range(3):
        try:
            run(str(binary),'--config',str(CACHE/'install.yaml'),'install','--profile','minimal',env={**os.environ,'OLLAMA_API_KEY':'unused-scripted-demo'})
            break
        except subprocess.CalledProcessError:
            if attempt==2:raise
            print('Retrying the idempotent upstream install after its timeout; image pulls may still be completing.',flush=True)
    run('python3','scripts/build.py')
    run('minikube','-p','auth-demo','image','load','--daemon',*[f'localhost:5001/auth-demo/{name}:demo' for name in ('control-plane','ext-proc','test-api','import-bbolt')])
    run('python3','scripts/deploy.py')
    run('python3','scripts/permissions_demo.py')

if __name__=='__main__':main()
