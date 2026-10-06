// Added in capcut-mcp-kit (2026): capcut_get_timeline shows the saves that stopped halfway.
import { test, after } from 'node:test';
import assert from 'node:assert/strict';
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js';
import { fakeBackend, KIT_HEALTH } from './helpers.mjs';

const note = "'P' was changed by something else after a save of this draft stopped halfway: it was left as it is";
let unsettled = [];
const fake = await fakeBackend((req) => req.url === '/health' ? KIT_HEALTH : [200, {
  success: true,
  output: { draft_id: 'd', revision: 3, duration: 4, tracks: [], unsettled_saves: unsettled }
}]);
const { registerMediaTools } = await import('../dist/tools/media.js');
const server = new McpServer({ name: 'test', version: '0' });
registerMediaTools(server);
const client = new Client({ name: 'test', version: '0' });
const [a, b] = InMemoryTransport.createLinkedPair();
await Promise.all([server.connect(a), client.connect(b)]);
after(async () => { await client.close(); await fake.close(); });

test('the timeline lists saves that stopped halfway', async () => {
  unsettled = [{ state: 'abandoned', kind: 'existing', folders: ['/tmp/P'], started: '2026-10-06T18:00:00', note }];
  const res = await client.callTool({ name: 'capcut_get_timeline', arguments: { draft_id: 'd' } });
  assert.ok(!res.isError, JSON.stringify(res));
  assert.ok(res.content[0].text.includes('Saves that stopped halfway'));
  assert.ok(res.content[0].text.includes(note));
});

test('and says nothing when there are none', async () => {
  unsettled = [];
  const res = await client.callTool({ name: 'capcut_get_timeline', arguments: { draft_id: 'd' } });
  assert.ok(!res.content[0].text.includes('stopped halfway'));
});
