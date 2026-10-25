// Imported only by the Mastra server, never by benchmark/administrative execs.
import http from 'node:http';
import fs from 'node:fs';
import { randomUUID } from 'node:crypto';

if (process.argv[1] === '/app/agent/server.mjs') {
  const directory = process.env.PILOT_STATE_DIR ?? '/sandbox/pilot-state';
  fs.mkdirSync(directory, { recursive: true });
  const persistentPath = directory + '/disk-token';
  const temporaryPath = '/tmp/pilot-volatile-token';
  if (!fs.existsSync(persistentPath)) fs.writeFileSync(persistentPath, randomUUID());
  if (!fs.existsSync(temporaryPath)) fs.writeFileSync(temporaryPath, randomUUID());
  const memoryToken = randomUUID();
  const startedAt = new Date().toISOString();
  let ticks = 0;
  setInterval(() => ticks++, 100).unref();
  http.createServer((request, response) => {
    response.writeHead(200, { 'content-type': 'application/json' });
    response.end(JSON.stringify({ memoryToken, ticks, startedAt, pid: process.pid,
      persistentToken: fs.readFileSync(persistentPath, 'utf8'),
      temporaryToken: fs.readFileSync(temporaryPath, 'utf8'), directory }));
  }).listen(3001, '127.0.0.1');
}
