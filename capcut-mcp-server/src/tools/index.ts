// Tool registration and implementation for CapCut MCP server

import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { apiClient } from '../services/api-client.js';
import { ResponseFormat } from '../types.js';
import {
  CreateDraftSchema,
  AddVideoSchema,
  AddAudioSchema,
  AddTextSchema,
  AddImageSchema,
  AddSubtitleSchema,
  AddKeyframeSchema,
  AddEffectSchema,
  AddStickerSchema,
  SaveDraftSchema,
  GetDurationSchema,
  ListTypesSchema,
  LIST_TYPE_ENDPOINTS,
  type CreateDraftInput,
  type AddVideoInput,
  type AddAudioInput,
  type AddTextInput,
  type AddImageInput,
  type AddSubtitleInput,
  type AddKeyframeInput,
  type AddEffectInput,
  type AddStickerInput,
  type SaveDraftInput,
  type GetDurationInput,
  type ListTypesInput
} from '../schemas/index.js';

// Utility function to format responses
function formatResponse(data: any, format: ResponseFormat): {
  content: Array<{ type: "text"; text: string }>;
  structuredContent?: any;
} {
  if (format === ResponseFormat.JSON) {
    return {
      content: [{ type: "text" as const, text: JSON.stringify(data, null, 2) }],
      structuredContent: data
    };
  } else {
    // Markdown format
    let markdown = '';
    if (data.draft_id) {
      markdown += `## Draft Created\n\n`;
      markdown += `- **Draft ID**: \`${data.draft_id}\`\n`;
      markdown += `- **Dimensions**: ${data.width}x${data.height}\n`;
      markdown += `- **FPS**: ${data.fps}\n`;
    } else if (data.duration !== undefined) {
      markdown += `## Media Duration\n\n`;
      markdown += `- **Duration**: ${data.duration.toFixed(2)}s\n`;
      if (data.format) markdown += `- **Format**: ${data.format}\n`;
      if (data.width) markdown += `- **Resolution**: ${data.width}x${data.height}\n`;
    } else if (data.draft_url) {
      markdown += `## Draft Saved\n\n`;
      markdown += `Draft saved successfully at:\n\`${data.draft_url}\`\n\n`;
      if (data.backups?.length) {
        markdown += `The previous version was moved to:\n${data.backups.map((b: string) => `\`${b}\``).join('\n')}\n\n`;
      }
      markdown += `The project is already in CapCut's projects folder: restart CapCut to see it in the list.\n`;
    } else {
      markdown += `## Operation Successful\n\n`;
      markdown += JSON.stringify(data, null, 2);
    }
    return {
      content: [{ type: "text" as const, text: markdown }],
      structuredContent: data
    };
  }
}

function handleError(error: unknown): {
  content: Array<{ type: "text"; text: string }>;
  isError: true;
} {
  const message = error instanceof Error ? error.message : 'Unknown error occurred';
  return {
    isError: true,
    content: [{
      type: "text" as const,
      text: `Error: ${message}\n\nPlease check that:\n- The CapCut API server is running\n- All required parameters are valid\n- Media URLs are accessible`
    }]
  };
}

