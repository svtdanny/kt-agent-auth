#!/usr/bin/env python3
"""Check Vault-backed rotation, warm reads, and absence of real keys in workloads."""
from datetime import datetime, timezone
import json
import secrets
import subprocess
import sys
import time

from common import ROOT, CACHE, ENV, CLI, cli, kube, forward, terminate
from setup import Vault
from test_functional import placeholders, invoke, request


def main():
    report = {'startedAtUTC': datetime.now(timezone.utc).isoformat(), 'completed': False, 'tests': []}
    handles = [forward('openshell', 'service/openshell', '18551:8080'), forward('openshell', 'pod/default--mastra-pilot', '18546:3000'),
               forward('openshell', 'pod/default--mastra-pilot', '18547:3001'), forward('demo', 'service/auth-vault', '18500:8200')]
    original = None
    modified = False
    def record(name, **evidence):
        report['tests'].append({'test': name, 'passed': True, **evidence})
        print('PASS:', name, flush=True)
    def update(value):
        result = subprocess.run([CLI, 'provider', 'update', 'pilot-mcp', '--credential', 'PILOT_MCP_TOKEN', '--wait'],
                                env={**ENV, 'PILOT_MCP_TOKEN': value}, text=True, capture_output=True)
        assert result.returncode == 0, 'provider update failed'
    try:
        time.sleep(1)
        admin = Vault()
        admin.token = json.loads((CACHE/'vault-admin.json').read_text())['root_token']
        if 'pilot/' not in admin.request('GET', 'sys/audit'):
            admin.request('PUT', 'sys/audit/pilot', {'type': 'file', 'options': {'file_path': '/vault/data/pilot-audit.jsonl'}})
        def reads():
            raw = kube('-n', 'demo', 'exec', 'deployment/auth-vault', '--', 'cat', '/vault/data/pilot-audit.jsonl')
            return sum(e.get('type') == 'request' and e.get('request', {}).get('operation') == 'read'
                       and e['request'].get('path', '').startswith('secret/data/openshell/provider-credentials/')
                       for e in (json.loads(line) for line in raw.splitlines()))
        sys.path.insert(0, str(ROOT/'scripts'))
        from vault_demo import Admin
        with Admin() as source:
            original = source.read('mcp-token')
            real_keys = [original, source.read('external-token')]
        values = placeholders()
        raw = cli('sandbox', 'exec', 'mastra-pilot', '--', 'env')
        assert all(value not in raw for value in real_keys)
        assert values['mcp'] != original
        record('workload environment contains opaque placeholders rather than real Vault keys')
        status, before_state = request(18547, '/state')
        assert status == 200
        before = reads()
        for _ in range(50):
            status, _, calls = invoke('http', 'mcp', values)
            assert status == 200 and calls[0]['status'] == 200
        after = reads()
        assert after == before, ('unexpected warm Vault reads', before, after)
        record('50 warm HTTP requests cause zero Vault credential reads', requests=50, vaultReads=after-before)
        update('invalid-pilot-key-'+secrets.token_urlsafe(24))
        modified = True
        updated = placeholders()
        assert updated['mcp'] == values['mcp'], 'stable placeholder changed during value-only rotation'
        for mode in ('http', 'command'):
            status, _, calls = invoke(mode, 'mcp', values)
            assert status == 200 and calls[0]['status'] == 401
            record('value-only rotation reaches upstream without workload restart', mode=mode, status=401)
        update(original)
        modified = False
        for mode in ('http', 'command'):
            status, _, calls = invoke(mode, 'mcp', values)
            assert status == 200 and calls[0]['status'] == 200
            record('restored Vault-backed provider recovers with the original placeholder', mode=mode, status=200)
        status, after_state = request(18547, '/state')
        assert status == 200 and before_state['memoryToken'] == after_state['memoryToken']
        record('credential rotation retains the same application process and RAM marker')
        # Real role, verified against this pilot cluster, cannot read a key
        # outside OpenShell-managed storage even when the key does not exist.
        jwt = kube('-n', 'openshell', 'create', 'token', 'openshell', '--duration=10m').strip()
        login = admin.request('POST', 'auth/kubernetes/login', {'role': 'openshell-pilot', 'jwt': jwt})
        admin.token = login['auth']['client_token']
        try:
            admin.request('GET', 'secret/data/outside-pilot')
        except RuntimeError as error:
            assert 'HTTP 403' in str(error)
        else:
            raise AssertionError('Vault role accessed an unauthorized path')
        record('OpenShell gateway Vault role denies paths outside managed provider storage')
        report['completed'] = True
    except Exception as error:
        report['error'] = str(error)
        raise
    finally:
        if modified and original:
            update(original)
        for handle in handles:
            terminate(handle)
        report['finishedAtUTC'] = datetime.now(timezone.utc).isoformat()
        (ROOT/'artifacts/openshell-credential-results.json').write_text(json.dumps(report, indent=2)+'\n')


if __name__ == '__main__':
    main()
