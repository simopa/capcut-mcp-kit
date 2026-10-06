// Added in capcut-mcp-kit (2026): requests carry the token and MCP positions are mapped to CapCut's.
import { test, after } from 'node:test';
import assert from 'node:assert/strict';
import { fakeBackend, KIT_HEALTH } from './helpers.mjs';

process.env.CAPCUT_MCP_TOKEN = 'secret-for-test';
const fake = await fakeBackend((req) => {
  if (req.url === '/health') return KIT_HEALTH;
  if (req.url === '/refuse') return [401, { success: false, error: 'Missing or wrong token' }];
  return [200, { success: true, output: { draft_id: 'd1' }, error: '' }];
});
const { apiClient } = await import('../dist/services/api-client.js');
after(() => fake.close());

test('sends the token and maps 0..1 top-left positions to centered half-canvas units', async () => {
  const res = await apiClient.addText({ draft_id: 'd1', text: 'Hi', position_x: 0, position_y: 1, scale: 2 });
  assert.equal(res.success, true);
  assert.deepEqual(res.result, { draft_id: 'd1' });
  const call = fake.requests.find((r) => r.url === '/add_text');
  assert.equal(call.headers['x-capcut-kit-token'], 'secret-for-test');
  assert.equal(call.body.transform_x, -1);
  assert.equal(call.body.transform_y, -1);
  assert.equal(call.body.scale_x, 2);
  assert.equal(call.body.position_x, undefined);
});

test('the center maps to 0,0', async () => {
  await apiClient.addImage({ draft_id: 'd1', image_url: '/x.png', position_x: 0.5, position_y: 0.5 });
  const call = fake.requests.findLast((r) => r.url === '/add_image');
  assert.equal(call.body.transform_x, 0);
  assert.equal(call.body.transform_y, 0);
});

test('a refused request is an error with the reason', async () => {
  const res = await apiClient.request('/refuse', 'POST', {});
  assert.equal(res.success, false);
  assert.match(res.error, /refused the request: Missing or wrong token/);
});
