// Content-aware editing tools: transcription, pause detection, pause-free assembly, auto subtitles.
// Heavy data (word timings) stays in the backend; tools return compact summaries to save tokens.

import { z } from 'zod';
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { apiClient } from '../services/api-client.js';
import * as C from '../contracts.js';
import { backendDir } from '../services/backend.js';

const PAGE_CHARS = 12000;

// Names and descriptions shared with the backend: vectcut-api/camera_moves.json
const CAMERA_CATALOG = JSON.parse(readFileSync(join(backendDir(), 'camera_moves.json'), 'utf8')) as {
  keyframes: Array<{ name: string; description: string }>;
  effects: Array<{ name: string; description: string }>;
};
const CAMERA_MOVES = {
  keyframes: CAMERA_CATALOG.keyframes.map((m) => [m.name, m.description] as const),
  effects: CAMERA_CATALOG.effects.map((m) => [m.name, m.description] as const)
};
const ALL_MOVES = [...CAMERA_MOVES.keyframes, ...CAMERA_MOVES.effects].map(([n]) => n) as unknown as [string, ...string[]];

const Point = z.tuple([z.number().min(0).max(1), z.number().min(0).max(1)]);

const LocalPath = z.string()
  .min(1)
  .describe('Absolute local path to the video or audio file');


function clock(t: number): string {
  const h = Math.floor(t / 3600);
  const m = Math.floor((t % 3600) / 60);
  const s = (t % 60).toFixed(1).padStart(4, '0');
  return h ? `${h}:${String(m).padStart(2, '0')}:${s}` : `${String(m).padStart(2, '0')}:${s}`;
}

// Whisper segments are often a few words long: merge them into readable blocks
// that end on a sentence or after ~20 s, each with one time range.

function text(t: string) {
  return { content: [{ type: 'text' as const, text: t }] };
}

function fail(error: unknown) {
  return { ...text(`Error: ${error instanceof Error ? error.message : String(error)}`), isError: true };
}

function describeAdded(p: C.CameraMoveAdded, start: number, end: number): string {
  if (p.kind === 'effect') return `Added ${p.move} (effect, id ${p.move_id}) from ${start}s to ${end}s.`;
  const extras = [p.effects ? `${p.effects} CapCut effect(s) inside the range` : '',
    p.fill === 'blur' ? 'blurred background behind the clip' : ''].filter(Boolean).join(', ');
  return `Added ${p.move} (id ${p.move_id}) from ${start}s to ${end}s on ${p.clips} clip(s)` +
    (extras ? ` (${extras}).` : '.');
}

