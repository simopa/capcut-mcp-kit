// API client for CapCut server communication

import axios, { AxiosError, AxiosInstance, AxiosRequestConfig } from 'axios';
import { API_BASE_URL } from '../constants.js';
import type { ApiResponse } from '../types.js';
import { ensureBackend } from './backend.js';

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

    // Add response interceptor for error handling
    this.client.interceptors.response.use(
      (response) => response,
      (error: AxiosError) => {
        if (error.response) {
          // Server responded with error status
          const status = error.response.status;
          const data = error.response.data as any;
          
          if (status === 404) {
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
          // Request made but no response
          throw new Error('CapCut API server is not responding. Please ensure the server is running.');
        } else {
          // Error setting up request
          throw new Error(`Request error: ${error.message}`);
        }
      }
    );
  }

  async request<T>(
    endpoint: string,
    method: 'GET' | 'POST' | 'PUT' | 'DELETE' = 'POST',
    data?: any,
    params?: Record<string, any>
  ): Promise<ApiResponse<T>> {
    try {
      await ensureBackend();
      const config: AxiosRequestConfig = {
        method,
        url: endpoint,
        ...(data && { data }),
        ...(params && { params }),
      };

      const response = await this.client.request<ApiResponse<T> & { output?: T }>(config);
      // VectCutAPI returns its payload in `output`; the tools read `result`
      const { output, ...rest } = response.data;
      return { ...rest, result: rest.result ?? output };
    } catch (error) {
      if (error instanceof Error) {
        return {
          success: false,
          error: error.message
        };
      }
      return {
        success: false,
        error: 'Unknown error occurred'
      };
    }
  }

  // Specific API methods
  async createDraft(config: { width: number; height: number; fps?: number }) {
    return this.request('/create_draft', 'POST', config);
  }

  // MCP positions are 0..1 from the top-left; VectCutAPI uses CapCut's transform,
  // measured in half-canvas units from the center with +y pointing up.
  private mapPlacement(data: any) {
    const { position_x, position_y, scale, ...rest } = data;
    if (position_x !== undefined) rest.transform_x = (position_x - 0.5) * 2;
    if (position_y !== undefined) rest.transform_y = (0.5 - position_y) * 2;
    if (scale !== undefined) {
      rest.scale_x = scale;
      rest.scale_y = scale;
    }
    return rest;
  }

  async addVideo(data: any) {
    return this.request('/add_video', 'POST', data);
  }

  async addAudio(data: any) {
    return this.request('/add_audio', 'POST', data);
  }

  async addText(data: any) {
    const payload = this.mapPlacement(data);
    // Without a background color the backend would still draw its default black box
    if (!payload.background_color) delete payload.background_alpha;
    return this.request('/add_text', 'POST', payload);
  }

  async addImage(data: any) {
    return this.request('/add_image', 'POST', this.mapPlacement(data));
  }

  async addSubtitle(data: any) {
    const { srt_content, background_enabled, ...rest } = data;
    return this.request('/add_subtitle', 'POST', {
      ...this.mapPlacement(rest),
      srt: srt_content,
      background_alpha: background_enabled ? (rest.background_alpha ?? 0.7) : 0
    });
  }

  async addKeyframe(data: any) {
    return this.request('/add_video_keyframe', 'POST', data);
  }

  async addEffect(data: any) {
    return this.request('/add_effect', 'POST', data);
  }

  async addSticker(data: any) {
    return this.request('/add_sticker', 'POST', this.mapPlacement(data));
  }

  async saveDraft(draftId: string, projectName?: string, overwrite?: boolean) {
    return this.request('/save_draft', 'POST', { draft_id: draftId, project_name: projectName, overwrite });
  }

  async listTypes(endpoint: string) {
    return this.request<Array<{ name: string }>>(endpoint, 'GET');
  }

  async getDuration(url: string) {
    return this.request('/get_duration', 'POST', { url });
  }
}

// Singleton instance
export const apiClient = new CapCutApiClient();
