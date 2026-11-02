#!/usr/bin/env python3
"""Light live test of hardcoded Envoy atespace -> namespace authorizer routing.

Temporarily uses the stock Substrate API and one shared Envoy. Restores the
previous API template and removes the shared test resources on exit.
"""
import argparse
import concurrent.futures
import copy
import datetime
import hashlib
import json
import subprocess
import time
import uuid
import urllib.request
import yaml

from local import ROOT, CACHE, ENV, kubectl, apply
from deploy import AGENT, SCOPES, INTERNAL, EXTERNAL
from shared_egress import AUTHORIZERS, FILTER, bootstrap, authorizer_service
from test_e2e import Forward, request, payload, wait_for

NAME = 'shared-egress-mvp'
NS = 'ate-system'
STOCK_API = 'ghcr.io/kagent-dev/substrate/ateapi:v0.6.0-alpha3'
STOCK_ATENET = 'localhost:5001/auth-demo/substrate-atenet-stock:shared-mvp'
REPORT = ROOT / 'artifacts/shared-egress-mvp.json'


def run(args, **kwargs):
    return subprocess.run(args, cwd=ROOT, env=ENV, check=True, text=True, **kwargs)


def build():
    info = json.loads(subprocess.check_output(['go', 'list', '-m', '-json', 'go.ate.dev/substrate'], cwd=ROOT, text=True))['Replace']
    assert info['Version'] == 'v0.6.0-alpha3'
    env = {**ENV, 'CGO_ENABLED': '0', 'GOTOOLCHAIN': subprocess.check_output(['go', 'env', 'GOVERSION'], text=True).strip(),
           'GOCACHE': str(CACHE / 'go-build')}
    subprocess.run(['go', 'build', '-trimpath', '-o', str(CACHE / 'build/substrate-atenet-stock'), './cmd/atenet'],
                   cwd=info['Dir'], env=env, check=True)
    run(['docker', 'build', '-f', 'deploy/Dockerfile.services', '--build-arg', 'BINARY=substrate-atenet-stock', '-t', STOCK_ATENET, '.'])
    run(['docker', 'push', STOCK_ATENET])


def metadata(kind, name, namespace=NS):
    return {'apiVersion': 'apps/v1' if kind == 'Deployment' else 'networking.k8s.io/v1' if kind == 'NetworkPolicy' else 'v1',
            'kind': kind, 'metadata': {'name': name, 'namespace': namespace}}


def configmap(config):
    # Use the exact existing SDS references and public upstream trust.
    data = json.loads(kubectl('-n', SCOPES[0], 'get', 'cm', 'agent-envoy', '-o', 'json'))['data']
    data['envoy.yaml'] = yaml.safe_dump(config, sort_keys=False)
    return {**metadata('ConfigMap', NAME), 'data': data}


def resources(config):
    existing = json.loads(kubectl('-n', SCOPES[0], 'get', 'deploy', 'agent-egress', '-o', 'json'))
    spec = copy.deepcopy(existing['spec'])
    spec.update(replicas=1, selector={'matchLabels': {'app': NAME}})
    spec['template']['metadata'] = {'labels': {'app': NAME}, 'annotations': {'auth.demo.dev/run': uuid.uuid4().hex}}
    pod = spec['template']['spec']
    pod['serviceAccountName'] = 'atenet-egress'
    pod['containers'] = [c for c in pod['containers'] if c['name'] != 'authorizer']
    keep = {'config', 'trust', 'sdsmint-sock', 'servicedns', 'podidentity', 'actor-id-ca-certs', 'egress-mitm-ca-pool'}
    pod['volumes'] = [v for v in pod['volumes'] if v['name'] in keep]
    for volume in pod['volumes']:
        if volume['name'] == 'config': volume['configMap']['name'] = NAME
        if volume['name'] == 'trust': volume['configMap']['name'] = NAME + '-trust'
    for container in pod['containers'] + pod['initContainers']:
        container['volumeMounts'] = [v for v in container['volumeMounts'] if v['name'] in keep]
        if container['name'] in ['native-egress', 'sdsmint']:
            container['image'] = STOCK_ATENET
        if container['name'] == 'native-egress':
            container['args'] = ['--namespace=ate-system' if a.startswith('--namespace=') else a for a in container['args']]
        if container['name'] == 'envoy':
            container['ports'] = [{'name': 'https', 'containerPort': 8443}]
    objects = []
    envoy = next(c for c in pod['containers'] if c['name'] == 'envoy')
    for namespace in AUTHORIZERS:
        secret = json.loads(kubectl('-n', namespace, 'get', 'secret', 'auth-client', '-o', 'json'))
        secret_name = NAME + '-' + namespace
        objects.append({**metadata('Secret', secret_name), 'data': secret['data']})
        volume = 'auth-' + namespace
        pod['volumes'].append({'name': volume, 'secret': {'secretName': secret_name}})
        envoy['volumeMounts'].append({'name': volume, 'mountPath': '/' + volume, 'readOnly': True})
        objects.append(authorizer_service(namespace))
    trust = json.loads(kubectl('-n', SCOPES[0], 'get', 'cm', 'upstream-trust', '-o', 'json'))['data']
    objects += [configmap(config), {**metadata('ConfigMap', NAME + '-trust'), 'data': trust},
        {**metadata('Service', NAME), 'spec': {'selector': {'app': NAME}, 'ports': [{'name': 'https', 'port': 443, 'targetPort': 8443}]}},
        {**metadata('Deployment', NAME), 'spec': spec},
        {**metadata('NetworkPolicy', NAME), 'spec': {'podSelector': {'matchLabels': {'app': NAME}}, 'policyTypes': ['Ingress'],
            'ingress': [{'from': [{'namespaceSelector': {}, 'podSelector': {'matchExpressions': [{'key': 'ate.dev/worker-pool', 'operator': 'Exists'}]}}],
                         'ports': [{'protocol': 'TCP', 'port': 8443}]}]}},
        {**metadata('NetworkPolicy', NAME + '-api'), 'spec': {'podSelector': {'matchLabels': {'app': 'ate-api-server'}},
            'policyTypes': ['Ingress'], 'ingress': [{'from': [{'podSelector': {'matchLabels': {'app': NAME}}}],
                                                   'ports': [{'protocol': 'TCP', 'port': 443}]}]}}]
    return objects


