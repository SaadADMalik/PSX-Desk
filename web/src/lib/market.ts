"use client"

// Every PSX stock, loaded once and kept in the browser, so search answers on every
// keystroke with no network round trip. Prices are refreshed from the live stream.

import { useMemo } from "react"
import useSWR from "swr"
import { useLive } from "@/lib/live"
import type { Listing, MarketBoard } from "@/lib/types"

export function useMarket() {
  const { data, error, isLoading } = useSWR<MarketBoard>("/api/market", {
    revalidateOnFocus: false,
    dedupingInterval: 5 * 60_000, // names and sectors barely change; prices come live
  })
  const { quotes } = useLive()
  const stocks = useMemo<Listing[]>(() => {
    if (!data) return []
    const live = quotes?.quotes ?? {}
    return data.stocks.map((s) => {
      const q = live[s.symbol]
      return q ? { ...s, price: q.price, change_pct: q.change_pct ?? s.change_pct } : s
    })
  }, [data, quotes])
  return { board: data, stocks, error, isLoading }
}

function norm(s: string) {
  return s.toLowerCase().replace(/[^a-z0-9 ]+/g, " ").replace(/\s+/g, " ").trim()
}

/** How well a stock matches what has been typed so far; 0 = no match.
 *  Ticker matches beat name matches; starts-with beats contains. */
export function matchScore(s: Listing, query: string): number {
  const q = norm(query)
  if (!q) return 1
  const sym = s.symbol.toLowerCase()
  const name = norm(s.name)
  const compact = q.replace(/ /g, "")
  if (sym === compact) return 100
  if (sym.startsWith(compact)) return 80 - Math.min(sym.length - compact.length, 10)
  if (name.startsWith(q)) return 70
  if (name.split(" ").some((w) => w.startsWith(q))) return 60
  if (q.includes(" ") && q.split(" ").every((part) => name.split(" ").some((w) => w.startsWith(part)))) return 55
  if (sym.includes(compact)) return 40
  if (name.includes(q)) return 30
  if (norm(s.sector).includes(q)) return 10
  return 0
}

/** Average rupees traded per day: price x 30-day average volume. PSX's market cap field
 *  is mostly empty, so this is the size-and-liquidity measure used for ordering. */
export const tradedValue = (s: Listing) => (s.price ?? 0) * (s.volume_avg_30d ?? 0)

/** Matches, best first; ties go to the more heavily traded stock. */
export function searchStocks(stocks: Listing[], query: string, limit?: number): Listing[] {
  const scored: [number, Listing][] = []
  for (const s of stocks) {
    const sc = matchScore(s, query)
    if (sc > 0) scored.push([sc, s])
  }
  scored.sort((a, b) => b[0] - a[0] || tradedValue(b[1]) - tradedValue(a[1]))
  const out = scored.map(([, s]) => s)
  return limit ? out.slice(0, limit) : out
}

/** Wraps the typed letters in <mark> wherever they appear in `text`. */
export function highlightParts(text: string, query: string): { text: string; hit: boolean }[] {
  const q = query.trim().toLowerCase()
  if (!q) return [{ text, hit: false }]
  const i = text.toLowerCase().indexOf(q)
  if (i < 0) return [{ text, hit: false }]
  return [{ text: text.slice(0, i), hit: false }, { text: text.slice(i, i + q.length), hit: true },
          { text: text.slice(i + q.length), hit: false }]
}
