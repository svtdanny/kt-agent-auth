#!/usr/bin/env python3
"""Bounded full-path load: native kagent and a captured command Pod share Envoy."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import http.client
import json
import ssl
import subprocess
import threading
import time
import uuid

import benchmark
import test_e2e as demo
from permissions_demo import CATALOG, MCP, permission_set
from shared_gateway import EXTERNAL_ORIGIN


def load_result(value):
    if isinstance(value, dict):
        if isinstance(value.get('load'), dict) and 'completed' in value['load']:
            return value['load']
        for key, child in value.items():
            if key == 'text' and isinstance(child, str):
                try:
                    result = load_result(json.loads(child))
                    if result is not None:
                        return result
                except json.JSONDecodeError:
                    pass
            result = load_result(child)
            if result is not None:
                return result
    elif isinstance(value, list):
        for child in value:
            result = load_result(child)
            if result is not None:
                return result
    return None


def request(port, path, body=None, *, tls=False, internal=False, token=None):
    # Each invocation owns its connection, including concurrent A2A invocations.
    if tls:
        context = ssl.create_default_context(cafile=str(demo.CACHE/'tls/ca.crt'))
        if internal:
            context.load_cert_chain(str(demo.CACHE/'tls/ext-proc.crt'), str(demo.CACHE/'tls/ext-proc.key'))
        connection = http.client.HTTPSConnection('localhost', port, context=context, timeout=180)
    else:
        connection = http.client.HTTPConnection('localhost', port, timeout=180)
    try:
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['Authorization'] = 'Bearer '+token
        connection.request('GET' if body is None else 'POST', path,
                           None if body is None else json.dumps(body), headers)
        response = connection.getresponse()
        raw = response.read()
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            value = raw.decode()
        return response.status, value
    finally:
        connection.close()


def workload_state():
    data = json.loads(demo.kubectl('get', 'pods', '-A', '-o', 'json'))
    return [{'namespace': p['metadata']['namespace'], 'name': p['metadata']['name'],
             'uid': p['metadata']['uid'], 'phase': p['status']['phase'],
             'containers': [{'name': c['name'], 'imageID': c.get('imageID'),
                             'restarts': c['restartCount'], 'state': c['state']}
                            for key in ('containerStatuses', 'initContainerStatuses')
                            for c in p['status'].get(key, [])]}
            for p in data['items'] if p['metadata']['namespace'] in ('demo', 'kagent', 'ate-system', 'test-services')]


def main():
    started = time.monotonic()
    report = {'startedAtUTC': datetime.now(timezone.utc).isoformat(), 'cluster': 'auth-demo',
              'completed': False, 'stages': [], 'mixedGroups': [], 'resourceSamples': [], 'cleanupErrors': [],
              'loadModel': 'Closed-loop load inside actual Mastra tools; elapsed and percentiles include failures and drain; no timed request retries.',
              'limitations': ['Single local Minikube node; one gateway, processor, and bridge replica.',
                              'Native tools use Node fetch; the command Pod spawns curl for every request, including TCP/TLS setup.',
                              'Internal MCP is an HTTP authentication mock, not a JSON-RPC tool server.',
                              'External HTTPS is a controlled Docker fixture outside Kubernetes, not a live provider API.',
                              'Six-second solo stages and a twelve-second mixed stage are bounded load, not a soak or production capacity guarantee.',
                              'Mixed groups release invocations together; A2A setup causes slight differences in actual tool start times.',
                              'Resource samples are collected every five seconds and may miss instantaneous peaks.']}
    handles, sessions = [], {}
    stop = threading.Event()
    sampler = None
    token = None
    error = None
    try:
        assert demo.kubectl('config', 'current-context').strip() == 'auth-demo'
        sa = json.loads(demo.kubectl('-n', 'demo', 'get', 'serviceaccount', 'multi-reader', '-o', 'json'))
        assert json.loads(sa['metadata']['annotations']['auth.demo.dev/policy-config']) == permission_set(['catalog', 'mcp', 'external']), 'restore default demo grants before benchmarking'
        gateway = json.loads(demo.kubectl('-n', 'ate-system', 'get', 'pods', '-l', 'app=atenet-egress', '-o', 'json'))['items']
        assert len(gateway) == 1 and {c['name'] for c in gateway[0]['spec']['containers']} == {'envoy', 'native-egress'}
        pod = json.loads(demo.kubectl('-n', 'demo', 'get', 'pods', '-l', 'app=command-runner', '-o', 'json'))['items'][0]
        report.update({'gatewayPodUid': gateway[0]['metadata']['uid'], 'commandPodUid': pod['metadata']['uid'],
                       'gatewayImages': [{k: c[k] for k in ('name', 'image', 'imageID')} for c in gateway[0]['status']['containerStatuses']],
                       'externalFixture': json.loads((demo.CACHE/'external-fixture.json').read_text()),
                       'podStateBefore': workload_state()})
        for namespace, target, ports in [('demo', 'service/auth-control-plane', '18443:8443'),
                ('demo', 'service/catalog-api', '18444:443'), ('test-services', 'service/mock-mcp', '18445:8080'),
                ('demo', 'pod/'+pod['metadata']['name'], '18446:3000')]:
            with open(demo.CACHE/('benchmark-forward-'+ports[:5]+'.log'), 'w') as log:
                handles.append(subprocess.Popen(['kubectl', '--context', 'auth-demo', '-n', namespace, 'port-forward', target, ports],
                                                env=demo.ENV, stdout=log, stderr=log))
        for _ in range(60):
            assert all(h.poll() is None for h in handles), 'port-forward exited'
            try:
                if all(request(port, '/readyz', tls=tls)[0] == 200 for port, tls in [(18443, True), (18444, True), (18445, False), (18446, False), (18448, True)]):
                    break
            except OSError:
                pass
            time.sleep(1)
        else:
            raise AssertionError('benchmark fixtures unreachable')

        def sample():
            while not stop.is_set():
                try:
                    report['resourceSamples'].append(benchmark.resources())
                except Exception:
                    report.setdefault('resourceSamplingErrors', []).append('Kubelet resource sample failed')
                stop.wait(5)

        sampler = threading.Thread(target=sample, daemon=True)
        sampler.start()
        token = demo.kubectl('-n', 'demo', 'create', 'token', 'test-user', '--audience=auth-demo', '--duration=1h').strip()
        for name, account in [('session/mcp', 'multi-reader'), ('session/external', 'multi-reader'), ('writer', 'catalog-writer')]:
            status, value = request(18443, '/v1/sessions', {'agent': {'namespace': 'demo', 'name': 'mastra-http'},
                                    'serviceAccount': account, 'requestId': 'shared-load-'+uuid.uuid4().hex}, tls=True, token=token)
            assert status == 200, ('session creation failed', status)
            sessions[name] = value['sessionId']
        report['sessionIds'] = sessions.copy()

        def invoke(source, destination, concurrency=None, duration=6, forbidden=False):
            origin, method, path = destinations[destination]
            if forbidden:
                method, path = 'DELETE', '/private'
            body = {'origin': origin, 'method': method, 'path': path,
                    'headers': {'X-Service-Account': 'catalog-writer', 'Authorization': 'Bearer forged-benchmark-placeholder'}}
            if concurrency is not None:
                body['load'] = {'concurrency': concurrency, 'durationSeconds': duration}
            if source == 'pod':
                body['execution'] = 'command'
                status, value = request(18446, '/invoke', body)
            else:
                status, value = request(18443, '/v1/sessions/'+sessions[source]+'/invoke', body, tls=True, token=token)
            assert status == 200, ('agent invocation failed', source, destination, status)
            if concurrency is None:
                calls = demo.calls(value)
                assert calls and len(calls) == 1 and calls[0]['status'] == 200, ('warmup failed', source, destination)
                event = json.loads(calls[0]['body'])
                assert event['credentialAccepted'] and event['credential'] == ('writer' if source == 'writer' else 'reader')
                return
            stats = load_result(value)
            assert stats is not None, ('load summary missing', source, destination)
            stats.update({'source': source, 'destination': destination, 'origin': origin, 'forbiddenWorkload': forbidden,
                          'expectedCredential': 'writer' if source == 'writer' else 'reader',
                          'execution': 'command/curl' if source == 'pod' else 'http/fetch'})
            return stats

        def audit(destination):
            port, tls = {'mcp': (18445, False), 'external': (18448, True), 'catalog': (18444, True)}[destination]
            status, events = request(port, '/audit', tls=tls)
            assert status == 200, ('audit failed', destination, status)
            return len(events or [])

        def stats():
            status, value = request(18443, '/internal/stats', tls=True, internal=True)
            assert status == 200, ('authority stats unavailable', status)
            return value

        destinations = {'mcp': (MCP, 'POST', '/mcp'), 'external': (EXTERNAL_ORIGIN, 'GET', '/external'), 'catalog': (CATALOG, 'POST', '/orders')}
        for source, destination in [('session/mcp', 'mcp'), ('session/external', 'external'), ('writer', 'catalog'), ('pod', 'mcp'), ('pod', 'external')]:
            invoke(source, destination)
        before = stats()

        def group(name, specs, duration):
            print('STAGE', name, flush=True)
            targets = sorted({s[1] for s in specs})
            audit_before = {d: audit(d) for d in targets}
            ready = threading.Barrier(len(specs))

            def run(spec):
                source, destination, concurrency, forbidden = spec
                ready.wait(timeout=30)
                start = time.monotonic()
                value = invoke(source, destination, concurrency, duration, forbidden)
                value['wallSecondsIncludingInvocation'] = time.monotonic()-start
                value['name'] = name+'/'+source+'/'+destination
                return value

            group_started = time.monotonic()
            with ThreadPoolExecutor(max_workers=len(specs)) as pool:
                values = list(pool.map(run, specs))
            elapsed = time.monotonic()-group_started
            deltas = {d: audit(d)-audit_before[d] for d in targets}
            expected = {d: sum(v['statuses'].get('200', 0) for v in values if v['destination'] == d) for d in targets}
            group_value = {'name': name, 'offeredConcurrency': sum(s[2] for s in specs), 'elapsedSecondsIncludingInvocations': elapsed,
                           'completed': sum(v['completed'] for v in values), 'upstreamAuditDeltas': deltas,
                           'upstreamSuccesses': expected, 'upstreamCountMatchesSuccesses': deltas == expected}
            group_value['requestsPerSecondIncludingInvocations'] = group_value['completed']/elapsed
            report['mixedGroups'].append(group_value)
            report['stages'].extend(values)
            for value in values:
                print(json.dumps({k: value[k] for k in ['name', 'completed', 'statuses', 'requestsPerSecond', 'latencyMs']}), flush=True)

        for concurrency in (1, 8, 32):
            for destination in ('mcp', 'external'):
                for source in ('session/'+destination, 'pod'):
                    group('solo/c'+str(concurrency), [(source, destination, concurrency, False)], 6)
        for destination in ('mcp', 'external'):
            for source in ('session/'+destination, 'pod'):
                group('forbidden/c8', [(source, destination, 8, True)], 4)
        group('mixed/c80', [('session/mcp', 'mcp', 16, False), ('session/external', 'external', 16, False),
                           ('pod', 'mcp', 16, False), ('pod', 'external', 16, False), ('writer', 'catalog', 16, False)], 12)
        after = stats()
        report['authorityTraffic'] = {'before': before, 'after': after, 'delta': {k: after[k]-before.get(k, 0) for k in after}}
        recovery = []
        for source, destination in [('session/mcp', 'mcp'), ('session/external', 'external'), ('writer', 'catalog'), ('pod', 'mcp'), ('pod', 'external')]:
            invoke(source, destination)
            recovery.append({'source': source, 'destination': destination, 'status': 200})
        report['recovery'] = recovery
        full = report['stages']
        delta = report['authorityTraffic']['delta']
        assessment = {
            'allowedWorkloadFailures': sum(v['completed']-v['statuses'].get('200', 0) for v in full if not v['forbiddenWorkload']),
            'forbiddenWorkloadFailures': sum(v['completed']-v['statuses'].get('403', 0) for v in full if v['forbiddenWorkload']),
            'authorizationBypasses': sum(v['statuses'].get('200', 0) for v in full if v['forbiddenWorkload']),
            'transportErrors': sum(v['statuses'].get('transportError', 0) for v in full),
            'wrongCredentialResponses': sum(v['statuses'].get('200', 0)-v[v['expectedCredential']+'Credentials']+v['credentialFailures'] for v in full if not v['forbiddenWorkload']),
            'upstreamAuditMatchesSuccessfulRequests': all(g['upstreamCountMatchesSuccesses'] for g in report['mixedGroups']),
            'emptyStages': sum(v['completed'] == 0 for v in full),
            'warmPolicyGETs': delta.get('accountGETs', 0), 'warmPodGETs': delta.get('podGETs', 0),
            'warmVaultReads': delta.get('vaultReads', 0), 'warmVaultLogins': delta.get('vaultLogins', 0),
            'recoveredAfterLoad': len(recovery) == 5}
        report['assessment'] = assessment
        assert all(value if isinstance(value, bool) else value == 0 for value in assessment.values()), assessment
        report['completed'] = True
    except Exception as exc:
        error = exc
        report['error'] = str(exc)
    finally:
        stop.set()
        if sampler:
            sampler.join(timeout=15)
        for sid in sessions.values():
            try:
                demo.delete_session(token, sid)
            except Exception:
                report['cleanupErrors'].append('Session cleanup failed: '+sid)
        demo.close_connections()
        try:
            report['podStateAfter'] = workload_state()
            current = {p['uid']: p for p in report['podStateAfter']}
            changed = []
            for previous in report.get('podStateBefore', []):
                if previous['phase'] != 'Running':
                    continue
                new = current.get(previous['uid'])
                if not new or {c['name']: c['restarts'] for c in previous['containers']} != {c['name']: c['restarts'] for c in new['containers']}:
                    changed.append(previous['namespace']+'/'+previous['name'])
            report['restartedOrReplacedWorkloads'] = changed
            if changed:
                report['completed'] = False
                error = error or AssertionError('workloads restarted or were replaced')
        except Exception:
            report['cleanupErrors'].append('Final workload state unavailable')
        for handle in handles:
            handle.terminate()
            try:
                handle.wait(timeout=10)
            except subprocess.TimeoutExpired:
                handle.kill()
                handle.wait()
        if report['cleanupErrors']:
            report['completed'] = False
        report.update({'finishedAtUTC': datetime.now(timezone.utc).isoformat(), 'elapsedSeconds': time.monotonic()-started})
        destination = demo.ROOT/'artifacts/benchmark-shared-envoy-results.json'
        destination.write_text(json.dumps(report, indent=2)+'\n')
        print('Report:', destination, flush=True)
    if error:
        raise error
    assert report['completed'], 'benchmark did not finish cleanly'


if __name__ == '__main__':
    main()
