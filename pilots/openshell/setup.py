#!/usr/bin/env python3
"""Create isolated Vault, authentication mocks, and OpenShell configuration."""
import http.client
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
import time

import yaml
from common import CACHE, ROOT, ENV, CONTEXT, obj, secret, kube, apply, forward, terminate


class Vault:
    def __init__(self):
        self.token = None
        self.context = ssl.create_default_context(cafile=str(ROOT/'.cache/tls/ca.crt'))

    def request(self, method, path, body=None):
        connection = http.client.HTTPSConnection('localhost', 18500, context=self.context, timeout=10)
        try:
            headers = {'Content-Type': 'application/json'}
            if self.token:
                headers['X-Vault-Token'] = self.token
            connection.request(method, '/v1/'+path, None if body is None else json.dumps(body), headers)
            response = connection.getresponse()
            raw = response.read()
            data = json.loads(raw) if raw else {}
            if response.status >= 300:
                raise RuntimeError('pilot Vault '+path+' HTTP '+str(response.status))
            return data
        finally:
            connection.close()


def deploy(name, container, volumes=None):
    return obj('Deployment', name, spec={'replicas': 1, 'strategy': {'type': 'Recreate'},
        'selector': {'matchLabels': {'app': name}}, 'template': {'metadata': {'labels': {'app': name}},
        'spec': {'containers': [container], 'volumes': volumes or []}}})


def service(name, port, target=None, namespace='demo'):
    return obj('Service', name, namespace, spec={'selector': {'app': name}, 'ports': [{'port': port, 'targetPort': target or port}]})


