#!/usr/bin/env python3
"""Small live MVP check: encrypted OAuth state, replica restart and HPA scale-out.

Uses the existing auth-demo cluster. Four requests per replica check, one real
Agent Session, at most three egress Pods and two workers. No throughput benchmark.
"""
import argparse
import base64
import copy
import datetime
import json
import re
import ssl
import subprocess
import time
import urllib.request
import uuid

from local import ROOT, CACHE, ENV, kubectl, apply
from deploy import AGENT, SCOPES, EXTERNAL, auth_state_key, certificates, keys, install_fixture, sql
from dedicated_gateway import demo_config, resources
from test_e2e import Forward, request, invoke, wait_for

NS = SCOPES[0]
REPORT = ROOT / 'artifacts/scaling-mvp.json'


def run(args, **kwargs):
    return subprocess.run(args, cwd=ROOT, env=ENV, check=True, text=True, **kwargs)


def build():
    for name in ['authorizer', 'test-api']:
        subprocess.run(['go', 'build', '-trimpath', '-o', '.cache/build/' + name, './cmd/' + name],
                       cwd=ROOT, env={**ENV, 'CGO_ENABLED': '0'}, check=True)
        image = 'localhost:5001/auth-demo/' + name + ':scaling-mvp'
        run(['docker', 'build', '-f', 'deploy/Dockerfile.services', '--build-arg', 'BINARY=' + name, '-t', image, '.'])
        run(['docker', 'push', image])
    run(['go', 'test', '-c', '-o', '.cache/build/extproc-probe', './internal/extproc'])


def fixture_stats():
    ctx = ssl.create_default_context(cafile=str(certificates() / 'ca.crt'))
    with urllib.request.urlopen('https://localhost:18458/oauth/stats', context=ctx, timeout=5) as response:
        return json.load(response).get('reader', 0)


def pods():
    return [p['metadata']['name'] for p in json.loads(kubectl('-n', NS, 'get', 'pods', '-l', 'app=agent-egress', '-o', 'json'))['items']
            if not p['metadata'].get('deletionTimestamp')
            and any(c['type'] == 'Ready' and c['status'] == 'True' for c in p['status'].get('conditions', []))]


