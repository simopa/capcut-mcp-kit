// Added in capcut-mcp-kit (2026): following the transcript's "call again with from_time=..." hint
// reads every block once and always moves on.
import { test, after } from 'node:test';
import assert from 'node:assert/strict';
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js';
import { fakeBackend, KIT_HEALTH } from './helpers.mjs';

// Ends past whole seconds: rounding the cursor down would bring the last block back
const BLOCKS = [
  { start: 0.37, end: 10.5, text: 'One.' },
  { start: 10.5, end: 21.37, text: 'Two.' },
  { start: 21.4, end: 30.91, text: 'Three.' },
  { start: 31.2, end: 40.000001, text: 'Four.' }
];

// The backend's paging: blocks overlapping from_time, one per page here; next_from = last block's end
const fake = await fakeBackend((req, body) => {
  if (req.url === '/health') return KIT_HEALTH;
  const from = body.page.from_time;
  const blocks = BLOCKS.filter((b) => b.end > from && (body.page.to_time === undefined || b.start < body.page.to_time));
  const page = blocks.slice(0, 1);
  const next_from = blocks.length > 1 ? page[0].end : null;
  return [200, { success: true, error: '', output: { status: 'done', page: {
    source: '/v.mp4', language: 'it', duration: 41, engine: 'test', model: 'turbo', blocks: page, next_from } } }];
});
const { registerMediaTools } = await import('../dist/tools/media.js');
const server = new McpServer({ name: 'test', version: '0' });
registerMediaTools(server);
const client = new Client({ name: 'test', version: '0' });
const [a, b] = InMemoryTransport.createLinkedPair();
await Promise.all([server.connect(a), client.connect(b)]);
after(async () => { await client.close(); await fake.close(); });

async function transcribe(args) {
  const res = await client.callTool({ name: 'capcut_transcribe', arguments: { path: '/v.mp4', ...args } });
  assert.ok(!res.isError, res.content[0].text);
  return res.content[0].text;
}

test('the suggested from_time is next_from exactly, and following it reads each block once', async () => {
  const seen = [];
  let args = {};
  for (let i = 0; i <= BLOCKS.length; i++) {
    const out = await transcribe(args);
    seen.push(...BLOCKS.filter((b) => out.includes(b.text)).map((b) => b.text));
    const hint = out.match(/call again with from_time=(\S+) for more/);
    if (!hint) {
      assert.match(out, /End of transcript/);
      break;
    }
    const sent = fake.requests.findLast((r) => r.url === '/transcribe').body.page.from_time;
    const next = Number(hint[1]);
    assert.ok(next > sent, `cursor ${next} does not move past ${sent}`);
    args = { from_time: next };
  }
  assert.deepEqual(seen, BLOCKS.map((b) => b.text));
});

test('the hint keeps to_time', async () => {
  const out = await transcribe({ from_time: 0, to_time: 35 });
  assert.match(out, /from_time=10\.5 and to_time=35 for more/);
});
