# capcut-mcp-kit

**Talk to your AI assistant, get a CapCut project.** Give Claude (or any MCP client: Codex CLI,
Cursor, …) your raw footage and say what you want. It transcribes the talk on your Mac, cuts the
pauses, adds subtitles that follow the cuts, music that ducks under your voice, titles, transitions
and professional camera movements, then saves a real CapCut desktop project you open, polish and
export.

- **Edits by what is said.** Local Whisper transcription (nothing is uploaded; about 9 minutes for
  a 52-minute talk on an M1), jump cuts in one call, subtitles in sync with the edit.
- **Works on your own projects.** Open a project you made in CapCut and add titles, music or B-roll
  on new tracks; what is already there is never touched.
- **Built not to lose your work.** Nothing is overwritten: previous versions go to the backups,
  saving waits until CapCut is closed, and a crash at any step leaves the old or the new version,
  never a mix. Tested by killing the backend after every step, and through five external
  adversarial reviews.
- **Local and private.** Everything runs on your Mac; the backend listens on localhost only and
  requires a token.

Built on [VectCutAPI](https://github.com/sun-guannan/VectCutAPI) and
[capcut-mcp-server](https://github.com/Atx-Guy/capcut-mcp-server), which did not work together out
of the box; this kit fixes and extends them (details in [NOTICE](NOTICE)).

> **Status: 0.6.** macOS with CapCut desktop 9.1 (international). Windows untested; `setup.sh` is
> macOS-only.

## How it works

```
Claude / MCP client ──► capcut-mcp-server (Node, stdio) ──► VectCutAPI (Python, :9001) ──► CapCut drafts folder
```

The assistant builds the project through tools; VectCutAPI writes it into
`~/Movies/CapCut/User Data/Projects/com.lveditor.draft/<project name>`, copying local media next to
it. **CapCut only rescans its project list at launch: restart CapCut to see a new project.**

**Content-aware editing.** The kit transcribes speech locally with Whisper (mlx-whisper on Apple
Silicon: about 9 minutes for a 52-minute talk on an M1; nothing is uploaded). The transcript is saved
next to the video (`talk.mp4.transcript.json` and a readable `talk.mp4.transcript.txt`), so each file is
transcribed once. From it the assistant can cut by what is said, remove pauses (jump cuts) in one
call, and add subtitles that follow the edit.

**Media files.** CapCut is sandboxed and can only open files in `~/Movies` by itself. Videos in
`~/Movies` are used where they are; other local files are placed in the project as an APFS clone
(instant, no extra disk space until modified).

## Requirements

- macOS, [CapCut desktop](https://www.capcut.com/) (opened at least once)
- [Homebrew](https://brew.sh), then `brew install node python ffmpeg`
- About 3 GB free: Python packages for Whisper (~1.3 GB) and the Whisper model (~1.6 GB)
- [Claude Code](https://claude.com/claude-code) (or another MCP client)

ffmpeg/ffprobe are used to read media width, height and duration, which CapCut needs in the project.

## Install

```bash
git clone https://github.com/simopa/capcut-mcp-kit.git
cd capcut-mcp-kit
./setup.sh                    # registers the MCP server for all your Claude Code projects
# or: ./setup.sh ~/my-videos  # register it only for one project folder
```

`setup.sh` installs the exact package versions in `vectcut-api/requirements*.lock.txt` and
`capcut-mcp-server/package-lock.json`. It can be run again at any time (to update, or on a new Mac):
it builds everything aside and switches only when the new build works (`vectcut-api/venv` is a
symlink to the active environment in `vectcut-api/venvs/`), then stops a running backend so the
next tool call starts the new one. It stops it only if it can confirm the process is this kit's
backend; otherwise it tells you to restart it yourself. Drafts in progress live in
`~/Library/Application Support/capcut-mcp-kit`; copy that folder to keep them on a new Mac.

Tests: `cd vectcut-api && venv/bin/python -m pip install pytest && venv/bin/python -m pytest` and
`cd capcut-mcp-server && npm test`; GitHub Actions runs both on macOS. The backend's replies are
described once, in `capcut-mcp-server/src/contracts.ts`: the MCP server checks every reply against
it, and the backend's tests check their real replies against the same contracts (exported to
`vectcut-api/tests/contracts.json` by `npm run contracts`).

## Use

1. Open Claude Code, approve the `capcut` MCP server, and ask, for example:
   > Vertical 9:16 project from `~/Desktop/interview.mp4`, keep 0:05–0:40, title "Episode 3" at the
   > top with a fade-in, background music `~/Music/bed.mp3` at 30% with a 2 s fade-out, save it as
   > "Episode 3".
2. Restart CapCut and open the project.

The MCP server starts the VectCutAPI backend by itself on the first tool call and leaves it running,
so several clients (say, Claude Code and Codex) can share it; it checks that the program on the port
is really the kit's backend, starts it only once even when several clients ask at the same time, and
starts it again if it stops. Its log is `vectcut-api/server.log`. To start it by hand instead, run `./start-server.sh`
(and set `CAPCUT_AUTOSTART=0` in the MCP server's environment to disable autostart).

## Tools

| Tool | What it does |
|---|---|
| `capcut_create_draft` | New project (width, height, fps) |
| `capcut_add_video` | Clip from a local path or URL: trim (`start`/`end`), timeline position (`target_start`), speed, volume, transition to the next clip |
| `capcut_add_audio` | Music or voice: trim, `target_start`, volume, `fade_in`/`fade_out` |
| `capcut_add_text` | Text with font, size, color, background, shadow, position, entrance/exit animation |
| `capcut_add_subtitle` | Subtitles from SRT text, an `.srt` path or URL; style, position, time offset |
| `capcut_add_image` | Image or logo (PNG etc.): position, scale, rotation, animations, transition |
| `capcut_add_effect` | CapCut video effects (Effects panel) over a time range, with parameters |
| `capcut_add_keyframe` | Keyframes: position, scale, rotation, opacity, volume, **saturation, contrast, brightness** |
| `capcut_add_sticker` | Sticker by CapCut resource ID (see limitations) |
| `capcut_list_types` | Exact CapCut names: transitions, animations, text animations, effects, masks, audio effects, fonts |
| `capcut_save_draft` | Save into CapCut's projects folder under `project_name` |
| `capcut_add_background_music` | Music under the whole edit: loops to fit, fades, ducks under the voice while it speaks |
| `capcut_get_timeline` | Tracks of a draft with where each one ends |
| `capcut_list_projects` | Projects in CapCut's folder, most recent first |
| `capcut_open_project` | Open a project made in CapCut to add to it (new tracks only; existing clips untouched) |
| `capcut_list_clips` | Clips already in an opened project, with which can be edited (and why not) |
| `capcut_edit_clip` | Trim or move a clip already in an opened project (`trim_start`, `trim_end`, `move_to`, `ripple`) |
| `capcut_get_duration` | Duration and size of a media file |
| `capcut_transcribe` | Local Whisper transcript as time-stamped blocks (paginated, cached next to the file) |
| `capcut_detect_pauses` | Preview speech vs pauses and how much would be cut |
| `capcut_add_video_without_pauses` | Add a video with its pauses removed (jump cuts), in one call |
| `capcut_add_auto_subtitles` | Subtitles from the transcript, short social-style lines, aligned to the edit |
| `capcut_add_camera_move` | Professional virtual camera moves over a time range: punch-in, zoom in/out, push, pull, punch, bounce, pans, tilts, rotate-in, dutch tilt, handheld drift, whips (keyframes, continuous across cuts) and CapCut effects (shake, lens zoom, fisheye, focus pull, flash, glitch…) |

Conventions:

- **Positions** are 0–1 from the top-left corner (0.5, 0.5 = center).
- **Font size** uses CapCut's own scale: about 5 small, 8 normal, 12–15 big title.
- **Names** of transitions, animations and effects must be exact CapCut names: look them up with
  `capcut_list_types` (e.g. `category: "transition", search: "dissolve"`).
- **Transitions** go on the *earlier* clip: they lead from that clip into the next one.
- **Tracks:** items on one track cannot overlap in time. An item that would overlap goes by itself to
  the first free track like it (`text_main_2`, `video_main_2`… stacked just above) and the reply says
  so; `auto_track: false` makes an overlap an error instead.
- **Camera moves** start and end on the clip's own framing, so they only affect their range; they
  stack on keyframes already there (`mode: "replace"` redoes a range, `"refuse"` errors instead).
  `easing` picks smooth, linear, snappy or dramatic motion; very short ranges squeeze the move. With
  `capcut_add_video_without_pauses`, `punch_in_zoom: 1.12` alternates a tighter framing on every
  other piece to hide jump cuts.
- **Existing projects:** `capcut_open_project` gives a draft that adds new tracks to a project made
  in CapCut. Saving writes the original timeline back unchanged plus the additions, in all the copies
  CapCut keeps; it refuses while CapCut is open (or if that cannot be told), if the project changed
  since it was opened (its timeline files, or which timeline is the main one), if its timeline copies
  disagree, or if a path inside it is a symbolic link. It backs up the whole project folder first and
  undoes a write that fails halfway. Media files already in the project are never overwritten: a
  file replaced at the same path is added under its own name, and clips added earlier keep the
  version they were made with.
- **Editing existing clips** (trim and move for now): `capcut_list_clips` lists the clips of an
  opened project and which can be edited: video, photo and audio clips at a constant 1× speed whose
  every reference the kit understands and which it can rewrite unchanged. `capcut_edit_clip` trims
  or moves one like dragging it in CapCut: `trim_start` / `trim_end` cut from either end (negative
  extends), `move_to` sets its start, `ripple` shifts the clips after it on the same track. An edit
  is refused, with the reason, if the clip would overlap another, go past its file, leave keyframes
  outside it, break a transition or no longer fit its fades, or change length with intro/outro
  animations. On CapCut's main track with its magnet on (the default) the kit does what CapCut does:
  a trim keeps the clip's start and the clips after it close up, a move is refused, and the reply
  says when clips on other tracks (titles, music) did not follow. Elsewhere, without `ripple` a trim
  leaves a gap. Edits
  are recorded in the draft and written at save time, after a check that the saved timeline differs
  from the original only in the fields of the clips that were edited.
- **Saving** again under the same `project_name` replaces the project that draft saved before; the
  old folder is moved to `~/Movies/CapCut MCP Backups` (set `CAPCUT_MCP_BACKUP_DIR` to change it),
  never deleted, and backups are not pruned. A project the draft did not save, or that was changed in
  CapCut since the draft saved it, is replaced only with `overwrite: true` (to add to a project you
  edited, use `capcut_open_project`). Which folders a draft saved is kept in the draft itself, not
  in the folder, so a file in a project cannot authorize replacing it. Replacing is refused while
  CapCut is open (or if that cannot be told), because CapCut writes the project it holds back to disk
  when it closes; saving under a new name works with CapCut open. Media inside the project being
  replaced is copied into the new one, not referenced where it was.
- **Color correction:** saturation/contrast/brightness keyframes with the same value at the clip's
  start and end act as a constant adjustment, still editable in CapCut.

## Limitations

- **Restart CapCut** after saving to see the project in the list.
- **Stickers** need a CapCut sticker resource ID and there is no catalog to search; the sample ID from
  VectCutAPI does not render in CapCut 9.1. Use `capcut_add_image` with a PNG instead.
- **Color filters** (CapCut's Filters panel) and in-app AI features (retouch, background removal,
  stabilization, auto captions) are not available.
- **Retries are safe:** every call carries a request id, and if its reply is lost the MCP server
  sends it once more with the same id; the backend returns the first reply instead of applying the
  change (or creating/opening a draft) twice. An id reused for a different call is refused. Replies
  are kept 7 days; a retry after that is refused, not applied again. Replies report the draft's
  `revision`; `capcut_save_draft` accepts `expected_revision` to save only if no other client changed
  the draft meanwhile.
- **Another projects folder:** `CAPCUT_PROJECTS_DIR` points the kit at another folder of CapCut
  projects instead of CapCut's own (the tests use it to stay away from real projects).
- **Drafts in progress** are kept in `~/Library/Application Support/capcut-mcp-kit/drafts.sqlite3`
  (set `CAPCUT_MCP_STATE_DIR` to move it; folder and files readable only by you), so they survive a
  backend restart. A change runs under a lock shared by every backend process, on the draft as last
  stored; the new draft, its revision and the reply to its request id are stored in one transaction.
  A change that fails leaves the draft as it was (except that a save which stopped halfway before it
  is settled and recorded first, as a change of its own: the revision then moves on by one, and
  `expected_revision` is checked before that), and an unknown draft ID is an error rather than a
  new empty project. A created or opened draft is stored with the reply to its request id: if that
  fails, neither is kept. A read never brings back an older version of a draft. The stored drafts are Python pickles: do not load a state folder you got from
  someone else.
- **Saving is journalled, not atomic across files:** a save writes several files and folders. Before
  it creates anything it journals every path it will use; before it changes a project folder it
  journals what is there and what replaces it: the folder's identity, its timeline selector, the
  hash of every file it writes, the full content list of both old and new folders in a replacement,
  each media file it moves in, where the previous version will go in the backups. Each file or folder is
  written aside and renamed in. If the backend stops halfway, the next change of that draft (or the
  next start of the backend) completes or undoes the save from that plan, so the project, its
  metadata, its media and a `draft_folder` copy end up all at the old version or all at the new
  one, and the draft records what was written. A save that fails is undone the same way.
  Before doing anything, recovery checks that every journalled item is still the old or the new
  version. If that check finds a conflict (an edit in a planned timeline, a file added to a replaced
  folder, a different main timeline, the project folder moved or replaced by a link), the project
  is preserved, including media the save added; previous versions still hidden are moved to the backups, and the note
  names where every version is. A backup's identity and full content are verified before its original
  is removed; a foreign or damaged backup is preserved alongside the original and reported. Interrupted
  backup copies are removed only when owned by the operation and their remaining data are still in
  the verified source. Media installed by the save are checked before a new timeline is adopted.
  On undo, media are kept when referenced or when a reference cannot be ruled out (for example a
  large, unreadable, binary or escaped metadata file). This can leave extra media after a rollback,
  with a warning. Path conflicts are reported even if temporary files cannot safely be cleaned up.
  Undoing inside CapCut's projects folder waits until CapCut is known to be closed, and the draft
  cannot be saved again until then. Warnings are in every reply format, and
  `capcut_get_timeline` lists the draft's saves still pending or left as they were in the last 7
  days, with what to do (reading the timeline does not settle them). These checks are tested by
  killing the backend after every step; a power cut (data not yet on disk) is not tested. CapCut is
  detected by process name, not by the project it has open.
- **Media are identified by content:** a local file added with kit 0.5 or later is named after a hash
  of its content, so a file replaced at the same path, even with the same size and modification
  time, is a new material, and every file copied into a project is checked against that hash. Media
  added with kit 0.4 are checked by size and modification time, older ones and http(s) downloads are
  not checked. Media under `~/Movies` are referenced where they are, so a later change to such a
  file shows in CapCut.
- The backend listens on 127.0.0.1 only, refuses requests from web pages, and requires the header
  `X-CapCut-Kit-Token` with the token in `~/Library/Application Support/capcut-mcp-kit/token`
  (created on first start, readable only by you; the MCP server sends it). Only `GET /health` is
  open. VectCutAPI's web preview is off; `CAPCUT_ENABLE_PREVIEW=1` turns it back on (it serves local
  files by path). Remote media downloads are http(s) only and capped at 20 GB
  (`CAPCUT_MAX_DOWNLOAD_BYTES`).
- The draft format is CapCut's `capcut_legacy` profile. A future CapCut version could change it.

## Other MCP clients

The kit is a standard local (stdio) MCP server, so any client that supports local MCP servers can
use it: Claude Code, Claude Desktop, OpenAI Codex (CLI and app), Cursor, and others. Run `./setup.sh`
(it skips the Claude Code registration if `claude` is not installed), then register the server in
your client. Desktop apps may not see Homebrew's PATH: if `node` is not found, use its full path
(`/opt/homebrew/bin/node` on Apple Silicon).

**OpenAI Codex CLI** (`~/.codex/config.toml`):

```toml
[mcp_servers.capcut]
command = "node"
args = ["/absolute/path/to/capcut-mcp-kit/capcut-mcp-server/dist/index.js"]
env = { CAPCUT_API_URL = "http://localhost:9001" }
```

**JSON-configured clients** (Claude Desktop, Cursor, …):

```json
{
  "mcpServers": {
    "capcut": {
      "command": "node",
      "args": ["/absolute/path/to/capcut-mcp-kit/capcut-mcp-server/dist/index.js"],
      "env": { "CAPCUT_API_URL": "http://localhost:9001" }
    }
  }
}
```

## Credits and license

- VectCutAPI by [sun-guannan](https://github.com/sun-guannan/VectCutAPI), Apache-2.0
- capcut-mcp-server by [Atx-Guy](https://github.com/Atx-Guy/capcut-mcp-server), MIT
- Kit files (setup, docs) MIT. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

Not affiliated with CapCut or ByteDance.

An Italian guide is in [docs/GUIDA-IT.md](docs/GUIDA-IT.md); what comes next is in [docs/ROADMAP.md](docs/ROADMAP.md).
