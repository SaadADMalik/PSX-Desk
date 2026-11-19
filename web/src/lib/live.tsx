"use client"

// One EventSource per tab. Quotes are kept in context (prices tick in place); other
// events tell SWR which data changed, so screens refresh themselves.

import { createContext, useContext, useEffect, useRef, useState } from "react"
import { useSWRConfig } from "swr"
import { toast } from "sonner"
import type { QuotesEvent } from "@/lib/types"

interface Live {
  quotes: QuotesEvent | null
  connected: boolean
  marketOpen: boolean | null
}

const LiveContext = createContext<Live>({ quotes: null, connected: false, marketOpen: null })

export function LiveProvider({ children }: { children: React.ReactNode }) {
  const { mutate } = useSWRConfig()
  const [state, setState] = useState<Live>({ quotes: null, connected: false, marketOpen: null })
  const pending = useRef<number | null>(null)

  useEffect(() => {
    const es = new EventSource("/api/events")
    es.onopen = () => setState((s) => ({ ...s, connected: true }))
    es.onerror = () => setState((s) => ({ ...s, connected: false }))
    es.onmessage = (e) => {
      const msg = JSON.parse(e.data)
      if (msg.type === "quotes") {
        setState((s) => ({ ...s, quotes: msg.data, marketOpen: msg.data.market_open ?? s.marketOpen }))
      } else if (msg.type === "brief") {
        const d = msg.data
        if (d.type === "brief.completed") {
          toast.success(`New research on ${d.symbol}`, {
            description: `${d.stance} · confidence ${Math.round(d.confidence * 100)}%` +
              (d.proposal && d.proposal !== "none" ? ` · suggested trade: ${d.proposal}` : ""),
          })
        } else if (d.type === "brief.failed") {
          toast.error(`Research on ${d.symbol} failed`)
        }
        mutate((k) => typeof k === "string" && k.startsWith("/api/"))
      } else if (msg.type === "portfolio") {
        if (pending.current) window.clearTimeout(pending.current)
        pending.current = window.setTimeout(() =>
          mutate((k) => typeof k === "string" &&
            (k.startsWith("/api/overview") || k.startsWith("/api/portfolio") ||
             k.startsWith("/api/proposals") || k.startsWith("/api/stocks"))), 300)
      }
    }
    return () => es.close()
  }, [mutate])

  return <LiveContext.Provider value={state}>{children}</LiveContext.Provider>
}

export const useLive = () => useContext(LiveContext)

/** Live price for a symbol, falling back to the value the page loaded with. */
export function useLivePrice(symbol: string, fallback?: { price: number | null;
                             change_pct: number | null } | null) {
  const { quotes } = useLive()
  const q = quotes?.quotes?.[symbol]
  return { price: q?.price ?? fallback?.price ?? null,
           change_pct: q?.change_pct ?? fallback?.change_pct ?? null }
}
