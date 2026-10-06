// Added in capcut-mcp-kit (2026): a fake backend for the Node tests.
import { createServer } from 'node:http';

/** Starts a server; `handler(req, body)` returns [status, json]. Sets CAPCUT_API_URL to it. */
export async function fakeBackend(handler) {
  const requests = [];
  const server = createServer((req, res) => {
    let body = '';
    req.on('data', (c) => (body += c));
    req.on('end', () => {
      const parsed = body ? JSON.parse(body) : undefined;
      requests.push({ method: req.method, url: req.url, headers: req.headers, body: parsed });
      const [status, json] = handler(req, parsed);
      res.writeHead(status, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify(json));
    });
  });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  process.env.CAPCUT_API_URL = `http://127.0.0.1:${server.address().port}`;
  process.env.CAPCUT_AUTOSTART = '0';
  return { server, requests, close: () => new Promise((r) => server.close(r)) };
}

export const KIT_HEALTH = [200, { success: true, output: { service: 'capcut-mcp-kit', api: 1, version: 'test' } }];
