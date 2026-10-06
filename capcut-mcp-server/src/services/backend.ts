// Modified in capcut-mcp-kit (2026): health check with identity, one autostart at a time across
// processes, re-check after a lost connection, port passed to the backend.
//
// Starts the bundled VectCutAPI backend on demand, so users don't have to run start-server.sh.
// The backend is spawned detached and left running after this process exits: several MCP
// clients (e.g. Claude Code and Codex) can share it; drafts are persisted by the backend.

import { spawn } from 'node:child_process';
import { closeSync, existsSync, mkdirSync, openSync, statSync, unlinkSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import axios from 'axios';
import { API_BASE_URL } from '../constants.js';
import { stateDir } from './auth.js';

const STARTUP_TIMEOUT_MS = 30000;
const POLL_INTERVAL_MS = 500;
const RECHECK_AFTER_MS = 60000;
export const KIT_SERVICE = 'capcut-mcp-kit';
export const KIT_API = 1;

// dist/services/backend.js -> <kit>/vectcut-api
const DEFAULT_BACKEND_DIR = resolve(dirname(fileURLToPath(import.meta.url)), '../../../vectcut-api');

let ready: Promise<void> | null = null;
let checkedAt = 0;

type Probe = 'up' | 'down' | 'foreign';

async function probe(): Promise<Probe> {
  try {
    const res = await axios.get(`${API_BASE_URL}/health`, { timeout: 1500, validateStatus: () => true });
    const out = res.data?.output;
    if (res.status === 200 && out?.service === KIT_SERVICE) {
      if (out.api !== KIT_API) {
        throw new Error(`The backend on ${API_BASE_URL} speaks API ${out.api}, this MCP server needs ${KIT_API}: ` +
          `restart the backend (it may be an older version still running)`);
      }
      return 'up';
    }
    return 'foreign';
  } catch (error) {
    if (error instanceof Error && error.message.includes('speaks API')) throw error;
    return 'down';
  }
}

function isLocal(url: string): boolean {
  const host = new URL(url).hostname;
  return host === 'localhost' || host === '127.0.0.1' || host === '::1' || host === '[::1]';
}

/** Take the start lock; false if another process is starting the backend right now. */
function takeStartLock(lockPath: string): boolean {
  try {
    if (Date.now() - statSync(lockPath).mtimeMs > STARTUP_TIMEOUT_MS) unlinkSync(lockPath); // stale
  } catch { /* no lock */ }
  try {
    closeSync(openSync(lockPath, 'wx'));
    return true;
  } catch {
    return false;
  }
}

function startBackend(): void {
  const dir = process.env.CAPCUT_BACKEND_DIR || DEFAULT_BACKEND_DIR;
  const python = process.platform === 'win32'
    ? join(dir, 'venv', 'Scripts', 'python.exe')
    : join(dir, 'venv', 'bin', 'python');
  if (!existsSync(python)) {
    throw new Error(`VectCutAPI backend not found at ${dir} (missing venv): run setup.sh, or start it yourself`);
  }
  const url = new URL(API_BASE_URL);
  const port = url.port || (url.protocol === 'https:' ? '443' : '80');
  const log = openSync(join(dir, 'server.log'), 'a');
  try {
    const child = spawn(python, ['capcut_server.py'], {
      cwd: dir,
      detached: true,
      stdio: ['ignore', log, log],
      env: { ...process.env, CAPCUT_PORT: port }
    });
    child.on('error', (error) => console.error(`VectCutAPI backend failed to start: ${error.message}`));
    child.unref();
    console.error(`Started VectCutAPI backend (pid ${child.pid}) on port ${port}, log: ${join(dir, 'server.log')}`);
  } finally {
    closeSync(log); // the child has its own copy
  }
}

async function waitUntilUp(): Promise<boolean> {
  const deadline = Date.now() + STARTUP_TIMEOUT_MS;
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS));
    if (await probe() === 'up') return true;
  }
  return false;
}

async function ensure(): Promise<void> {
  const state = await probe();
  if (state === 'up') return;
  if (state === 'foreign') {
    throw new Error(`Another program answers on ${API_BASE_URL} (not the capcut-mcp-kit backend): ` +
      `free the port or set CAPCUT_API_URL to another one`);
  }
  if (process.env.CAPCUT_AUTOSTART === '0' || !isLocal(API_BASE_URL)) {
    throw new Error(`CapCut API server is not responding at ${API_BASE_URL}. Please ensure the server is running.`);
  }
  mkdirSync(stateDir(), { recursive: true });
  const lockPath = join(stateDir(), 'backend-start.lock');
  if (takeStartLock(lockPath)) {
    try {
      startBackend();
      if (await waitUntilUp()) return;
    } finally {
      try { unlinkSync(lockPath); } catch { /* already gone */ }
    }
  } else if (await waitUntilUp()) {
    return; // another client started it
  }
  throw new Error(`VectCutAPI backend did not start within ${STARTUP_TIMEOUT_MS / 1000}s; see vectcut-api/server.log`);
}

/** Resolves once the backend answers, starting it if needed. Re-checks after a while, and after
 *  forgetBackend() (called when a request could not connect). Retries after a failure. */
export function ensureBackend(): Promise<void> {
  if (ready && Date.now() - checkedAt > RECHECK_AFTER_MS) ready = null;
  if (!ready) {
    checkedAt = Date.now();
    ready = ensure().catch((error) => {
      ready = null;
      throw error;
    });
  }
  return ready;
}

export function forgetBackend(): void {
  ready = null;
}
