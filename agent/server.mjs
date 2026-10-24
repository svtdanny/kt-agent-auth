import http from 'node:http';
import { randomUUID } from 'node:crypto';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { Agent } from '@mastra/core/agent';
import { createTool } from '@mastra/core/tools';
import { z } from 'zod';

const origin = process.env.DEMO_API_ORIGIN ?? 'https://catalog-api.demo.svc.cluster.local';

// A deterministic model drives the real Mastra agent loop and real tool execution.
// Network enforcement is entirely outside this process.
function scriptedModel(input) {
  return {
    specificationVersion: 'v2', provider: 'demo', modelId: 'scripted-http', supportedUrls: {},
    async doGenerate(options) {
      const result = options.prompt.findLast(message => message.role === 'tool');
      return {
        content: result
          ? [{ type: 'text', text: JSON.stringify(result.content) }]
          : [{ type: 'tool-call', toolCallId: randomUUID(), toolName: 'httpRequest', input: JSON.stringify(input) }],
        finishReason: result ? 'stop' : 'tool-calls',
        usage: { inputTokens: 0, outputTokens: 0, totalTokens: 0 }, warnings: [],
      };
    },
  };
}

const schema = z.object({
	origin: z.string().url().optional(), execution: z.enum(['http', 'command']).optional(),
  method: z.string(), path: z.string(), headers: z.record(z.string(), z.string()).optional(),
  load: z.object({ concurrency: z.number().int().min(1).max(128), durationSeconds: z.number().int().min(1).max(20) }).optional(),
});

async function httpRequest({ method, path, headers, origin: requestedOrigin, execution }) {
  const target = (requestedOrigin ?? origin) + path;
  if (execution === 'command') {
    const args = ['--silent', '--show-error', '--noproxy', '*', '--max-time', '15',
      '--request', method, '--write-out', '\nAUTH_DEMO_HTTP_STATUS:%{http_code}'];
    const supplied = Object.entries(headers ?? {});
    if (!supplied.some(([name]) => name.toLowerCase() === 'authorization')) args.push('--header', 'Authorization: sandbox-placeholder');
    for (const [name, value] of supplied) args.push('--header', `${name}: ${value}`);
    if (method === 'POST') args.push('--data', JSON.stringify({ item: 'demo' }));
    args.push(target);
    // This is a real subprocess with proxy environment variables explicitly
    // ignored. Capture belongs to the Pod network, not the Mastra tool.
    const { stdout } = await promisify(execFile)('curl', args, { maxBuffer: 1 << 20 });
    const separator = '\nAUTH_DEMO_HTTP_STATUS:';
    const marker = stdout.lastIndexOf(separator);
    if (marker < 0) throw new Error('curl did not report HTTP status');
    return { status: Number(stdout.slice(marker + separator.length)), body: stdout.slice(0, marker) };
  }
  const response = await fetch(target, {
    method, headers: { authorization: 'sandbox-placeholder', ...headers },
    redirect: 'manual', signal: AbortSignal.timeout(15000),
    ...(method === 'POST' ? { body: JSON.stringify({ item: 'demo' }) } : {}),
  });
  return { status: response.status, body: await response.text() };
}

async function loadRequests(input) {
  const started = performance.now();
  const deadline = started + input.load.durationSeconds * 1000;
  const latencies = [], statuses = {}, errors = {}, denialReasons = {};
  let credentialFailures = 0, readerCredentials = 0, writerCredentials = 0;
  await Promise.all(Array.from({ length: input.load.concurrency }, async () => {
    while (performance.now() < deadline && latencies.length < 10000) {
      const start = performance.now();
      try {
        const response = await httpRequest(input);
        statuses[response.status] = (statuses[response.status] ?? 0) + 1;
        if (response.status >= 400) {
          const known = ['method or path is not permitted', 'agent is not permitted by this policy',
            'destination is not permitted', 'service account denies all requests',
            'session authorization denied', 'session authorization unavailable',
            'credential authorization denied', 'credential authorization unavailable',
            'missing or invalid gateway context', 'policy evaluation failed'];
          const reason = known.find(value => response.body === 'request denied: ' + value) ?? ('HTTP ' + response.status);
          denialReasons[reason] = (denialReasons[reason] ?? 0) + 1;
        }
        if (response.status === 200) {
          const event = JSON.parse(response.body);
          if (!event.credentialAccepted) credentialFailures++;
          if (event.credential === 'reader') readerCredentials++;
          if (event.credential === 'writer') writerCredentials++;
        }
      } catch (error) {
        statuses.transportError = (statuses.transportError ?? 0) + 1;
        errors[error.name] = (errors[error.name] ?? 0) + 1;
      }
      latencies.push(performance.now() - start);
    }
  }));
  const elapsedSeconds = (performance.now() - started) / 1000;
  latencies.sort((a, b) => a - b);
  const percentile = p => latencies[Math.max(0, Math.ceil(p * latencies.length) - 1)] ?? 0;
  return { completed: latencies.length, elapsedSeconds, requestsPerSecond: latencies.length / elapsedSeconds,
    concurrency: input.load.concurrency, offeredDurationSeconds: input.load.durationSeconds,
    statuses, errors, denialReasons, credentialFailures, readerCredentials, writerCredentials,
    requestLimitReached: latencies.length >= 10000,
    latencyMs: { p50: percentile(.50), p95: percentile(.95), p99: percentile(.99), max: latencies.at(-1) ?? 0 } };
}

async function invoke(input) {
  const calls = [];
  let load;
  const tool = createTool({
    id: 'http-request', description: 'Make an HTTP request to the demo catalog API',
    inputSchema: schema,
    execute: async (input) => {
      if (input.load) { load = await loadRequests(input); return load; }
      const result = await httpRequest(input);
      calls.push(result);
      return result;
    },
  });
  const agent = new Agent({
    id: 'mastra-http', name: 'Mastra HTTP demo',
    instructions: 'Execute the requested HTTP operation once and report its result.',
    model: scriptedModel(input), tools: { httpRequest: tool },
  });
  const result = await agent.generate('Execute the HTTP test.');
  return { agent: 'mastra-http', calls, ...(load ? { load } : {}), text: result.text };
}

http.createServer(async (request, response) => {
  if (request.url === '/readyz') { response.end('ready'); return; }
  if (request.url !== '/invoke' || request.method !== 'POST') { response.writeHead(404).end(); return; }
  try {
    let body = '';
    for await (const chunk of request) { body += chunk; if (body.length > 65536) throw new Error('request too large'); }
    const result = await invoke(schema.parse(JSON.parse(body)));
    response.writeHead(200, { 'content-type': 'application/json' }).end(JSON.stringify(result));
  } catch (error) {
    console.error('Mastra invocation failed:', error.message);
    response.writeHead(500, { 'content-type': 'application/json' }).end(JSON.stringify({ error: error.message }));
  }
}).listen(Number(process.env.MASTRA_PORT ?? 3000), '127.0.0.1');
