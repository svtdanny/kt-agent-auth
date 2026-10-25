#!/usr/bin/env python3
"""Sequential, matched Mastra HTTP/curl load on OpenShell and our existing paths."""
from datetime import datetime, timezone
import json
import os
import subprocess
import sys
import threading
import time
import uuid

from common import ROOT, CACHE, ENV, CONTEXT, cli, kube, forward, terminate
from policy import TARGETS, write_policy
from test_functional import request, placeholders

sys.path.insert(0, str(ROOT/'scripts'))
import test_e2e as demo
from benchmark_shared_gateway import load_result


def resource_sample(context, env):
    data = json.loads(subprocess.check_output(['kubectl', '--context', context, 'get', '--raw', '/api/v1/nodes/'+context+'/proxy/stats/summary'], env=env, text=True))
    return {'context': context, 'atUTC': datetime.now(timezone.utc).isoformat(),
            'pods': [{'namespace': p['podRef']['namespace'], 'name': p['podRef']['name'],
                      'containers': [{'name': c['name'], 'cpuNanoCores': c.get('cpu', {}).get('usageNanoCores'),
                                      'workingSetBytes': c.get('memory', {}).get('workingSetBytes')}
                                     for c in p.get('containers', [])]}
                     for p in data.get('pods', []) if p['podRef']['namespace'] in ('demo', 'test-services', 'openshell', 'ate-system')]}


