// Content-aware editing tools: transcription, pause detection, pause-free assembly, auto subtitles.
// Heavy data (word timings) stays in the backend; tools return compact summaries to save tokens.

import { z } from 'zod';
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { apiClient } from '../services/api-client.js';

const PAGE_CHARS = 12000;

// Mirrors vectcut-api/camera_moves.py
const CAMERA_MOVES = {
  keyframes: [
    ['punch_in', 'cut in closer for the range, then cut back'],
    ['zoom_in_out', 'smoothly move in, hold, ease back out'],
    ['push_in', 'slow continuous push in (Ken Burns)'],
    ['pull_out', 'start close, slowly pull back'],
    ['punch', 'impact hit: fast zoom kick that settles (~0.5 s)'],
    ['zoom_bounce', 'quick zoom in with a small elastic overshoot'],
    ['pan_left', 'camera pans left'],
    ['pan_right', 'camera pans right'],
    ['tilt_up', 'camera tilts up'],
    ['tilt_down', 'camera tilts down'],
    ['rotate_in', 'enter rotated and zoomed, straighten out'],
    ['dutch_tilt', 'hold a tilted (dutch angle) framing'],
    ['handheld', 'subtle floating handheld drift'],
    ['whip_left', 'fast whip pan out to the left at the end of the range'],
    ['whip_right', 'fast whip pan out to the right at the end of the range']
  ],
  effects: [
    ['shake', 'camera shake / quake'],
    ['shake_strong', 'stronger, rougher shake'],
    ['lens_zoom', 'lens zoom pulse'],
    ['mini_zoom', 'small rhythmic zoom'],
    ['chroma_zoom', 'zoom with chromatic aberration'],
    ['fisheye', 'fisheye lens distortion'],
    ['focus_pull', 'rack focus (blur to sharp)'],
    ['motion_blur', 'motion blur (use with whips)'],
    ['swing', 'swinging camera'],
    ['flash', 'white flash'],
    ['flash_black', 'black flash'],
    ['glitch', 'digital glitch']
  ]
} as const;
const ALL_MOVES = [...CAMERA_MOVES.keyframes, ...CAMERA_MOVES.effects].map(([n]) => n) as unknown as [string, ...string[]];

const LocalPath = z.string()
  .min(1)
  .describe('Absolute local path to the video or audio file');

type Segment = { start: number; end: number; text: string };

function clock(t: number): string {
  const h = Math.floor(t / 3600);
  const m = Math.floor((t % 3600) / 60);
  const s = (t % 60).toFixed(1).padStart(4, '0');
  return h ? `${h}:${String(m).padStart(2, '0')}:${s}` : `${String(m).padStart(2, '0')}:${s}`;
}

// Whisper segments are often a few words long: merge them into readable blocks
// that end on a sentence or after ~20 s, each with one time range.
function toBlocks(segments: Segment[], maxSeconds = 20): Segment[] {
  const blocks: Segment[] = [];
  let cur: Segment | null = null;
  for (const s of segments) {
    if (cur && (s.end - cur.start > maxSeconds || s.start - cur.end > 2)) {
      blocks.push(cur);
      cur = null;
    }
    cur = cur ? { start: cur.start, end: s.end, text: `${cur.text} ${s.text}` } : { ...s };
    if (/[.!?]$/.test(s.text) && cur.end - cur.start > maxSeconds / 2) {
      blocks.push(cur);
      cur = null;
    }
  }
  if (cur) blocks.push(cur);
  return blocks;
}

function text(t: string) {
  return { content: [{ type: 'text' as const, text: t }] };
}

function fail(error: unknown) {
  return { ...text(`Error: ${error instanceof Error ? error.message : String(error)}`), isError: true };
}

