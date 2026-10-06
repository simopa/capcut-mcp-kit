// API client for CapCut server communication. Modified in capcut-mcp-kit: typed requests, and every
// reply is checked against its contract (contracts.ts).

import axios, { AxiosError, AxiosInstance, AxiosRequestConfig } from 'axios';
import { API_BASE_URL } from '../constants.js';
import { z } from 'zod';
import type { ApiResponse } from '../types.js';
import * as C from '../contracts.js';
import type {
  AddAudioInput, AddEffectInput, AddImageInput, AddKeyframeInput, AddStickerInput, AddSubtitleInput,
  AddTextInput, AddVideoInput, CreateDraftInput
} from '../schemas/index.js';
import { ensureBackend, forgetBackend } from './backend.js';
import { readToken, TOKEN_HEADER } from './auth.js';

/** What every VectCutAPI route answers. */
interface BackendReply {
  success: boolean;
  output?: unknown;
  error?: string;
}

/** Tool input without the MCP-only response_format. */
type Payload<T> = Omit<T, 'response_format'>;

type Placement = { position_x?: number; position_y?: number; scale?: number };
type Mapped<T> = Omit<T, keyof Placement> &
  { transform_x?: number; transform_y?: number; scale_x?: number; scale_y?: number };

export class CapCutApiClient {
  private client: AxiosInstance;

  constructor(baseURL: string = API_BASE_URL) {
    this.client = axios.create({
      baseURL,
      timeout: 60000, // 60 seconds timeout
      headers: {
        'Content-Type': 'application/json',
      },
    });

    // Every request carries the backend's token (read each time: the backend creates it on first start)
    this.client.interceptors.request.use((config) => {
      const token = readToken();
      if (token) config.headers.set(TOKEN_HEADER, token);
      return config;
    });

    // Add response interceptor for error handling
    this.client.interceptors.response.use(
      (response) => response,
      (error: AxiosError) => {
        if (error.response) {
          // Server responded with error status
          const status = error.response.status;
          const data = error.response.data as BackendReply | undefined;
          
          if (status === 401) {
            throw new Error(`The backend refused the request: ${data?.error || 'missing or wrong token'}`);
          } else if (status === 404) {
            throw new Error(`Resource not found: ${error.config?.url}`);
          } else if (status === 400) {
            throw new Error(`Bad request: ${data?.error || error.message}`);
          } else if (status === 500) {
            throw new Error(`Server error: ${data?.error || 'Internal server error'}`);
          } else if (status === 429) {
            throw new Error('Rate limit exceeded. Please try again later.');
          }
          
          throw new Error(data?.error || `API error (${status})`);
        } else if (error.request) {
          // Request made but no response: check (and restart) the backend on the next call
          forgetBackend();
          throw new Error('CapCut API server is not responding. Please ensure the server is running.');
        } else {
          // Error setting up request
          throw new Error(`Request error: ${error.message}`);
        }
      }
    );
  }

  /** Calls the backend and checks its reply against `schema` (see contracts.ts). */
  async request<S extends z.ZodTypeAny>(
    endpoint: string,
    method: 'GET' | 'POST',
    data: object | undefined,
    schema: S
  ): Promise<ApiResponse<z.infer<S>>> {
    try {
      await ensureBackend();
      const config: AxiosRequestConfig = { method, url: endpoint, ...(data && { data }) };
      const response = await this.client.request<BackendReply>(config);
      // VectCutAPI returns its payload in `output`
      const { success, output, error } = response.data;
      if (!success) return { success: false, error: error || `The backend reported a failure for ${endpoint}` };
      const parsed = schema.safeParse(output);
      if (!parsed.success) {
        const issues = parsed.error.issues.slice(0, 3)
          .map((i: z.ZodIssue) => `${i.path.join('.') || '(reply)'}: ${i.message}`).join('; ');
        return { success: false, error: `Unexpected reply from the backend for ${endpoint} (${issues})` };
      }
      return { success: true, result: parsed.data };
    } catch (error) {
      return { success: false, error: error instanceof Error ? error.message : 'Unknown error occurred' };
    }
  }

  async createDraft(config: Payload<CreateDraftInput>) {
    return this.request('/create_draft', 'POST', config, C.CreateDraftResult);
  }

  // MCP positions are 0..1 from the top-left; VectCutAPI uses CapCut's transform,
  // measured in half-canvas units from the center with +y pointing up.
  private mapPlacement<T extends Placement>(data: T): Mapped<T> {
    const { position_x, position_y, scale, ...rest } = data;
    const out: Mapped<T> = { ...rest };
    if (position_x !== undefined) out.transform_x = (position_x - 0.5) * 2;
    if (position_y !== undefined) out.transform_y = (0.5 - position_y) * 2;
    if (scale !== undefined) {
      out.scale_x = scale;
      out.scale_y = scale;
    }
    return out;
  }

  async addVideo(data: Payload<AddVideoInput>) {
    return this.request('/add_video', 'POST', data, C.DraftRefResult);
  }

  async addAudio(data: Payload<AddAudioInput>) {
    return this.request('/add_audio', 'POST', data, C.DraftRefResult);
  }

  async addText(data: Payload<AddTextInput>) {
    const payload = this.mapPlacement(data);
    // Without a background color the backend would still draw its default black box
    const { background_alpha: _unused, ...withoutBackground } = payload;
    return this.request('/add_text', 'POST', payload.background_color ? payload : withoutBackground,
      C.DraftRefResult);
  }

  async addImage(data: Payload<AddImageInput>) {
    return this.request('/add_image', 'POST', this.mapPlacement(data), C.DraftRefResult);
  }

  async addSubtitle(data: Payload<AddSubtitleInput>) {
    const { srt_content, background_enabled, ...rest } = data;
    return this.request('/add_subtitle', 'POST', {
      ...this.mapPlacement(rest),
      srt: srt_content,
      background_alpha: background_enabled ? 0.7 : 0
    }, C.DraftRefResult);
  }

  async addKeyframe(data: Payload<AddKeyframeInput>) {
    return this.request('/add_video_keyframe', 'POST', data, C.DraftRefResult);
  }

  async addEffect(data: Payload<AddEffectInput>) {
    return this.request('/add_effect', 'POST', data, C.DraftRefResult);
  }

  async addSticker(data: Payload<AddStickerInput>) {
    return this.request('/add_sticker', 'POST', this.mapPlacement(data), C.DraftRefResult);
  }

  async saveDraft(draftId: string, projectName?: string, overwrite?: boolean) {
    return this.request('/save_draft', 'POST', { draft_id: draftId, project_name: projectName, overwrite },
      C.SaveDraftResult);
  }

  async listProjects() {
    return this.request('/list_projects', 'GET', undefined, C.ListProjectsResult);
  }

  async openProject(projectName: string) {
    return this.request('/open_project', 'POST', { project_name: projectName }, C.OpenProjectResult);
  }

  async listTypes(endpoint: string) {
    return this.request(endpoint, 'GET', undefined, C.ListTypesResult);
  }

  async getDuration(url: string) {
    return this.request('/get_duration', 'POST', { url }, C.DurationResult);
  }
}

// Singleton instance
export const apiClient = new CapCutApiClient();
