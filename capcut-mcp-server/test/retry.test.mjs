// Added in capcut-mcp-kit (2026): a lost reply is retried once with the same request_id.
import { test, after } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { KIT_HEALTH } from './helpers.mjs';

const seen = [];
let dropNext = true;
const server = createServer((req, res) => {
  let body = '';
  req.on('data', (c) => (body += c));
  req.on('end', () => {
    if (req.url === '/health') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      return res.end(JSON.stringify(KIT_HEALTH[1]));
    }
    seen.push(JSON.parse(body));
    if (dropNext) {
      dropNext = false;
      return req.socket.destroy(); // the change "happened" but the reply is lost
    }
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ success: true, error: '', output: { draft_id: 'd1', draft_url: 'u', revision: 2 } }));
  });
});
await new Promise((r) => server.listen(0, '127.0.0.1', r));
process.env.CAPCUT_API_URL = `http://127.0.0.1:${server.address().port}`;
process.env.CAPCUT_AUTOSTART = '0';
process.env.CAPCUT_MCP_TOKEN = 't';
const { apiClient } = await import('../dist/services/api-client.js');
after(() => new Promise((r) => server.close(r)));

test('a lost reply is sent again with the same request_id', async () => {
  const res = await apiClient.addText({ draft_id: 'd1', text: 'Hi', start: 0, end: 1 });
  assert.equal(res.success, true);
  assert.equal(seen.length, 2);
  assert.ok(seen[0].request_id);
  assert.equal(seen[0].request_id, seen[1].request_id);
});

test('different calls get different request_ids', async () => {
  await apiClient.addText({ draft_id: 'd1', text: 'A', start: 0, end: 1 });
  await apiClient.addText({ draft_id: 'd1', text: 'B', start: 0, end: 1 });
  const ids = seen.slice(-2).map((b) => b.request_id);
  assert.notEqual(ids[0], ids[1]);
});
