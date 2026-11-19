"use client"

import Link from "next/link"
import useSWR from "swr"
import { ArrowRight, Bot, Inbox, Wallet, TrendingUp, Landmark } from "lucide-react"
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Change, LivePrice, RiskBadge, Sparkline, StanceBadge } from "@/components/desk"
import { ProposalCard } from "@/components/proposal-card"
import { ResearchButton } from "@/components/research-button"
import { TradeDialog } from "@/components/trade-dialog"
import { useLive } from "@/lib/live"
import { ago, money, pct, tone } from "@/lib/format"
import type { Overview } from "@/lib/types"

function Stat({ icon: Icon, label, value, sub, subTone }: {
  icon: typeof Wallet; label: string; value: string; sub?: string; subTone?: string }) {
  return (
    <Card size="sm">
      <CardContent className="space-y-1">
        <div className="flex items-center gap-2 text-xs text-muted-foreground"><Icon className="size-3.5" />{label}</div>
        <div className="text-2xl font-semibold tracking-tight tabular">{value}</div>
        {sub && <div className={`text-xs tabular ${subTone ?? "text-muted-foreground"}`}>{sub}</div>}
      </CardContent>
    </Card>
  )
}

export default function Today() {
  const { data, isLoading } = useSWR<Overview>("/api/overview")
  const { quotes } = useLive()
  const live = quotes?.quotes ?? {}

  if (isLoading || !data) return <div className="grid gap-4"><Skeleton className="h-28" /><Skeleton className="h-96" /></div>

  const pf = data.portfolio
  const equity = pf.cash + pf.positions.reduce((s, p) => s + p.quantity * (live[p.symbol]?.price ?? p.mark ?? p.avg_cost), 0)
  const gain = equity - data.initial_capital
  const dayMove = pf.positions.reduce((s, p) => {
    const q = live[p.symbol]
    if (!q?.change_pct) return s
    return s + p.quantity * q.price * (q.change_pct / (100 + q.change_pct))
  }, 0)
  const hour = new Date().getHours()
  const greet = hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening"

  return (
    <div className="space-y-8">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">{greet}</h1>
          <p className="text-sm text-muted-foreground">
            What the research desk thinks about your watchlist, and what needs your decision.</p>
        </div>
        <Button render={<Link href="/advisor" />} variant="outline"><Bot />Ask the advisor</Button>
      </div>

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat icon={Wallet} label="Demo account value" value={money(equity)}
              sub={`${gain >= 0 ? "+" : ""}${money(gain)} since start`} subTone={tone(gain)} />
        <Stat icon={TrendingUp} label="Today" value={`${dayMove >= 0 ? "+" : ""}${money(dayMove)}`}
              sub={pf.positions.length ? "on your holdings" : "no holdings yet"} subTone={tone(dayMove)} />
        <Stat icon={Landmark} label="Cash available" value={money(pf.cash)}
              sub={`${pct((pf.cash / Math.max(equity, 1)) * 100, false)} of account`} />
        <Stat icon={Inbox} label="Waiting for you" value={String(data.pending.length)}
              sub={data.pending.length ? "suggested trades to review" : "nothing to decide"} />
      </div>

      {data.pending.length > 0 && (
        <section className="space-y-3">
          <h2 className="text-sm font-medium text-muted-foreground">Suggested trades waiting for your decision</h2>
          <div className="grid gap-3 lg:grid-cols-2">
            {data.pending.map((p) => <ProposalCard key={p.id} p={p} />)}
          </div>
        </section>
      )}

      <Card>
        <CardHeader>
          <CardTitle>Watchlist</CardTitle>
          <CardDescription>Live prices, and the research desk&apos;s latest call on each stock.
            Five AI agents research a stock, argue both sides, and every number is checked.</CardDescription>
          <CardAction className="hidden text-xs text-muted-foreground sm:block">
            {data.quotes_at ? `prices ${ago(quotes?.fetched_at ?? data.quotes_at)}` : ""}
          </CardAction>
        </CardHeader>
        <CardContent className="px-0">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="pl-6">Stock</TableHead>
                <TableHead className="text-right">Price</TableHead>
                <TableHead className="text-right">Today</TableHead>
                <TableHead className="hidden md:table-cell">30 days</TableHead>
                <TableHead>Desk&apos;s call</TableHead>
                <TableHead className="hidden lg:table-cell">Researched</TableHead>
                <TableHead className="pr-6 text-right" />
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.watchlist.map((r) => {
                const q = live[r.symbol]
                const held = pf.positions.find((p) => p.symbol === r.symbol)?.quantity ?? 0
                return (
                  <TableRow key={r.symbol} className="group">
                    <TableCell className="pl-6">
                      <Link href={`/stocks/${r.symbol}`} className="font-semibold hover:text-primary">{r.symbol}</Link>
                      {held > 0 && <div className="text-xs text-muted-foreground tabular">you hold {held}</div>}
                    </TableCell>
                    <TableCell className="text-right font-medium"><LivePrice value={q?.price ?? r.price} /></TableCell>
                    <TableCell className="text-right"><Change value={q?.change_pct ?? r.change_pct} /></TableCell>
                    <TableCell className="hidden md:table-cell"><Sparkline data={r.spark} /></TableCell>
                    <TableCell>
                      <div className="space-y-1">
                        <StanceBadge stance={r.desk?.stance} confidence={r.desk?.confidence} />
                        {r.desk && <div><RiskBadge verdict={r.desk.risk} /></div>}
                      </div>
                    </TableCell>
                    <TableCell className="hidden text-xs text-muted-foreground lg:table-cell">{ago(r.desk?.as_of)}</TableCell>
                    <TableCell className="pr-6">
                      <div className="flex justify-end gap-1.5 opacity-80 transition group-hover:opacity-100">
                        <ResearchButton symbol={r.symbol} variant="ghost" />
                        <TradeDialog symbol={r.symbol} held={held} cash={pf.cash}
                          trigger={<Button size="sm" variant="outline">Trade</Button>} />
                        <Button size="icon-sm" variant="ghost" render={<Link href={`/stocks/${r.symbol}`} />}>
                          <ArrowRight /></Button>
                      </div>
                    </TableCell>
                  </TableRow>
                )
              })}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      <Card size="sm" className="bg-muted/40">
        <CardContent className="grid gap-4 text-sm sm:grid-cols-3">
          <div><div className="font-medium">1 · The desk researches</div>
            <p className="text-muted-foreground">Agents study price trends, valuation and company news, a bull and a bear argue, a judge decides, and code checks every number.</p></div>
          <div><div className="font-medium">2 · You decide</div>
            <p className="text-muted-foreground">When a case is strong enough it suggests a demo trade. You approve or reject it, or trade yourself.</p></div>
          <div><div className="font-medium">3 · See if it was right</div>
            <p className="text-muted-foreground">Your demo account tracks every trade at real prices, against simply holding the KSE-100.</p></div>
        </CardContent>
      </Card>
    </div>
  )
}
