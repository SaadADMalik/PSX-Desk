// Talks to the app backend through Next's /api rewrite. Same origin, so the session
// cookies ride along; every write carries X-Desk-Request (the backend's CSRF guard).
//
// Sessions: a 15-minute access token and a longer-lived refresh token, both HttpOnly
// cookies this code never sees. When a call comes back 401 the access token has run
// out, so we swap the refresh token for a new pair (once, however many calls failed
// together) and retry. Only when that fails too is it time to sign in again.

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message)
  }
}

let refreshing: Promise<boolean> | null = null

/** Swap the refresh token for a new pair. True if signed in afterwards. Shared, so many
 *  calls failing at once refresh once. */
export function refreshSession(): Promise<boolean> {
  refreshing ??= (async () => {
    for (let attempt = 0; attempt < 3; attempt++) {
      const r = await fetch("/api/auth/refresh", {
        method: "POST", credentials: "same-origin", headers: { "X-Desk-Request": "1" },
      }).catch(() => null)
      if (r?.ok) return true
      if (r?.status !== 409) return false
      // 409: another tab refreshed with this token a moment ago; its new cookie is ours too
      await new Promise((res) => setTimeout(res, 300))
    }
    return false
  })().finally(() => { refreshing = null })
  return refreshing
}

const isAuthCall = (url: string) => url.startsWith("/api/auth/")

/** fetch, refreshing the session and retrying once on 401. */
async function authed(url: string, init: RequestInit): Promise<Response> {
  const r = await fetch(url, init)
  if (r.status !== 401 || isAuthCall(url) || typeof window === "undefined") return r
  return (await refreshSession()) ? fetch(url, init) : r
}

async function handle<T>(r: Response): Promise<T> {
  if (r.status === 401 && typeof window !== "undefined" && !r.url.includes("/api/auth/") &&
      !window.location.pathname.startsWith("/login")) {
    // A full page load on purpose: this runs outside React (no router here), and a
    // reload drops every cached screen of the ended session.
    // eslint-disable-next-line @next/next/no-location-assign-relative-destination
    window.location.href = "/login"
  }
  if (!r.ok) {
    let msg = r.statusText
    try {
      const body = await r.json()
      msg = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail)
    } catch {}
    throw new ApiError(r.status, msg)
  }
  return r.json() as Promise<T>
}

export const fetcher = <T,>(url: string) =>
  authed(url, { credentials: "same-origin" }).then((r) => handle<T>(r))

/** POST, then read the reply as server-sent events, calling `onEvent` for each one as
 *  it arrives (EventSource only does GET, so this reads the stream by hand). */
export async function streamPost(url: string, body: unknown,
                                 onEvent: (e: Record<string, unknown>) => void,
                                 signal?: AbortSignal): Promise<void> {
  const r = await authed(url, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-Desk-Request": "1",
               Accept: "text/event-stream" },
    body: JSON.stringify(body),
    signal,
  })
  if (!r.ok || !r.body) {
    await handle(r)
    return
  }
  const reader = r.body.pipeThrough(new TextDecoderStream()).getReader()
  let buf = ""
  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    buf += value
    let i
    while ((i = buf.indexOf("\n\n")) >= 0) {
      const frame = buf.slice(0, i)
      buf = buf.slice(i + 2)
      for (const line of frame.split("\n")) {
        if (line.startsWith("data:")) onEvent(JSON.parse(line.slice(5)))
      }
    }
  }
}

export async function post<T>(url: string, body?: unknown): Promise<T> {
  return send<T>("POST", url, body)
}

export async function send<T>(method: "POST" | "PUT" | "DELETE", url: string,
                              body?: unknown): Promise<T> {
  const r = await authed(url, {
    method,
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-Desk-Request": "1" },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  return handle<T>(r)
}
