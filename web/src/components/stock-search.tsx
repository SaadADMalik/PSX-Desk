"use client"

import { useEffect, useMemo, useRef, useState } from "react"
import { useRouter } from "next/navigation"
import { Search } from "lucide-react"
import { Change } from "@/components/desk"
import { highlightParts, searchStocks, useMarket } from "@/lib/market"
import { price } from "@/lib/format"
import { cn } from "@/lib/utils"

function Hl({ text, q }: { text: string; q: string }) {
  return <>{highlightParts(text, q).map((p, i) =>
    p.hit ? <span key={i} className="text-primary">{p.text}</span> : <span key={i}>{p.text}</span>)}</>
}

/** Search every PSX stock from anywhere: results update on each letter, from data the
 *  browser already holds. "/" or Ctrl+K to focus, arrows to move, Enter to open. */
export function StockSearch() {
  const router = useRouter()
  const { stocks } = useMarket()
  const [q, setQ] = useState("")
  const [open, setOpen] = useState(false)
  const [active, setActive] = useState(0)
  const input = useRef<HTMLInputElement>(null)
  const box = useRef<HTMLDivElement>(null)

  const results = useMemo(() => (q.trim() ? searchStocks(stocks, q, 8) : []), [stocks, q])

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      const typing = e.target instanceof HTMLElement && ["INPUT", "TEXTAREA", "SELECT"].includes(e.target.tagName)
      if ((e.key === "k" && (e.ctrlKey || e.metaKey)) || (e.key === "/" && !typing)) {
        e.preventDefault()
        input.current?.focus()
        setOpen(true)
      }
    }
    function onClick(e: MouseEvent) {
      if (box.current && !box.current.contains(e.target as Node)) setOpen(false)
    }
    window.addEventListener("keydown", onKey)
    window.addEventListener("mousedown", onClick)
    return () => { window.removeEventListener("keydown", onKey); window.removeEventListener("mousedown", onClick) }
  }, [])

  function go(symbol: string) {
    router.push(`/stocks/${symbol}`)
    setQ("")
    setOpen(false)
    input.current?.blur()
  }

  return (
    <div ref={box} className="relative w-full max-w-xs">
      <Search className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground" />
      <input ref={input} value={q} placeholder="Search any PSX stock…" aria-label="Search stocks"
        className="h-9 w-full rounded-lg border border-input bg-muted/40 pr-12 pl-8 text-sm outline-none transition placeholder:text-muted-foreground focus:border-primary/50 focus:bg-background"
        onChange={(e) => { setQ(e.target.value); setActive(0); setOpen(true) }}
        onFocus={() => setOpen(true)}
        onKeyDown={(e) => {
          if (e.key === "ArrowDown") { e.preventDefault(); setActive((i) => Math.min(i + 1, results.length - 1)) }
          else if (e.key === "ArrowUp") { e.preventDefault(); setActive((i) => Math.max(i - 1, 0)) }
          else if (e.key === "Enter" && results[active]) go(results[active].symbol)
          else if (e.key === "Escape") { setOpen(false); input.current?.blur() }
        }} />
      <kbd className="pointer-events-none absolute top-1/2 right-2 hidden -translate-y-1/2 rounded border bg-background px-1.5 text-[10px] text-muted-foreground sm:block">Ctrl K</kbd>
      {open && q.trim() && (
        <div className="absolute top-11 right-0 left-0 z-50 overflow-hidden rounded-xl border bg-popover shadow-xl sm:-right-24">
          {results.length === 0 ? (
            <div className="px-3 py-4 text-sm text-muted-foreground">No PSX stock matches “{q}”.</div>
          ) : results.map((s, i) => (
            <button key={s.symbol} onMouseEnter={() => setActive(i)} onClick={() => go(s.symbol)}
              className={cn("flex w-full items-center gap-3 px-3 py-2 text-left text-sm", i === active && "bg-accent")}>
              <div className="min-w-0 flex-1">
                <div className="font-semibold"><Hl text={s.symbol} q={q} /></div>
                <div className="truncate text-xs text-muted-foreground"><Hl text={s.name} q={q} /></div>
              </div>
              <div className="text-right">
                <div className="tabular">{price(s.price)}</div>
                <Change value={s.change_pct} className="text-xs" />
              </div>
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
