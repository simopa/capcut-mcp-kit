// Added in capcut-mcp-kit (2026): the autostart recognises the kit's backend and nothing else.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { fakeBackend, KIT_HEALTH } from './helpers.mjs';

let mode = 'kit';
const fake = await fakeBackend((req) => {
  if (mode === 'kit') return KIT_HEALTH;
  if (mode === 'old-api') return [200, { success: true, output: { service: 'capcut-mcp-kit', api: 99 } }];
  return [418, { hello: 'I am something else' }];
});
const { ensureBackend, forgetBackend } = await import('../dist/services/backend.js');

test('accepts the kit backend', async () => {
  await ensureBackend();
});

test('refuses another program on the port', async () => {
  mode = 'foreign';
  forgetBackend();
  await assert.rejects(ensureBackend(), /Another program/);
});

test('refuses a backend speaking another API version', async () => {
  mode = 'old-api';
  forgetBackend();
  await assert.rejects(ensureBackend(), /speaks API 99/);
});

test('notices when the backend is gone', async () => {
  mode = 'kit';
  forgetBackend();
  await ensureBackend();
  await fake.close();
  forgetBackend();
  await assert.rejects(ensureBackend(), /not responding/);
});