def probe(sid):
    forwards = []
    try:
        addresses = []
        for i, pod in enumerate(pods()):
            port = 19300 + i
            log = open(CACHE / (pod + '-probe.log'), 'w')
            proc = subprocess.Popen(['kubectl', '--context', 'auth-demo', '-n', NS, 'port-forward', 'pod/' + pod,
                                     f'{port}:50052'], env=ENV, stdout=log, stderr=log)
            forwards.append((proc, log))
            def listening():
                import socket
                with socket.create_connection(('127.0.0.1', port), timeout=.3):
                    return True
            wait_for(listening, seconds=15)
            addresses.append('127.0.0.1:' + str(port))
        assert len(addresses) >= 2, 'need at least two ready replicas'
        result = subprocess.run([str(CACHE / 'build/extproc-probe'), '-test.run=^TestLiveReplicas$', '-test.v'],
                                cwd=ROOT, env={**ENV, 'AUTH_DEMO_REPLICA_ADDRS': ','.join(addresses),
                                    'AUTH_DEMO_PROJECT_TLS': str(certificates(NS)),
                                    'AUTH_DEMO_EXTERNAL_CA': str(certificates() / 'ca.crt'),
                                    'AUTH_DEMO_EXTERNAL_URL': 'https://localhost:18458',
                                    'AUTH_DEMO_ACTOR': f'spiffe://substrate-actor.local/worker-for-actor/{NS}/session-{sid}'},
                                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        assert result.returncode == 0, result.stdout
        match = re.search(r'TOKEN_FINGERPRINT=([a-f0-9]{64})', result.stdout)
        assert match, result.stdout
        return match.group(1), len(addresses)
    finally:
        for proc, log in forwards:
            proc.terminate()
            proc.wait(timeout=10)
            log.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--skip-build', action='store_true')
    args = parser.parse_args()
    if not args.skip_build:
        build()
    run([str(CACHE / 'bin/minikube'), '-p', 'auth-demo', 'addons', 'enable', 'metrics-server'])
    install_fixture(certificates(), keys(), 'localhost:5001/auth-demo/test-api:scaling-mvp')
    current = json.loads(kubectl('-n', NS, 'get', 'deploy', 'agent-egress', '-o', 'json'))
    workers_before = json.loads(kubectl('-n', NS, 'get', 'workerpool', 'agent-workers', '-o', 'json'))['spec']['replicas']
    original_secret = json.loads(kubectl('-n', NS, 'get', 'secret', 'application-credentials', '-o', 'json'))['data']
    objects = resources('namespace', demo_config(NS), 'unused')
    deployment = next(o for o in objects if o['kind'] == 'Deployment')
    hpas = [o for o in objects if o['kind'] == 'HorizontalPodAutoscaler']
    live_images = {c['name']: c['image'] for c in current['spec']['template']['spec']['containers'] + current['spec']['template']['spec'].get('initContainers', [])}
    digest = json.loads(subprocess.check_output(['docker', 'inspect', 'localhost:5001/auth-demo/authorizer:scaling-mvp'], text=True))[0]['RepoDigests']
    image = next(d for d in digest if d.startswith('localhost:5001/'))
    for c in deployment['spec']['template']['spec']['containers'] + deployment['spec']['template']['spec'].get('initContainers', []):
        c['image'] = image if c['name'] == 'authorizer' else live_images[c['name']]
    # Keep the existing public trust/configuration and native gateway images.
    deployment['spec']['template']['metadata']['annotations'] = current['spec']['template']['metadata'].get('annotations', {})
    # Fresh Pods must start with this run's Secret, rather than a previous mount
    # waiting for Kubernetes' eventually consistent projection refresh.
    run_id = uuid.uuid4().hex
    deployment['spec']['template']['metadata']['annotations']['auth.demo.dev/scaling-run'] = run_id
    credential = {'api-token': {'type': 'oauth2', 'tokenEndpoint': EXTERNAL + '/oauth/token',
                              'clientId': 'reader', 'clientSecret': keys()['reader'], 'audience': run_id}}
    secret = {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': 'application-credentials', 'namespace': NS},
              'data': {'credentials.json': base64.b64encode(json.dumps(credential).encode()).decode()}}
    checks = []
    report = {'timestamp': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'namespace': NS,
              'scope': 'MVP correctness and HPA control; no capacity or HA claim', 'complete': False, 'checks': checks}
    REPORT.write_text(json.dumps(report, indent=2) + '\n')
    sid = None
    forward = None
    def record(name, **evidence):
        checks.append({'test': name, 'passed': True, **evidence})
        print('PASS:', name, flush=True)
        REPORT.write_text(json.dumps(report, indent=2) + '\n')
    try:
        apply([auth_state_key(NS), secret, deployment] + hpas)
        kubectl('-n', NS, 'rollout', 'status', 'deploy/agent-egress', '--timeout=180s')
        wait_for(lambda: len(pods()) == 2)
        forward = Forward(NS, 'authorizer', 18543)
        wait_for(lambda: request(18543, '/readyz')[0] == 200, seconds=15)
        caller = kubectl('-n', NS, 'create', 'token', 'test-user', '--audience=auth-demo', '--duration=30m').strip()
        status, binding = request(18543, '/v1/sessions', {'agent': {'namespace': NS, 'name': AGENT},
                                  'serviceAccount': 'agent-caller', 'requestId': uuid.uuid4().hex}, caller)
        assert status == 200, (status, binding)
        sid = binding['sessionId']
        previous_ids = set(sql('SELECT id FROM auth_state.tokens;', 'auth_agent_a').splitlines())
        first, count = probe(sid)
        assert fixture_stats() == 1, 'cold replicas repeated external login'
        record('replicas share one external credential', replicas=count, requests=count * 2, externalLogins=1, sessionId=sid)
        assert probe(sid)[0] == first and fixture_stats() == 1, 'restart changed credential identity or repeated login'
        record('warm reads reuse shared state without external login')
        new_ids = set(sql('SELECT id FROM auth_state.tokens;', 'auth_agent_a').splitlines()) - previous_ids
        assert len(new_ids) == 1, 'expected one new auth context for this run'
        state_id = new_ids.pop()
        assert re.fullmatch('[a-f0-9]{64}', state_id)
        assert int(sql(f"SELECT length(ciphertext) FROM auth_state.tokens WHERE id='{state_id}';", 'auth_agent_a')) > 0
        for pod in json.loads(kubectl('-n', NS, 'get', 'pods', '-l', 'app=agent-egress', '-o', 'json'))['items']:
            for c in pod['spec']['containers'] + pod['spec'].get('initContainers', []):
                assert any(m['name'] == 'auth-state-key' for m in c.get('volumeMounts', [])) == (c['name'] == 'authorizer')
        record('encrypted state persisted; encryption key mounted only in authorizer')
        forward.close(); forward = None
        kubectl('-n', NS, 'rollout', 'restart', 'deploy/agent-egress')
        kubectl('-n', NS, 'rollout', 'status', 'deploy/agent-egress', '--timeout=180s')
        wait_for(lambda: len(pods()) == 2)
        assert probe(sid)[0] == first and fixture_stats() == 1
        record('all replicas restarted and reused persisted token', externalLogins=1)
        # Move only the server-side safe reuse deadline to exercise expiry promptly.
        sql(f"UPDATE auth_state.tokens SET expires_at=clock_timestamp()-interval '1 second' WHERE id='{state_id}';", 'auth_agent_a')
        time.sleep(1.1)
        second, _ = probe(sid)
        assert second != first and fixture_stats() == 2
        record('expired shared state refreshes once across replicas', externalLogins=2)
        forward = Forward(NS, 'authorizer', 18543)
        wait_for(lambda: request(18543, '/readyz')[0] == 200, seconds=15)
        assert request(18543, '/internal/credentials/invalidate', {'namespace': NS, 'name': 'api-token'}, admin=True)[0] == 200
        time.sleep(1.1)
        third, _ = probe(sid)
        assert third != second and fixture_stats() == 3
        record('explicit invalidation reaches replicas and refreshes once', externalLogins=3)
        # Start the real Actor before sampling worker CPU; an idle pool may use
        # less than Kubernetes' one-millicore resource-metric precision.
        result = invoke(18543, caller, sid, origin=EXTERNAL, path='/external')
        assert result['status'] == 200 and json.loads(result['body'])['credentialVersion'] == 3, result
        # Deliberately low thresholds exercise real HPA using light/idle CPU.
        low = copy.deepcopy(hpas)
        for hpa in low:
            for metric in hpa['spec']['metrics']:
                source = metric.get('containerResource') or metric['resource']
                source['target']['averageValue'] = '1m'
        apply(low)
        def scaled():
            e = json.loads(kubectl('-n', NS, 'get', 'deploy', 'agent-egress', '-o', 'json'))
            w = json.loads(kubectl('-n', NS, 'get', 'workerpool', 'agent-workers', '-o', 'json'))
            return e['spec']['replicas'] == 3 and e.get('status', {}).get('readyReplicas') == 3 and w['spec']['replicas'] == 2 and w.get('status', {}).get('readyReplicas') == 2
        # Kubelet CPU samples can lag readiness by over a minute on this lab.
        # Poll slowly: this is a control-path check, not API-server load.
        deadline = time.monotonic() + 300
        while not scaled():
            if time.monotonic() >= deadline:
                raise AssertionError('HPA targets did not become ready within five minutes')
            time.sleep(3)
        statuses = json.loads(kubectl('-n', NS, 'get', 'hpa', '-o', 'json'))['items']
        for hpa in statuses:
            assert any(c['type'] == 'ScalingActive' and c['status'] == 'True' for c in hpa['status']['conditions'])
        assert probe(sid)[0] == third and fixture_stats() == 3
        record('HPA reaches bounded targets; new egress replica reuses token', egressBefore=2, egressReplicas=3,
               workersBefore=workers_before, workerReplicas=2, workerScaleOutObserved=workers_before < 2,
               testCpuThreshold='1m (control-path test only)', externalLogins=3,
               hpaStatus=[{'name': h['metadata']['name'], 'status': h['status']} for h in statuses])
        result = invoke(18543, caller, sid, origin=EXTERNAL, path='/external')
        assert result['status'] == 200 and json.loads(result['body'])['credentialVersion'] == 3, result
        assert invoke(18543, caller, sid, origin=EXTERNAL, path='/private')['status'] == 403
        record('real Agent Session uses OAuth through Envoy after scaling; denied path remains denied')
    finally:
        # Preserve the MVP deployments; restore normal HPA thresholds and demo credentials.
        apply(hpas)
        try:
            if sid:
                if forward is None:
                    forward = Forward(NS, 'authorizer', 18543)
                    wait_for(lambda: request(18543, '/readyz')[0] == 200, seconds=15)
                for _ in range(15):
                    status, _ = request(18543, '/v1/sessions/' + sid, token=caller, method='DELETE')
                    if status == 200: break
                    if status != 409: raise RuntimeError('Session cleanup failed')
                    time.sleep(1)
                else:
                    raise RuntimeError('Session cleanup did not converge')
        finally:
            if forward is not None: forward.close()
            apply([{'apiVersion': 'v1', 'kind': 'Secret', 'metadata': secret['metadata'], 'data': original_secret}])
    report['complete'] = True
    REPORT.write_text(json.dumps(report, indent=2) + '\n')


if __name__ == '__main__':
    main()
