// Shared constants for CapCut MCP Server

export const API_BASE_URL = process.env.CAPCUT_API_URL || 'http://localhost:9001';
export const CHARACTER_LIMIT = 15000;
export const DEFAULT_FPS = 30;
export const DEFAULT_VIDEO_RESOLUTION = {
  width: 1920,
  height: 1080
};

export const SUPPORTED_VIDEO_FORMATS = [
  'mp4', 'mov', 'avi', 'mkv', 'webm', 'flv'
];

export const SUPPORTED_AUDIO_FORMATS = [
  'mp3', 'wav', 'aac', 'm4a', 'flac', 'ogg'
];

export const SUPPORTED_IMAGE_FORMATS = [
  'jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp'
];
