#!/usr/bin/env python3
"""Exercise native OpenShell admission, Vault providers, and network fencing."""
from datetime import datetime, timezone
import http.client
import itertools
import json
import subprocess
import time

from common import CACHE, ROOT, ENV, CONTEXT, CLI, cli, forward, terminate, kube, apply
from policy import TARGETS, policy, write_policy


def request(port, path, body=None, tls=False):
    import ssl
    c = (http.client.HTTPSConnection('localhost', port, timeout=60, context=ssl.create_default_context(cafile=str(ROOT/'.cache/tls/ca.crt')))
         if tls else http.client.HTTPConnection('localhost', port, timeout=60))
    try:
        c.request('GET' if body is None else 'POST', path, None if body is None else json.dumps(body), {'Content-Type': 'application/json'})
        response = c.getresponse()
        raw = response.read()
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            value = raw.decode()
        return response.status, value
    finally:
        c.close()


def placeholders(sandbox='mastra-pilot'):
    return json.loads(cli('sandbox', 'exec', sandbox, '--', 'node', '-e',
        'console.log(JSON.stringify({mcp:process.env.PILOT_MCP_TOKEN,external:process.env.PILOT_EXTERNAL_TOKEN}))'))


def invoke(mode, target, values, method=None, path=None, authorization=None, load=None):
    origin, default_method, default_path, _ = TARGETS[target]
    body = {'origin': origin, 'method': method or default_method, 'path': path or default_path, 'execution': mode,
            'headers': {'authorization': authorization or 'Bearer '+values.get(target, 'withheld-placeholder'),
                        'X-Service-Account': 'catalog-writer'}}
    if load:
        body['load'] = load
    status, value = request(18546, '/invoke', body)
    calls = value.get('calls', []) if isinstance(value, dict) else []
    return status, value, calls


def audit(target):
    status, value = request(18545 if target == 'mcp' else 18548, '/audit', tls=target == 'external')
    assert status == 200
    return len(value or [])


