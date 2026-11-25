import type { Page, Route } from "@playwright/test"
import overview from "../fixtures/overview.json"
import market from "../fixtures/market.json"
import system from "../fixtures/system.json"
import portfolio from "../fixtures/portfolio.json"
import stockHBL from "../fixtures/stocks_HBL.json"

// The browser talks only to /api/*; every call is answered here from responses
// recorded from the real backend, so the UI is tested against real shapes.

const json = (route: Route, body: unknown, status = 200) =>
  route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) })

export const sse = (events: object[]) =>
  events.map((e) => `data: ${JSON.stringify(e)}\n\n`).join("")

export interface MockOptions {
  signedIn?: boolean
  chat?: object[]                       // events for /api/chat/stream
  system?: unknown
}

export async function mockApi(page: Page, opts: MockOptions = {}) {
  const signedIn = opts.signedIn ?? true
  const calls: { url: string; body: unknown }[] = []

  await page.route("**/api/**", async (route) => {
    const req = route.request()
    const path = new URL(req.url()).pathname
    const body = req.postData() ? JSON.parse(req.postData()!) : undefined
    calls.push({ url: path, body })

    if (path === "/api/login") {
      const ok = (body as { password?: string })?.password === "test-password"
      return ok ? json(route, { user: "operator" }) : json(route, { detail: "wrong user or password" }, 401)
    }
    if (path === "/api/logout") return json(route, {})
    if (!signedIn) return json(route, { detail: "not signed in" }, 401)

    if (path === "/api/events") {
      return route.fulfill({ status: 200, contentType: "text/event-stream", body: ": connected\n\n" })
    }
    if (path === "/api/overview") return json(route, overview)
    if (path === "/api/market") return json(route, market)
    if (path === "/api/system") return json(route, opts.system ?? system)
    if (path === "/api/portfolio") return json(route, portfolio)
    if (path.startsWith("/api/watchlist/")) return json(route, { watchlist: [] })
    if (path === "/api/alerts" && req.method() === "POST") {
      const b = body as { symbol: string; kind: string; threshold: number; days: number | null }
      return json(route, { id: "00000000-0000-4000-8000-000000000001", account: "operator",
        symbol: b.symbol, kind: b.kind, threshold: b.threshold, ref_price: 304, ref_basis: "price_when_set",
        starts_at: new Date().toISOString(), ends_at: null, state: "armed", status: "active",
        last_price: null, last_checked_at: null, times_triggered: 0, created_by: "operator",
        created_at: new Date().toISOString() })
    }
    if (path === "/api/stocks/HBL") return json(route, stockHBL)
    if (path.startsWith("/api/stocks/")) return json(route, { detail: "unknown symbol" }, 404)
    if (path === "/api/chat/stream") {
      return route.fulfill({ status: 200, contentType: "text/event-stream", body: sse(opts.chat ?? []) })
    }
    return json(route, { detail: `not mocked: ${path}` }, 501)
  })
  return calls
}