def wait_for_native_api(state):
    """Wait for the native gateway's real ListActors dependency check."""
    log = open(state / 'native-health-forward.log', 'w')
    proc = subprocess.Popen(['kubectl', '--context', 'auth-demo', '-n', NS, 'port-forward',
                             'deploy/' + NAME, '19440:4040'], env=ENV, stdout=log, stderr=log)
    try:
        def healthy():
            with urllib.request.urlopen('http://localhost:19440/statusz?format=json', timeout=4) as response:
                return json.load(response)['health']['ate_api']['healthy']
        wait_for(healthy, seconds=120)
    finally:
        proc.terminate(); proc.wait(timeout=10); log.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--skip-build', action='store_true')
    args = parser.parse_args()
    if not args.skip_build: build()
    config = bootstrap()
    original_api = json.loads(kubectl('-n', NS, 'get', 'deploy', 'ate-api-server', '-o', 'json'))['spec']['template']
    report = {'timestamp': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'complete': False,
        'topology': 'shared Envoy -> hardcoded namespace authorizer gRPC Services; application traffic stays in shared Envoy',
        'stockApiImage': STOCK_API, 'nativeEgress': 'built from untouched v0.6.0-alpha3 module',
        'routes': AUTHORIZERS, 'checks': []}
    REPORT.write_text(json.dumps(report, indent=2) + '\n')
    forwards, sessions, callers = [], [], []
    installed, changed_api = [], False
    state = CACHE / 'shared-egress-mvp'
    state.mkdir(exist_ok=True, mode=0o700)
    rendered = yaml.safe_dump(config, sort_keys=False)
    (state / 'envoy.yaml').write_text(rendered)
    report['configSha256'] = hashlib.sha256(rendered.encode()).hexdigest()
    report['httpRequests'] = 0
    backup = state / 'original-api-template.json'
    backup.write_text(json.dumps(original_api)); backup.chmod(0o600)
    def record(name, **details):
        report['checks'].append({'test': name, 'passed': True, **details})
        REPORT.write_text(json.dumps(report, indent=2) + '\n')
        print('PASS:', name, flush=True)
    def replace_config(value):
        apply([configmap(value)])
        kubectl('-n', NS, 'rollout', 'restart', 'deploy/' + NAME)
        kubectl('-n', NS, 'rollout', 'status', 'deploy/' + NAME, '--timeout=150s')
        # A restarted gateway closes old pooled atunnel connections. All retry
        # checks below use explicit GETs; application writes are not replayed.
    def call(i, **kwargs):
        body = {'method': 'GET', 'path': '/catalog', 'origin': INTERNAL, 'headers': {}, **kwargs}
        for _ in range(60):
            status, value = request(18543+i, '/v1/sessions/' + sessions[i] + '/invoke', body, callers[i])
            if status == 502 and 'input was not accepted' in str(value):
                time.sleep(1)
                continue
            break
        assert status == 200, (status, value)
        calls = payload(value, 'calls')
        assert calls, ('Agent did not return an HTTP result', value)
        report['httpRequests'] += 1
        return calls[0]
    try:
        objects = resources(config)
        for obj in objects:
            existing = kubectl('-n', obj['metadata']['namespace'], 'get', obj['kind'], obj['metadata']['name'], '--ignore-not-found=true', '-o', 'name').strip()
            assert not existing, 'test resource already exists; refusing to overwrite: ' + existing
        installed = [(o['kind'], o['metadata']['namespace'], o['metadata']['name']) for o in objects]
        apply(objects)
        kubectl('-n', NS, 'rollout', 'status', 'deploy/' + NAME, '--timeout=150s')
        template = copy.deepcopy(original_api)
        api = next(c for c in template['spec']['containers'] if c['name'] == 'ate-api-server')
        api['image'] = STOCK_API
        api['args'] = ['--default-egress-gateway-address=' + NAME + '.' + NS + '.svc:443' if a.startswith('--default-egress-gateway-address=') else a for a in api['args']]
        api['env'] = [e for e in api.get('env', []) if e['name'] != 'AGENT_EGRESS_GATEWAYS_FILE']
        api['volumeMounts'] = [m for m in api.get('volumeMounts', []) if m['name'] != 'agent-egress-gateways']
        template['spec']['volumes'] = [v for v in template['spec']['volumes'] if v['name'] != 'agent-egress-gateways']
        template['metadata'].setdefault('annotations', {})['auth.demo.dev/shared-egress-mvp'] = uuid.uuid4().hex
        changed_api = True
        kubectl('-n', NS, 'patch', 'deploy', 'ate-api-server', '--type=json', '-p', json.dumps([{'op': 'replace', 'path': '/spec/template', 'value': template}]))
        kubectl('-n', NS, 'rollout', 'status', 'deploy/ate-api-server', '--timeout=240s')
        def stock_api_pods_only():
            pods = json.loads(kubectl('-n', NS, 'get', 'pods', '-l', 'app=ate-api-server', '-o', 'json'))['items']
            return len(pods) == live_api_replicas and all(not p['metadata'].get('deletionTimestamp')
                and any(c['name'] == 'ate-api-server' and c['image'] == STOCK_API for c in p['spec']['containers']) for p in pods)
        live_api_replicas = json.loads(kubectl('-n', NS, 'get', 'deploy', 'ate-api-server', '-o', 'json'))['spec']['replicas']
        wait_for(stock_api_pods_only, seconds=120)
        live = json.loads(kubectl('-n', NS, 'get', 'deploy', 'ate-api-server', '-o', 'json'))
        assert live['spec']['template']['spec']['containers'][0]['image'] == STOCK_API
        wait_for_native_api(state)
        record('official stock Substrate API uses one shared gateway address; routing patch not active')
        for i, namespace in enumerate(SCOPES):
            forwards.append(Forward(namespace, 'authorizer', 18543+i))
            wait_for(lambda i=i: request(18543+i, '/readyz')[0] == 200, seconds=15)
            callers.append(kubectl('-n', namespace, 'create', 'token', 'test-user', '--audience=auth-demo', '--duration=30m').strip())
            body = {'agent': {'namespace': namespace, 'name': AGENT},
                    'serviceAccount': 'agent-caller', 'requestId': uuid.uuid4().hex}
            deadline = time.monotonic() + 120
            while True:
                status, binding = request(18543+i, '/v1/sessions', body, callers[i])
                # Kagent has its own headless-service gRPC connections. Creation
                # is idempotent: preserve requestId across API-rollout recovery.
                if status != 503 or time.monotonic() >= deadline: break
                time.sleep(2)
            assert status == 200, (status, binding)
            sessions.append(binding['sessionId'])
        report['sessions'] = dict(zip(SCOPES, sessions))
        for i, identity in enumerate(['reader', 'writer']):
            for origin, path in [(INTERNAL, '/catalog'), (EXTERNAL, '/external')]:
                result = call(i, origin=origin, path=path)
                assert result['status'] == 200, result
                event = json.loads(result['body'])
                assert event['credential'] == identity and event['authorizationHeaderCount'] == 1, event
        record('two real Agent Sessions select the correct namespace credentials for HTTP and HTTPS')
        def observed_both_actors():
            # Envoy stdout access logging is buffered independently of request completion.
            logs = kubectl('-n', NS, 'logs', 'deploy/' + NAME, '-c', 'envoy', '--tail=200')
            return all('/' + namespace + '/session-' + sid in logs for namespace, sid in zip(SCOPES, sessions))
        wait_for(observed_both_actors, seconds=25)
        record('shared gateway access logs confirm both certificate-derived Actor identities')
        for i, other in [(0, 1), (1, 0)]:
            result = call(i, origin=EXTERNAL, path='/external', headers={'X-Namespace': SCOPES[other],
                'X-Atespace': SCOPES[other], 'X-Agent-ID': sessions[other], 'Authorization': 'Bearer forged'})
            assert result['status'] == 200 and json.loads(result['body'])['credential'] == ['reader', 'writer'][i], result
            assert call(i, path='/private')['status'] == 403
        record('forged namespace/identity headers cannot select another authorizer; path denials preserved')
        assert call(0, method='POST', path='/mcp')['status'] == 403
        result = call(1, method='POST', path='/mcp')
        assert result['status'] == 200 and json.loads(result['body'])['credential'] == 'writer', result
        record('project-specific method permissions preserved')
        for _ in range(4):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda i: call(i, origin=EXTERNAL, path='/external'), [0, 1]))
            for i, result in enumerate(results):
                assert result['status'] == 200 and json.loads(result['body'])['credential'] == ['reader', 'writer'][i], result
        record('eight interleaved requests keep project tokens separate', requests=8, concurrency=2)
        wrong = copy.deepcopy(config)
        for listener in wrong['static_resources']['listeners']:
            for chain in listener.get('filter_chains', []):
                hcm = chain['filters'][0].get('typed_config', {})
                for host in hcm.get('route_config', {}).get('virtual_hosts', []):
                    for route in host['routes']:
                        override = route.get('typed_per_filter_config', {}).get(FILTER, {}).get('overrides')
                        if override and override['grpc_service']['envoy_grpc']['cluster_name'] == 'authorizer_auth-agent-a':
                            override['grpc_service']['envoy_grpc']['cluster_name'] = 'authorizer_auth-agent-b'
        replace_config(wrong)
        assert call(0, origin=EXTERNAL, path='/external')['status'] == 403
        result = call(1, origin=EXTERNAL, path='/external')
        assert result['status'] == 200 and json.loads(result['body'])['credential'] == 'writer', result
        record('deliberate A -> B misroute is rejected by B before releasing credentials')
        missing = copy.deepcopy(config)
        for c in missing['static_resources']['clusters']:
            if c['name'] == 'authorizer_auth-agent-a':
                c['load_assignment']['endpoints'][0]['lb_endpoints'][0]['endpoint']['address']['socket_address']['port_value'] = 1
        replace_config(missing)
        result = call(0, origin=EXTERNAL, path='/external')
        assert result['status'] in [500, 503, 504], result
        assert call(1, origin=EXTERNAL, path='/external')['status'] == 200
        record('unavailable A authorizer fails closed while B continues; no fallback across projects')
        replace_config(config)
        assert call(0, origin=EXTERNAL, path='/external')['status'] == 200
        record('normal hardcoded routes recover after restoring configuration')
    except BaseException as error:
        # Capture bounded diagnostics before deleting the isolated gateway.
        for container in ['envoy', 'native-egress']:
            try:
                logs = kubectl('-n', NS, 'logs', 'deploy/' + NAME, '-c', container, '--tail=60')
                (state / (container + '.log')).write_text(logs)
                summary = [line for line in logs.splitlines() if 'Checking health' not in line
                           and any(word in line.lower() for word in ['error', 'warn', '[egress]', 'actor'])]
                print('\n'.join(summary[-12:]), flush=True)
            except subprocess.CalledProcessError:
                pass
        report['error'] = str(error)
        REPORT.write_text(json.dumps(report, indent=2) + '\n')
        raise
    finally:
        # Remove only Sessions created by this test, before restoring the API.
        try:
            for i, sid in enumerate(sessions):
                for _ in range(15):
                    status, _ = request(18543+i, '/v1/sessions/' + sid, token=callers[i], method='DELETE')
                    if status == 200: break
                    if status != 409: raise RuntimeError('test Session cleanup failed')
                    time.sleep(1)
                else: raise RuntimeError('test Session cleanup did not converge')
        finally:
            for f in forwards: f.close()
            if changed_api:
                kubectl('-n', NS, 'patch', 'deploy', 'ate-api-server', '--type=json', '-p', json.dumps([{'op': 'replace', 'path': '/spec/template', 'value': original_api}]))
                kubectl('-n', NS, 'rollout', 'status', 'deploy/ate-api-server', '--timeout=240s')
            for kind, namespace, name in reversed(installed):
                kubectl('-n', namespace, 'delete', kind, name, '--ignore-not-found=true', '--wait=false')
    report['complete'] = True
    report['restoredPreviousDeployment'] = True
    REPORT.write_text(json.dumps(report, indent=2) + '\n')


if __name__ == '__main__':
    main()
