"use client"

import { useDeferredValue, useMemo, useState } from "react"
import Link from "next/link"
import { useRouter } from "next/navigation"
import { ArrowDown, ArrowUp, ChevronsUpDown, Search, Star, X } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Skeleton } from "@/components/ui/skeleton"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { Change, LivePrice } from "@/components/desk"
import { highlightParts, searchStocks, tradedValue, useMarket } from "@/lib/market"
import { ago, compact, num, pct } from "@/lib/format"
import { cn } from "@/lib/utils"
import type { Listing } from "@/lib/types"

type View = "all" | "watchlist" | "gainers" | "losers" | "active"
type SortKey = "symbol" | "price" | "change_pct" | "change_1y_pct" | "pe_ratio" | "dividend_yield" | "traded" | "volume_avg_30d"
const PAGE = 50

function Hl({ text, q }: { text: string; q: string }) {
  return <>{highlightParts(text, q).map((p, i) =>
    p.hit ? <mark key={i} className="rounded-sm bg-primary/25 px-0 text-foreground">{p.text}</mark> : <span key={i}>{p.text}</span>)}</>
}

export default function StocksPage() {
  const router = useRouter()
  const { board, stocks, error, isLoading } = useMarket()
  const [query, setQuery] = useState("")
  const q = useDeferredValue(query)
  const [view, setView] = useState<View>("all")
  const [sector, setSector] = useState("")
  const [sort, setSort] = useState<{ key: SortKey; dir: 1 | -1 } | null>(null)
  const [shown, setShown] = useState(PAGE)

  const sectors = useMemo(() => [...new Set(stocks.map((s) => s.sector).filter(Boolean))].sort(), [stocks])
  const watch = useMemo(() => new Set(board?.watchlist ?? []), [board])

  const rows = useMemo(() => {
    let list: Listing[] = q.trim() ? searchStocks(stocks, q) : stocks
    if (sector) list = list.filter((s) => s.sector === sector)
    if (view === "watchlist") list = list.filter((s) => watch.has(s.symbol))
    const val = (s: Listing, key: SortKey) => (key === "traded" ? tradedValue(s) : s[key])
    const by = (key: SortKey, dir: 1 | -1) => (a: Listing, b: Listing) => {
      const x = val(a, key), y = val(b, key)
      if (x == null) return 1
      if (y == null) return -1
      return (x < y ? -1 : x > y ? 1 : 0) * dir
    }
    if (sort) return [...list].sort(by(sort.key, sort.dir))
    if (view === "gainers") return [...list].filter((s) => (s.change_pct ?? 0) > 0).sort(by("change_pct", -1))
    if (view === "losers") return [...list].filter((s) => (s.change_pct ?? 0) < 0).sort(by("change_pct", 1))
    if (view === "active") return [...list].sort(by("traded", -1))
    if (!q.trim()) return [...list].sort(by("traded", -1)) // most traded first
    return list // search order: best match first
  }, [stocks, q, sector, view, sort, watch])

  function toggleSort(key: SortKey) {
    setSort((s) => (s?.key === key ? (s.dir === -1 ? { key, dir: 1 } : null) : { key, dir: -1 }))
  }

  function Th({ k, children, className }: { k: SortKey; children: React.ReactNode; className?: string }) {
    const Icon = sort?.key === k ? (sort.dir === -1 ? ArrowDown : ArrowUp) : ChevronsUpDown
    return (
      <TableHead className={className}>
        <button onClick={() => toggleSort(k)} className={cn("inline-flex items-center gap-1 hover:text-foreground",
          sort?.key === k && "text-foreground")}>{children}<Icon className="size-3 opacity-60" /></button>
      </TableHead>
    )
  }

  if (error) return <p className="text-sm text-loss">Could not load the market: {error.message}</p>

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Stocks</h1>
          <p className="text-sm text-muted-foreground">
            {board ? `All ${board.count} stocks trading on PSX · prices ${ago(board.fetched_at)}` : "Every stock trading on PSX"}
          </p>
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <div className="relative w-full sm:max-w-sm">
          <Search className="absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input autoFocus value={query} placeholder="Search by ticker or company, e.g. meezan or LUC"
                 className="h-10 pr-9 pl-9" onChange={(e) => { setQuery(e.target.value); setShown(PAGE) }}
                 onKeyDown={(e) => { if (e.key === "Enter" && rows[0]) router.push(`/stocks/${rows[0].symbol}`) }} />
          {query && <button aria-label="Clear" onClick={() => setQuery("")}
                            className="absolute top-1/2 right-3 -translate-y-1/2 text-muted-foreground hover:text-foreground"><X className="size-4" /></button>}
        </div>
        <select value={sector} onChange={(e) => { setSector(e.target.value); setShown(PAGE) }}
                className="h-10 rounded-md border border-input bg-background px-3 text-sm text-foreground dark:bg-input/30">
          <option value="">All sectors</option>
          {sectors.map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
        <Tabs value={view} onValueChange={(v) => { setView(v as View); setSort(null); setShown(PAGE) }}>
          <TabsList>
            <TabsTrigger value="all">All</TabsTrigger>
            <TabsTrigger value="watchlist">Watchlist</TabsTrigger>
            <TabsTrigger value="gainers">Gainers</TabsTrigger>
            <TabsTrigger value="losers">Losers</TabsTrigger>
            <TabsTrigger value="active">Most active</TabsTrigger>
          </TabsList>
        </Tabs>
      </div>

      <Card>
        <CardContent className="px-0">
          {isLoading ? <div className="space-y-2 px-6">{Array.from({ length: 8 }, (_, i) => <Skeleton key={i} className="h-10" />)}</div> : (
            <>
              <div className="px-6 pb-2 text-xs text-muted-foreground">
                {rows.length === stocks.length ? `${rows.length} stocks` : `${rows.length} of ${stocks.length} stocks`}
              </div>
              <Table>
                <TableHeader>
                  <TableRow>
                    <Th k="symbol" className="pl-6">Stock</Th>
                    <TableHead className="hidden xl:table-cell">Sector</TableHead>
                    <Th k="price" className="text-right">Price</Th>
                    <Th k="change_pct" className="text-right">Today</Th>
                    <Th k="change_1y_pct" className="hidden text-right md:table-cell">1 year</Th>
                    <Th k="pe_ratio" className="hidden text-right lg:table-cell">P/E</Th>
                    <Th k="dividend_yield" className="hidden text-right lg:table-cell">Div. yield</Th>
                    <Th k="traded" className="hidden text-right lg:table-cell">Traded / day</Th>
                    <Th k="volume_avg_30d" className="hidden pr-6 text-right sm:table-cell">Avg volume</Th>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {rows.slice(0, shown).map((s) => (
                    <TableRow key={s.symbol} className="cursor-pointer" onClick={() => router.push(`/stocks/${s.symbol}`)}>
                      <TableCell className="max-w-[16rem] pl-6">
                        <div className="flex items-center gap-1.5">
                          <Link href={`/stocks/${s.symbol}`} className="font-semibold hover:text-primary" onClick={(e) => e.stopPropagation()}>
                            <Hl text={s.symbol} q={q} /></Link>
                          {watch.has(s.symbol) && <Star className="size-3 fill-amber-400 text-amber-400" />}
                          {s.etf && <Badge variant="outline" className="h-4 px-1 text-[10px]">ETF</Badge>}
                          {s.ex && <Badge variant="outline" className="h-4 px-1 text-[10px]" title="Ex-dividend / bonus / rights period">{s.ex}</Badge>}
                        </div>
                        <div className="truncate text-xs text-muted-foreground"><Hl text={s.name} q={q} /></div>
                      </TableCell>
                      <TableCell className="hidden max-w-[12rem] truncate text-xs text-muted-foreground xl:table-cell">{s.sector}</TableCell>
                      <TableCell className="text-right font-medium"><LivePrice value={s.price} /></TableCell>
                      <TableCell className="text-right"><Change value={s.change_pct} /></TableCell>
                      <TableCell className="hidden text-right md:table-cell"><Change value={s.change_1y_pct} /></TableCell>
                      <TableCell className="hidden text-right tabular lg:table-cell">{s.pe_ratio ? s.pe_ratio.toFixed(1) : "—"}</TableCell>
                      <TableCell className="hidden text-right tabular lg:table-cell">{s.dividend_yield ? pct(s.dividend_yield, false) : "—"}</TableCell>
                      <TableCell className="hidden text-right tabular lg:table-cell">{compact(tradedValue(s))}</TableCell>
                      <TableCell className="hidden pr-6 text-right tabular text-muted-foreground sm:table-cell">{num(s.volume_avg_30d)}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
              {rows.length === 0 && (
                <p className="px-6 py-8 text-center text-sm text-muted-foreground">
                  No stock matches “{query}”. Try part of the company name or ticker.</p>
              )}
              {rows.length > shown && (
                <div className="flex justify-center pt-4">
                  <Button variant="outline" size="sm" onClick={() => setShown((n) => n + PAGE * 2)}>
                    Show more ({rows.length - shown} left)</Button>
                </div>
              )}
            </>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
