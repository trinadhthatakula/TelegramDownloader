export type Scope = { chat_id: number; topic_id?: number | null };
export type Chat = { id: number; name: string; kind: string; archived: boolean; forum: boolean; topics: { id: number; title: string }[]; scan: { status: string; scanned: number; error: string; updated: number } | null };
export type QueueItem = { id: string; chat_id: number; chat_name: string; file_name: string; file_size: number | null; status: string; progress: number; downloaded_bytes: number; speed: number; last_error: string | null; output_path: string | null; topic_id: number | null; topic_name: string | null };
export type Preview = { id: string; counts: Record<string, number>; total: number; bytes: number; unknown_sizes: number; output_dir: string; sample: { chat: string; topic: string | null; file: string }[] };
export type Operation = { id: string; label: string; status: string; message: string; result: unknown };
export type State = {
  authorized: boolean; account: string | null; profile_id: string | null;
  login: { stage: string; message: string; retry_after?: number };
  profile: { name: string; detail: string; photo: string | null } | null;
  profiles: { id: string; label: string; api_id: number }[];
  chats: Chat[]; config: { output_dir: string; max_parallel_files: number; scan_depth: number };
  issues: { id: string; message: string; time: number }[]; operations: Operation[]; flood_seconds: number;
  stats: { active: number; queued: number; done: number; failed: number; paused: number; total: number; speed: number; downloaded: number; total_bytes: number };
  queue: QueueItem[]; queue_total: number;
  jobs: { id: string; created: number; output_dir: string; counts: Record<string, number> }[];
  selection: { scopes?: Scope[]; quantities?: Record<string, number | null> };
};
export type Inventory = { category: string; found: number; verified: number; bytes: number; unknown: number }[];
export type Directory = { path: string; parent: string; directories: { name: string; path: string }[]; free_bytes: number; writable: boolean };

export async function api<T>(path: string, body?: unknown): Promise<T> {
  const response = await fetch('/api' + path, { credentials: 'same-origin', method: body === undefined ? 'GET' : 'POST', headers: { 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) });
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'The request could not be completed. Check your inputs.');
  return data as T;
}

export async function connectSession() {
  const token = new URLSearchParams(location.hash.slice(1)).get('token');
  if (token) {
    history.replaceState(null, '', location.pathname + location.search);
    const response = await fetch('/api/session', { method: 'POST', headers: { Authorization: `Bearer ${token}` } });
    if (!response.ok) throw new Error('The launcher link has expired. Open a new link from the app launcher.');
  }
}

export const categories = [
  ['images', 'Images'], ['videos', 'Videos'], ['animations', 'Animations'], ['audio', 'Audio'],
  ['voice', 'Voice messages'], ['video_messages', 'Video messages'], ['stickers', 'Stickers'], ['documents', 'Documents'],
] as const;
export function size(value: number | null | undefined) {
  if (value == null) return 'Unknown size';
  if (value === 0) return '0 B';
  const index = Math.min(4, Math.floor(Math.log(Math.max(1, value)) / Math.log(1024)));
  return `${(value / 1024 ** index).toFixed(index ? 1 : 0)} ${['B', 'KB', 'MB', 'GB', 'TB'][index]}`;
}
export function normalizedScopes(scopes: Scope[]): Scope[] {
  const parents = new Set(scopes.filter(s => s.topic_id == null).map(s => s.chat_id));
  const seen = new Set<string>();
  return scopes.filter(s => {
    const key = `${s.chat_id}:${s.topic_id ?? 'all'}`;
    if (seen.has(key) || (s.topic_id != null && parents.has(s.chat_id))) return false;
    seen.add(key); return true;
  });
}
