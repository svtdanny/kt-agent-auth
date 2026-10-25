#!/usr/bin/env python3
"""Measure workspace/process state across native OpenShell and kagent lifecycles."""
from datetime import datetime, timezone
import json
import subprocess
import sys
import time
import uuid

from common import ROOT, CACHE, cli, kube, forward, terminate
from test_functional import request

sys.path.insert(0, str(ROOT/'scripts'))
import test_e2e as demo


def native_helper(action, sid):
    name = 'pilot-kagent-lifecycle'
    demo.kubectl('-n', 'demo', 'delete', 'pod', name, '--ignore-not-found=true', '--wait=true')
    pod = {'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {'name': name, 'namespace': 'demo'}, 'spec': {
        'serviceAccountName': 'auth-control-plane', 'restartPolicy': 'Never', 'activeDeadlineSeconds': 240,
        'containers': [{'name': 'helper', 'image': 'localhost:5001/auth-demo/kagent-lifecycle:demo', 'imagePullPolicy': 'Never',
                        'args': ['-action', action, '-session', sid]}]}}
    subprocess.run(['kubectl', '--context', 'auth-demo', 'apply', '-f', '-'], env=demo.ENV, input=json.dumps(pod), text=True, check=True, stdout=subprocess.DEVNULL)
    try:
        for _ in range(240):
            phase = json.loads(demo.kubectl('-n', 'demo', 'get', 'pod', name, '-o', 'json'))['status']['phase']
            if phase in ('Succeeded', 'Failed'):
                break
            time.sleep(1)
        output = demo.kubectl('-n', 'demo', 'logs', name)
        assert phase == 'Succeeded', ('native lifecycle helper', action, output)
        return json.loads(output)
    finally:
        demo.kubectl('-n', 'demo', 'delete', 'pod', name, '--ignore-not-found=true', '--wait=true')


