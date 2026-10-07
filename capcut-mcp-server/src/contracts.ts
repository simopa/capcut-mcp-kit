// Added in capcut-mcp-kit (2026): what the VectCutAPI backend returns for each endpoint (its
// "output"). The TypeScript types derive from these schemas; every reply is checked against them
// at runtime, and the same schemas, exported as JSON (npm run contracts), are checked by the
// backend's own tests against its real replies, so the two sides cannot drift apart unnoticed.

import { z } from 'zod';

const MovedItem = z.object({
  requested_track: z.string(),
  track: z.string(),
  start: z.number()
});

/** Reply of the routes that add something to a draft. */
export const DraftRefResult = z.object({
  draft_id: z.string(),
  draft_url: z.string(),
  revision: z.number(),
  moved_to_free_track: z.array(MovedItem).optional()
}).passthrough();

export const CreateDraftResult = z.object({
  draft_id: z.string(),
  draft_url: z.string(),
  width: z.number(),
  height: z.number(),
  fps: z.number()
}).passthrough();

export const SaveDraftResult = z.object({
  draft_url: z.string(),
  revision: z.number(),
  backups: z.array(z.string()),
  added_tracks: z.number().optional(),
  warnings: z.array(z.string()).optional()
}).passthrough();

export const DurationResult = z.object({
  duration: z.number(),
  format: z.string()
}).passthrough();

export const ListTypesResult = z.array(z.object({ name: z.string() }).passthrough());

export const ProjectEntry = z.object({
  name: z.string(),
  modified_time: z.string(),
  is_capcut_project: z.boolean()
}).passthrough();

export const ListProjectsResult = z.object({
  projects: z.array(ProjectEntry)
}).passthrough();

const ProjectTrack = z.object({
  type: z.string().nullable(),
  name: z.string(),
  clips: z.number(),
  end: z.number()
});

export const OpenProjectResult = z.object({
  draft_id: z.string(),
  project_name: z.string(),
  path: z.string(),
  width: z.number(),
  height: z.number(),
  fps: z.number().nullable(),
  duration: z.number(),
  tracks: z.array(ProjectTrack),
  exact_round_trip: z.boolean(),
  note: z.string()
}).passthrough();

const TranscriptBlock = z.object({ start: z.number(), end: z.number(), text: z.string() });

export const TranscriptPage = z.object({
  source: z.string(),
  language: z.string().nullable(),
  duration: z.number(),
  engine: z.string(),
  model: z.string(),
  blocks: z.array(TranscriptBlock),
  next_from: z.number().nullable()
});

export const TranscribeResult = z.discriminatedUnion('status', [
  z.object({ status: z.literal('running'), elapsed: z.number() }),
  z.object({ status: z.literal('done'), page: TranscriptPage })
]);

export const SpeechRangesResult = z.object({
  method: z.enum(['transcript', 'audio']),
  start: z.number(),
  end: z.number(),
  ranges: z.array(z.tuple([z.number(), z.number()])),
  original_duration: z.number(),
  kept_duration: z.number(),
  removed_duration: z.number(),
  cuts: z.number()
});

export const WithoutPausesResult = DraftRefResult.extend({
  method: z.enum(['transcript', 'audio']),
  segments: z.number(),
  cuts: z.number(),
  original_duration: z.number(),
  new_duration: z.number(),
  removed_duration: z.number(),
  timeline_end: z.number()
});

export const AutoSubtitlesResult = z.object({
  draft_id: z.string(),
  revision: z.number(),
  subtitles: z.number(),
  first: z.string()
}).passthrough();

export const MusicResult = DraftRefResult.extend({
  track: z.string(),
  pieces: z.number(),
  start: z.number(),
  end: z.number(),
  ducked_spans: z.number()
});

const TimelineTrack = z.object({ name: z.string(), type: z.string(), clips: z.number(), end: z.number() });

const UnsettledSave = z.object({
  state: z.enum(['pending', 'abandoned']),
  kind: z.string(),
  folders: z.array(z.string()),
  started: z.string(),
  note: z.string()
});

export const TimelineResult = z.object({
  draft_id: z.string(),
  revision: z.number(),
  duration: z.number(),
  tracks: z.array(TimelineTrack),
  unsettled_saves: z.array(UnsettledSave).optional(),
  existing_project: z.object({
    name: z.string(),
    duration: z.number(),
    tracks: z.array(ProjectTrack)
  }).optional()
});

const Clip = z.object({
  id: z.string(),
  track: z.number(),
  track_type: z.string().nullable(),
  track_name: z.string(),
  kind: z.string(),
  name: z.string(),
  start: z.number(),
  end: z.number(),
  source_start: z.number().optional(),
  source_end: z.number().optional(),
  keyframes: z.array(z.string().nullable()),
  transition: z.boolean(),
  animation: z.boolean(),
  editable: z.boolean(),
  locked_reason: z.string().nullable()
}).passthrough();

export const ClipListResult = z.object({
  draft_id: z.string(),
  revision: z.number(),
  project_name: z.string(),
  total: z.number(),
  offset: z.number(),
  next_offset: z.number().nullable(),
  edits: z.number(),
  clips: z.array(Clip)
}).passthrough();

export const EditClipResult = z.object({
  draft_id: z.string(),
  revision: z.number(),
  clip: Clip,
  shifted: z.array(z.string()),
  notes: z.array(z.string())
}).passthrough();

export const CameraMoveResult = DraftRefResult.extend({
  move: z.string(),
  kind: z.enum(['keyframes', 'effect']),
  clips: z.number(),
  mode: z.enum(['compose', 'replace', 'refuse']).optional()
});

/** Every contract by name: exported to vectcut-api/tests/contracts.json for the backend's tests. */
export const CONTRACTS = {
  DraftRefResult, CreateDraftResult, SaveDraftResult, DurationResult, ListTypesResult,
  ListProjectsResult, OpenProjectResult, TranscribeResult, SpeechRangesResult, WithoutPausesResult,
  AutoSubtitlesResult, MusicResult, TimelineResult, CameraMoveResult, ClipListResult, EditClipResult
} as const;

export type DraftRef = z.infer<typeof DraftRefResult>;
export type CreatedDraft = z.infer<typeof CreateDraftResult>;
export type SavedDraft = z.infer<typeof SaveDraftResult>;
export type MediaDuration = z.infer<typeof DurationResult>;
export type OpenedProject = z.infer<typeof OpenProjectResult>;
export type Timeline = z.infer<typeof TimelineResult>;
