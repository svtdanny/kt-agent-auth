#!/usr/bin/env python3
"""Reproduce the OpenShell pilot without modifying the auth-demo cluster."""
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import time
import urllib.request

from common import CACHE, ROOT, ENV, CONTEXT, kube, cli, forward, terminate

VERSION = '0.1.5'
AGENT_SANDBOX = 'v1.0.8'
CLI_SHA256 = 'ca1ad6e6fbb4a6cd407e95763391b8ecff02912e7d9c467e7ca22696fe80af69'
BASE = 'docker.io/kicbase/stable:v0.0.51@sha256:4a1c825b61479e6c898851ea66f13c620aaeab6002746e95067fc2c4b38a0b24'


def download(url, destination):
    if not destination.exists():
        with urllib.request.urlopen(url, timeout=90) as response:
            destination.write_bytes(response.read())


def main():
    os.chdir(ROOT)
    CACHE.mkdir(parents=True, exist_ok=True)
    minikube = shutil.which('minikube') or str(ROOT/'.cache/bin/minikube')
    subprocess.run([minikube, 'start', '-p', CONTEXT, '--driver=docker', '--base-image='+BASE,
        '--container-runtime=containerd', '--kubernetes-version=v1.37.0', '--cpus=8', '--memory=16384',
        '--cni=calico', '--keep-context', '--cache-images=false'], env=ENV, check=True)
    manifest = CACHE/('agent-sandbox-'+AGENT_SANDBOX+'.yaml')
    download('https://github.com/kubernetes-sigs/agent-sandbox/releases/download/'+AGENT_SANDBOX+'/sandbox.yaml', manifest)
    kube('apply', '-f', str(manifest))
    archive = CACHE/'openshell-x86_64-unknown-linux-musl.tar.gz'
    download('https://github.com/NVIDIA/OpenShell/releases/download/v'+VERSION+'/'+archive.name, archive)
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == CLI_SHA256, 'OpenShell CLI checksum mismatch'
    with tarfile.open(archive) as package:
        member = next(m for m in package.getmembers() if m.isfile() and Path(m.name).name == 'openshell')
        binary = CACHE/'openshell'
        binary.write_bytes(package.extractfile(member).read())
        binary.chmod(0o755)
    helm = ROOT/'.cache/bin/helm3'
    if not (CACHE/('helm-chart-'+VERSION+'.tgz')).exists():
        subprocess.run([str(helm), 'pull', 'oci://ghcr.io/nvidia/openshell/helm-chart', '--version', VERSION, '--destination', str(CACHE)], env=ENV, check=True)
    context = CACHE/'supervisor-context'
    context.mkdir(exist_ok=True)
    (context/'upstream-ca.pem').write_bytes(Path('/etc/ssl/certs/ca-certificates.crt').read_bytes()+(ROOT/'.cache/tls/ca.crt').read_bytes())
    for dockerfile, image, build_context in [('Dockerfile', 'openshell-mastra', ROOT), ('Dockerfile.supervisor', 'openshell-supervisor', context)]:
        subprocess.run(['docker', 'build', '-f', str(ROOT/'pilots/openshell'/dockerfile), '-t', 'localhost:5001/auth-demo/'+image+':demo', str(build_context)], check=True)
    subprocess.run(['docker', 'push', 'localhost:5001/auth-demo/openshell-mastra:demo'], check=True)
    subprocess.run([minikube, '-p', CONTEXT, 'image', 'load', '--daemon', *[
        'localhost:5001/auth-demo/'+name+':demo' for name in ('openshell-mastra', 'openshell-supervisor', 'test-api')], 'hashicorp/vault:1.21.4'], env=ENV, check=True)
    subprocess.run([minikube, '-p', 'auth-demo', 'image', 'load', '--daemon', 'localhost:5001/auth-demo/openshell-mastra:demo'], check=True)
    subprocess.run(['go', 'build', '-trimpath', '-ldflags=-s -w', '-o', '.cache/build/kagent-lifecycle', './pilots/kagent-lifecycle'], env={**ENV, 'CGO_ENABLED': '0'}, check=True)
    subprocess.run(['docker', 'build', '-f', 'deploy/Dockerfile.services', '--build-arg', 'BINARY=kagent-lifecycle', '-t', 'localhost:5001/auth-demo/kagent-lifecycle:demo', '.'], check=True)
    subprocess.run([minikube, '-p', 'auth-demo', 'image', 'load', '--daemon', 'localhost:5001/auth-demo/kagent-lifecycle:demo'], check=True)
    subprocess.run(['python3', 'pilots/openshell/setup.py'], env=ENV, check=True)
    secret = json.loads(kube('-n', 'openshell', 'get', 'secret', 'openshell-client-tls', '-o', 'json'))
    directory = CACHE/'config/openshell/gateways/pilot/mtls'
    directory.mkdir(parents=True, exist_ok=True)
    for key, value in secret['data'].items():
        path = directory/key
        with os.fdopen(os.open(path, os.O_WRONLY|os.O_CREAT|os.O_TRUNC, 0o600), 'wb') as out:
            out.write(base64.b64decode(value))
    handle = forward('openshell', 'service/openshell', '18551:8080')
    try:
        time.sleep(1)
        if not (directory.parent/'metadata.json').exists():
            cli('gateway', 'add', 'https://localhost:18551', '--local', '--name', 'pilot')
        cli('status')
    finally:
        terminate(handle)
    subprocess.run(['python3', 'pilots/openshell/configure.py'], env=ENV, check=True)
    print('OpenShell pilot ready; auth-demo remains on its original shared Envoy path.', flush=True)


if __name__ == '__main__':
    main()
