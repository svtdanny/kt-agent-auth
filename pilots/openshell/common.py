"""All pilot state and Kubernetes operations are isolated from auth-demo."""
import base64
import json
import os
from pathlib import Path
import subprocess
import time

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT/'.cache/openshell-pilot'
CONTEXT = 'openshell-pilot'
ENV = {**os.environ, 'KUBECONFIG': str(ROOT/'.cache/openshell-kubeconfig'),
       'XDG_CONFIG_HOME': str(CACHE/'config'), 'XDG_STATE_HOME': str(CACHE/'state'),
       'OPENSHELL_GATEWAY': 'pilot', 'OPENSHELL_COLOR': 'never'}
CLI = str(CACHE/'openshell')


def run(args, **kwargs):
    return subprocess.run(args, env=ENV, text=True, check=True, **kwargs)


def kube(*args, input=None):
    return subprocess.check_output(['kubectl', '--context', CONTEXT, *args], env=ENV, text=True, input=input)


def apply(items):
    kube('apply', '-f', '-', input=json.dumps({'apiVersion': 'v1', 'kind': 'List', 'items': items}))


def obj(kind, name, namespace='demo', **fields):
    return {'apiVersion': 'apps/v1' if kind == 'Deployment' else 'v1', 'kind': kind,
            'metadata': {'name': name, **({'namespace': namespace} if namespace else {})}, **fields}


def secret(name, data, namespace='demo'):
    return obj('Secret', name, namespace, data={k: base64.b64encode(v if isinstance(v, bytes) else v.encode()).decode() for k, v in data.items()})


def forward(namespace, target, ports):
    with open(CACHE/('forward-'+ports.split(':')[0]+'.log'), 'w') as log:
        return subprocess.Popen(['kubectl', '--context', CONTEXT, '-n', namespace, 'port-forward', target, ports], env=ENV, stdout=log, stderr=log)


def terminate(handle):
    handle.terminate()
    try:
        handle.wait(timeout=10)
    except subprocess.TimeoutExpired:
        handle.kill()
        handle.wait()


def cli(*args, **kwargs):
    kwargs.setdefault('timeout', 240)
    return subprocess.check_output([CLI, *args], env=ENV, text=True, **kwargs)


def wait(check, seconds=120):
    for _ in range(seconds):
        if check():
            return
        time.sleep(1)
    raise TimeoutError('pilot condition did not become ready')