function moveLine(m: C.CameraMoveListed): string {
  const focus = m.focus !== undefined ? `, focus ${JSON.stringify(m.focus)}` : '';
  return `- ${m.id}: ${m.move} ${m.start}-${m.end}s on ${m.where}, intensity ${m.intensity}${focus}` +
    (m.effects ? `, ${m.effects} effect(s)` : '');
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
        const r = await apiClient.request('/transcribe', 'POST', {
          path: params.path, language: params.language, model: params.model, wait: 45,
          page: { from_time: params.from_time, to_time: params.to_time, max_chars: PAGE_CHARS }
        }, C.TranscribeResult);
        if (!r.success || !r.result) throw new Error(r.error);
        const out = r.result;
        if (out.status === 'running') {
          return text(`Transcription running (${out.elapsed}s elapsed). Call capcut_transcribe again with the same path to keep waiting.`);
        }
        const t = out.page;
        const body = t.blocks.map((b) => `[${clock(b.start)}–${clock(b.end)}] ${b.text}\n`).join('');
        const header = `Transcript of ${t.source} (${t.language}, ${clock(t.duration)}, ${t.engine} ${t.model})\n\n`;
        // next_from is passed back exactly: rounding it down would repeat the last block (or the whole page)
        const range = params.to_time !== undefined ? ` and to_time=${params.to_time}` : '';
        const footer = t.next_from !== null
          ? `\n[Page ends at ${clock(t.next_from)}: call again with from_time=${t.next_from}${range} for more]`
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
        const r = await apiClient.request('/detect_pauses', 'POST', params, C.SpeechRangesResult);
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
        const r = await apiClient.request('/add_video_without_pauses', 'POST', params, C.WithoutPausesResult);
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
        const r = await apiClient.request('/add_auto_subtitles', 'POST',
          { ...rest, transform_y: (0.5 - position_y) * 2 }, C.AutoSubtitlesResult);
        if (!r.success) throw new Error(r.error);
        return text(`Added ${r.result.subtitles} subtitles to draft ${r.result.draft_id}. First:\n${r.result.first}`);
      } catch (error) {
        return fail(error);
      }
    }
  );

  server.registerTool(
    'capcut_list_camera_moves',
    {
      title: 'List Camera Moves',
      description: `List the camera moves added to this draft with capcut_add_camera_move: id, move, range,
where, intensity, focus and the CapCut effects each one added. Use the ids to take a move off or move it.
Moves saved in an earlier session are not listed (they are plain keyframes in the project by then).`,
      inputSchema: z.object({ draft_id: z.string().min(1) }).strict(),
      annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false }
    },
    async (params) => {
      try {
        const r = await apiClient.request('/list_camera_moves', 'POST', params, C.CameraMoveListResult);
        if (!r.success) throw new Error(r.error);
        const moves = r.result.moves;
        return text(moves.length ? `## Camera moves (${moves.length})\n${moves.map(moveLine).join('\n')}` : 'No camera moves in this draft.');
      } catch (error) {
        return fail(error);
      }
    }
  );

  server.registerTool(
    'capcut_remove_camera_move',
    {
      title: 'Remove Camera Move',
      description: `Take off a camera move added to this draft: its clips get back the keyframes they had in
its range, and the CapCut effects and background it added go. Give move_id (from capcut_list_camera_moves),
or start and end to take off every move of the draft inside that range. A move with a later move over the
same stretch of the same clip is refused until the later one is taken off.`,
      inputSchema: z.object({
        draft_id: z.string().min(1),
        move_id: z.string().min(1).optional(),
        start: z.number().min(0).optional(),
        end: z.number().positive().optional()
      }).strict(),
      annotations: { readOnlyHint: false, destructiveHint: true, idempotentHint: false, openWorldHint: false }
    },
    async (params) => {
      try {
        if (params.move_id === undefined && (params.start === undefined || params.end === undefined)) {
          throw new Error('Give move_id, or start and end');
        }
        const r = await apiClient.request('/remove_camera_move', 'POST', params, C.CameraMoveRemoveResult);
        if (!r.success) throw new Error(r.error);
        const t = r.result;
        return text(`Took off ${t.removed.map((m) => `${m.id} (${m.move} ${m.start}-${m.end}s)`).join(', ')}.` +
          (t.moves.length ? `\nStill there:\n${t.moves.map(moveLine).join('\n')}` : '\nNo camera moves left.'));
      } catch (error) {
        return fail(error);
      }
    }
  );

  server.registerTool(
    'capcut_move_camera_move',
    {
      title: 'Move Camera Move',
      description: `Place a camera move of this draft somewhere else, exactly or by nudging it: it is taken off
and added again with the same settings; intensity or focus can change at the same time. The move gets a new id.
  - where: start (seconds on the timeline), or shift (seconds, negative = earlier), or shift_frames
    (frames of the draft, negative = earlier); none = it stays where it is
  - length: end (seconds), or extend (seconds added at its end, negative = shorter), or extend_frames;
    none = same length
E.g. "3 frames earlier": shift_frames -3; "half a second longer": extend 0.5.`,
      inputSchema: z.object({
        draft_id: z.string().min(1),
        move_id: z.string().min(1),
        start: z.number().min(0).optional(),
        end: z.number().positive().optional(),
        shift: z.number().optional().describe('Seconds to move it by (negative = earlier)'),
        shift_frames: z.number().int().optional().describe('Frames to move it by (negative = earlier)'),
        extend: z.number().optional().describe('Seconds added at its end (negative = shorter)'),
        extend_frames: z.number().int().optional().describe('Frames added at its end (negative = shorter)'),
        intensity: z.number().min(0.1).max(3).optional(),
        focus: z.union([Point, z.array(Point).min(1).max(8)]).optional()
      }).strict(),
      annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: false, openWorldHint: false }
    },
    async (params) => {
      try {
        const where = [params.start, params.shift, params.shift_frames].filter((v) => v !== undefined).length;
        const length = [params.end, params.extend, params.extend_frames].filter((v) => v !== undefined).length;
        if (where > 1) throw new Error('Give start, shift or shift_frames, not more than one');
        if (length > 1) throw new Error('Give end, extend or extend_frames, not more than one');
        const r = await apiClient.request('/move_camera_move', 'POST', params, C.CameraMoveResult);
        if (!r.success) throw new Error(r.error);
        const p = r.result;
        return text(`Moved ${params.move_id}: ` + describeAdded(p, p.start, p.end).replace(/^Added /, ''));
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
        const r = await apiClient.request('/add_background_music', 'POST', params, C.MusicResult);
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
from capcut_open_project, the existing project's tracks), to place new items after or over them. Also lists
the draft's saves that stopped halfway: pending (settled at the next change or start; the draft cannot be
saved until then) or left as they were because the project changed since (the note says where the previous
version is).`,
      inputSchema: z.object({ draft_id: z.string().min(1) }).strict(),
      annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false }
    },
    async (params) => {
      try {
        const r = await apiClient.request('/timeline', 'POST', params, C.TimelineResult);
        if (!r.success) throw new Error(r.error);
        const t = r.result;
        const line = (x: { type: string | null; name: string; clips: number; end: number }) =>
          `- ${x.type} "${x.name}": ${x.clips} clip(s), ends at ${x.end}s`;
        let out = `## Timeline (${t.duration}s, revision ${t.revision})\n${t.tracks.map(line).join('\n') || '_empty_'}`;
        if (t.existing_project) {
          out += `\n\n### Existing project "${t.existing_project.name}" (${t.existing_project.duration}s)\n` +
            t.existing_project.tracks.map(line).join('\n');
        }
        if (t.unsettled_saves?.length) {
          out += `\n\n### Saves that stopped halfway\n` + t.unsettled_saves.map((u) =>
            `- ${u.state} (${u.started}, ${u.folders.join(', ')}): ${u.note}`).join('\n');
        }
        return { content: [{ type: 'text' as const, text: out }], structuredContent: t };
      } catch (error) {
        return fail(error);
      }
    }
  );

  server.registerTool(
    'capcut_list_clips',
    {
      title: 'List Clips of an Opened Project',
      description: `List the clips already in a project opened with capcut_open_project: id, track, kind
(video, photo, audio, text, ...), file or text, start/end on the timeline and in the source (seconds),
keyframes, transition/animation, and whether the clip can be edited (if not, why). The list reflects
edits made in this draft. Read only. Filter with kind or editable_only; page with offset/limit.`,
      inputSchema: z.object({
        draft_id: z.string().min(1),
        kind: z.string().optional(),
        editable_only: z.boolean().optional(),
        offset: z.number().int().min(0).optional(),
        limit: z.number().int().min(1).max(500).optional()
      }).strict(),
      annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false }
    },
    async (params) => {
      try {
        const r = await apiClient.request('/list_clips', 'POST', params, C.ClipListResult);
        if (!r.success) throw new Error(r.error);
        const t = r.result;
        const lines = t.clips.map((c) => {
          const src = c.source_start !== undefined ? `, source ${c.source_start}-${c.source_end}s` : '';
          const extra = [c.keyframes.length ? `keyframes: ${c.keyframes.join(', ')}` : '',
            c.transition ? 'transition' : '', c.animation ? 'animation' : ''].filter(Boolean).join(', ');
          return `- \`${c.id}\` track ${c.track} (${c.track_type}${c.track_name ? ` "${c.track_name}"` : ''}): ` +
            `${c.kind} "${c.name}" ${c.start}-${c.end}s${src}${extra ? `; ${extra}` : ''} — ` +
            (c.editable ? 'editable' : `locked: ${c.locked_reason}`);
        });
        let out = `## Clips of "${t.project_name}" (${t.total}, revision ${t.revision}` +
          `${t.edits ? `, ${t.edits} edit(s) in this draft` : ''})\n${lines.join('\n') || '_none_'}`;
        if (t.next_offset !== null) out += `\n\nMore clips: call again with offset ${t.next_offset}.`;
        return { content: [{ type: 'text' as const, text: out }], structuredContent: t };
      } catch (error) {
        return fail(error);
      }
    }
  );

  server.registerTool(
    'capcut_edit_clip',
    {
      title: 'Trim or Move a Clip of an Opened Project',
      description: `Trim or move a clip already in a project opened with capcut_open_project, like dragging it
in CapCut (ids from capcut_list_clips; only clips listed as editable). Seconds:
  - trim_start: cut from the clip's beginning (its start moves later); negative extends it back
  - trim_end: cut from its end; negative extends it
  - move_to: new start on the timeline
  - ripple: the clips after it on the same track shift by the same amount (closes or opens the gap)
  - ripple_all: the clips after it on every track shift too (titles, music, overlays stay in step),
    those this draft added included
On CapCut's main track (magnet on) a trim keeps the clip's start and the clips after it close up, and
moves are refused. Video, photo, audio and text clips. Refused, with the reason, if a clip would
overlap another, go past its file, lose keyframes, fades or a transition, or change length with
intro/outro animations. Nothing is written to the project until capcut_save_draft, which checks that
only these clips changed.`,
      inputSchema: z.object({
        draft_id: z.string().min(1),
        clip_id: z.string().min(1),
        trim_start: z.number().optional(),
        trim_end: z.number().optional(),
        move_to: z.number().min(0).optional(),
        ripple: z.boolean().optional(),
        ripple_all: z.boolean().optional(),
        expected_revision: z.number().int().optional()
      }).strict(),
      annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: false, openWorldHint: false }
    },
    async (params) => {
      try {
        const r = await apiClient.request('/edit_clip', 'POST', params, C.EditClipResult);
        if (!r.success) throw new Error(r.error);
        const t = r.result;
        const c = t.clip;
        let out = `Clip \`${c.id}\` now ${c.start}-${c.end}s on the timeline` +
          (c.source_start !== undefined ? `, source ${c.source_start}-${c.source_end}s` : '') +
          ` (revision ${t.revision}; written to the project at capcut_save_draft).`;
        if (t.shifted.length) out += `\nShifted with it: ${t.shifted.map((s) => `\`${s}\``).join(', ')}.`;
        for (const n of t.notes) out += `\n- ${n}`;
        return { content: [{ type: 'text' as const, text: out }], structuredContent: t };
      } catch (error) {
        return fail(error);
      }
    }
  );

  server.registerTool(
    'capcut_view_frames',
    {
      title: 'View Frames',
      description: `Look at still frames of the footage, to see the framing before choosing camera moves:
