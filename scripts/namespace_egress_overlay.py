#!/usr/bin/env python3
"""Add namespace authorizer routing to a pinned upstream gateway manifest.

No namespace registry, application route rewrites, or upstream source patches.
"""
import argparse
import copy
import json
from pathlib import Path
import subprocess

import yaml

ROOT = Path(__file__).resolve().parents[1]
ADDITIONS = ROOT / 'k8s/shared-egress'
FILTER = 'auth.agent.ext_proc'
VERSION = 'v0.6.0-alpha3'


def additions():
    value = yaml.safe_load((ADDITIONS / 'namespace-router.yaml').read_text())
    hcm = value['listener']['filter_chains'][0]['filters'][0]['typed_config']
    hcm['http_filters'][0]['typed_config']['default_source_code']['inline_string'] = (
        ADDITIONS / 'namespace-router.lua').read_text()
    return value


def overlay(bootstrap):
    """Copy the upstream bootstrap; insert only our filter and new resources."""
    result = copy.deepcopy(bootstrap)
    extra = additions()
    resources = result['static_resources']
    names = {item['name'] for item in resources['listeners'] + resources['clusters']}
    if 'additional_egress_ext_proc' in names:
        raise ValueError('disable additionalExtprocService before applying this overlay')
    if names & {extra['listener']['name'], *(c['name'] for c in extra['clusters'])}:
        raise ValueError('namespace overlay already applied or resource names conflict')
    if not any(e['name'] == 'envoy.bootstrap.internal_listener'
               for e in result.get('bootstrap_extensions', [])):
        raise ValueError('upstream internal-listener bootstrap extension missing')
    listener = next(l for l in resources['listeners'] if l['name'] == 'mitm_listener')
    chains = {c.get('name'): c for c in listener['filter_chains']}
    if set(chains) != {'egress_tls_mitm', 'egress_cleartext', 'egress_passthrough'}:
        raise ValueError('upstream HTTP/HTTPS filter chains changed; review overlay')
    for chain_name in ('egress_tls_mitm', 'egress_cleartext'):
        chain = chains[chain_name]
        hcm = chain['filters'][0]['typed_config']
        filters = hcm['http_filters']
        if any(f['name'] == FILTER for f in filters):
            raise ValueError('custom processor already present; use an unmodified base')
        position = next(i for i, f in enumerate(filters)
                        if f['name'] == 'envoy.filters.http.dynamic_forward_proxy')
        expected_policy = 'egress_policy_mitm' if chain_name == 'egress_tls_mitm' else 'egress_policy_cleartext'
        if not any(f['typed_config'].get('stat_prefix') == expected_policy
                   for f in filters[:position]):
            raise ValueError('native policy processor missing before injection point')
        filters.insert(position, copy.deepcopy(extra['processor']))
    resources['listeners'].append(extra['listener'])
    resources['clusters'].extend(extra['clusters'])
    return result


def pinned_manifest():
    info = json.loads(subprocess.check_output(
        ['go', 'list', '-m', '-json', 'go.ate.dev/substrate'], cwd=ROOT, text=True))
    module = info.get('Replace', info)
    if module['Version'] != VERSION:
        raise ValueError('review overlay before changing the pinned Substrate version')
    return Path(module['Dir']) / 'manifests/ate-install/atenet-egress.yaml'


def render(text):
    documents = list(yaml.safe_load_all(text))
    configs = [doc for doc in documents if isinstance(doc, dict) and
               doc.get('kind') == 'ConfigMap' and 'envoy.yaml' in doc.get('data', {})]
    if len(configs) != 1:
        raise ValueError('expected exactly one Envoy ConfigMap in upstream manifests')
    target = configs[0]['data']
    extra = additions()
    if target.keys() & extra['secrets'].keys():
        raise ValueError('authorizer SDS filenames already present')
    target['envoy.yaml'] = yaml.safe_dump(overlay(yaml.safe_load(target['envoy.yaml'])), sort_keys=False)
    target.update({name: yaml.safe_dump(secret, sort_keys=False)
                   for name, secret in extra['secrets'].items()})
    return yaml.safe_dump_all(documents, sort_keys=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, help='Upstream rendered manifest; defaults to pinned module')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    source = args.input or pinned_manifest()
    args.output.write_text(render(source.read_text()))


if __name__ == '__main__':
    main()
