// Added in capcut-mcp-kit (2026): capcut_list_clips shows each clip and whether it can be edited.
import { test, after } from 'node:test';
import assert from 'node:assert/strict';
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js';
import { fakeBackend, KIT_HEALTH } from './helpers.mjs';

const clip = (id, extra) => ({ id, track: 0, track_type: 'video', track_name: '', kind: 'video', name: 'a.mov',
  start: 0, end: 4, source_start: 1, source_end: 5, keyframes: [], transition: false, animation: false,
  editable: true, locked_reason: null, ...extra });
let nextOffset = null;
const edited = {
  draft_id: 'd', revision: 3, clip: clip('SEG-1', { end: 3, source_end: 4 }), shifted: ['SEG-2'],
  notes: ['The main track now has a gap at 3.000s (1.000s long): use ripple to close it, or fill it']
};
const fake = await fakeBackend((req) => req.url === '/health' ? KIT_HEALTH :
  req.url === '/edit_clip' ? [200, { success: true, output: edited }] : [200, { success: true, output: {
  draft_id: 'd', revision: 2, project_name: 'P', total: 2, offset: 0, next_offset: nextOffset, edits: 1,
  clips: [clip('SEG-1'), clip('SEG-2', { kind: 'text', name: 'Titolo', editable: false,
    locked_reason: 'text clips cannot be edited yet', source_start: undefined, source_end: undefined })]
} }]);
const { registerMediaTools } = await import('../dist/tools/media.js');
const server = new McpServer({ name: 'test', version: '0' });
registerMediaTools(server);
const client = new Client({ name: 'test', version: '0' });
const [a, b] = InMemoryTransport.createLinkedPair();
await Promise.all([server.connect(a), client.connect(b)]);
after(async () => { await client.close(); await fake.close(); });

test('clips are listed with whether they can be edited', async () => {
  const res = await client.callTool({ name: 'capcut_list_clips', arguments: { draft_id: 'd' } });
  assert.ok(!res.isError, JSON.stringify(res));
  const text = res.content[0].text;
  assert.ok(text.includes('`SEG-1`') && text.includes('source 1-5s') && text.includes('editable'));
  assert.ok(text.includes('locked: text clips cannot be edited yet'));
  assert.ok(text.includes('1 edit(s) in this draft') && !text.includes('More clips'));
});

test('a further page is announced', async () => {
  nextOffset = 2;
  const res = await client.callTool({ name: 'capcut_list_clips', arguments: { draft_id: 'd', limit: 2 } });
  assert.ok(res.content[0].text.includes('offset 2'));
});

test('an edit reports the clip, what moved with it and any gap', async () => {
  const res = await client.callTool({ name: 'capcut_edit_clip', arguments: { draft_id: 'd', clip_id: 'SEG-1', trim_end: 1 } });
  assert.ok(!res.isError, JSON.stringify(res));
  const text = res.content[0].text;
  assert.ok(text.includes('now 0-3s') && text.includes('`SEG-2`') && text.includes('gap at 3.000s'));
});
