// Zod validation schemas for CapCut MCP tools

import { z } from 'zod';
import { ResponseFormat } from '../types.js';

// Common schemas
export const ResponseFormatSchema = z.nativeEnum(ResponseFormat)
  .default(ResponseFormat.MARKDOWN)
  .describe("Output format: 'markdown' for human-readable or 'json' for machine-readable");

// The backend copies local files directly, so absolute paths are accepted alongside URLs
const UrlSchema = z.string()
  .refine(
    (v) => v.startsWith('/') || z.string().url().safeParse(v).success,
    'Must be a valid URL or an absolute local file path'
  )
  .describe('URL or absolute local path to the media file');

// CapCut names are exact identifiers (e.g. "Zoom_Lens"); look them up with capcut_list_types
const capcutName = (category: string) => z.string()
  .min(1)
  .optional()
  .describe(`Exact CapCut name from capcut_list_types category="${category}"`);

// Items on the same track cannot overlap in time: by default an overlapping item goes to the first
// free track like it ("text_main_2"...), and the reply says so
const trackName = (defaultTrack: string) => z.string()
  .min(1)
  .optional()
  .describe(`Track to place this on (default "${defaultTrack}"). If it is busy at that time the item goes to the first free track like it ("${defaultTrack}_2"...) unless auto_track is false`);
const autoTrack = z.boolean()
  .default(true)
  .describe('Move an item that would overlap another one on its track to a free track (default true); false makes an overlap an error');

const animationDuration = z.number()
  .positive()
  .default(0.5)
  .describe('Animation duration in seconds');

// Draft creation schema
export const CreateDraftSchema = z.object({
  width: z.number()
    .int()
    .min(360, 'Width must be at least 360')
    .max(4096, 'Width must not exceed 4096')
    .default(1920)
    .describe('Video width in pixels'),
  height: z.number()
    .int()
    .min(360, 'Height must be at least 360')
    .max(4096, 'Height must not exceed 4096')
    .default(1080)
    .describe('Video height in pixels'),
  fps: z.union([z.literal(24), z.literal(25), z.literal(30), z.literal(50), z.literal(60)])
    .default(30)
    .describe('Frames per second: 24, 25, 30, 50 or 60'),
  response_format: ResponseFormatSchema
}).strict();

// Video track schema
export const AddVideoSchema = z.object({
  draft_id: z.string()
    .min(1, 'Draft ID is required')
    .describe('The ID of the draft to add video to'),
  video_url: UrlSchema,
  start: z.number()
    .min(0, 'Start time must be non-negative')
    .describe('Trim start within the source file, in seconds'),
  end: z.number()
    .positive('End time must be positive')
    .describe('Trim end within the source file, in seconds'),
  target_start: z.number()
    .min(0)
    .default(0)
    .describe('Where the clip starts on the timeline, in seconds (place clips one after another)'),
  volume: z.number()
    .min(0, 'Volume must be between 0 and 1')
    .max(1, 'Volume must be between 0 and 1')
    .default(1.0)
    .describe('Audio volume (0.0 to 1.0)'),
  transition: capcutName('transition')
    .describe('Transition from THIS clip into the NEXT one on the track (set it on the earlier clip); exact name from capcut_list_types category="transition"'),
  transition_duration: z.number()
    .positive()
    .default(0.5)
    .describe('Transition duration in seconds'),
  speed: z.number()
    .min(0.1, 'Speed must be at least 0.1x')
    .max(10, 'Speed must not exceed 10x')
    .default(1.0)
    .describe('Playback speed multiplier'),
  track_name: trackName('video_main'),
  auto_track: autoTrack,
  response_format: ResponseFormatSchema
}).strict();