export function registerMediaTools(server: McpServer): void {
  server.registerTool(
    'capcut_transcribe',
    {
      title: 'Transcribe Speech',
      description: `Transcribe the speech of a local video/audio file with Whisper (runs locally, nothing is uploaded).

Returns the transcript as time-stamped blocks ("[mm:ss.s–mm:ss.s] text"), paginated. Word-level
timings are kept in the backend and used by capcut_add_video_without_pauses and
capcut_add_auto_subtitles, so transcribe a file once before using those.

Long files take a while (about 1/5 of their length on an Apple Silicon Mac; ~9 min for 52 min).
If the result is not ready the tool answers "running": call it again with the same arguments
to wait more. Results are cached, so repeat calls on a transcribed file are instant.

Args:
  - path (string): Absolute local path
  - language (string): Optional ISO code, e.g. "it", "en" (auto-detected if omitted)
  - model ('turbo' | 'large' | 'small'): turbo = best speed/quality (default), small = fastest
  - from_time / to_time (number): Only return blocks in this range, in seconds
Pages hold ~${PAGE_CHARS} characters; the reply says where to continue (from_time).`,
      inputSchema: z.object({
        path: LocalPath,
        language: z.string().min(2).max(5).optional().describe('ISO language code, e.g. "it"'),
        model: z.enum(['turbo', 'large', 'small']).default('turbo').describe('Whisper model'),
        from_time: z.number().min(0).default(0).describe('Return blocks starting from this time (s)'),
        to_time: z.number().positive().optional().describe('Return blocks up to this time (s)')
      }).strict(),
      annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false }
    },
    async (params) => {
      try {
        const r = await apiClient.request<any>('/transcribe', 'POST',
          { path: params.path, language: params.language, model: params.model, wait: 45 });
        if (!r.success) throw new Error(r.error);
        const out = r.result;
        if (out.status === 'running') {
          return text(`Transcription running (${out.elapsed}s elapsed). Call capcut_transcribe again with the same path to keep waiting.`);
        }
        const t = out.transcript;
        const blocks = toBlocks(t.segments).filter((b) =>
          b.end > params.from_time && (params.to_time === undefined || b.start < params.to_time));
        let body = '';
        let shownUntil = params.from_time;
        let truncated = false;
        for (const b of blocks) {
          const line = `[${clock(b.start)}–${clock(b.end)}] ${b.text}\n`;
          if (body.length + line.length > PAGE_CHARS) {
            truncated = true;
            break;
          }
          body += line;
          shownUntil = b.end;
        }
        const header = `Transcript of ${t.source} (${t.language}, ${clock(t.duration)}, ${t.engine} ${t.model})\n\n`;
        const footer = truncated
          ? `\n[Page ends at ${clock(shownUntil)}: call again with from_time=${Math.floor(shownUntil)} for more]`
          : '\n[End of transcript]';
        return text(header + body + footer);
      } catch (error) {
        return fail(error);
      }
    }
  );

  server.registerTool(
    'capcut_detect_pauses',
    {
      title: 'Detect Pauses',
      description: `Preview which parts of a file are speech and which are pauses, without changing any draft.

Uses the transcript's word timings when the file has been transcribed (robust to background noise),
otherwise ffmpeg's silence detection. Returns how much would be removed and the first kept ranges.
Use it to tune min_pause before capcut_add_video_without_pauses.

Args:
  - path (string): Absolute local path
  - start / end (number): Source range to analyse, in seconds (default: whole file)
  - min_pause (number): Pauses at least this long are cut (default 0.7 s)
  - padding (number): Seconds kept around speech so cuts don't clip words (default 0.15)
  - method ('auto' | 'transcript' | 'audio'): auto = transcript if available, else audio
  - noise_db (number): Silence threshold for method audio (default -35 dB)`,
      inputSchema: z.object({
        path: LocalPath,
        start: z.number().min(0).default(0),
        end: z.number().positive().optional(),
        min_pause: z.number().min(0.1).max(10).default(0.7),
        padding: z.number().min(0).max(1).default(0.15),
        method: z.enum(['auto', 'transcript', 'audio']).default('auto'),
        noise_db: z.number().min(-80).max(-10).default(-35)
      }).strict(),
      annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false }
    },
    async (params) => {
      try {
        const r = await apiClient.request<any>('/detect_pauses', 'POST', params);
        if (!r.success) throw new Error(r.error);
        const p = r.result;
        const sample = p.ranges.slice(0, 15).map(([s, e]: number[]) => `${clock(s)}–${clock(e)}`).join(', ');
        return text(
          `Method: ${p.method}\n` +
          `Range: ${clock(p.start)}–${clock(p.end)} (${p.original_duration}s)\n` +
          `Kept: ${p.kept_duration}s in ${p.ranges.length} pieces (${p.cuts} cuts), removed ${p.removed_duration}s\n` +
          `First kept ranges: ${sample}${p.ranges.length > 15 ? ', …' : ''}`);
      } catch (error) {
        return fail(error);
      }
    }
  );

  server.registerTool(
    'capcut_add_video_without_pauses',
    {
      title: 'Add Video Without Pauses',
      description: `Add a source range of a local video to the draft with its pauses cut out, in one call:
the speech pieces are placed back to back on the track (jump cuts), sharing one media file.

Transcribe the file first (capcut_transcribe) for word-accurate cuts; without a transcript the
cuts come from audio silence detection. Afterwards capcut_add_auto_subtitles places subtitles
correctly on the cut timeline. Preview the effect with capcut_detect_pauses.

Args:
  - draft_id (string): Draft ID (a new draft is created if it does not exist)
  - video_url (string): Absolute local path to the video
  - start / end (number): Source range in seconds (default: whole file)
  - target_start (number): Where the first piece starts on the timeline (default 0)
  - min_pause / padding / method / noise_db: as in capcut_detect_pauses
  - volume (number): 0.0-1.0 (default 1.0)
  - punch_in_zoom (number): e.g. 1.12 = every other piece is framed 12% tighter, the classic way
    to hide jump cuts in talking-head videos (default 1 = off)
  - track_name (string): Video track (default "video_main")
  - width / height (number): Canvas size if the draft has to be created (default 1080x1920)

Returns the number of pieces, the new duration and timeline_end (where the next item can go).`,
      inputSchema: z.object({
        draft_id: z.string().min(1),
        video_url: LocalPath,
        start: z.number().min(0).default(0),
        end: z.number().positive().optional(),
        target_start: z.number().min(0).default(0),
        min_pause: z.number().min(0.1).max(10).default(0.7),
        padding: z.number().min(0).max(1).default(0.15),
        method: z.enum(['auto', 'transcript', 'audio']).default('auto'),
        noise_db: z.number().min(-80).max(-10).default(-35),
        volume: z.number().min(0).max(1).default(1.0),
        punch_in_zoom: z.number().min(1).max(2).default(1.0)
          .describe('Alternate framing on every other piece (e.g. 1.12) to hide jump cuts; 1 = off'),
        track_name: z.string().min(1).optional(),
        width: z.number().int().min(360).max(4096).default(1080),
        height: z.number().int().min(360).max(4096).default(1920)
      }).strict(),
      annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: false, openWorldHint: false }
    },
    async (params) => {
      try {
        const r = await apiClient.request<any>('/add_video_without_pauses', 'POST', params);
        if (!r.success) throw new Error(r.error);
        const p = r.result;
        return text(
          `Added ${p.segments} pieces (${p.cuts} cuts, method ${p.method}) to draft ${p.draft_id}.\n` +
          `Duration ${p.original_duration}s -> ${p.new_duration}s (removed ${p.removed_duration}s). ` +
          `Timeline now ends at ${p.timeline_end}s.`);
      } catch (error) {
        return fail(error);
      }
    }
  );

  server.registerTool(
    'capcut_add_auto_subtitles',
    {
      title: 'Add Automatic Subtitles',
      description: `Generate subtitles from the transcript of a video already on the draft's timeline and add them.

Subtitles follow the edit: words from parts that were cut are dropped and the rest are shifted to
where they play on the timeline (works with capcut_add_video_without_pauses and capcut_add_video).
The file must have been transcribed with capcut_transcribe. Lines are short, social-media style.

Args:
  - draft_id (string): Draft ID
  - video_url (string): Absolute local path of the video on the timeline
  - max_chars (number): Max characters per subtitle (default 32)
  - max_duration (number): Max seconds per subtitle (default 3)
  - font_size (number): CapCut scale (default 8)
  - font_color (string): Hex color (default #FFFFFF)
  - bold (boolean): default true
  - border_color / border_width: Text outline, e.g. "#000000" and 40 for a readable outline (default none)
  - background_color / background_alpha: Box behind the text (alpha 0 = no box, default)
  - position_y (number): Vertical position 0 (top) to 1 (bottom) (default 0.8)
  - track_name (string): Default "subtitle"`,
      inputSchema: z.object({
        draft_id: z.string().min(1),
        video_url: LocalPath,
        max_chars: z.number().int().min(8).max(80).default(32),
        max_duration: z.number().min(0.5).max(10).default(3),
        font: z.string().optional(),
        font_size: z.number().min(1).max(100).default(8),
        font_color: z.string().regex(/^#[0-9A-Fa-f]{6}$/).default('#FFFFFF'),
        bold: z.boolean().default(true),
        border_color: z.string().regex(/^#[0-9A-Fa-f]{6}$/).optional(),
        border_width: z.number().min(0).max(100).optional(),
        background_color: z.string().regex(/^#[0-9A-Fa-f]{6}$/).optional(),
        background_alpha: z.number().min(0).max(1).optional(),
        position_y: z.number().min(0).max(1).default(0.8),
        track_name: z.string().min(1).optional()
      }).strict(),
      annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: false, openWorldHint: false }
    },
    async (params) => {
      try {
        const { position_y, ...rest } = params;
        const r = await apiClient.request<any>('/add_auto_subtitles', 'POST',
          { ...rest, transform_y: (0.5 - position_y) * 2 });
        if (!r.success) throw new Error(r.error);
        return text(`Added ${r.result.subtitles} subtitles to draft ${r.result.draft_id}. First:\n${r.result.first}`);
      } catch (error) {
        return fail(error);
      }
    }
  );

  server.registerTool(
    'capcut_add_background_music',
    {
      title: 'Add Background Music',
      description: `Lay a music file under the whole edit (or a range), looping it if it is shorter, with a
fade-in and fade-out, and optionally ducking under the voice: the music dips while someone speaks
(volume keyframes, editable in CapCut). Ducking needs the voice video on the timeline and its
transcript (capcut_transcribe). Add it after the edit is assembled: by default it runs to the end.

Args:
  - draft_id (string), audio_url (string): local music file
  - volume (number): Music level, 1 = original (default 0.25)
  - fade_in / fade_out (number): Seconds (default 1 / 2)
  - start / end (number): Range on the timeline (default: 0 to the end of the edit)
  - duck_under (string): Path of the voice video already on the timeline
  - duck_level (number): Music level while speaking, as a fraction of volume (default 0.35)
  - track_name (string): Audio track (default "music")`,
      inputSchema: z.object({
        draft_id: z.string().min(1),
        audio_url: LocalPath,
        volume: z.number().positive().max(2).default(0.25),
        fade_in: z.number().min(0).default(1),
        fade_out: z.number().min(0).default(2),
        start: z.number().min(0).default(0),
        end: z.number().positive().optional(),
        duck_under: LocalPath.optional(),
        duck_level: z.number().min(0).max(1).default(0.35),
        track_name: z.string().min(1).optional()
      }).strict(),
      annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: false, openWorldHint: false }
    },
    async (params) => {
      try {
        const r = await apiClient.request<any>('/add_background_music', 'POST', params);
        if (!r.success) throw new Error(r.error);
        const p = r.result;
        return text(`Added music on track "${p.track}" from ${p.start}s to ${p.end}s (${p.pieces} piece(s))` +
          (params.duck_under ? `, ducked under ${p.ducked_spans} speech span(s).` : '.'));
      } catch (error) {
        return fail(error);
      }
    }
  );

  server.registerTool(
    'capcut_get_timeline',
    {
      title: 'Get Timeline',
      description: `List the draft's tracks with their clip count and where each one ends (and, for a draft
from capcut_open_project, the existing project's tracks), to place new items after or over them.`,
      inputSchema: z.object({ draft_id: z.string().min(1) }).strict(),
      annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false }
    },
    async (params) => {
      try {
        const r = await apiClient.request<any>('/timeline', 'POST', params);
        if (!r.success) throw new Error(r.error);
        const t = r.result;
        const line = (x: any) => `- ${x.type} "${x.name}": ${x.clips} clip(s), ends at ${x.end}s`;
        let out = `## Timeline (${t.duration}s)\n${t.tracks.map(line).join('\n') || '_empty_'}`;
        if (t.existing_project) {
          out += `\n\n### Existing project "${t.existing_project.name}" (${t.existing_project.duration}s)\n` +
            t.existing_project.tracks.map(line).join('\n');
        }
        return { content: [{ type: 'text' as const, text: out }], structuredContent: t };
      } catch (error) {
        return fail(error);
      }
    }
  );

  server.registerTool(
    'capcut_add_camera_move',
    {
      title: 'Add Camera Move',
      description: `Add a virtual camera move over a time range of the timeline (professional camera movements).

Keyframe moves animate the clips on a video track (default "video_main"); a move can span several
clips, e.g. after pause removal, and stays continuous across the cuts:
${CAMERA_MOVES.keyframes.map(([n, d]) => `  - ${n}: ${d}`).join('\n')}

Effect moves add a CapCut effect on the "camera_fx" track:
${CAMERA_MOVES.effects.map(([n, d]) => `  - ${n}: ${d}`).join('\n')}

Moves stack on keyframes already on the clips (mode "compose"); to redo a move over the same range use
mode "replace", or "refuse" to error if the range already has keyframes. Keyframes outside the range
are never changed. Short ranges (min 0.1 s) squeeze the move's shape; every move ends on the
starting framing. easing shapes keyframe moves: smooth (default), linear, snappy (fast start, soft
landing), dramatic (slow-fast-slow). For effect moves, intensity scales the effect's strength.

Use the transcript to place moves on meaningful moments: a punch_in on a key sentence, a punch on
a strong word, push_in during a build-up, shake on an emotional beat. Don't overdo it: a move every
10-20 seconds reads as edited, constant motion reads as noise.

Args:
  - draft_id (string), move (string)
  - start / end (number): Timeline range in seconds
  - intensity (number): 0.3 subtle - 1 default - 2 strong
  - track_name (string): Video track to move (keyframe moves)
  - flash (boolean): Add a short white flash at the start (good with punch / punch_in)
  - mode ('compose' | 'replace' | 'refuse'), easing ('smooth' | 'linear' | 'snappy' | 'dramatic')`,
      inputSchema: z.object({
        draft_id: z.string().min(1),
        move: z.enum(ALL_MOVES),
        start: z.number().min(0),
        end: z.number().positive(),
        intensity: z.number().min(0.1).max(3).default(1),
        track_name: z.string().min(1).optional(),
        flash: z.boolean().default(false),
        mode: z.enum(['compose', 'replace', 'refuse']).default('compose'),
        easing: z.enum(['smooth', 'linear', 'snappy', 'dramatic']).default('smooth')
      }).strict(),
      annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: false, openWorldHint: false }
    },
    async (params) => {
      try {
        const r = await apiClient.request<any>('/add_camera_move', 'POST', params);
        if (!r.success) throw new Error(r.error);
        const p = r.result;
        return text(p.kind === 'effect'
          ? `Added ${p.move} (effect) from ${params.start}s to ${params.end}s.`
          : `Added ${p.move} from ${params.start}s to ${params.end}s on ${p.clips} clip(s).`);
      } catch (error) {
        return fail(error);
      }
    }
  );
}
