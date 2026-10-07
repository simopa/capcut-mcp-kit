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
- Media: never overwritten in a project, versioned (by content since the third review), copied
  (not referenced) when inside a folder being replaced.
- The authorization to replace a folder lives in the draft, not in a marker file; an unknown CapCut
  state blocks writing; request ids cover create/open, are tied to the call and expire safely.
- `setup.sh` stops only a backend it can identify by socket, owner and command line; Python
  environments are built at their final path behind a symlink.
- Camera `replace` keeps the curve outside its range; ducking holds under speech when the music
  ends first; the transcript cursor is passed back exactly.

**Crash safety (third review)**
- A save journals its whole plan before changing anything: every path it will use, the identity
  (device, inode) and hash of what is there and of what replaces it, the new metadata, the identity
  of each media file it moves in and what the draft records once saved. A crash at any step ends
  in the old or the new version, checked by tests that kill the backend after every step.
- Recovery never touches what it did not create: a project changed since is left as it is, with a
  note of where the previous version is; undoing inside CapCut's folder waits for CapCut to be closed.
- The project locks are held until the draft is committed; a draft cannot be saved while one of its
  saves is unsettled.
- The draft cache holds (revision, draft) pairs and never goes back to an older revision; a created
  or opened draft is stored in the same transaction as its reply.
- Media are versioned by content, and every copy put in a project is checked against it.
- Save warnings appear in the Markdown reply too; `capcut_get_timeline` lists saves that stopped
  halfway (pending, or left as they were) with what to do.

**Recovery that proves before it acts (fourth review)**
- One check, shared by the recovery after a crash and the undo of a failed save, before anything is
  written, completed or removed: the project folder's identity (no link in its place), the timeline
  selector and set of copies, every planned file at the old or new version, the full content of
  every folder the save put in place.
- On any difference nothing in the project is touched; every previous version still hidden goes to
  the backups and the note names where each version is. Media are removed on undo only if nothing
  in the project mentions them.
- Backup destinations and temporary file names are journalled before use: a stop while copying to
  another volume or between writing and renaming a file leaves nothing behind.

**Recovery conflicts and backup verification (fifth review)**
- A replacement compares inventories of the old folder and the new one. Changed old folders or
  asides block destructive rollback of the other destination.
- An existing backup is accepted only with the expected identity and complete inventory. EXDEV
  copies use an exclusively created, journalled staging container; cleanup keeps foreign or edited
  partial copies and reports them. A completed backup can safely finish an interrupted removal of
  the original when every remaining entry is still present in that backup.
- A new timeline is adopted only while its newly installed media still match the plan. Reference
  scans keep media when files are too large, unreadable, linked, binary or use Unicode escapes.
- Unsafe paths become reported conflicts even when temporary cleanup cannot finish. Regression
  tests cover all five findings, corrupted backups and removal interrupted after a complete copy.

## Next

**Editing clips already in a project: trim and move first**

The project's own timeline stays the source of truth: each edit is an operation recorded in the
draft (clip id, field, the original value it expects) and applied to the original JSON at save
time, before the additions. All save and recovery guarantees stay as they are.

1. *Clip inventory (read-only)* — done. `capcut_list_clips`: every clip of the opened project with its id,
   track, type, timeline start/end, source in/out, speed, file, existing keyframes, and whether it
   is editable. A clip is editable only if its adapter reads and rewrites it identically (no-change
   round trip); anything not fully understood is listed but locked, with the reason.
2. *Edit log and extended check* — done. At save: original → edits → additions. The check
   restores the original value of every declared field and removes the additions: the result must be exactly
   the original timeline, or nothing is written. An edit whose expected value is no longer there
   (project changed) is refused.
3. *Timing adapter* — done. `capcut_edit_clip` trims (start, end) and moves video, photo and audio
   clips at a constant 1× speed, with optional ripple on the clip's track. Only the segments'
   `target_timerange`/`source_timerange` (and keyframe offsets on a start trim) and the project
   duration change. Refused with a reason: source beyond the file, overlap on the track, keyframes
   that would fall outside the clip, a transition that would no longer join or fit its clips, a
   length change with intro/outro animations, fades that no longer fit. Trial on copies of all real
   projects: every editable clip trimmed with ripple and saved, only the declared fields changed.
4. *Proof* — synthetic tests and round trips on copies of real projects done; remaining: a visual
   check in CapCut (gaps on the main track, transitions, keyframes after a trim).

Seen in the projects analysed (read-only copies, 10 projects, 379 clips): video/photo/audio clips
carry six kinds of material references (speeds, canvases, placeholder infos, sound channel
mappings, colors, vocal separations), plus transitions and animations on a few; keyframes are
linear (`Line`) with offsets relative to the clip start; source ranges stay within the file's
duration; text clips made by the kit carry references to missing materials (to be locked or
understood before text editing).

Later adapters: keyframes and camera moves on existing clips, volume, text, deleting clips, speed,
ripple editing.

**Projects from CapCut's cloud (made on the phone)**

A cloud project downloaded by CapCut desktop is a local folder like any other, but CapCut may sync
it again: it could overwrite the kit's changes with the cloud version, or upload them. Before any
code, an experiment on a throwaway cloud project: create it on the phone, download it on the Mac,
analyse a copy of its folder (sync fields, `.cloud_cache`), add a track with the kit while CapCut is
closed, then check what CapCut shows on the Mac and on the phone. The result decides whether cloud
projects are supported as they are, with a procedure (e.g. sync paused), or not at all.

## Later

- Full Windows support and a tested Intel Mac path.
- Measuring the disk space actually saved by copy-on-write clones.
