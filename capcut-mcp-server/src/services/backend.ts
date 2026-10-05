// Starts the bundled VectCutAPI backend on demand, so users don't have to run start-server.sh.
//
// The backend is spawned detached and left running after this process exits: several MCP
// clients (e.g. Claude Code and Codex) can share it, and drafts being edited live in its memory.

import { spawn } from 'node:child_process';
import { existsSync, openSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import axios from 'axios';
import { API_BASE_URL } from '../constants.js';

const STARTUP_TIMEOUT_MS = 30000;
const POLL_INTERVAL_MS = 500;

// dist/services/backend.js -> <kit>/vectcut-api
const DEFAULT_BACKEND_DIR = resolve(dirname(fileURLToPath(import.meta.url)), '../../../vectcut-api');

let ready: Promise<void> | null = null;

async function isUp(): Promise<boolean> {
  try {
    // Any HTTP answer (the root path is a 404) means the server is listening
    await axios.get(API_BASE_URL, { timeout: 1000, validateStatus: () => true });
    return true;
  } catch {
    return false;
  }
}

function isLocal(url: string): boolean {
  const host = new URL(url).hostname;
  return host === 'localhost' || host === '127.0.0.1' || host === '::1';
}

function startBackend(): void {
  const dir = process.env.CAPCUT_BACKEND_DIR || DEFAULT_BACKEND_DIR;
  const python = process.platform === 'win32'
    ? join(dir, 'venv', 'Scripts', 'python.exe')
    : join(dir, 'venv', 'bin', 'python');
  if (!existsSync(python)) {
    throw new Error(`VectCutAPI backend not found at ${dir} (missing venv): run setup.sh, or start it yourself`);
  }
  const log = openSync(join(dir, 'server.log'), 'a');
  const child = spawn(python, ['capcut_server.py'], {
    cwd: dir,
    detached: true,
    stdio: ['ignore', log, log]
  });
  child.unref();
  console.error(`Started VectCutAPI backend (pid ${child.pid}), log: ${join(dir, 'server.log')}`);
}

async function ensure(): Promise<void> {
  if (await isUp()) return;
  if (process.env.CAPCUT_AUTOSTART === '0' || !isLocal(API_BASE_URL)) {
    throw new Error(`CapCut API server is not responding at ${API_BASE_URL}. Please ensure the server is running.`);
  }
  startBackend();
  const deadline = Date.now() + STARTUP_TIMEOUT_MS;
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS));
    if (await isUp()) return;
  }
  throw new Error(`VectCutAPI backend did not start within ${STARTUP_TIMEOUT_MS / 1000}s; see vectcut-api/server.log`);
}

/** Resolves once the backend answers, starting it if needed. Retries after a failure. */
export function ensureBackend(): Promise<void> {
  if (!ready) {
    ready = ensure().catch((error) => {
      ready = null;
      throw error;
    });
  }
  return ready;
}
