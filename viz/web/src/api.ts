/** 与服务端的传输层：页面与 /api 永远同源——开发态由 Vite 代理转给 8765，生产态由 server.py 直接托管。 */

// 需要指向别的实例时用 VITE_API_BASE 覆盖；默认空串 = 同源
export const API_BASE: string = (import.meta.env.VITE_API_BASE as string | undefined) ?? "";

export const apiUrl = (path: string): string => `${API_BASE}${path}`;

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(apiUrl(path), init);
  const text = await res.text();
  let body: any;
  try {
    body = text ? JSON.parse(text) : {};
  } catch {
    body = { raw: text };
  }
  if (!res.ok) throw new Error(body?.error || body?.detail || `HTTP ${res.status}`);
  return body as T;
}

export const postJson = <T>(path: string, payload: unknown): Promise<T> =>
  api<T>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
