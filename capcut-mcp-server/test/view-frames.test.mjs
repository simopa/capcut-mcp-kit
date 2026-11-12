// Added in capcut-mcp-kit (2026): capcut_view_frames returns each frame as an image after a line with its
// time, says why a frame is missing, and refuses ambiguous requests before calling the backend.
import { test, after } from 'node:test';
import assert from 'node:assert/strict';
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js';
import { fakeBackend, KIT_HEALTH } from './helpers.mjs';

const calls = [];
const fake = await fakeBackend((req, body) => {
  if (req.url === '/health') return KIT_HEALTH;
  calls.push(req.url);
  return [200, { success: true, output: { of: 'track "video_main"', timeline: true, frames: [
    { time: 1.5, source: 'talk.mp4', source_time: 12.25, image: 'AAAA' },
    { time: 70, note: 'no clip at this time on the track' }
  ] } }];
});
const { registerMediaTools } = await import('../dist/tools/media.js');
const server = new McpServer({ name: 'test', version: '0' });
registerMediaTools(server);
const client = new Client({ name: 'test', version: '0' });
const [a, b] = InMemoryTransport.createLinkedPair();
await Promise.all([server.connect(a), client.connect(b)]);
after(async () => { await client.close(); await fake.close(); });

test('frames come back as images, each after a line with its time', async () => {
  const res = await client.callTool({ name: 'capcut_view_frames', arguments: { draft_id: 'd', times: [1.5, 70] } });
  assert.ok(!res.isError, JSON.stringify(res));
  assert.deepEqual(res.content.map((c) => c.type), ['text', 'text', 'image', 'text']);
  assert.ok(res.content[0].text.includes('times of the timeline'));
  assert.equal(res.content[1].text, '00:01.5 — talk.mp4 at 00:12.3');
  assert.deepEqual(res.content[2], { type: 'image', data: 'AAAA', mimeType: 'image/jpeg' });
  assert.equal(res.content[3].text, '01:10.0: no clip at this time on the track');
});

test('a request must name one source and one way of choosing times', async () => {
  const before = calls.length;
  for (const args of [{ times: [1] }, { path: '/a.mp4', draft_id: 'd', times: [1] }, { path: '/a.mp4' },
    { path: '/a.mp4', times: [1], every: 2 }]) {
    const res = await client.callTool({ name: 'capcut_view_frames', arguments: args });
    assert.ok(res.isError, JSON.stringify(args));
  }
  assert.equal(calls.length, before);
});
