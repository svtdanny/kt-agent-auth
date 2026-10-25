#!/usr/bin/env python3
"""Summarize measured stages without averaging latency percentiles."""
from collections import defaultdict
import json

from common import ROOT


def main():
    root = ROOT/'artifacts'
    performance = json.loads((root/'openshell-performance-comparison.json').read_text())
    assert performance['completed'] and not performance.get('cleanupErrors')
    groups = defaultdict(list)
    for stage in performance['stages']:
        groups[(stage['concurrency'], stage['target'], stage['source'])].append(stage)
    summary = {'totalRequests': sum(s['completed'] for s in performance['stages']), 'stages': len(performance['stages']),
               'elapsedSeconds': performance['elapsedSeconds'], 'assessment': performance['assessment'], 'groups': [],
               'latencyAggregation': 'Min/max of the two individual stage p95 values; not a pooled percentile.',
               'rateAggregation': 'Total completed requests divided by total tool execution time across repetitions.'}
    for (concurrency, target, source), stages in groups.items():
        assert len(stages) == 2
        summary['groups'].append({'source': source, 'target': target, 'concurrency': concurrency,
            'requests': sum(s['completed'] for s in stages),
            'requestsPerSecond': sum(s['completed'] for s in stages)/sum(s['elapsedSeconds'] for s in stages),
            'p95MinMs': min(s['latencyMs']['p95'] for s in stages), 'p95MaxMs': max(s['latencyMs']['p95'] for s in stages)})
    for name, path in [('openshellFunctional', 'openshell-functional-results.json'), ('openshellCredentials', 'openshell-credential-results.json'),
                       ('baselineFunctional', 'openshell-baseline-shared-gateway-results.json')]:
        report = json.loads((root/path).read_text())
        assert report['completed']
        summary[name] = {'completed': report['completed'], 'checks': len(report['tests'])}
    lifecycle = json.loads((root/'openshell-lifecycle-comparison.json').read_text())
    assert lifecycle['completed']
    summary['lifecycle'] = {'openshell': {k: lifecycle['openshell']['stopStart'][k] for k in [
        'sameSandboxId', 'persistentWorkspacePreserved', 'memoryPreserved', 'tmpPreserved']},
        'kagent': {k: lifecycle['kagent'][k] for k in ['sameSessionId', 'persistentDataPreserved', 'memoryPreserved', 'tmpPreserved', 'bindingPreserved']}}
    (root/'openshell-comparison-summary.json').write_text(json.dumps(summary, indent=2)+'\n')
    def value(source, target):
        return next(g for g in summary['groups'] if g['source'] == source and g['target'] == target and g['concurrency'] == 8)
    rows = ['| Workload | Target | Our enrolled Pod, req/s | Our kagent, req/s | OpenShell, req/s | OpenShell p95 range |',
            '| --- | --- | ---: | ---: | ---: | ---: |']
    for mode in ('fetch', 'curl'):
        for target in ('mcp', 'external'):
            ours = value('ours/pod-'+mode, target)
            oshell = value('openshell/'+mode, target)
            native = f'{value("ours/kagent-fetch", target)["requestsPerSecond"]:.2f}' if mode == 'fetch' else '—'
            rows.append(f'| {mode} | {target} | {ours["requestsPerSecond"]:.2f} | {native} | {oshell["requestsPerSecond"]:.2f} | {oshell["p95MinMs"]:.2f}–{oshell["p95MaxMs"]:.2f} ms |')
    table = '\n'.join(rows)
    readme = ROOT/'pilots/openshell/README.md'
    if readme.exists():
        text = readme.read_text()
        start = '<!-- comparison-table:start -->'
        end = '<!-- comparison-table:end -->'
        if start in text and end in text:
            text = text[:text.index(start)+len(start)]+'\n\n'+table+'\n\n'+text[text.index(end):]
            readme.write_text(text)
    print(table)
    print('Verified comparison summary:', summary['totalRequests'], 'requests across', summary['stages'], 'stages.')


if __name__ == '__main__':
    main()
