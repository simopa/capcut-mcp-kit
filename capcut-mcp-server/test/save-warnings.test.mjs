// Added in capcut-mcp-kit (2026): a save that succeeds with warnings says so in every reply format.
import { test, after } from 'node:test';
import assert from 'node:assert/strict';
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js';
import { fakeBackend, KIT_HEALTH } from './helpers.mjs';

const warning = "the replaced version of 'P' could not be moved to the backups";
const fake = await fakeBackend((req) => req.url === '/health' ? KIT_HEALTH : [200, {
  success: true,
  output: { draft_url: '/tmp/P', revision: 2, backups: ['/tmp/.capcut-mcp-old-1'], warnings: [warning] }
}]);
const { registerTools } = await import('../dist/tools/index.js');
const server = new McpServer({ name: 'test', version: '0' });
registerTools(server);
const client = new Client({ name: 'test', version: '0' });
const [a, b] = InMemoryTransport.createLinkedPair();
await Promise.all([server.connect(a), client.connect(b)]);
after(async () => { await client.close(); await fake.close(); });

for (const format of ['markdown', 'json']) {
  test(`save warnings are in the ${format} reply`, async () => {
    const res = await client.callTool({ name: 'capcut_save_draft', arguments: { draft_id: 'd', response_format: format } });
    assert.ok(!res.isError, JSON.stringify(res));
    assert.deepEqual(res.structuredContent.warnings, [warning]);
    assert.ok(res.content[0].text.includes(warning));
  });
}
