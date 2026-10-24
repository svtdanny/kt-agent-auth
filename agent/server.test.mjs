import { test } from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { spawn } from 'node:child_process';
import { once } from 'node:events';

test('real Mastra loop executes an ordinary HTTP fetch', async () => {
  const received = [];
  const upstream = http.createServer((req, res) => {
    received.push({ method: req.method, path: req.url, authorization: req.headers.authorization });
    res.writeHead(200, { 'content-type': 'application/json' }).end(req.headers['x-load-test']
      ? '{"credentialAccepted":true,"credential":"reader"}' : '{"ok":true}');
  }).listen(0, '127.0.0.1');
  await once(upstream, 'listening');
  const child = spawn(process.execPath, ['server.mjs'], {
    cwd: new URL('.', import.meta.url),
    env: { ...process.env, MASTRA_PORT: '13000', DEMO_API_ORIGIN: `http://127.0.0.1:${upstream.address().port}` },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  let logs = '';
  child.stderr.on('data', data => { logs += data; });
  try {
    for (let i = 0; i < 100; i++) {
      assert.equal(child.exitCode, null, logs);
      try { if ((await fetch('http://127.0.0.1:13000/readyz')).ok) break; } catch {}
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    const response = await fetch('http://127.0.0.1:13000/invoke', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ method: 'GET', path: '/catalog' }),
    });
    const result = await response.json();
    assert.equal(response.status, 200, JSON.stringify(result) + logs);
    assert.deepEqual(result.calls, [{ status: 200, body: '{"ok":true}' }]);
    assert.deepEqual(received, [{ method: 'GET', path: '/catalog', authorization: 'sandbox-placeholder' }]);

    const command = await fetch('http://127.0.0.1:13000/invoke', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ execution: 'command', origin: `http://127.0.0.1:${upstream.address().port}`, method: 'POST', path: '/mcp' }),
    });
    assert.equal(command.status, 200);
    assert.deepEqual((await command.json()).calls, [{ status: 200, body: '{"ok":true}' }]);
    assert.deepEqual(received[1], { method: 'POST', path: '/mcp', authorization: 'sandbox-placeholder' });

    const loadResponse = await fetch('http://127.0.0.1:13000/invoke', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ method: 'GET', path: '/catalog', headers: { 'X-Load-Test': '1' },
        load: { concurrency: 4, durationSeconds: 1 } }),
    });
    const batch = await loadResponse.json();
    assert.equal(loadResponse.status, 200, JSON.stringify(batch) + logs);
    assert.ok(batch.load.completed > 4);
    assert.equal(batch.load.completed, received.length - 2);
    assert.equal(batch.load.statuses['200'], batch.load.completed);
    assert.equal(batch.load.readerCredentials, batch.load.completed);
    assert.equal(batch.load.credentialFailures, 0);
    assert.ok(batch.load.elapsedSeconds >= 1);
    assert.ok(batch.load.latencyMs.p99 >= batch.load.latencyMs.p50);
  } finally {
    child.kill('SIGTERM');
    await once(child, 'exit');
    upstream.close();
  }
});