def main():
    report = {'startedAtUTC': datetime.now(timezone.utc).isoformat(), 'cluster': CONTEXT, 'version': '0.1.5',
              'completed': False, 'tests': [], 'limitations': ['MCP admission checks use valid JSON-RPC request payloads against an HTTP authentication mock; this is not a complete MCP client/server integration.']}
    handles = [forward('openshell', 'service/openshell', '18551:8080'),
               forward('openshell', 'pod/default--mastra-pilot', '18546:3000'),
               forward('test-services', 'service/mock-mcp', '18545:8080')]
    def record(name, **evidence):
        report['tests'].append({'test': name, 'passed': True, **evidence})
        print('PASS:', name, flush=True)
    def set_policy(names):
        p = write_policy(CACHE/'selected-policy.json', names)
        start = time.monotonic()
        cli('policy', 'set', 'mastra-pilot', '--policy', str(p), '--wait')
        return time.monotonic()-start
    try:
        time.sleep(1)
        metadata = json.loads(cli('sandbox', 'get', 'mastra-pilot', '--output', 'json'))
        report['sandboxId'] = metadata['id']
        assert metadata['phase'] == 'Ready'
        values = placeholders()
        assert set(values) == {'mcp', 'external'}
        record('sandbox ready with hard-required Landlock and opaque provider placeholders')
        for bits in itertools.product([False, True], repeat=2):
            names = [name for bit, name in zip(bits, TARGETS) if bit]
            elapsed = set_policy(names)
            values = placeholders()
            for mode in ('http', 'command'):
                for target in TARGETS:
                    before = audit(target)
                    status, _, calls = invoke(mode, target, values)
                    allowed = target in names
                    if allowed:
                        assert status == 200 and len(calls) == 1 and calls[0]['status'] == 200, (mode, target, names, status)
                        event = json.loads(calls[0]['body'])
                        assert event['credentialAccepted'] and event['credential'] == 'reader' and event['authorizationHeaderCount'] == 1
                    else:
                        assert status != 200 or not calls or calls[0]['status'] != 200
                    assert audit(target)-before == int(allowed), ('audit mismatch', mode, target, names)
                    record('permission subset through Mastra', mode=mode, target=target, grants=names,
                           allowed=allowed, upstreamDelta=int(allowed), policyApplyWallSeconds=elapsed)
        set_policy(list(TARGETS))
        values = placeholders()
        for mode in ('http', 'command'):
            for target in TARGETS:
                before = audit(target)
                status, _, calls = invoke(mode, target, values, method='DELETE', path='/private')
                assert status == 200 and calls[0]['status'] == 403 and audit(target) == before
                record('forbidden method/path blocked before upstream', mode=mode, target=target)
                # Static provider substitution differs from our unconditional
                # Authorization overwrite: an arbitrary literal reaches the mock.
                status, _, calls = invoke(mode, target, values, authorization='Bearer forged-pilot-placeholder')
                assert status == 200 and calls[0]['status'] == 401 and audit(target) == before+1
                record('literal forged key is rejected by upstream rather than overwritten', mode=mode, target=target, status=401)
                before = audit(target)
                other = 'external' if target == 'mcp' else 'mcp'
                status, _, calls = invoke(mode, target, values, authorization='Bearer '+values[other])
                assert status == 200 and calls[0]['status'] == 403 and audit(target) == before
                record('cross-destination credential placeholder blocked before upstream', mode=mode, target=target)

        # Confirm the network fence independently of syscall mediation. An
        # operator-created process has no OpenShell child seccomp filter but
        # still must not open a direct connection out of the workload Pod.
        service_ip = json.loads(kube('-n', 'test-services', 'get', 'service', 'mock-mcp', '-o', 'json'))['spec']['clusterIP']
        result = subprocess.run(['kubectl', '--context', CONTEXT, '-n', 'openshell', 'exec', 'default--mastra-pilot', '-c', 'sandbox', '--',
            'curl', '--noproxy', '*', '--silent', '--max-time', '3', 'http://'+service_ip+':8080/readyz'], env=ENV, text=True, capture_output=True)
        assert result.returncode != 0, 'direct workload traffic bypassed supervisor'
        record('enforced NetworkPolicy blocks a process launched outside mediation', curlExit=result.returncode)
        apply([{'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {'name': 'pilot-unrelated', 'namespace': 'openshell'},
            'spec': {'restartPolicy': 'Never', 'automountServiceAccountToken': False, 'containers': [{'name': 'probe',
                'image': 'localhost:5001/auth-demo/openshell-mastra:demo', 'imagePullPolicy': 'Never',
                'command': ['curl', '--noproxy', '*', '--silent', '--fail', '--max-time', '10', 'http://'+service_ip+':8080/readyz']}]}}])
        for _ in range(30):
            phase = json.loads(kube('-n', 'openshell', 'get', 'pod', 'pilot-unrelated', '-o', 'json'))['status']['phase']
            if phase in ('Succeeded', 'Failed'):
                break
            time.sleep(1)
        assert phase == 'Succeeded', 'unrelated workload connectivity changed'
        record('unrelated Pod retains direct cluster access')

        # Exercise the built-in MCP method/tool request evaluator separately
        # from the REST workload used for matched throughput measurements.
        p = policy(['mcp', 'external'])
        endpoint = p['network_policies']['mcp']['endpoints'][0]
        endpoint.update({'protocol': 'mcp', 'path': '/mcp', 'rules': [
            {'allow': {'method': 'initialize'}}, {'allow': {'method': 'tools/list'}},
            {'allow': {'method': 'tools/call', 'tool': 'read_status'}}]})
        path = CACHE/'mcp-policy.json'
        path.write_text(json.dumps(p))
        cli('policy', 'set', 'mastra-pilot', '--policy', str(path), '--wait')
        values = placeholders()
        for tool, expected in [('read_status', 200), ('delete_resource', 403)]:
            body = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': tool, 'arguments': {}}}
            before = audit('mcp')
            raw = cli('sandbox', 'exec', 'mastra-pilot', '--', 'curl', '--noproxy', '*', '--silent', '--show-error', '--max-time', '10',
                '--request', 'POST', '--header', 'Content-Type: application/json', '--header', 'MCP-Protocol-Version: 2025-11-25',
                '--header', 'authorization: Bearer '+values['mcp'], '--data', json.dumps(body), '--write-out', '\n%{http_code}', TARGETS['mcp'][0]+'/mcp')
            status = int(raw.rsplit('\n', 1)[1])
            assert status == expected and audit('mcp')-before == int(expected == 200), (tool, status)
            record('built-in MCP tool request filtering', tool=tool, status=status, upstreamDelta=int(expected == 200))
        set_policy(['mcp', 'external', 'public'])
        for binary in ('curl', 'node'):
            args = (['curl', '--noproxy', '*', '--silent', '--fail', '--max-time', '15', 'https://example.com/'] if binary == 'curl'
                    else ['node', '-e', 'console.log(await (await fetch("https://example.com/")).text())'])
            raw = cli('sandbox', 'exec', 'mastra-pilot', '--', *args)
            assert 'Example Domain' in raw
            record('public Internet HTTPS without credentials', binary=binary)
        report['completed'] = True
    except Exception as error:
        report['error'] = str(error)
        raise
    finally:
        try:
            set_policy(list(TARGETS))
        except Exception:
            report['completed'] = False
            report.setdefault('cleanupErrors', []).append('default policy restoration failed')
        finally:
            kube('-n', 'openshell', 'delete', 'pod', 'pilot-unrelated', '--ignore-not-found=true', '--wait=true')
            for h in handles:
                terminate(h)
            report['finishedAtUTC'] = datetime.now(timezone.utc).isoformat()
            (ROOT/'artifacts/openshell-functional-results.json').write_text(json.dumps(report, indent=2)+'\n')
    assert report['completed'], 'functional test cleanup did not finish'


if __name__ == '__main__':
    main()
