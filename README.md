# capcut-mcp-kit

Let any AI assistant that speaks MCP (Claude Code, Codex CLI, Cursor, …) assemble **CapCut desktop projects**: cut and
sequence clips, add titles, subtitles, music, transitions, effects, animations, keyframes and color
tweaks. The result is a regular CapCut project that you open, polish and export in CapCut.

It bundles two open-source projects, fixed so they actually work together:

- **[VectCutAPI](https://github.com/sun-guannan/VectCutAPI)**: a local Python server (port 9001)
  that writes CapCut draft files.
- **[capcut-mcp-server](https://github.com/Atx-Guy/capcut-mcp-server)**: the MCP server that exposes
  those features as tools.

Out of the box the two didn't line up: the MCP server read the wrong response field (so no tool
worked), used parameter names the backend ignores, and offered made-up effect and transition names.
This kit aligns them, adds a few missing features and saves projects straight into CapCut.
Details in [NOTICE](NOTICE).

> **Status: 0.1.** Tested on macOS with CapCut desktop 9.1.0 (international). Windows should work for
> the servers but is untested, and `setup.sh` is macOS-only.

## How it works

```
Claude / MCP client ──► capcut-mcp-server (Node, stdio) ──► VectCutAPI (Python, :9001) ──► CapCut drafts folder
```

The assistant builds the project through tools; VectCutAPI writes it into
`~/Movies/CapCut/User Data/Projects/com.lveditor.draft/<project name>`, copying local media next to
it. **CapCut only rescans its project list at launch: restart CapCut to see a new project.**

**Content-aware editing.** The kit transcribes speech locally with Whisper (mlx-whisper on Apple
Silicon: about 9 minutes for a 52-minute talk on an M1; nothing is uploaded). The transcript is saved
next to the video (`<name>.transcript.json` and a readable `<name>.transcript.txt`), so each file is
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

## Use

1. Open Claude Code, approve the `capcut` MCP server, and ask, for example:
   > Vertical 9:16 project from `~/Desktop/interview.mp4`, keep 0:05–0:40, title "Episode 3" at the
   > top with a fade-in, background music `~/Music/bed.mp3` at 30% with a 2 s fade-out, save it as
   > "Episode 3".
2. Restart CapCut and open the project.

The MCP server starts the VectCutAPI backend by itself on the first tool call and leaves it running,
so several clients (say, Claude Code and Codex) can share it and drafts in progress survive a closed
session. Its log is `vectcut-api/server.log`. To start it by hand instead, run `./start-server.sh`
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
| `capcut_get_duration` | Duration and size of a media file |
| `capcut_transcribe` | Local Whisper transcript as time-stamped blocks (paginated, cached next to the file) |
| `capcut_detect_pauses` | Preview speech vs pauses and how much would be cut |
| `capcut_add_video_without_pauses` | Add a video with its pauses removed (jump cuts), in one call |
| `capcut_add_auto_subtitles` | Subtitles from the transcript, short social-style lines, aligned to the edit |

Conventions:

- **Positions** are 0–1 from the top-left corner (0.5, 0.5 = center).
- **Font size** uses CapCut's own scale: about 5 small, 8 normal, 12–15 big title.
- **Names** of transitions, animations and effects must be exact CapCut names: look them up with
  `capcut_list_types` (e.g. `category: "transition", search: "dissolve"`).
- **Transitions** go on the *earlier* clip: they lead from that clip into the next one.
- **Tracks:** items on one track cannot overlap in time. Give a second title, a picture-in-picture
  clip or a second audio a different `track_name`.
- **Color correction:** saturation/contrast/brightness keyframes with the same value at the clip's
  start and end act as a constant adjustment, still editable in CapCut.

## Limitations

- **Restart CapCut** after saving to see the project in the list.
- **Stickers** need a CapCut sticker resource ID and there is no catalog to search; the sample ID from
  VectCutAPI does not render in CapCut 9.1. Use `capcut_add_image` with a PNG instead.
- **Color filters** (CapCut's Filters panel) and in-app AI features (retouch, background removal,
  stabilization, auto captions) are not available.
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

An Italian guide is in [docs/GUIDA-IT.md](docs/GUIDA-IT.md).
