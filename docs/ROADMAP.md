# Roadmap

What is done and what comes next, in order. Each step lands with tests that never touch the real
CapCut drafts folder.

## Done

**Safe saving**
- Project names are validated and every destination must stay inside the drafts folder.
- Drafts are written to a staging folder and swapped in only when complete; a replaced project is
  moved to `~/Movies/CapCut MCP Backups`, never deleted.
- A project not saved from the same draft is replaced only with `overwrite: true`; replacing is
  refused while CapCut is open.
- Failed copies, unknown drafts and invalid names are reported as failures (`isError` in MCP).
- The backend refuses non-loopback Host/Origin; the web preview (arbitrary file reads) is off.

**No silent failures**
- Subtitles never overlap; pause detection reports ffmpeg errors instead of "all speech".
- The transcript cache is keyed by file (with extension), size, mtime, model and language, written
  atomically; a transcript deleted or invalidated is redone; one Whisper run at a time.
- Unknown animation or audio effect names, keyframes outside any clip and unsupported fps are errors.

**Reliable state**
- An unknown draft ID is an error, never a new empty draft.
- Drafts are stored in SQLite and survive a backend restart, pending keyframes included.
- One lock per draft; a change that fails leaves the draft exactly as it was.
- Subtitles are placed by reading the clips on the timeline (repeats and speed included).

**Adding to existing projects**
- `capcut_open_project`: the project's timeline is kept as the exact bytes read; saving writes it back
  unchanged plus the new tracks, in every copy CapCut keeps, after a full backup.
- Refused while CapCut is open, if the project changed since it was opened, or if its timeline copies
  disagree. Tested on CapCut 9.1 projects, which round-trip byte for byte.

**Keyframes and camera moves**
- Moves compose with keyframes already on the clip, including queued ones (or `replace` / `refuse`);
  keyframes outside the range are never touched and no two share a timestamp.
- Short ranges squeeze the move and always end on the starting framing; easing presets for
  professional camera movements; effect moves honour intensity and flash.

**Infrastructure**
- `GET /health` identifies the backend; the autostart accepts only the kit's backend at a compatible
  API version, starts it once across clients, passes the port and re-checks after a lost connection.
- A shared token (mode 600 file) is required on every other request; remote downloads are http(s)
  only, size-capped and never left partial.
- `setup.sh` checks prerequisites, installs locked versions, builds aside and swaps only on success.
- Python and Node tests run on GitHub Actions (macOS).

**Placement, music, catalog**
- An item that would overlap goes to the first free track like it; `capcut_get_timeline` shows where
  each track ends.
- `capcut_add_background_music`: loops to fill the edit, fades, ducks under the voice.
- One camera-move catalog for backend and MCP server; tool descriptions cut by about 40%;
  transcripts paged in the backend without word timings.

**Typed contracts**
- The backend's replies are zod schemas (`contracts.ts`); requests are typed from the tool schemas,
  every reply is checked at runtime, and the backend's tests check their real replies against the
  same contracts, so the two sides cannot drift apart.

**Safe retries**
- Every change carries a request id: a lost reply is retried with the same id and applied once.
- Replies report the draft's revision; `expected_revision` refuses a change if another client moved
  the draft on. An old draft cannot replace a project edited in CapCut without `overwrite`.

**Commit safety (second review)**
- One lock per project folder and per draft, shared between processes; every check is repeated
  under the lock just before writing, and a new folder never replaces one that appeared meanwhile.
- SQLite owns the drafts: a change starts from the stored revision (compare-and-swap) and the draft,
  its revision and the reply to its request id are committed together.
- Existing projects: a full manifest of the timeline files (selector, main timeline, every copy
  and its hash) taken at opening and compared before writing; no path through a symbolic link.
- Writes to project folders are journalled: undone on failure, settled after a crash or restart,
  reconciled when the project was written but the draft could not be recorded.
- The replaced project stays on the same volume until the new one is in; backups to another volume
  are copied in full before anything is removed.
- Media: never overwritten in a project, versioned by the file's size and modification time,
  copied (not referenced) when inside a folder being replaced.
- The authorization to replace a folder lives in the draft, not in a marker file; an unknown CapCut
  state blocks writing; request ids cover create/open, are tied to the call and expire safely.
- `setup.sh` stops only a backend it can identify by socket, owner and command line; Python
  environments are built at their final path behind a symlink.
- Camera `replace` keeps the curve outside its range; ducking holds under speech when the music
  ends first; the transcript cursor is passed back exactly.

## Next

**Editing clips already in a project**
- Today an opened project can only receive new tracks. Changing its own clips (trim, keyframes,
  camera moves) needs an adapter per clip type, each proven by a no-change round trip first.

## Later

- Full Windows support and a tested Intel Mac path.
- Measuring the disk space actually saved by copy-on-write clones.