export function registerTools(server: McpServer): void {
  // Tool 1: Create Draft
  server.registerTool(
    'capcut_create_draft',
    {
      title: 'Create CapCut Draft',
      description: `Create a new video editing draft with specified dimensions and frame rate.

This tool initializes a new draft project that can be edited by adding videos, audio, text, images, and effects.

Args:
  - width (number): Video width in pixels (360-4096, default: 1920)
  - height (number): Video height in pixels (360-4096, default: 1080)
  - fps (number): Frames per second (24-120, default: 30)
  - response_format ('markdown' | 'json'): Output format (default: 'markdown')

Returns:
  {
    "draft_id": string,      // Unique draft identifier for subsequent operations
    "width": number,         // Video width
    "height": number,        // Video height
    "fps": number,           // Frame rate
    "duration": number,      // Current duration (starts at 0)
    "created_at": string     // ISO timestamp
  }

Examples:
  - Create HD draft: params with width=1920, height=1080
  - Create vertical video: params with width=1080, height=1920
  - Create 4K draft: params with width=3840, height=2160`,
      inputSchema: CreateDraftSchema,
      annotations: {
        readOnlyHint: false,
        destructiveHint: false,
        idempotentHint: false,
        openWorldHint: false
      }
    },
    async (params: CreateDraftInput) => {
      try {
        const response = await apiClient.createDraft({
          width: params.width,
          height: params.height,
          fps: params.fps
        });

        if (!response.success || !response.result) {
          throw new Error(response.error || 'Failed to create draft');
        }

        return formatResponse(response.result, params.response_format);
      } catch (error) {
        return handleError(error);
      }
    }
  );

  // Tool 2: Add Video
  server.registerTool(
    'capcut_add_video',
    {
      title: 'Add Video to Draft',
      description: `Add a video clip to an existing draft with timing, volume, and effects.

This tool adds video content to the timeline with support for transitions, speed adjustments, and volume control.

Args:
  - draft_id (string): The draft ID from create_draft
  - video_url (string): URL to video file (mp4, mov, avi, mkv, webm, flv)
  - start (number): Trim start within the source file, in seconds (>= 0)
  - end (number): Trim end within the source file, in seconds (> 0)
  - target_start (number): Where the clip starts on the timeline, in seconds (default: 0).
    To put clips one after another, set target_start to the end of the previous clip.
  - volume (number): Audio volume 0.0-1.0 (default: 1.0)
  - transition (string): Optional CapCut transition from THIS clip into the NEXT clip on the same
    track, so set it on the earlier clip. Exact name from capcut_list_types category="transition".
  - transition_duration (number): Transition length in seconds (default: 0.5)
  - speed (number): Playback speed 0.1-10x (default: 1.0)
  - track_name (string): Optional track; items on one track can't overlap in time, so use a new
    track name to layer simultaneous items (two texts at once, picture-in-picture, music + voice)
  - response_format ('markdown' | 'json'): Output format

Examples:
  - Add background video: draft_id="abc123", video_url="https://...", start=0, end=10
  - Add with slow motion: speed=0.5
  - Two clips with a dissolve between them: first clip (start=0, end=10, transition="Dissolve"),
    then second clip with target_start=10`,
      inputSchema: AddVideoSchema,
      annotations: {
        readOnlyHint: false,
        destructiveHint: false,
        idempotentHint: false,
        openWorldHint: true
      }
    },
    async (params: AddVideoInput) => {
      try {
        const response = await apiClient.addVideo(params);

        if (!response.success || !response.result) {
          throw new Error(response.error || 'Failed to add video');
        }

        return formatResponse(response.result, params.response_format);
      } catch (error) {
        return handleError(error);
      }
    }
  );

  // Tool 3: Add Audio
  server.registerTool(
    'capcut_add_audio',
    {
      title: 'Add Audio to Draft',
      description: `Add audio track to draft with volume and fade effects.

This tool adds background music or sound effects to the video timeline.

Args:
  - draft_id (string): The draft ID
  - audio_url (string): URL or absolute local path to audio file (mp3, wav, aac, m4a, flac, ogg)
  - start (number): Trim start within the source file, in seconds
  - end (number): Trim end within the source file, in seconds
  - target_start (number): Where the audio starts on the timeline, in seconds (default: 0)
  - volume (number): Audio volume 0.0-1.0 (default: 1.0)
  - fade_in (number): Fade in duration in seconds (default: 0)
  - fade_out (number): Fade out duration in seconds (default: 0)
  - track_name (string): Optional track; items on one track can't overlap in time, so use a new
    track name to layer simultaneous items (two texts at once, picture-in-picture, music + voice)
  - response_format ('markdown' | 'json'): Output format

Examples:
  - Add background music: audio_url="https://...", volume=0.5
  - Add with fade: fade_in=2, fade_out=2`,
      inputSchema: AddAudioSchema,
      annotations: {
        readOnlyHint: false,
        destructiveHint: false,
        idempotentHint: false,
        openWorldHint: true
      }
    },
    async (params: AddAudioInput) => {
      try {
        const response = await apiClient.addAudio(params);

        if (!response.success || !response.result) {
          throw new Error(response.error || 'Failed to add audio');
        }

        return formatResponse(response.result, params.response_format);
      } catch (error) {
        return handleError(error);
      }
    }
  );

  // Tool 4: Add Text
  server.registerTool(
    'capcut_add_text',
    {
      title: 'Add Text to Draft',
      description: `Add styled text overlay to video with positioning, colors, shadows, and animations.

This tool creates text elements with full styling control including fonts, colors, backgrounds, shadows, and animations.

Args:
  - draft_id (string): The draft ID
  - text (string): Text content to display (1-500 characters)
  - start (number): Start time in seconds
  - end (number): End time in seconds
  - font (string): Font family name (optional)
  - font_size (number): CapCut scale 1-100, ~5 small, 8 normal, 12-15 big title (default: 8)
  - font_color (string): Hex color e.g., #FFFFFF (default: #FFFFFF)
  - background_color (string): Background hex color (optional)
  - background_alpha (number): Background opacity 0.0-1.0 (default: 0.8, only with background_color)
  - shadow_enabled (boolean): Enable shadow (default: false)
  - shadow_color (string): Shadow hex color (default: #000000)
  - position_x (number): Horizontal position 0.0 (left edge) to 1.0 (right edge), 0.5 = center
  - position_y (number): Vertical position 0.0 (top) to 1.0 (bottom), 0.5 = center
  - intro_animation (string): Entrance animation, exact name from capcut_list_types category="text_intro"
  - intro_duration (number): Entrance duration in seconds (default: 0.5)
  - outro_animation (string): Exit animation, exact name from capcut_list_types category="text_outro"
  - outro_duration (number): Exit duration in seconds (default: 0.5)
  - track_name (string): Optional track; items on one track can't overlap in time, so use a new
    track name to layer simultaneous items (two texts at once, picture-in-picture, music + voice)
  - response_format ('markdown' | 'json'): Output format

Examples:
  - Add title: text="Welcome", font_size=14, position_y=0.2, intro_animation="<name from text_intro>"
  - Add subtitle: text="Subscribe!", font_size=8, background_color="#000000"`,
      inputSchema: AddTextSchema,
      annotations: {
        readOnlyHint: false,
        destructiveHint: false,
        idempotentHint: false,
        openWorldHint: false
      }
    },
    async (params: AddTextInput) => {
      try {
        const response = await apiClient.addText(params);

        if (!response.success || !response.result) {
          throw new Error(response.error || 'Failed to add text');
        }

        return formatResponse(response.result, params.response_format);
      } catch (error) {
        return handleError(error);
      }
    }
  );

  // Tool 5: Add Image
  server.registerTool(
    'capcut_add_image',
    {
      title: 'Add Image to Draft',
      description: `Add image overlay to video with positioning, scaling, rotation, and animation.

This tool adds static or animated images to the video timeline.

Args:
  - draft_id (string): The draft ID
  - image_url (string): URL to image file (jpg, jpeg, png, gif, webp, bmp)
  - start (number): Start time in seconds
  - end (number): End time in seconds
  - position_x (number): Horizontal position 0.0 (left edge) to 1.0 (right edge), 0.5 = center
  - position_y (number): Vertical position 0.0 (top) to 1.0 (bottom), 0.5 = center
  - scale (number): Scale multiplier 0.1-5.0 (default: 1.0)
  - rotation (number): Clockwise rotation in degrees, -360 to 360 (default: 0)
  - intro_animation / outro_animation / combo_animation (string): exact names from
    capcut_list_types categories "intro", "outro", "combo" (each with a *_duration, default 0.5s)
  - transition (string): transition from this image into the next item on its track;
    exact name from capcut_list_types category="transition"
  - track_name (string): Optional track; items on one track can't overlap in time, so use a new
    track name to layer simultaneous items (two texts at once, picture-in-picture, music + voice)
  - response_format ('markdown' | 'json'): Output format

Examples:
  - Add logo: image_url="https://...", position_x=0.9, position_y=0.1, scale=0.3
  - Add rotating image: rotation=45, intro_animation="<name from intro>"`,
      inputSchema: AddImageSchema,
      annotations: {
        readOnlyHint: false,
        destructiveHint: false,
        idempotentHint: false,
        openWorldHint: true
      }
    },
    async (params: AddImageInput) => {
      try {
        const response = await apiClient.addImage(params);

        if (!response.success || !response.result) {
          throw new Error(response.error || 'Failed to add image');
        }

        return formatResponse(response.result, params.response_format);
      } catch (error) {
        return handleError(error);
      }
    }
  );

  // Tool 6: Add Subtitle
  server.registerTool(
    'capcut_add_subtitle',
    {
      title: 'Add Subtitles to Draft',
      description: `Add subtitles from SRT file content with styling options.

This tool imports subtitles in SRT format and applies styling.

Args:
  - draft_id (string): The draft ID
  - srt_content (string): SRT text, or an absolute path / URL to an .srt file
  - time_offset (number): Shift all subtitles by this many seconds (default: 0)
  - position_y (number): Vertical position 0.0 (top) to 1.0 (bottom), optional
  - font (string): Font family name (optional)
  - font_size (number): CapCut scale 1-100, ~5 normal subtitle (default: 5)
  - font_color (string): Hex color (default: #FFFFFF)
  - background_enabled (boolean): Enable background (default: true)
  - background_color (string): Background hex color (default: #000000)
  - response_format ('markdown' | 'json'): Output format

Example SRT format:
  1
  00:00:01,000 --> 00:00:03,000
  Welcome to my video

  2
  00:00:03,500 --> 00:00:05,000
  Subscribe for more content`,
      inputSchema: AddSubtitleSchema,
      annotations: {
        readOnlyHint: false,
        destructiveHint: false,
        idempotentHint: false,
        openWorldHint: false
      }
    },
    async (params: AddSubtitleInput) => {
      try {
        const response = await apiClient.addSubtitle(params);

        if (!response.success || !response.result) {
          throw new Error(response.error || 'Failed to add subtitle');
        }

        return formatResponse(response.result, params.response_format);
      } catch (error) {
        return handleError(error);
      }
    }
  );

  // Tool 7: Add Keyframe
  server.registerTool(
    'capcut_add_keyframe',
    {
      title: 'Add Keyframe Animation',
      description: `Add keyframe-based property animation to tracks.

CapCut interpolates between keyframes. Keyframes are given as three parallel lists of the same
length: keyframe i = (property_types[i], times[i], values[i]). Keyframes land on the clip of the
track that covers that time.

Args:
  - draft_id (string): The draft ID
  - track_name (string): "video_main" for video clips (default), "image_main" for images
  - property_types (string[]): Property of each keyframe
  - times (number[]): Timeline time of each keyframe, in seconds
  - values (string[]): Value of each keyframe; formats:
      position_x / position_y: "-1" to "1" from the center (half-canvas units)
      rotation: "45deg"   scale_x / scale_y / uniform_scale: "1.5"   alpha: "50%"
      saturation / contrast / brightness: "+0.3" / "-0.2" (relative, -1 to 1)   volume: "80%"
  - response_format ('markdown' | 'json'): Output format

Saturation/contrast/brightness keyframes are also the way to apply a constant color correction:
set the same value at the clip's start and end.

Examples:
  - Slow zoom in over 0-4s: property_types=["uniform_scale","uniform_scale"], times=[0,4], values=["1.0","1.3"]
  - Fade from transparent: property_types=["alpha","alpha"], times=[0,1], values=["0%","100%"]
  - Warmer punchier look 0-8s: property_types=["saturation","saturation","contrast","contrast"],
    times=[0,8,0,8], values=["+0.2","+0.2","+0.1","+0.1"]`,
      inputSchema: AddKeyframeSchema,
      annotations: {
        readOnlyHint: false,
        destructiveHint: false,
        idempotentHint: false,
        openWorldHint: false
      }
    },
    async (params: AddKeyframeInput) => {
      try {
        const response = await apiClient.addKeyframe(params);

        if (!response.success || !response.result) {
          throw new Error(response.error || 'Failed to add keyframe');
        }

        return formatResponse(response.result, params.response_format);
      } catch (error) {
        return handleError(error);
      }
    }
  );

  // Tool 8: Add Effect
  server.registerTool(
    'capcut_add_effect',
    {
      title: 'Add Visual Effect',
      description: `Apply visual effects to video segments.

This tool adds CapCut's own video effects (the Effects panel) on an effect track over the timeline.
For color correction (brightness/contrast/saturation) use capcut_add_keyframe instead.

Args:
  - draft_id (string): The draft ID
  - effect_type (string): Exact CapCut effect name. Look it up first with capcut_list_types
    category="scene_effect" (whole frame) or "character_effect" (follows a person).
  - effect_category ('scene' | 'character'): Must match the list the name came from (default: scene)
  - start (number): Start time in seconds
  - end (number): End time in seconds
  - params (number[]): Optional effect parameters, each 0-100, in the effect's own order; omit for defaults
  - response_format ('markdown' | 'json'): Output format

Examples:
  - Blur on 0-3s: effect_type="Blur", start=0, end=3, params=[70]
  - Find zoom effects first: capcut_list_types category="scene_effect", search="zoom"`,
      inputSchema: AddEffectSchema,
      annotations: {
        readOnlyHint: false,
        destructiveHint: false,
        idempotentHint: false,
        openWorldHint: false
      }
    },
    async (params: AddEffectInput) => {
      try {
        const response = await apiClient.addEffect(params);

        if (!response.success || !response.result) {
          throw new Error(response.error || 'Failed to add effect');
        }

        return formatResponse(response.result, params.response_format);
      } catch (error) {
        return handleError(error);
      }
    }
  );

  // Tool 9: Add Sticker
  server.registerTool(
    'capcut_add_sticker',
    {
      title: 'Add Sticker to Draft',
      description: `Add sticker/emoji overlay with positioning and transformation.

This tool adds decorative stickers or emojis to the video.

Args:
  - draft_id (string): The draft ID
  - sticker_id (string): CapCut sticker resource ID from CapCut's sticker library. There is no
    catalog to search here: for a custom sticker/emoji/logo use capcut_add_image with a PNG instead.
  - start (number): Start time in seconds
  - end (number): End time in seconds
  - position_x (number): Horizontal position 0.0 (left edge) to 1.0 (right edge), 0.5 = center
  - position_y (number): Vertical position 0.0 (top) to 1.0 (bottom), 0.5 = center
  - scale (number): Scale multiplier 0.1-5.0 (default: 1.0)
  - rotation (number): Clockwise rotation in degrees (default: 0)
  - response_format ('markdown' | 'json'): Output format

Examples:
  - Add corner sticker: position_x=0.9, position_y=0.1, scale=0.2
  - Add rotating emoji: rotation=15, scale=0.5`,
      inputSchema: AddStickerSchema,
      annotations: {
        readOnlyHint: false,
        destructiveHint: false,
        idempotentHint: false,
        openWorldHint: true
      }
    },
    async (params: AddStickerInput) => {
      try {
        const response = await apiClient.addSticker(params);

        if (!response.success || !response.result) {
          throw new Error(response.error || 'Failed to add sticker');
        }

        return formatResponse(response.result, params.response_format);
      } catch (error) {
        return handleError(error);
      }
    }
  );

  // Tool 10: Save Draft
  server.registerTool(
    'capcut_save_draft',
    {
      title: 'Save Draft',
      description: `Save the draft to a file that can be imported into CapCut.

This tool finalizes the draft and writes it directly into the local CapCut drafts directory
(macOS: ~/Movies/CapCut/User Data/Projects/com.lveditor.draft), copying local media alongside it.
CapCut only rescans its projects list on launch, so the user must restart CapCut to see the new project.

Saving again under the same project_name replaces the project this draft saved before; the old folder is
moved to ~/Movies/CapCut MCP Backups, never deleted. Replacing a project is refused while CapCut is open
(quit it first), and a project not saved from this draft is replaced only with overwrite=true.

Args:
  - draft_id (string): The draft ID to save
  - project_name (string, optional): Name shown in the CapCut projects list
  - overwrite (boolean, optional): Replace an existing project not saved from this draft
  - response_format ('markdown' | 'json'): Output format

Returns:
  {
    "draft_url": string,    // Path to the saved draft folder
    "backups": string[]     // Where replaced folders were moved
  }`,
      inputSchema: SaveDraftSchema,
      annotations: {
        readOnlyHint: false,
        destructiveHint: true,
        idempotentHint: false,
        openWorldHint: false
      }
    },
    async (params: SaveDraftInput) => {
      try {
        const response = await apiClient.saveDraft(params.draft_id, params.project_name, params.overwrite);

        if (!response.success || !response.result) {
          throw new Error(response.error || 'Failed to save draft');
        }

        return formatResponse(response.result, params.response_format);
      } catch (error) {
        return handleError(error);
      }
    }
  );

  // Tool 11: Get Media Duration
  server.registerTool(
    'capcut_get_duration',
    {
      title: 'Get Media Duration',
      description: `Get duration and metadata of video or audio file.

This tool analyzes media files to retrieve duration, format, and resolution information.

Args:
  - url (string): URL to media file
  - response_format ('markdown' | 'json'): Output format

Returns:
  {
    "duration": number,     // Duration in seconds
    "format": string,       // File format
    "width": number,        // Video width (if video)
    "height": number        // Video height (if video)
  }

Examples:
  - Check video length before adding: url="https://example.com/video.mp4"
  - Verify audio duration: url="https://example.com/music.mp3"`,
      inputSchema: GetDurationSchema,
      annotations: {
        readOnlyHint: true,
        destructiveHint: false,
        idempotentHint: true,
        openWorldHint: true
      }
    },
    async (params: GetDurationInput) => {
      try {
        const response = await apiClient.getDuration(params.url);

        if (!response.success || !response.result) {
          throw new Error(response.error || 'Failed to get duration');
        }

        return formatResponse(response.result, params.response_format);
      } catch (error) {
        return handleError(error);
      }
    }
  );

  // Tool 12: List CapCut catalog names
  server.registerTool(
    'capcut_list_types',
    {
      title: 'List CapCut Names',
      description: `List the exact names CapCut accepts for transitions, animations, effects, masks, audio effects and fonts.

Use this before capcut_add_video (transition), capcut_add_text (text_intro/text_outro),
capcut_add_image (intro/outro/combo/transition) and capcut_add_effect (scene_effect/character_effect):
those tools only accept names exactly as returned here.

Args:
  - category: transition | intro | outro | combo | text_intro | text_outro | scene_effect |
    character_effect | mask | audio_effect | font
  - search (string): Optional case-insensitive substring filter (catalogs have up to ~400 names)
  - response_format ('markdown' | 'json'): Output format`,
      inputSchema: ListTypesSchema,
      annotations: {
        readOnlyHint: true,
        destructiveHint: false,
        idempotentHint: true,
        openWorldHint: false
      }
    },
    async (params: ListTypesInput) => {
      try {
        const response = await apiClient.listTypes(LIST_TYPE_ENDPOINTS[params.category]);

        if (!response.success || !Array.isArray(response.result)) {
          throw new Error(response.error || 'Failed to list types');
        }

        const needle = params.search?.toLowerCase();
        const names = (response.result as Array<{ name: string }>)
          .map((item) => item.name)
          .filter((name) => !needle || name.toLowerCase().includes(needle));

        if (params.response_format === ResponseFormat.JSON) {
          return {
            content: [{ type: "text" as const, text: JSON.stringify(names) }],
            structuredContent: { category: params.category, names }
          };
        }
        return {
          content: [{
            type: "text" as const,
            text: `## ${params.category} (${names.length})\n\n${names.join(', ') || '_no matches_'}`
          }]
        };
      } catch (error) {
        return handleError(error);
      }
    }
  );
}