def main():
    report = {'startedAtUTC': datetime.now(timezone.utc).isoformat(), 'completed': False, 'openshell': {}, 'kagent': {}}
    handles, sid, token = [], None, None
    try:
        handles += [forward('openshell', 'service/openshell', '18551:8080'), forward('openshell', 'pod/default--mastra-pilot', '18547:3001')]
        time.sleep(1)
        initial = json.loads(cli('sandbox', 'get', 'mastra-pilot', '--output', 'json'))
        status, before = request(18547, '/state')
        assert status == 200
        # This is a diagnostic process freeze, explicitly not an OpenShell API
        # checkpoint. RAM and the Pod remain allocated throughout it.
        cli('sandbox', 'exec', 'mastra-pilot', '--', 'sh', '-c', 'kill -STOP '+str(before['pid']))
        time.sleep(3)
        cli('sandbox', 'exec', 'mastra-pilot', '--', 'sh', '-c', 'kill -CONT '+str(before['pid']))
        status, after_pause = request(18547, '/state')
        assert status == 200 and before['memoryToken'] == after_pause['memoryToken']
        report['openshell']['manualProcessFreeze'] = {'seconds': 3, 'memoryPreserved': True, 'releasesRAM': False,
            'nativeOpenShellPauseAPI': False, 'before': before, 'after': after_pause}
        print('PASS: manual process freeze retains RAM; it is not a durable suspend API', flush=True)
        start = time.monotonic()
        cli('sandbox', 'stop', 'mastra-pilot')
        stop_seconds = time.monotonic()-start
        stopped = json.loads(cli('sandbox', 'get', 'mastra-pilot', '--output', 'json'))
        assert stopped['phase'] == 'Stopped' and stopped['id'] == initial['id']
        assert not kube('-n', 'openshell', 'get', 'pod', 'default--mastra-pilot', '--ignore-not-found=true').strip()
        terminate(handles.pop())
        time.sleep(3)
        start = time.monotonic()
        cli('sandbox', 'start', 'mastra-pilot')
        start_seconds = time.monotonic()-start
        handles.append(forward('openshell', 'pod/default--mastra-pilot', '18547:3001'))
        for _ in range(60):
            try:
                status, after = request(18547, '/state')
                if status == 200:
                    break
            except OSError:
                pass
            time.sleep(1)
        resumed = json.loads(cli('sandbox', 'get', 'mastra-pilot', '--output', 'json'))
        assert initial['id'] == resumed['id']
        assert before['persistentToken'] == after['persistentToken']
        assert before['memoryToken'] != after['memoryToken']
        assert before['temporaryToken'] != after['temporaryToken']
        report['openshell']['stopStart'] = {'stopWallSeconds': stop_seconds, 'startWallSeconds': start_seconds,
            'sameSandboxId': True, 'persistentWorkspacePreserved': True, 'memoryPreserved': False, 'tmpPreserved': False,
            'oldGeneration': initial['annotations']['internal.openshell.ai/runtime-generation'],
            'newGeneration': resumed['annotations']['internal.openshell.ai/runtime-generation'], 'before': before, 'after': after}
        print('PASS: OpenShell stop/start preserves workspace and ID, restarts memory and temporary rootfs', flush=True)

        # A separate Harness/Agent leaves the original demo's prepared image and
        # Sessions untouched. The state probe is an image-only test instrument.
        harness = json.loads(demo.kubectl('-n', 'demo', 'get', 'harness', 'mastra', '-o', 'json'))
        harness['metadata'] = {'name': 'mastra-lifecycle-pilot', 'namespace': 'demo'}
        harness.pop('status', None)
        image = next(d for d in json.loads(subprocess.check_output(['docker', 'inspect', 'localhost:5001/auth-demo/openshell-mastra:demo'], text=True))[0]['RepoDigests'] if d.startswith('localhost:5001/'))
        harness['spec']['workload']['image'] = image
        harness['spec']['env'] += [{'name': 'NODE_OPTIONS', 'value': '--import=/app/pilot-state.mjs'}, {'name': 'PILOT_STATE_DIR', 'value': '/data/pilot-state'}]
        agent = json.loads(demo.kubectl('-n', 'demo', 'get', 'agent.api.kagent.dev', 'mastra-http', '-o', 'json'))
        agent['metadata'] = {'name': 'mastra-lifecycle-pilot', 'namespace': 'demo'}
        agent.pop('status', None)
        agent['spec']['harnessRef']['name'] = 'mastra-lifecycle-pilot'
        subprocess.run(['kubectl', '--context', 'auth-demo', 'apply', '-f', '-'], env=demo.ENV,
                       input=json.dumps({'apiVersion': 'v1', 'kind': 'List', 'items': [harness, agent]}), text=True, check=True, stdout=subprocess.DEVNULL)
        for _ in range(150):
            ready = json.loads(demo.kubectl('-n', 'demo', 'get', 'agent.api.kagent.dev', 'mastra-lifecycle-pilot', '-o', 'json')).get('status', {})
            if ready.get('workloadImage') == image and ready.get('desiredRevision') and ready['desiredRevision'] == ready.get('latestSuccessfulRevision'):
                break
            time.sleep(2)
        else:
            raise TimeoutError('native pilot image not prepared')
        with open(CACHE/'native-lifecycle-forward.log', 'w') as log:
            handles.append(subprocess.Popen(['kubectl', '--context', 'auth-demo', '-n', 'demo', 'port-forward', 'service/auth-control-plane', '18443:8443'], env=demo.ENV, stdout=log, stderr=log))
        time.sleep(1)
        token = demo.kubectl('-n', 'demo', 'create', 'token', 'test-user', '--audience=auth-demo', '--duration=1h').strip()
        status, binding = demo.request(18443, '/v1/sessions', {'agent': {'namespace': 'demo', 'name': 'mastra-lifecycle-pilot'},
                                      'serviceAccount': 'multi-reader', 'requestId': 'lifecycle-'+uuid.uuid4().hex}, token)
        assert status == 200
        sid = binding['sessionId']

        def state():
            status, value = demo.request(18443, '/v1/sessions/'+sid+'/invoke', {'origin': 'http://127.0.0.1:3001', 'method': 'GET', 'path': '/state'}, token)
            calls = demo.calls(value)
            assert status == 200 and calls and calls[0]['status'] == 200, ('native state probe failed', status)
            return json.loads(calls[0]['body'])

        before = state()
        suspended = native_helper('suspend', sid)
        assert suspended['state'] == 'RUNTIME_STATE_SUSPENDED'
        status, current = demo.context_for(sid)
        assert status == 200 and current['binding']['active'], 'suspended Session lost authorization binding'
        time.sleep(3)
        resumed = native_helper('resume', sid)
        assert resumed['state'] == 'RUNTIME_STATE_READY'
        after = state()
        assert before['persistentToken'] == after['persistentToken']
        assert before['memoryToken'] != after['memoryToken']
        assert before['temporaryToken'] != after['temporaryToken']
        # Existing permission/key insertion must still work with the same ID.
        status, value = demo.request(18443, '/v1/sessions/'+sid+'/invoke', {'origin': 'http://mock-mcp.test-services.svc.cluster.local:8080', 'method': 'POST', 'path': '/mcp'}, token)
        calls = demo.calls(value)
        assert status == 200 and calls and calls[0]['status'] == 200
        report['kagent'] = {'sessionId': sid, 'sameSessionId': suspended['sessionId'] == resumed['sessionId'] == sid,
            'suspend': suspended, 'resume': resumed, 'persistentDataPreserved': True, 'memoryPreserved': False, 'tmpPreserved': False,
            'bindingPreserved': True, 'authorizedTrafficAfterResume': True, 'before': before, 'after': after,
            'configuredSnapshotFidelity': 'SNAPSHOT_FIDELITY_VOLUMES'}
        print('PASS: kagent suspend/resume preserves /data, Session ID, and permissions; process RAM restarts', flush=True)
        report['completed'] = True
    except Exception as error:
        report['error'] = str(error)
        raise
    finally:
        if sid:
            try:
                demo.delete_session(token, sid)
            except Exception:
                report['cleanupError'] = 'native Session cleanup failed'
        demo.close_connections()
        demo.kubectl('-n', 'demo', 'delete', 'agent.api.kagent.dev/mastra-lifecycle-pilot', 'harness/mastra-lifecycle-pilot', '--ignore-not-found=true')
        for h in handles:
            terminate(h)
        report['finishedAtUTC'] = datetime.now(timezone.utc).isoformat()
        (ROOT/'artifacts/openshell-lifecycle-comparison.json').write_text(json.dumps(report, indent=2)+'\n')


if __name__ == '__main__':
    main()