// Audio track schema
export const AddAudioSchema = z.object({
  draft_id: z.string()
    .min(1, 'Draft ID is required')
    .describe('The ID of the draft to add audio to'),
  audio_url: UrlSchema,
  start: z.number()
    .min(0, 'Start time must be non-negative')
    .describe('Trim start within the source file, in seconds'),
  end: z.number()
    .positive('End time must be positive')
    .describe('Trim end within the source file, in seconds'),
  target_start: z.number()
    .min(0)
    .default(0)
    .describe('Where the audio starts on the timeline, in seconds'),
  volume: z.number()
    .min(0, 'Volume must be between 0 and 1')
    .max(1, 'Volume must be between 0 and 1')
    .default(1.0)
    .describe('Audio volume (0.0 to 1.0)'),
  fade_in: z.number()
    .min(0)
    .default(0)
    .describe('Fade in duration in seconds'),
  fade_out: z.number()
    .min(0)
    .default(0)
    .describe('Fade out duration in seconds'),
  track_name: trackName('audio_main'),
  auto_track: autoTrack,
  response_format: ResponseFormatSchema
}).strict();

// Text schema
export const AddTextSchema = z.object({
  draft_id: z.string()
    .min(1, 'Draft ID is required')
    .describe('The ID of the draft to add text to'),
  text: z.string()
    .min(1, 'Text content is required')
    .max(500, 'Text must not exceed 500 characters')
    .describe('The text content to display'),
  start: z.number()
    .min(0, 'Start time must be non-negative')
    .describe('Start time in seconds'),
  end: z.number()
    .positive('End time must be positive')
    .describe('End time in seconds'),
  font: z.string()
    .optional()
    .describe('Font family name'),
  font_size: z.number()
    .min(1, 'Font size must be at least 1')
    .max(100, 'Font size must not exceed 100')
    .default(8)
    .describe('Font size on CapCut\'s own scale (not points): ~5 small caption, 8 normal, 12-15 big title'),
  font_color: z.string()
    .regex(/^#[0-9A-Fa-f]{6}$/, 'Must be a valid hex color (e.g., #FFFFFF)')
    .default('#FFFFFF')
    .describe('Font color in hex format'),
  background_color: z.string()
    .regex(/^#[0-9A-Fa-f]{6}$/, 'Must be a valid hex color')
    .optional()
    .describe('Background color in hex format'),
  background_alpha: z.number()
    .min(0, 'Alpha must be between 0 and 1')
    .max(1, 'Alpha must be between 0 and 1')
    .default(0.8)
    .describe('Background opacity (0.0 to 1.0); only used when background_color is set'),
  shadow_enabled: z.boolean()
    .default(false)
    .describe('Enable text shadow'),
  shadow_color: z.string()
    .regex(/^#[0-9A-Fa-f]{6}$/, 'Must be a valid hex color')
    .default('#000000')
    .describe('Shadow color in hex format'),
  position_x: z.number()
    .min(0)
    .max(1)
    .default(0.5)
    .describe('Horizontal position (0.0 to 1.0, where 0.5 is center)'),
  position_y: z.number()
    .min(0)
    .max(1)
    .default(0.5)
    .describe('Vertical position (0.0 to 1.0, where 0.5 is center)'),
  intro_animation: capcutName('text_intro'),
  intro_duration: animationDuration,
  outro_animation: capcutName('text_outro'),
  outro_duration: animationDuration,
  track_name: trackName('text_main'),
  auto_track: autoTrack,
  response_format: ResponseFormatSchema
}).strict();

// Image schema
export const AddImageSchema = z.object({
  draft_id: z.string()
    .min(1, 'Draft ID is required')
    .describe('The ID of the draft to add image to'),
  image_url: UrlSchema,
  start: z.number()
    .min(0, 'Start time must be non-negative')
    .describe('Start time in seconds'),
  end: z.number()
    .positive('End time must be positive')
    .describe('End time in seconds'),
  position_x: z.number()
    .min(0)
    .max(1)
    .default(0.5)
    .describe('Horizontal position (0.0 to 1.0)'),
  position_y: z.number()
    .min(0)
    .max(1)
    .default(0.5)
    .describe('Vertical position (0.0 to 1.0)'),
  scale: z.number()
    .min(0.1, 'Scale must be at least 0.1')
    .max(5, 'Scale must not exceed 5')
    .default(1.0)
    .describe('Scale multiplier'),
  rotation: z.number()
    .min(-360)
    .max(360)
    .default(0)
    .describe('Clockwise rotation in degrees'),
  intro_animation: capcutName('intro'),
  intro_animation_duration: animationDuration,
  outro_animation: capcutName('outro'),
  outro_animation_duration: animationDuration,
  combo_animation: capcutName('combo'),
  combo_animation_duration: animationDuration,
  transition: capcutName('transition'),
  transition_duration: z.number()
    .positive()
    .default(0.5)
    .describe('Transition duration in seconds'),
  track_name: trackName('image_main'),
  auto_track: autoTrack,
  response_format: ResponseFormatSchema
}).strict();

// Subtitle schema
export const AddSubtitleSchema = z.object({
  draft_id: z.string()
    .min(1, 'Draft ID is required')
    .describe('The ID of the draft to add subtitles to'),
  srt_content: z.string()
    .min(1, 'SRT content is required')
    .describe('SRT text, or an absolute path / URL to an .srt file'),
  time_offset: z.number()
    .default(0)
    .describe('Shift all subtitles by this many seconds'),
  position_y: z.number()
    .min(0)
    .max(1)
    .optional()
    .describe('Vertical position 0.0 (top) to 1.0 (bottom); omit for CapCut default'),
  font: z.string()
    .optional()
    .describe('Font family name'),
  font_size: z.number()
    .min(1)
    .max(100)
    .default(5)
    .describe('Font size on CapCut\'s own scale (not points): ~5 normal subtitle, 8 large'),
  font_color: z.string()
    .regex(/^#[0-9A-Fa-f]{6}$/, 'Must be a valid hex color')
    .default('#FFFFFF')
    .describe('Font color in hex format'),
  background_enabled: z.boolean()
    .default(true)
    .describe('Enable background behind text'),
  background_color: z.string()
    .regex(/^#[0-9A-Fa-f]{6}$/, 'Must be a valid hex color')
    .default('#000000')
    .describe('Background color in hex format'),
  response_format: ResponseFormatSchema
}).strict();

// Keyframe schema
export const AddKeyframeSchema = z.object({
  draft_id: z.string()
    .min(1, 'Draft ID is required')
    .describe('The ID of the draft to add keyframes to'),
  track_name: z.string()
    .min(1)
    .default('video_main')
    .describe('Video/image track to animate: "video_main" for clips, "image_main" for images (or a custom track_name)'),
  property_types: z.array(z.enum(['position_x', 'position_y', 'rotation', 'scale_x', 'scale_y',
    'uniform_scale', 'alpha', 'saturation', 'contrast', 'brightness', 'volume']))
    .min(1, 'At least one keyframe is required')
    .describe('Property of each keyframe (one entry per keyframe)'),
  times: z.array(z.number().min(0))
    .min(1)
    .describe('Time of each keyframe in seconds on the timeline (same length as property_types)'),
  values: z.array(z.string())
    .min(1)
    .describe('Value of each keyframe (same length as property_types), see tool description for formats'),
  response_format: ResponseFormatSchema
}).strict();

// Effect schema
export const AddEffectSchema = z.object({
  draft_id: z.string()
    .min(1, 'Draft ID is required')
    .describe('The ID of the draft to add effect to'),
  effect_type: z.string()
    .min(1)
    .describe('Exact CapCut effect name from capcut_list_types category="scene_effect" or "character_effect"'),
  effect_category: z.enum(['scene', 'character'])
    .default('scene')
    .describe('"scene" = whole-frame effects, "character" = effects that follow a person'),
  start: z.number()
    .min(0, 'Start time must be non-negative')
    .describe('Start time in seconds'),
  end: z.number()
    .positive('End time must be positive')
    .describe('End time in seconds'),
  params: z.array(z.number().min(0).max(100).nullable())
    .optional()
    .describe('Effect parameters, each 0-100, in the effect\'s own order (e.g. Blur: [strength]); null keeps the default'),
  response_format: ResponseFormatSchema
}).strict();

// List types schema
export const LIST_TYPE_ENDPOINTS = {
  transition: '/get_transition_types',
  intro: '/get_intro_animation_types',
  outro: '/get_outro_animation_types',
  combo: '/get_combo_animation_types',
  text_intro: '/get_text_intro_types',
  text_outro: '/get_text_outro_types',
  scene_effect: '/get_video_scene_effect_types',
  character_effect: '/get_video_character_effect_types',
  mask: '/get_mask_types',
  audio_effect: '/get_audio_effect_types',
  font: '/get_font_types'
} as const;

export const ListTypesSchema = z.object({
  category: z.enum(Object.keys(LIST_TYPE_ENDPOINTS) as [keyof typeof LIST_TYPE_ENDPOINTS, ...(keyof typeof LIST_TYPE_ENDPOINTS)[]])
    .describe('Which CapCut catalog to list'),
  search: z.string()
    .optional()
    .describe('Case-insensitive substring filter on the name (e.g. "zoom", "fade")'),
  response_format: ResponseFormatSchema
}).strict();

// Existing projects
export const ListProjectsSchema = z.object({
  search: z.string()
    .optional()
    .describe('Case-insensitive substring filter on the project name'),
  response_format: ResponseFormatSchema
}).strict();

export const OpenProjectSchema = z.object({
  project_name: z.string()
    .min(1, 'Project name is required')
    .describe('Exact name of a project in CapCut (see capcut_list_projects)'),
  response_format: ResponseFormatSchema
}).strict();

// Sticker schema
export const AddStickerSchema = z.object({
  draft_id: z.string()
    .min(1, 'Draft ID is required')
    .describe('The ID of the draft to add sticker to'),
  sticker_id: z.string()
    .min(1)
    .describe('CapCut sticker resource ID (from CapCut\'s sticker library). For a custom sticker use capcut_add_image with a PNG instead'),
  start: z.number()
    .min(0, 'Start time must be non-negative')
    .describe('Start time in seconds'),
  end: z.number()
    .positive('End time must be positive')
    .describe('End time in seconds'),
  position_x: z.number()
    .min(0)
    .max(1)
    .default(0.5)
    .describe('Horizontal position (0.0 to 1.0)'),
  position_y: z.number()
    .min(0)
    .max(1)
    .default(0.5)
    .describe('Vertical position (0.0 to 1.0)'),
  scale: z.number()
    .min(0.1)
    .max(5)
    .default(1.0)
    .describe('Scale multiplier'),
  rotation: z.number()
    .min(0)
    .max(360)
    .default(0)
    .describe('Rotation angle in degrees'),
  response_format: ResponseFormatSchema
}).strict();

// Save draft schema
export const SaveDraftSchema = z.object({
  draft_id: z.string()
    .min(1, 'Draft ID is required')
    .describe('The ID of the draft to save'),
  project_name: z.string()
    .optional()
    .describe('Name shown in the CapCut projects list (defaults to the draft ID); cannot start with "." or contain / \\ :'),
  overwrite: z.boolean()
    .default(false)
    .describe('Replace an existing project that was not saved from this draft (it is moved to backups)'),
  response_format: ResponseFormatSchema
}).strict();

// Get duration schema
export const GetDurationSchema = z.object({
  url: UrlSchema.describe('URL to the media file to analyze'),
  response_format: ResponseFormatSchema
}).strict();

// Export type inference helpers
export type CreateDraftInput = z.infer<typeof CreateDraftSchema>;
export type AddVideoInput = z.infer<typeof AddVideoSchema>;
export type AddAudioInput = z.infer<typeof AddAudioSchema>;
export type AddTextInput = z.infer<typeof AddTextSchema>;
export type AddImageInput = z.infer<typeof AddImageSchema>;
export type AddSubtitleInput = z.infer<typeof AddSubtitleSchema>;
export type AddKeyframeInput = z.infer<typeof AddKeyframeSchema>;
export type AddEffectInput = z.infer<typeof AddEffectSchema>;
export type AddStickerInput = z.infer<typeof AddStickerSchema>;
export type SaveDraftInput = z.infer<typeof SaveDraftSchema>;
export type GetDurationInput = z.infer<typeof GetDurationSchema>;
export type ListTypesInput = z.infer<typeof ListTypesSchema>;
export type ListProjectsInput = z.infer<typeof ListProjectsSchema>;
export type OpenProjectInput = z.infer<typeof OpenProjectSchema>;
