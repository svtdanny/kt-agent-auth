"""Translate the demo's complete HTTP grants to OpenShell's native schema."""
import json
from common import ROOT

MCP_HOST = 'mock-mcp.test-services.svc.cluster.local'
EXTERNAL_HOST = 'external-api.auth-demo.test'
CATALOG_HOST = 'catalog-api.demo.svc.cluster.local'
BINARIES = [{'path': '/usr/local/bin/node'}, {'path': '/usr/bin/curl'}]
TARGETS = {'mcp': ('http://'+MCP_HOST+':8080', 'POST', '/mcp', 'PILOT_MCP_TOKEN'),
           'external': ('https://'+EXTERNAL_HOST+':8443', 'GET', '/external', 'PILOT_EXTERNAL_TOKEN')}


def policy(names=('mcp', 'external')):
    rules = {}
    for name in names:
        host, port, method, path, ips = {
            'mcp': (MCP_HOST, 8080, 'POST', '/mcp', ['10.96.0.0/12']),
            'external': (EXTERNAL_HOST, 8443, 'GET', '/external', ['192.168.58.0/24']),
            'public': ('example.com', 443, 'GET', '/', []),
        }[name]
        endpoint = {'host': host, 'port': port, 'protocol': 'rest', 'enforcement': 'enforce',
                    'rules': [{'allow': {'method': method, 'path': path}}]}
        if ips:
            endpoint['allowed_ips'] = ips
        if name != 'public':
            endpoint['credential_binding'] = {'provider': 'pilot-'+name}
        rules[name] = {'endpoints': [endpoint], 'binaries': BINARIES}
    return {'version': 1, 'filesystem_policy': {'include_workdir': True,
        'read_only': ['/usr', '/lib', '/etc', '/app', '/proc', '/dev/urandom', '/var/log'],
        'read_write': ['/tmp', '/dev/null', '/sandbox']}, 'landlock': {'compatibility': 'hard_requirement'},
        'network_policies': rules}


def write_policy(path, names=('mcp', 'external')):
    path.write_text(json.dumps(policy(names), indent=2)+'\n')
    return path