def main():
    CACHE.mkdir(exist_ok=True)
    assert any(c['name'] == CONTEXT for c in json.loads(kube('config', 'view', '-o', 'json'))['contexts'])
    tls = ROOT/'.cache/tls'
    config = {'disable_mlock': True, 'storage': {'file': {'path': '/vault/data'}},
              'listener': {'tcp': {'address': '0.0.0.0:8200', 'tls_cert_file': '/tls/tls.crt', 'tls_key_file': '/tls/tls.key'}}}
    vault = deploy('auth-vault', {'name': 'vault', 'image': 'hashicorp/vault:1.21.4', 'command': ['vault'],
        'args': ['server', '-config=/config/vault.json'], 'env': [{'name': 'SKIP_SETCAP', 'value': 'true'}],
        'resources': {'requests': {'cpu': '50m', 'memory': '128Mi'}, 'limits': {'cpu': '1', 'memory': '512Mi'}},
        'volumeMounts': [{'name': n, 'mountPath': p} for n, p in [('tls', '/tls'), ('config', '/config'), ('data', '/vault/data')]]},
        [{'name': 'tls', 'secret': {'secretName': 'auth-vault-tls'}}, {'name': 'config', 'configMap': {'name': 'pilot-vault'}},
         {'name': 'data', 'persistentVolumeClaim': {'claimName': 'pilot-vault'}}])
    vault['spec']['template']['spec'].update({'serviceAccountName': 'auth-vault',
                                             'securityContext': {'runAsUser': 100, 'runAsGroup': 1000, 'fsGroup': 1000}})
    apply([obj('Namespace', 'demo', None), obj('Namespace', 'test-services', None), obj('Namespace', 'openshell', None), obj('ServiceAccount', 'auth-vault'),
        obj('LimitRange', 'pilot-container-budget', 'openshell', spec={'limits': [{'type': 'Container',
            'default': {'cpu': '1', 'memory': '512Mi'}, 'defaultRequest': {'cpu': '50m', 'memory': '64Mi'}}]}),
        {'apiVersion': 'rbac.authorization.k8s.io/v1', 'kind': 'ClusterRoleBinding', 'metadata': {'name': 'pilot-vault-reviewer'},
         'subjects': [{'kind': 'ServiceAccount', 'name': 'auth-vault', 'namespace': 'demo'}],
         'roleRef': {'apiGroup': 'rbac.authorization.k8s.io', 'kind': 'ClusterRole', 'name': 'system:auth-delegator'}},
        secret('auth-vault-tls', {'tls.crt': (tls/'auth-vault.crt').read_bytes(), 'tls.key': (tls/'auth-vault.key').read_bytes()}),
        obj('ConfigMap', 'pilot-vault', data={'vault.json': json.dumps(config)}),
        obj('PersistentVolumeClaim', 'pilot-vault', spec={'accessModes': ['ReadWriteOnce'], 'resources': {'requests': {'storage': '1Gi'}}}),
        vault, service('auth-vault', 8200)])
    kube('-n', 'demo', 'rollout', 'status', 'deployment/auth-vault', '--timeout=180s')
    handle = forward('demo', 'service/auth-vault', '18500:8200')
    try:
        admin = Vault()
        for _ in range(60):
            try:
                state = admin.request('GET', 'sys/seal-status')
                break
            except OSError:
                time.sleep(1)
        else:
            raise TimeoutError('pilot Vault unreachable')
        path = CACHE/'vault-admin.json'
        if not state['initialized']:
            material = admin.request('POST', 'sys/init', {'secret_shares': 1, 'secret_threshold': 1})
            with os.fdopen(os.open(path, os.O_WRONLY|os.O_CREAT|os.O_TRUNC, 0o600), 'w') as out:
                json.dump(material, out)
        material = json.loads(path.read_text())
        admin.token = material['root_token']
        if state['sealed']:
            admin.request('POST', 'sys/unseal', {'key': material['keys_base64'][0]})
        if 'secret/' not in admin.request('GET', 'sys/mounts'):
            admin.request('POST', 'sys/mounts/secret', {'type': 'kv', 'options': {'version': '2'}})
        if 'kubernetes/' not in admin.request('GET', 'sys/auth'):
            admin.request('POST', 'sys/auth/kubernetes', {'type': 'kubernetes'})
        ca = json.loads(kube('-n', 'demo', 'get', 'configmap', 'kube-root-ca.crt', '-o', 'json'))['data']['ca.crt']
        admin.request('POST', 'auth/kubernetes/config', {'kubernetes_host': 'https://kubernetes.default.svc:443', 'kubernetes_ca_cert': ca})
        admin.request('PUT', 'sys/policies/acl/openshell-pilot', {'policy': '\n'.join(
            'path "secret/'+p+'/openshell/provider-credentials/*" { capabilities = ["create", "read", "update", "delete", "list"] }'
            for p in ('data', 'metadata'))})
        admin.request('POST', 'auth/kubernetes/role/openshell-pilot', {'bound_service_account_names': ['openshell'],
            'bound_service_account_namespaces': ['openshell'], 'token_policies': ['openshell-pilot'], 'token_ttl': '1h'})
    finally:
        terminate(handle)

    # Expected synthetic fixture keys are private test data. OpenShell provider
    # credentials themselves live exclusively in the pilot Vault driver.
    sys.path.insert(0, str(ROOT/'scripts'))
    from vault_demo import Admin
    with Admin() as original:
        mcp_keys = {'reader': original.read('mock/mcp-reader'), 'writer': original.read('mock/mcp-writer')}
    mock = deploy('mock-mcp', {'name': 'api', 'image': 'localhost:5001/auth-demo/test-api:demo', 'imagePullPolicy': 'Never',
        'env': [{'name': 'MOCK_PLAINTEXT', 'value': 'true'}, {'name': 'MOCK_KEYS_FILE', 'value': '/fixture/keys.json'}],
        'resources': {'requests': {'cpu': '50m', 'memory': '64Mi'}, 'limits': {'cpu': '1', 'memory': '512Mi'}},
        'volumeMounts': [{'name': 'keys', 'mountPath': '/fixture', 'readOnly': True}]},
        [{'name': 'keys', 'secret': {'secretName': 'mock-expected-keys'}}])
    mock['metadata']['namespace'] = 'test-services'
    apply([secret('mock-expected-keys', {'keys.json': json.dumps(mcp_keys)}, 'test-services'),
        mock, service('mock-mcp', 8080, namespace='test-services'),
        obj('ConfigMap', 'pilot-private-ca', 'openshell', data={'ca.crt': (tls/'ca.crt').read_text()})])
    kube('-n', 'test-services', 'rollout', 'status', 'deployment/mock-mcp', '--timeout=120s')
    name = 'openshell-pilot-external-api'
    found = subprocess.run(['docker', 'inspect', name], capture_output=True, text=True)
    if found.returncode:
        subprocess.run(['docker', 'run', '-d', '--name', name, '--network', CONTEXT, '--user', f'{os.getuid()}:{os.getgid()}',
            '-p', '127.0.0.1:18548:8443', '--mount', 'type=bind,src='+str(ROOT/'.cache/external-api')+',dst=/tls,readonly',
            '-e', 'MOCK_KEYS_FILE=/tls/keys.json', 'localhost:5001/auth-demo/test-api:demo'], check=True, stdout=subprocess.DEVNULL)
    container = json.loads(subprocess.check_output(['docker', 'inspect', name], text=True))[0]
    ip = container['NetworkSettings']['Networks'][CONTEXT]['IPAddress']
    dns = json.loads(kube('-n', 'kube-system', 'get', 'configmap', 'coredns', '-o', 'json'))
    core = dns['data']['Corefile']
    if 'external-api.auth-demo.test' not in core:
        if 'hosts {' in core:
            at = core.index('hosts {')+len('hosts {')
            core = core[:at]+'\n        '+ip+' external-api.auth-demo.test\n'+core[at:]
        else:
            at = core.index('{')+1
            core = core[:at]+'\n    hosts {\n        '+ip+' external-api.auth-demo.test\n        fallthrough\n    }\n'+core[at:]
        kube('-n', 'kube-system', 'patch', 'configmap', 'coredns', '--type=merge', '-p', json.dumps({'data': {'Corefile': core}}))
        kube('-n', 'kube-system', 'rollout', 'restart', 'deployment/coredns')
        kube('-n', 'kube-system', 'rollout', 'status', 'deployment/coredns', '--timeout=120s')
    values = yaml.safe_load((ROOT/'pilots/openshell/values.yaml').read_text())
    values['server'] = {'credentialDrivers': {'vault': {'enabled': True, 'address': 'https://auth-vault.demo.svc.cluster.local:8200',
        'caConfigMapName': 'pilot-private-ca', 'mount': 'secret', 'kvVersion': '2', 'authMethod': 'kubernetes', 'role': 'openshell-pilot'}}}
    (CACHE/'values.yaml').write_text(yaml.safe_dump(values, sort_keys=False))
    subprocess.run([str(ROOT/'.cache/bin/helm3'), 'upgrade', '--install', 'openshell', str(CACHE/'helm-chart-0.1.5.tgz'),
        '--kube-context', CONTEXT, '--namespace', 'openshell', '-f', str(CACHE/'values.yaml'), '--wait', '--timeout', '5m'], env=ENV, check=True)
    (CACHE/'fixture.json').write_text(json.dumps({'externalIp': ip, 'externalContainer': name, 'cluster': CONTEXT}, indent=2)+'\n')
    print('Pilot Vault, internal MCP, external HTTPS, and gateway configured.', flush=True)


if __name__ == '__main__':
    main()
