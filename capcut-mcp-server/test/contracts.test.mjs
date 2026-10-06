// Added in capcut-mcp-kit (2026): the contracts the backend's tests use are the current ones.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { contractsJson, CONTRACTS_FILE } from '../scripts/export-contracts.mjs';

test('vectcut-api/tests/contracts.json matches src/contracts.ts (run npm run contracts)', () => {
  assert.equal(readFileSync(CONTRACTS_FILE, 'utf8'), contractsJson());
});
