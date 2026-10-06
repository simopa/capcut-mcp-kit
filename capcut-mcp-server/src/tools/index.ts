// Tool registration and implementation for CapCut MCP server

import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { apiClient } from '../services/api-client.js';
import { ResponseFormat } from '../types.js';
import type { CreatedDraft, DraftRef, MediaDuration, SavedDraft } from '../contracts.js';
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
  ListProjectsSchema,
  OpenProjectSchema,
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
  type ListTypesInput,
  type ListProjectsInput,
  type OpenProjectInput
} from '../schemas/index.js';

type ToolReply = {
  content: Array<{ type: "text"; text: string }>;
  structuredContent?: Record<string, unknown>;
};

/** JSON replies return the backend's result as is; markdown replies use the tool's own summary. */
function formatResponse<T extends object>(data: T, format: ResponseFormat, markdown: (d: T) => string): ToolReply {
  return {
    content: [{ type: "text" as const, text: format === ResponseFormat.JSON ? JSON.stringify(data, null, 2) : markdown(data) }],
    structuredContent: data as Record<string, unknown>
  };
}

const createdMarkdown = (d: CreatedDraft) =>
  `## Draft Created\n\n- **Draft ID**: \`${d.draft_id}\`\n- **Dimensions**: ${d.width}x${d.height}\n- **FPS**: ${d.fps}\n`;

const addedMarkdown = (what: string) => (d: DraftRef) => {
  let md = `Added ${what} to draft \`${d.draft_id}\`.\n`;
  for (const m of d.moved_to_free_track ?? []) {
    md += `- Track "${m.requested_track}" was busy at ${m.start}s: placed on "${m.track}" instead.\n`;
  }
  return md;
};

const savedMarkdown = (d: SavedDraft) => {
  let md = `## Draft Saved\n\nDraft saved successfully at:\n\`${d.draft_url}\`\n\n`;
  if (d.backups.length) {
    md += `The previous version was moved to:\n${d.backups.map((b) => `\`${b}\``).join('\n')}\n\n`;
  }
  md += d.added_tracks !== undefined
    ? `Added ${d.added_tracks} track(s) to the existing project; open it in CapCut to see them.\n`
    : `The project is already in CapCut's projects folder: restart CapCut to see it in the list.\n`;
  if (d.warnings?.length) {
    md += `\n**Warnings**:\n${d.warnings.map((w) => `- ${w}`).join('\n')}\n`;
  }
  return md;
};

const durationMarkdown = (d: MediaDuration) =>
  `## Media Duration\n\n- **Duration**: ${d.duration.toFixed(2)}s\n- **Format**: ${d.format}\n`;

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

        return formatResponse(response.result, params.response_format, createdMarkdown);
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

        return formatResponse(response.result, params.response_format, addedMarkdown('the video'));
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

        return formatResponse(response.result, params.response_format, addedMarkdown('the audio'));
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

        return formatResponse(response.result, params.response_format, addedMarkdown('the text'));
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

        return formatResponse(response.result, params.response_format, addedMarkdown('the image'));
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

        return formatResponse(response.result, params.response_format, addedMarkdown('the subtitles'));
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

        return formatResponse(response.result, params.response_format, addedMarkdown('the keyframes'));
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

        return formatResponse(response.result, params.response_format, addedMarkdown('the effect'));
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

        return formatResponse(response.result, params.response_format, addedMarkdown('the sticker'));
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

For a draft from capcut_open_project, leave project_name out: the additions are written into that
project, existing clips untouched.

Saving again under the same project_name replaces the project this draft saved before; the old folder is
moved to ~/Movies/CapCut MCP Backups, never deleted. Replacing a project is refused while CapCut is open
(quit it first), and a project not saved from this draft is replaced only with overwrite=true.`,
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
        const response = await apiClient.saveDraft(params.draft_id, params.project_name, params.overwrite, params.expected_revision);

        if (!response.success || !response.result) {
          throw new Error(response.error || 'Failed to save draft');
        }

        return formatResponse(response.result, params.response_format, savedMarkdown);
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

        return formatResponse(response.result, params.response_format, durationMarkdown);
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
those tools only accept names exactly as returned here.`,
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

  // Tool 13: List existing CapCut projects
  server.registerTool(
    'capcut_list_projects',
    {
      title: 'List CapCut Projects',
      description: `List the projects in CapCut's projects folder, most recently modified first.`,
      inputSchema: ListProjectsSchema,
      annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false }
    },
    async (params: ListProjectsInput) => {
      try {
        const response = await apiClient.listProjects();
        if (!response.success || !response.result) {
          throw new Error(response.error || 'Failed to list projects');
        }
        const needle = params.search?.toLowerCase();
        const projects = (response.result.projects || [])
          .filter((p) => p.is_capcut_project && (!needle || p.name.toLowerCase().includes(needle)))
          .map((p) => ({ name: p.name, modified: p.modified_time }));
        if (params.response_format === ResponseFormat.JSON) {
          return { content: [{ type: "text" as const, text: JSON.stringify(projects) }], structuredContent: { projects } };
        }
        const lines = projects.map((p) => `- ${p.name} (modified ${p.modified.slice(0, 16).replace('T', ' ')})`);
        return { content: [{ type: "text" as const, text: `## CapCut projects (${projects.length})\n\n${lines.join('\n') || '_none_'}` }] };
      } catch (error) {
        return handleError(error);
      }
    }
  );

  // Tool 14: Open an existing project to add to it
  server.registerTool(
    'capcut_open_project',
    {
      title: 'Open Existing CapCut Project',
      description: `Open a project made in CapCut (or saved earlier) to add to it, and get a draft_id for it.

Use the draft_id with the other capcut_add_* tools, then capcut_save_draft (without project_name).
Additions go on new tracks above the existing ones; clips already in the project are never changed,
moved or removed, and the existing tracks cannot be edited through this draft. The result lists the
existing tracks with their end times, to place new items after or over them.

Saving refuses if CapCut is open (quit it first) or if the project was changed in CapCut after it was
opened (open it again). Before writing, the whole project folder is copied to ~/Movies/CapCut MCP Backups.`,
      inputSchema: OpenProjectSchema,
      annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: false, openWorldHint: false }
    },
    async (params: OpenProjectInput) => {
      try {
        const response = await apiClient.openProject(params.project_name);
        if (!response.success || !response.result) {
          throw new Error(response.error || 'Failed to open project');
        }
        const p = response.result;
        if (params.response_format === ResponseFormat.JSON) {
          return { content: [{ type: "text" as const, text: JSON.stringify(p, null, 2) }], structuredContent: p };
        }
        const tracks = (p.tracks as Array<{ type: string; name: string; clips: number; end: number }>)
          .map((t) => `- ${t.type}${t.name ? ` "${t.name}"` : ''}: ${t.clips} clip(s), ends at ${t.end}s`);
        return {
          content: [{
            type: "text" as const,
            text: `## Project opened: ${p.project_name}\n\n- **Draft ID**: \`${p.draft_id}\`\n- **Size**: ${p.width}x${p.height}, ${p.fps} fps\n` +
              `- **Duration**: ${p.duration}s\n\n### Existing tracks\n${tracks.join('\n') || '_none_'}\n\n${p.note}`
          }],
          structuredContent: p
        };
      } catch (error) {
        return handleError(error);
      }
    }
  );
}
