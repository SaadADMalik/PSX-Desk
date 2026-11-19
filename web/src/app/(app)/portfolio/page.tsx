"use client"

import Link from "next/link"
import useSWR from "swr"
import { CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { LivePrice } from "@/components/desk"
import { ProposalCard } from "@/components/proposal-card"
import { TradeDialog } from "@/components/trade-dialog"
import { useLive } from "@/lib/live"
import { money, num, pct, price, tone, who } from "@/lib/format"
import type { PortfolioData } from "@/lib/types"

const STATUS: Record<string, string> = {
  filled: "bg-gain/15 text-gain border-gain/25", pending: "bg-primary/15 text-primary border-primary/25",
  rejected: "text-muted-foreground", expired: "text-muted-foreground", failed: "bg-loss/15 text-loss border-loss/25",
}

export default function PortfolioPage() {
  const { data } = useSWR<PortfolioData>("/api/portfolio")
  const { quotes } = useLive()
  if (!data) return <div className="grid gap-4"><Skeleton className="h-28" /><Skeleton className="h-80" /></div>

  const live = quotes?.quotes ?? {}
  const pf = data.portfolio
  const rows = pf.positions.map((p) => {
    const mark = live[p.symbol]?.price ?? p.mark ?? p.avg_cost
    return { ...p, mark, value: mark * p.quantity, pnl: (mark - p.avg_cost) * p.quantity }
  })
  const equity = pf.cash + rows.reduce((s, r) => s + r.value, 0)
  const gain = equity - data.initial_capital
  const lastBench = data.curve.at(-1)?.benchmark
  const vsIndex = lastBench ? equity - lastBench : null
  const fees = data.fills.reduce((s, f) => s + f.fees, 0)

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Demo account</h1>
        <p className="text-sm text-muted-foreground">
          Started with {money(data.initial_capital)} of test money. Trades fill at real PSX prices, with realistic fees,
          so you can see where the money would have gone. Nothing touches a real brokerage.</p>
      </div>

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Kpi label="Account value" value={money(equity)} />
        <Kpi label="Gain / loss" value={`${gain >= 0 ? "+" : ""}${money(gain)}`}
             sub={pct((gain / data.initial_capital) * 100)} cls={tone(gain)} />
        <Kpi label="Versus just buying the KSE-100" value={vsIndex == null ? "—" : `${vsIndex >= 0 ? "+" : ""}${money(vsIndex)}`}
             sub={vsIndex == null ? "after your first trade" : vsIndex >= 0 ? "ahead of the index" : "behind the index"} cls={tone(vsIndex)} />
        <Kpi label="Fees paid" value={money(fees)} sub={`${data.fills.length} trades`} />
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Account value over time</CardTitle>
          <CardDescription>Your demo account against the same money put into the KSE-100 on day one</CardDescription>
        </CardHeader>
        <CardContent className="h-72">
          {data.curve.length < 2 ? (
            <div className="grid h-full place-items-center text-sm text-muted-foreground">
              The chart starts after your first trade.</div>
          ) : (
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={data.curve}>
                <CartesianGrid vertical={false} stroke="var(--border)" strokeDasharray="3 3" />
                <XAxis dataKey="date" tickLine={false} axisLine={false} minTickGap={40}
                       tick={{ fontSize: 11, fill: "var(--muted-foreground)" }} />
                <YAxis domain={["auto", "auto"]} tickLine={false} axisLine={false} width={72}
                       tick={{ fontSize: 11, fill: "var(--muted-foreground)" }}
                       tickFormatter={(v: number) => `${(v / 1000).toFixed(0)}k`} />
                <Tooltip contentStyle={{ background: "var(--popover)", border: "1px solid var(--border)", borderRadius: 8, fontSize: 12 }}
                         formatter={(v) => money(Number(v))} />
                <Legend wrapperStyle={{ fontSize: 12 }} />
                <Line name="Your demo account" dataKey="equity" stroke="var(--primary)" strokeWidth={2} dot={false} />
                <Line name="KSE-100" dataKey="benchmark" stroke="var(--muted-foreground)" strokeDasharray="4 4" dot={false} connectNulls />
              </LineChart>
            </ResponsiveContainer>
          )}
        </CardContent>
      </Card>

      <Tabs defaultValue="holdings">
        <TabsList>
          <TabsTrigger value="holdings">Holdings</TabsTrigger>
          <TabsTrigger value="trades">Trades</TabsTrigger>
          <TabsTrigger value="suggestions">Suggestions</TabsTrigger>
        </TabsList>

        <TabsContent value="holdings">
          <Card><CardContent className="px-0">
            <Table>
              <TableHeader><TableRow>
                <TableHead className="pl-6">Stock</TableHead><TableHead className="text-right">Shares</TableHead>
                <TableHead className="text-right">Avg cost</TableHead><TableHead className="text-right">Price</TableHead>
                <TableHead className="text-right">Value</TableHead><TableHead className="text-right">Gain / loss</TableHead>
                <TableHead className="pr-6" />
              </TableRow></TableHeader>
              <TableBody>
                {rows.map((r) => (
                  <TableRow key={r.symbol}>
                    <TableCell className="pl-6 font-semibold"><Link href={`/stocks/${r.symbol}`} className="hover:text-primary">{r.symbol}</Link></TableCell>
                    <TableCell className="text-right tabular">{num(r.quantity)}</TableCell>
                    <TableCell className="text-right tabular">{price(r.avg_cost)}</TableCell>
                    <TableCell className="text-right"><LivePrice value={r.mark} /></TableCell>
                    <TableCell className="text-right tabular">{money(r.value)}</TableCell>
                    <TableCell className={`text-right tabular ${tone(r.pnl)}`}>{r.pnl > 0 ? "+" : ""}{money(r.pnl)}</TableCell>
                    <TableCell className="pr-6 text-right">
                      <TradeDialog symbol={r.symbol} held={r.quantity} cash={pf.cash} defaultSide="sell"
                                   trigger={<Button size="sm" variant="outline">Trade</Button>} />
                    </TableCell>
                  </TableRow>
                ))}
                <TableRow className="bg-muted/30">
                  <TableCell className="pl-6 font-medium">Cash</TableCell>
                  <TableCell colSpan={3} /><TableCell className="text-right tabular">{money(pf.cash)}</TableCell>
                  <TableCell colSpan={2} />
                </TableRow>
              </TableBody>
            </Table>
            {rows.length === 0 && <p className="px-6 pt-4 text-sm text-muted-foreground">
              No holdings yet. Open a stock and press <b>Trade with demo money</b>, or approve a suggestion from the desk.</p>}
          </CardContent></Card>
        </TabsContent>

        <TabsContent value="trades">
          <Card><CardContent className="px-0">
            <Table>
              <TableHeader><TableRow>
                <TableHead className="pl-6">When</TableHead><TableHead>Trade</TableHead>
                <TableHead className="text-right">Shares</TableHead><TableHead className="text-right">Price</TableHead>
                <TableHead className="text-right">Fees</TableHead><TableHead className="pr-6 text-right">Total</TableHead>
              </TableRow></TableHeader>
              <TableBody>
                {data.fills.map((f) => (
                  <TableRow key={f.proposal_id}>
                    <TableCell className="pl-6 text-muted-foreground">{new Date(f.filled_at).toLocaleString()}</TableCell>
                    <TableCell><Badge variant="outline" className={f.side === "buy" ? "text-gain" : "text-loss"}>{f.side}</Badge> <b>{f.symbol}</b></TableCell>
                    <TableCell className="text-right tabular">{num(f.quantity)}</TableCell>
                    <TableCell className="text-right tabular">{price(f.price)}</TableCell>
                    <TableCell className="text-right tabular">{money(f.fees, true)}</TableCell>
                    <TableCell className="pr-6 text-right tabular">{money(f.quantity * f.price)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
            {data.fills.length === 0 && <p className="px-6 pt-4 text-sm text-muted-foreground">No trades yet.</p>}
          </CardContent></Card>
        </TabsContent>

        <TabsContent value="suggestions" className="space-y-3">
          {data.proposals.filter((p) => p.status === "pending").map((p) => <ProposalCard key={p.id} p={p} />)}
          <Card><CardContent className="px-0">
            <Table>
              <TableHeader><TableRow>
                <TableHead className="pl-6">Suggested</TableHead><TableHead>Trade</TableHead><TableHead>By</TableHead>
                <TableHead>Outcome</TableHead><TableHead className="pr-6">Why</TableHead>
              </TableRow></TableHeader>
              <TableBody>
                {data.proposals.map((p) => (
                  <TableRow key={p.id}>
                    <TableCell className="pl-6 text-muted-foreground">{new Date(p.created_at).toLocaleDateString()}</TableCell>
                    <TableCell className="whitespace-nowrap">{p.side} {num(p.quantity)} <b>{p.symbol}</b></TableCell>
                    <TableCell className="text-muted-foreground">{who(p.proposed_by)}</TableCell>
                    <TableCell><Badge variant="outline" className={STATUS[p.status]}>{p.status}</Badge></TableCell>
                    <TableCell className="max-w-md truncate pr-6 text-muted-foreground">{p.decision_note || p.rationale}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </CardContent></Card>
        </TabsContent>
      </Tabs>
    </div>
  )
}

function Kpi({ label, value, sub, cls }: { label: string; value: string; sub?: string; cls?: string }) {
  return (
    <Card size="sm"><CardContent className="space-y-1">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className={`text-2xl font-semibold tracking-tight tabular ${cls ?? ""}`}>{value}</div>
      {sub && <div className={`text-xs ${cls ?? "text-muted-foreground"}`}>{sub}</div>}
    </CardContent></Card>
  )
}