where the subject is, the room above the head, movement, scene changes.

Either a local file (times of the file) or a draft (times of the timeline: the clip on a video track
at each moment, default "video_main", or for a project opened with capcut_open_project its main
track, or project_track). Frames show the footage as filmed: CapCut's keyframes, camera moves,
effects and titles are not drawn on them.

Ask for times (e.g. the moments where a move might go) or every N seconds over start-end for an
overview. At most 24 frames per call; each frame at the default width costs about as much as a
page of text, so prefer a few well-chosen times.

Args:
  - path (string) or draft_id (string)
  - times (number[]): Seconds; or every (number) with start / end (default: the whole file or track)
  - width (number): Frame width in pixels, 160-960 (default 360)
  - track_name (string) / project_track (number): Video track of the draft / of the opened project`,
      inputSchema: z.object({
        path: LocalPath.optional(),
        draft_id: z.string().min(1).optional(),
        times: z.array(z.number().min(0)).min(1).max(24).optional(),
        every: z.number().positive().optional(),
        start: z.number().min(0).default(0),
        end: z.number().positive().optional(),
        width: z.number().int().min(160).max(960).default(360),
        track_name: z.string().min(1).optional(),
        project_track: z.number().int().min(0).optional()
      }).strict(),
      annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false }
    },
    async (params) => {
      try {
        if ((params.path === undefined) === (params.draft_id === undefined)) {
          throw new Error('Give either path (times of the file) or draft_id (times of the timeline)');
        }
        if ((params.times === undefined) === (params.every === undefined)) {
          throw new Error('Give either times or every');
        }
        const r = await apiClient.request('/view_frames', 'POST', params, C.FramesResult);
        if (!r.success) throw new Error(r.error);
        const p = r.result;
        const content: Array<{ type: 'text'; text: string } | { type: 'image'; data: string; mimeType: string }> = [
          { type: 'text', text: `Frames of ${p.of} (${p.frames.length}, times of the ${p.timeline ? 'timeline' : 'file'}):` }
        ];
        for (const f of p.frames) {
          const src = p.timeline && f.source ? ` — ${f.source} at ${clock(f.source_time ?? 0)}` : '';
          content.push({ type: 'text', text: `${clock(f.time)}${src}${f.note ? `: ${f.note}` : ''}` });
          if (f.image) content.push({ type: 'image', data: f.image, mimeType: 'image/jpeg' });
        }
        return { content };
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

Zooms aim at the centre, or at focus: a point [x, y] (0-1 from the top-left), e.g. a face found with
capcut_view_frames; multi-step moves (triple_punch...) take one point per step. Presets zoom as much as
their rotation and offsets need so no canvas edge shows; zoom-outs start from a closer framing and pull
back, so start them on a cut. far_pull, recoil, spin and whip_punch shrink the image on purpose: on a
new draft's bottom video track the clip gets a blurred background (fill "auto"), elsewhere the canvas
shows around it. Some presets add CapCut effects inside their range (flash, blur, fisheye, chromatic...).

Moves stack on keyframes already on the clips (mode "compose"); to redo a move over the same range use
mode "replace", or "refuse" to error if the clip is already animated in the range (a keyframe in it,
or an animation passing through it). Keyframes outside the range are never changed. Short ranges (min 0.1 s) squeeze the move's shape; every move ends on the
starting framing. easing shapes keyframe moves: smooth (default), linear, snappy (fast start, soft
landing), dramatic (slow-fast-slow). For effect moves, intensity scales the effect's strength.

Use the transcript to place moves on meaningful moments: a punch_in on a key sentence, a punch on
a strong word, push_in during a build-up, shake on an emotional beat. Look at the framing first with
capcut_view_frames: little room above the head calls for a lower intensity. Don't overdo it: a move
every 10-20 seconds reads as edited, constant motion reads as noise.

Args:
  - draft_id (string), move (string)
  - start / end (number): Timeline range in seconds
  - intensity (number): 0.3 subtle - 1 default - 2 strong
  - track_name (string): Video track to move (keyframe moves)
  - project_track (number): on a project opened with capcut_open_project, index of its own video track
    (from capcut_list_clips; default its main track): the move animates the clips already there
  - flash (boolean): Add a short white flash at the start (good with punch / punch_in)
  - mode ('compose' | 'replace' | 'refuse'), easing ('smooth' | 'linear' | 'snappy' | 'dramatic')
  - focus ([x, y] or [[x, y], ...]): where zooms aim (default the centre)
  - fill ('auto' | 'blur' | 'none'): background of the presets that shrink the image`,
      inputSchema: z.object({
        draft_id: z.string().min(1),
        move: z.enum(ALL_MOVES),
        start: z.number().min(0),
        end: z.number().positive(),
        intensity: z.number().min(0.1).max(3).default(1),
        track_name: z.string().min(1).optional(),
        flash: z.boolean().default(false),
        mode: z.enum(['compose', 'replace', 'refuse']).default('compose'),
        easing: z.enum(['smooth', 'linear', 'snappy', 'dramatic']).default('smooth'),
        project_track: z.number().int().min(0).optional(),
        focus: z.union([Point, z.array(Point).min(1).max(8)]).optional()
          .describe('Point [x, y] (0-1 from the top-left) zooms aim at, or one per step of a multi-step move'),
        fill: z.enum(['auto', 'blur', 'none']).default('auto')
      }).strict(),
      annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: false, openWorldHint: false }
    },
    async (params) => {
      try {
        const r = await apiClient.request('/add_camera_move', 'POST', params, C.CameraMoveResult);
        if (!r.success) throw new Error(r.error);
        const p = r.result;
        return text(describeAdded(p, p.start, p.end));
      } catch (error) {
        return fail(error);
      }
    }
  );
}
