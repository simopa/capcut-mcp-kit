// Added in capcut-mcp-kit (2026): capcut_move_camera_move passes absolute and relative placements to the
// backend and refuses mixed ones before calling it.
import { test, after } from 'node:test';
import assert from 'node:assert/strict';
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js';
import { fakeBackend, KIT_HEALTH } from './helpers.mjs';

const bodies = [];
const fake = await fakeBackend((req, body) => {
  if (req.url === '/health') return KIT_HEALTH;
  bodies.push(body);
  return [200, { success: true, output: { draft_id: 'd', draft_url: 'u', revision: 4, move: 'snap_in', move_id: 'm2',
    start: 3.9, end: 5.9, kind: 'keyframes', clips: 1, mode: 'compose', effects: 0, fill: 'none', replaced: 'm1' } }];
});
const { registerMediaTools } = await import('../dist/tools/media.js');
const server = new McpServer({ name: 'test', version: '0' });
registerMediaTools(server);
const client = new Client({ name: 'test', version: '0' });
const [a, b] = InMemoryTransport.createLinkedPair();
await Promise.all([server.connect(a), client.connect(b)]);
after(async () => { await client.close(); await fake.close(); });

test('a nudge in frames reaches the backend and the reply gives where the move is now', async () => {
  const res = await client.callTool({ name: 'capcut_move_camera_move', arguments: { draft_id: 'd', move_id: 'm1', shift_frames: -3 } });
  assert.ok(!res.isError, JSON.stringify(res));
  assert.equal(bodies.at(-1).shift_frames, -3);
  assert.ok(res.content[0].text.includes('from 3.9s to 5.9s') && res.content[0].text.includes('id m2'));
});

test('two ways of saying where, or two of saying how long, are refused before the backend', async () => {
  const before = bodies.length;
  for (const args of [{ start: 2, shift: 1 }, { shift: 1, shift_frames: 2 }, { end: 3, extend: 1 }]) {
    const res = await client.callTool({ name: 'capcut_move_camera_move', arguments: { draft_id: 'd', move_id: 'm1', ...args } });
    assert.ok(res.isError, JSON.stringify(args));
  }
  assert.equal(bodies.length, before);
});
