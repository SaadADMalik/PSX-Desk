// Talks to the app backend through Next's /api rewrite. Same origin, so the session
// cookie rides along; every write carries X-Desk-Request (the backend's CSRF guard).

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message)
  }
}

async function handle<T>(r: Response): Promise<T> {
  if (r.status === 401 && typeof window !== "undefined" &&
      !window.location.pathname.startsWith("/login")) {
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
  fetch(url, { credentials: "same-origin" }).then((r) => handle<T>(r))

/** POST, then read the reply as server-sent events, calling `onEvent` for each one as
 *  it arrives (EventSource only does GET, so this reads the stream by hand). */
export async function streamPost(url: string, body: unknown,
                                 onEvent: (e: Record<string, unknown>) => void,
                                 signal?: AbortSignal): Promise<void> {
  const r = await fetch(url, {
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
  const r = await fetch(url, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-Desk-Request": "1" },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  return handle<T>(r)
}
