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

## Next

**Safe retries**
- Optional request IDs so a retried call is not applied twice, and an expected revision so two
  clients cannot overwrite each other's changes unknowingly.

**Editing existing projects**
- A conservative adapter for projects made in CapCut: load and save with no changes must give back
  the same file, unknown fields included, before any editing is allowed.
- Write only if the project on disk is still the one that was read.

**Keyframes and camera moves**
- An explicit rule when a move overlaps existing keyframes (refuse, replace or compose).
- Minimum durations for very short moves; effect parameters honoured or refused.
- Easing presets for professional camera movements.

**Infrastructure**
- Backend health check with identity and version; one autostart at a time across clients.
- Shared token between the MCP server and the backend; limits on remote downloads.
- Non-destructive `setup.sh` with pinned dependencies (`npm ci`, locked Python packages).
- Node tests and GitHub Actions on macOS.

**New features**
- Multi-track placement without collisions (first free track or first free time) and automatic
  fades for music under a voice.
- One catalog of names shared by backend and MCP server; shorter tool descriptions; pagination in
  the backend.

## Later

- Full Windows support and a tested Intel Mac path.
- Measuring the disk space actually saved by copy-on-write clones.
