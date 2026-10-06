// Added in capcut-mcp-kit (2026): the shared token the backend requires on every request.
// The backend creates <state dir>/token (mode 600) on first start; CAPCUT_MCP_TOKEN overrides it.

import { readFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';

export const TOKEN_HEADER = 'X-CapCut-Kit-Token';

export function stateDir(): string {
  if (process.env.CAPCUT_MCP_STATE_DIR) return process.env.CAPCUT_MCP_STATE_DIR.replace(/^~(?=$|\/)/, homedir());
  if (process.platform === 'win32') return join(process.env.LOCALAPPDATA || homedir(), 'capcut-mcp-kit');
  if (process.platform === 'darwin') return join(homedir(), 'Library', 'Application Support', 'capcut-mcp-kit');
  return join(homedir(), '.local', 'state', 'capcut-mcp-kit');
}

/** The token, or undefined while the backend has not created it yet. */
export function readToken(): string | undefined {
  if (process.env.CAPCUT_MCP_TOKEN) return process.env.CAPCUT_MCP_TOKEN;
  try {
    return readFileSync(join(stateDir(), 'token'), 'utf8').trim() || undefined;
  } catch {
    return undefined;
  }
}
