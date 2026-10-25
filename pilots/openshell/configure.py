#!/usr/bin/env python3
"""Configure native endpoint-bound Vault providers and launch the Mastra image."""
import json
import subprocess
import sys
import time

import yaml
from common import CACHE, ROOT, ENV, CLI, cli, forward, terminate
from policy import TARGETS, write_policy


def main():
    handle = forward('openshell', 'service/openshell', '18551:8080')
    try:
        for _ in range(60):
            try:
                cli('status', stderr=subprocess.DEVNULL)
                break
            except subprocess.CalledProcessError:
                time.sleep(1)
        else:
            raise TimeoutError('OpenShell gateway unavailable')
        sys.path.insert(0, str(ROOT/'scripts'))
        from vault_demo import Admin
        with Admin() as vault:
            values = {'mcp': vault.read('mcp-token'), 'external': vault.read('external-token')}
        for name, (_, _, _, env) in TARGETS.items():
            profile = {'id': 'pilot-'+name, 'display_name': 'Pilot '+name, 'category': 'data',
                       'credentials': [{'name': 'api_token', 'env_vars': [env], 'required': True, 'stable_placeholder': True}],
                       'endpoints': [], 'binaries': ['/usr/local/bin/node', '/usr/bin/curl']}
            path = ROOT/'pilots/openshell'/('profile-'+name+'.yaml')
            path.write_text(yaml.safe_dump(profile, sort_keys=False))
            try:
                cli('profile', 'describe', 'pilot-'+name, stderr=subprocess.DEVNULL)
            except subprocess.CalledProcessError:
                cli('profile', 'import', '--file', str(path))
            result = subprocess.run([CLI, 'provider', 'create', '--name', 'pilot-'+name, '--type', 'pilot-'+name, '--credential', env],
                                    env={**ENV, env: values[name]}, text=True, capture_output=True)
            if result.returncode and 'already exists' not in result.stderr:
                raise RuntimeError('OpenShell provider creation failed: '+result.stderr)
            print('Vault-backed provider configured:', name, flush=True)
        path = write_policy(ROOT/'pilots/openshell/policy.json')
        existing = json.loads(cli('sandbox', 'list', '--output', 'json'))
        (CACHE/'sandboxes-before.json').write_text(json.dumps(existing, indent=2)+'\n')
        if not any(s.get('name') == 'mastra-pilot' for s in (existing if isinstance(existing, list) else existing.get('sandboxes', []))):
            print(cli('sandbox', 'create', '--name', 'mastra-pilot', '--from', 'localhost:5001/auth-demo/openshell-mastra:demo',
                '--cpu', '1', '--memory', '512Mi', '--policy', str(path), '--provider', 'pilot-mcp', '--provider', 'pilot-external',
                '--env', 'NODE_OPTIONS=--import=/app/pilot-state.mjs', '--env', 'PILOT_STATE_DIR=/sandbox/pilot-state',
                '--detach', '--', 'node', '/app/agent/server.mjs'), flush=True)
        value = json.loads(cli('sandbox', 'get', 'mastra-pilot', '--output', 'json'))
        print('Sandbox:', {k: value[k] for k in ('name', 'id', 'phase')}, flush=True)
    finally:
        terminate(handle)


if __name__ == '__main__':
    main()