def main():
    started = time.monotonic()
    report = {'startedAtUTC': datetime.now(timezone.utc).isoformat(), 'completed': False, 'stages': [], 'resourceSamples': [],
              'loadModel': 'Two repetitions; closed-loop concurrency 1/8/32; six seconds per stage plus drain; no timed request retries.',
              'limitations': ['Sequential runs on two Minikube Docker nodes on the same host. Profiles request 8 CPUs/16 GiB; Docker applies the memory limit but no per-node CPU quota. Kubernetes workload CPU limits are applied.',
                              'Original CNI is kindnet; OpenShell uses Calico with NetworkPolicy enforcement.',
                              'OpenShell native policy/provider resolution is local to one supervisor; our path includes Envoy, ext_proc, control plane, and PostgreSQL.',
                              'OpenShell gateway uses its default durable SQLite backend; our control plane uses shared PostgreSQL.',
                              'All application containers and the pilot supervisor are limited to one CPU; our gateway has two CPUs and processor/control plane one each.',
                              'Native kagent uses gVisor; OpenShell and the enrolled command Pod have different isolation boundaries.',
                              'Same Node/Mastra/curl versions, HTTP method/path, response semantics, synthetic credentials, and test-api binary; fixtures run in each respective cluster/network.',
                              'HTTP tools use fetch connection reuse; command tools launch curl and establish connections per request.',
                              'A2A/CLI invocation setup is excluded from tool request latencies and throughput, and recorded separately.',
                              'These measurements compare deployed systems; they do not isolate a proxy algorithm or establish production capacity.']}
    handles, sid, token = [], None, None
    stop = threading.Event()
    sampler = None
    try:
        handles.append(forward('openshell', 'service/openshell', '18551:8080'))
        handles.append(forward('test-services', 'service/mock-mcp', '18545:8080'))
        for namespace, target, ports in [('demo', 'service/auth-control-plane', '18443:8443'),
                ('test-services', 'service/mock-mcp', '18445:8080'), ('demo', 'deployment/command-runner', '18446:3000')]:
            with open(CACHE/('comparison-forward-'+ports[:5]+'.log'), 'w') as log:
                handles.append(subprocess.Popen(['kubectl', '--context', 'auth-demo', '-n', namespace, 'port-forward', target, ports], env=demo.ENV, stdout=log, stderr=log))
        time.sleep(1)
        # This benchmark sandbox has no state probe or application changes.
        current = json.loads(cli('sandbox', 'list', '--output', 'json'))
        if any(s['name'] == 'mastra-benchmark' for s in (current if isinstance(current, list) else current.get('sandboxes', []))):
            cli('sandbox', 'delete', 'mastra-benchmark')
        start = time.monotonic()
        cli('sandbox', 'create', '--name', 'mastra-benchmark', '--from', 'localhost:5001/auth-demo/openshell-mastra:demo',
            '--cpu', '1', '--memory', '512Mi', '--policy', str(write_policy(CACHE/'benchmark-policy.json')),
            '--provider', 'pilot-mcp', '--provider', 'pilot-external', '--detach', '--', 'node', '/app/agent/server.mjs')
        report['openshellColdCreateSeconds'] = time.monotonic()-start
        handles.append(forward('openshell', 'pod/default--mastra-benchmark', '18546:3000'))
        values = placeholders('mastra-benchmark')
        token = demo.kubectl('-n', 'demo', 'create', 'token', 'test-user', '--audience=auth-demo', '--duration=1h').strip()
        start = time.monotonic()
        status, binding = demo.request(18443, '/v1/sessions', {'agent': {'namespace': 'demo', 'name': 'mastra-http'},
                                      'serviceAccount': 'multi-reader', 'requestId': 'comparison-'+uuid.uuid4().hex}, token)
        assert status == 200
        sid = binding['sessionId']
        report['nativeSessionCreateSeconds'] = time.monotonic()-start

        def invoke(source, target, load=None):
            origin, method, path, _ = TARGETS[target]
            body = {'origin': origin, 'method': method, 'path': path,
                    'execution': 'command' if source.endswith('curl') else 'http',
                    'headers': {'authorization': 'Bearer '+(values[target] if source.startswith('openshell') else 'comparison-placeholder')}}
            if load:
                body['load'] = load
            if source == 'ours/kagent-fetch':
                status, value = demo.request(18443, '/v1/sessions/'+sid+'/invoke', body, token)
                result = demo.calls(value)
            else:
                status, value = request(18546 if source.startswith('openshell') else 18446, '/invoke', body)
                result = value.get('calls')
            assert status == 200, ('invocation failed', source, target, status)
            if load:
                stats = load_result(value)
                assert stats is not None, ('load result missing', source, target)
                return stats
            assert result and result[0]['status'] == 200, ('warmup failed', source, target)
            event = json.loads(result[0]['body'])
            assert event['credentialAccepted'] and event['credential'] == 'reader'

        def audit(source, target):
            port = (18545 if source.startswith('openshell') else 18445) if target == 'mcp' else (18548 if source.startswith('openshell') else 18448)
            status, value = request(port, '/audit', tls=target == 'external')
            assert status == 200
            return len(value or [])

        sources = ['ours/kagent-fetch', 'openshell/fetch', 'ours/pod-fetch', 'ours/pod-curl', 'openshell/curl']
        for source in sources:
            for target in TARGETS:
                start = time.monotonic()
                invoke(source, target)
                report.setdefault('warmup', []).append({'source': source, 'target': target, 'wallSeconds': time.monotonic()-start})
        status, before = demo.request(18443, '/internal/stats', internal_client=True)
        assert status == 200
        report['podsBefore'] = {context: json.loads(subprocess.check_output(['kubectl', '--context', context, 'get', 'pods', '-A', '-o', 'json'], env=env, text=True))
                                for context, env in [('auth-demo', demo.ENV), (CONTEXT, ENV)]}
        # Store only Pod identities, images, resources, and restart counts, never
        # arbitrary Pod environment variables or mounted Secret contents.
        for context in report['podsBefore']:
            report['podsBefore'][context] = [{'namespace': p['metadata']['namespace'], 'name': p['metadata']['name'], 'uid': p['metadata']['uid'],
                'containers': [{'name': c['name'], 'image': c['image'], 'resources': c.get('resources', {})} for c in p['spec']['containers']],
                'statuses': [{'name': c['name'], 'restarts': c['restartCount'], 'imageID': c.get('imageID')} for c in p['status'].get('containerStatuses', [])]}
                for p in report['podsBefore'][context]['items'] if p['metadata']['namespace'] in ('demo', 'test-services', 'openshell', 'ate-system')]

        def sample():
            while not stop.is_set():
                for context, env in [('auth-demo', demo.ENV), (CONTEXT, ENV)]:
                    try:
                        report['resourceSamples'].append(resource_sample(context, env))
                    except Exception:
                        report.setdefault('resourceSamplingErrors', []).append(context)
                stop.wait(5)
        sampler = threading.Thread(target=sample, daemon=True)
        sampler.start()
        for repetition in (1, 2):
            for concurrency in (1, 8, 32):
                for target in TARGETS:
                    for source in sources if repetition == 1 else reversed(sources):
                        name = f'r{repetition}/c{concurrency}/{source}/{target}'
                        print('STAGE', name, flush=True)
                        upstream_before = audit(source, target)
                        start = time.monotonic()
                        value = invoke(source, target, {'concurrency': concurrency, 'durationSeconds': 6})
                        value.update({'name': name, 'source': source, 'target': target, 'repetition': repetition,
                                      'wallSecondsIncludingInvocation': time.monotonic()-start,
                                      'upstreamAuditDelta': audit(source, target)-upstream_before})
                        value['upstreamCountMatchesSuccesses'] = value['upstreamAuditDelta'] == value['statuses'].get('200', 0)
                        report['stages'].append(value)
                        print(json.dumps({k: value[k] for k in ['name', 'completed', 'statuses', 'requestsPerSecond', 'latencyMs']}), flush=True)
        status, after = demo.request(18443, '/internal/stats', internal_client=True)
        assert status == 200
        report['authorityTrafficOurs'] = {'before': before, 'after': after, 'delta': {k: after[k]-before.get(k, 0) for k in after}}
        report['assessment'] = {'allowedWorkloadFailures': sum(v['completed']-v['statuses'].get('200', 0) for v in report['stages']),
            'transportErrors': sum(v['statuses'].get('transportError', 0) for v in report['stages']),
            'wrongCredentialResponses': sum(v['statuses'].get('200', 0)-v['readerCredentials']+v['credentialFailures'] for v in report['stages']),
            'allUpstreamAuditsMatch': all(v['upstreamCountMatchesSuccesses'] for v in report['stages']),
            'allStagesMeasured': len(report['stages']) == 60 and all(v['completed'] > 0 for v in report['stages'])}
        for source in sources:
            for target in TARGETS:
                invoke(source, target)
        assert all(v if isinstance(v, bool) else v == 0 for v in report['assessment'].values()), report['assessment']
        report['completed'] = True
    except Exception as error:
        report['error'] = str(error)
        raise
    finally:
        stop.set()
        if sampler:
            sampler.join(timeout=15)
        if sid:
            try:
                demo.delete_session(token, sid)
            except Exception:
                report.setdefault('cleanupErrors', []).append('native Session cleanup failed')
        try:
            cli('sandbox', 'delete', 'mastra-benchmark')
        except Exception:
            report.setdefault('cleanupErrors', []).append('OpenShell benchmark sandbox cleanup failed')
        if report.get('cleanupErrors'):
            report['completed'] = False
        demo.close_connections()
        for handle in handles:
            terminate(handle)
        report.update({'elapsedSeconds': time.monotonic()-started, 'finishedAtUTC': datetime.now(timezone.utc).isoformat()})
        (ROOT/'artifacts/openshell-performance-comparison.json').write_text(json.dumps(report, indent=2)+'\n')
    assert report['completed'], 'benchmark cleanup did not finish'


if __name__ == '__main__':
    main()
