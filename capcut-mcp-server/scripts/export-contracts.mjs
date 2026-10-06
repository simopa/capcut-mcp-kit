// Added in capcut-mcp-kit (2026): writes the backend reply contracts (src/contracts.ts) as JSON
// Schema to vectcut-api/tests/contracts.json, where the backend's tests check its real replies.
// Run with: npm run contracts
import { writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { zodToJsonSchema } from 'zod-to-json-schema';
import { CONTRACTS } from '../dist/contracts.js';

export const CONTRACTS_FILE = join(dirname(fileURLToPath(import.meta.url)), '../../vectcut-api/tests/contracts.json');

export function contractsJson() {
  const out = {};
  for (const [name, schema] of Object.entries(CONTRACTS)) {
    const { $schema, ...json } = zodToJsonSchema(schema, { $refStrategy: 'none' });
    out[name] = json;
  }
  return JSON.stringify(out, null, 2) + '\n';
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  writeFileSync(CONTRACTS_FILE, contractsJson());
  console.log(`Wrote ${CONTRACTS_FILE}`);
}
