// Type definitions for CapCut API (capcut-mcp-kit: the backend's replies are described in contracts.ts)

/** A backend call: the checked result, or why it failed. */
export type ApiResponse<T> =
  | { success: true; result: T; error?: undefined }
  | { success: false; result?: undefined; error: string };

export enum ResponseFormat {
  MARKDOWN = 'markdown',
  JSON = 'json'
}
