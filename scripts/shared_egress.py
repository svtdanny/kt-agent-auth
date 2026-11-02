"""Hardcoded two-project Envoy ext_proc routing experiment; no Substrate overlay."""
import copy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from k8s.render import config, envoy

FILTER = 'auth.agent.ext_proc'
IDENTITY = 'dev.ate.actor.identity'
# Deliberately fixed for the MVP. These DNS names select each namespace's proxy
# bundle's authorizer port, not the application's destination HTTP endpoint.
AUTHORIZERS = {
    'auth-agent-a': 'authorizer-extproc.auth-agent-a.svc',
    'auth-agent-b': 'authorizer-extproc.auth-agent-b.svc',
}


def bootstrap():
    result = envoy(config(NAMESPACE='ate-system'))
    result['node'] = {'id': 'shared-egress-mvp', 'cluster': 'shared-egress-mvp'}
    for listener in result['static_resources']['listeners']:
        if listener['name'] != 'mitm_listener':
            continue
        for chain in listener['filter_chains']:
            hcm = chain['filters'][0]['typed_config']
            processor = next(f for f in hcm['http_filters'] if f['name'] == FILTER)
            # A missing per-route override reaches an unavailable local endpoint
            # and fails closed; it never falls back to another project's processor.
            processor['typed_config']['message_timeout'] = '25s'
            processor['typed_config']['grpc_service']['timeout'] = '25s'
            processor['typed_config']['disable_clear_route_cache'] = True
            for host in hcm['route_config']['virtual_hosts']:
                if host['name'] == 'native_platform':
                    continue
                original = host['routes']
                host['routes'] = []
                for namespace in AUTHORIZERS:
                    for route in original:
                        item = copy.deepcopy(route)
                        item['match'].setdefault('filter_state', []).append({
                            'key': IDENTITY,
                            'string_match': {'prefix': 'spiffe://substrate-actor.local/worker-for-actor/' + namespace + '/'},
                        })
                        item.setdefault('typed_per_filter_config', {})[FILTER] = {
                            '@type': 'type.googleapis.com/envoy.extensions.filters.http.ext_proc.v3.ExtProcPerRoute',
                            'overrides': {'grpc_service': {'envoy_grpc': {'cluster_name': 'authorizer_' + namespace}}},
                        }
                        host['routes'].append(item)
    template = next(c for c in result['static_resources']['clusters'] if c['name'] == 'agent_authorizer')
    for namespace, dns in AUTHORIZERS.items():
        cluster = copy.deepcopy(template)
        name = 'authorizer_' + namespace
        cluster['name'] = name
        cluster['type'] = 'STRICT_DNS'
        cluster['load_assignment']['cluster_name'] = name
        address = cluster['load_assignment']['endpoints'][0]['lb_endpoints'][0]['endpoint']['address']['socket_address']
        address.update(address=dns, port_value=50052)
        tls = cluster['transport_socket']['typed_config']
        tls['sni'] = 'authorizer.' + namespace + '.svc'
        context = tls['common_tls_context']
        # Existing per-project gateway client identities keep the project
        # authorizer's existing peer checks unchanged for this first experiment.
        for cert in context['tls_certificates']:
            cert['certificate_chain']['filename'] = '/auth-' + namespace + '/tls.crt'
            cert['private_key']['filename'] = '/auth-' + namespace + '/tls.key'
        context['validation_context']['trusted_ca']['filename'] = '/auth-' + namespace + '/ca.crt'
        context['validation_context']['match_typed_subject_alt_names'] = [
            {'san_type': 'DNS', 'matcher': {'exact': tls['sni']}}
        ]
        result['static_resources']['clusters'].append(cluster)
    # Default is deliberately unusable; no project is selected by default.
    template.pop('transport_socket', None)
    template['load_assignment']['endpoints'][0]['lb_endpoints'][0]['endpoint']['address']['socket_address'].update(
        address='127.0.0.1', port_value=1)
    return result


def authorizer_service(namespace):
    return {'apiVersion': 'v1', 'kind': 'Service', 'metadata': {'name': 'authorizer-extproc', 'namespace': namespace},
            'spec': {'selector': {'app': 'agent-egress'},
                     'ports': [{'name': 'grpc-extproc', 'port': 50052, 'targetPort': 50052}]}}


if __name__ == '__main__':
    import yaml
    print(yaml.safe_dump(bootstrap(), sort_keys=False), end='')
