"use client"

import { Suspense, useMemo, useState } from "react"
import Link from "next/link"
import { useParams } from "next/navigation"
import useSWR from "swr"
import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts"
import { ArrowLeft, BellPlus, CheckCircle2, ThumbsDown, ThumbsUp, TriangleAlert } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { Change, ConfidenceMeter, LivePrice, RiskBadge, StanceBadge } from "@/components/desk"
import { AlertDialog, AlertList } from "@/components/price-alerts"
import { ResearchButton } from "@/components/research-button"
import { TradeDialog } from "@/components/trade-dialog"
import { WatchButton } from "@/components/watch-button"
import { ApiError } from "@/lib/api"
import { useLive } from "@/lib/live"
import { ago, money, num, pct, price, tone } from "@/lib/format"
import type { StockData } from "@/lib/types"

const RANGES = { "1M": 22, "3M": 66, "6M": 130, "1Y": 260 } as const
type Range = keyof typeof RANGES

const AGENT_LABEL: Record<string, string> = {
  technical: "Price trend", fundamentals: "Valuation", news: "Company news",
}

export default function StockPage() {
  return <Suspense fallback={<Loading />}><StockView /></Suspense>
}

function Loading() {
  return <div className="grid gap-4"><Skeleton className="h-16" /><Skeleton className="h-80" /></div>
}

function StockView() {
  const { symbol } = useParams<{ symbol: string }>()
  const sym = symbol.toUpperCase()
  const { data, error, isLoading } = useSWR<StockData>(`/api/stocks/${sym}`)
  const { quotes } = useLive()
  const [range, setRange] = useState<Range>("6M")

  const points = useMemo(() => {
    const all = data?.history?.points ?? []
    return all.slice(-RANGES[range])
  }, [data, range])

  if (error instanceof ApiError && error.status === 404) {
    return (
      <Card className="mx-auto mt-10 max-w-md text-center">
        <CardHeader>
          <CardTitle>{sym} isn&apos;t traded on PSX</CardTitle>
          <CardDescription>
            Check the ticker, or search for the company by name with the search box above.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Button variant="outline" render={<Link href="/stocks" />}><ArrowLeft />All stocks</Button>
        </CardContent>
      </Card>
    )
  }
  if (error) return <p className="text-sm text-loss">Could not load {sym}: {error.message}</p>
  if (isLoading || !data) return <Loading />

  const q = quotes?.quotes[sym] ?? data.quote
  const last = q?.price ?? points.at(-1)?.close ?? null
  const first = points[0]?.close
  const rangeMove = first && last ? ((last - first) / first) * 100 : null
  const up = (rangeMove ?? 0) >= 0
  const b = data.brief
  const pos = data.position
  const pnl = pos && last ? (last - pos.avg_cost) * pos.quantity : null

  return (
    <div className="space-y-6">
      <Link href="/stocks" className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground">
        <ArrowLeft className="size-4" />All stocks</Link>

      <div className="flex flex-wrap items-end justify-between gap-4">
        <div className="space-y-1">
          <div className="flex items-center gap-3">
            <h1 className="text-3xl font-semibold tracking-tight">{sym}</h1>
            <StanceBadge stance={b?.brief.stance} confidence={b?.brief.confidence} />
          </div>
          {data.info && (
            <p className="text-sm text-muted-foreground">{data.info.name}
              {data.info.sector && <> · {data.info.sector}</>}
              {data.info.pe_ratio ? <> · P/E {data.info.pe_ratio.toFixed(1)}</> : null}
              {data.info.dividend_yield ? <> · dividend yield {data.info.dividend_yield.toFixed(1)}%</> : null}
              {data.info.change_1y_pct != null && <> · 1 year <Change value={data.info.change_1y_pct} /></>}
            </p>
          )}
          <div className="flex items-baseline gap-3">
            <span className="text-2xl font-medium"><LivePrice value={last} /></span>
            <Change value={q?.change_pct ?? null} className="text-sm" />
            <span className="text-xs text-muted-foreground">today</span>
          </div>
        </div>
        <div className="flex flex-wrap gap-2">
          <WatchButton symbol={sym} watched={data.watched} />
          <AlertDialog symbol={sym} position={pos} livePrice={last}
                       trigger={<Button size="sm" variant="outline"><BellPlus />Set alert</Button>} />
          <ResearchButton symbol={sym} />
          <TradeDialog symbol={sym} held={pos?.quantity ?? 0} cash={data.cash}
                       trigger={<Button size="sm">Trade with demo money</Button>} />
        </div>
      </div>

      <div className="grid gap-6 lg:grid-cols-3">
        <Card className="lg:col-span-2">
          <CardHeader className="flex flex-row items-center justify-between">
            <div>
              <CardTitle>Price</CardTitle>
              <CardDescription>
                <span className={tone(rangeMove)}>{pct(rangeMove)}</span> over {range}
                {data.history?.stale && " · cached data"}
              </CardDescription>
            </div>
            <Tabs value={range} onValueChange={(v) => setRange(v as Range)}>
              <TabsList>{(Object.keys(RANGES) as Range[]).map((r) =>
                <TabsTrigger key={r} value={r}>{r}</TabsTrigger>)}</TabsList>
            </Tabs>
          </CardHeader>
          <CardContent className="h-72">
            {points.length < 2 ? <p className="text-sm text-muted-foreground">No price history.</p> : (
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={points} margin={{ left: 0, right: 4, top: 4, bottom: 0 }}>
                  <defs>
                    <linearGradient id="fill" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%" stopColor={up ? "var(--gain)" : "var(--loss)"} stopOpacity={0.3} />
                      <stop offset="100%" stopColor={up ? "var(--gain)" : "var(--loss)"} stopOpacity={0} />
                    </linearGradient>
                  </defs>
                  <CartesianGrid vertical={false} stroke="var(--border)" strokeDasharray="3 3" />
                  <XAxis dataKey="date" tickLine={false} axisLine={false} minTickGap={40}
                         tick={{ fontSize: 11, fill: "var(--muted-foreground)" }}
                         tickFormatter={(d: string) => new Date(d).toLocaleDateString("en-GB", { day: "numeric", month: "short" })} />
                  <YAxis domain={["auto", "auto"]} tickLine={false} axisLine={false} width={56}
                         tick={{ fontSize: 11, fill: "var(--muted-foreground)" }} />
                  <Tooltip cursor={{ stroke: "var(--border)" }}
                    contentStyle={{ background: "var(--popover)", border: "1px solid var(--border)", borderRadius: 8, fontSize: 12 }}
                    labelFormatter={(d) => new Date(String(d)).toDateString()}
                    formatter={(v) => [price(Number(v)), "Close"]} />
                  <Area type="monotone" dataKey="close" stroke={up ? "var(--gain)" : "var(--loss)"}
                        strokeWidth={2} fill="url(#fill)" isAnimationActive={false} />
                </AreaChart>
              </ResponsiveContainer>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Your demo position</CardTitle>
            <CardDescription>Test money, real prices</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3 text-sm">
            {pos ? (
              <>
                <Row k="Shares" v={num(pos.quantity)} />
                <Row k="Average cost" v={price(pos.avg_cost)} />
                <Row k="Value now" v={money(last ? last * pos.quantity : pos.market_value)} />
                <Row k="Gain / loss" v={<span className={tone(pnl)}>{pnl != null && pnl > 0 ? "+" : ""}{money(pnl)}</span>} />
                <Row k="Share of account" v={pct(pos.weight_pct, false)} />
              </>
            ) : <p className="text-muted-foreground">You don&apos;t hold {sym}. Cash available: {money(data.cash)}.</p>}
            <div className="space-y-1 border-t pt-3">
              <div className="text-xs font-medium text-muted-foreground">Price alerts</div>
              <AlertList alerts={data.alerts.filter((a) => a.status === "active")} />
            </div>
          </CardContent>
        </Card>
      </div>

      {!b ? (
        <Card><CardContent className="py-10 text-center text-sm text-muted-foreground">
          The desk hasn&apos;t researched {sym} yet. Press <b>Research now</b> and it takes about a minute.
        </CardContent></Card>
      ) : (
        <>
          <Card>
            <CardHeader>
              <CardTitle>The desk&apos;s view</CardTitle>
              <CardDescription>Researched {ago(b.finished_at)} · {b.verification.verified}/{b.verification.checked} numbers checked against the data</CardDescription>
            </CardHeader>
            <CardContent className="space-y-5">
              <p className="text-base leading-relaxed">{b.brief.thesis}</p>
              {b.lean?.stance && (
                <div className="rounded-lg border bg-muted/40 p-3 text-sm">
                  <div className="text-xs font-medium text-muted-foreground">How the desk decided</div>
                  <p className="mt-1">
                    The analysts and the debate pointed <span className="font-medium">{b.lean.stance}</span>
                    {" "}<span className="text-muted-foreground">({b.lean.reason})</span>.{" "}
                    {b.lean.stance === b.brief.stance
                      ? "The head of research agreed."
                      : b.lean.override_reason
                        ? <>The head of research chose <span className="font-medium">{b.brief.stance}</span> instead: {b.lean.override_reason}</>
                        : <>The head of research chose <span className="font-medium">{b.brief.stance}</span> without giving a reason.</>}
                  </p>
                </div>
              )}
              <div className="flex flex-wrap items-center gap-6">
                <div className="space-y-1"><div className="text-xs text-muted-foreground">Confidence</div>
                  <ConfidenceMeter value={b.risk.confidence_after} /></div>
                <div className="space-y-1"><div className="text-xs text-muted-foreground">Risk rules</div>
                  <RiskBadge verdict={b.risk.verdict} /></div>
                {b.proposal && <div className="space-y-1"><div className="text-xs text-muted-foreground">Suggested trade</div>
                  <span className="text-sm">{b.proposal.action === "none" ? "None" : b.proposal.action}</span></div>}
              </div>
              <div className="grid gap-5 md:grid-cols-2">
                <ClaimList title="Key points" items={b.brief.key_points.map((c) => c.text)} icon={<CheckCircle2 className="size-4 text-gain" />} />
                <ClaimList title="Risks" items={b.brief.risks.map((c) => c.text)} icon={<TriangleAlert className="size-4 text-amber-500" />} />
              </div>
              {b.risk.flags.length > 0 && (
                <div className="space-y-2 rounded-lg border bg-muted/40 p-3">
                  <div className="text-xs font-medium text-muted-foreground">Risk rule flags</div>
                  {b.risk.flags.map((f, i) => <div key={i} className="text-sm">
                    <Badge variant="outline" className="mr-2">{f.severity}</Badge>{f.detail}</div>)}
                </div>
              )}
            </CardContent>
          </Card>

          <div className="grid gap-6 md:grid-cols-2">
            {b.cases.map((c) => (
              <Card key={c.side} className={c.side === "bull" ? "border-gain/20" : "border-loss/20"}>
                <CardHeader>
                  <CardTitle className="flex items-center gap-2">
                    {c.side === "bull" ? <ThumbsUp className="size-4 text-gain" /> : <ThumbsDown className="size-4 text-loss" />}
                    The {c.side === "bull" ? "case for" : "case against"}
                  </CardTitle>
                  <CardDescription>{c.side === "bull" ? "Bull" : "Bear"} agent · {Math.round(c.confidence * 100)}% confident</CardDescription>
                </CardHeader>
                <CardContent className="space-y-3 text-sm">
                  <p>{c.thesis}</p>
                  <ul className="list-disc space-y-1 pl-5 text-muted-foreground">
                    {c.arguments.map((a, i) => <li key={i}>{a.text}</li>)}</ul>
                  {c.concession && <p className="text-xs text-muted-foreground"><b>Concedes:</b> {c.concession}</p>}
                </CardContent>
              </Card>
            ))}
          </div>

          <Card>
            <CardHeader><CardTitle>What each analyst found</CardTitle></CardHeader>
            <CardContent className="grid gap-4 md:grid-cols-3">
              {b.reports.map((r) => (
                <div key={r.agent} className="space-y-2 rounded-lg border p-3">
                  <div className="flex items-center justify-between">
                    <span className="text-sm font-medium">{AGENT_LABEL[r.agent] ?? r.agent}</span>
                    <StanceBadge stance={r.stance} />
                  </div>
                  <p className="text-sm text-muted-foreground">{r.summary}</p>
                </div>
              ))}
            </CardContent>
          </Card>

          {b.prior_views.length > 0 && (
            <p className="text-xs text-muted-foreground">Earlier calls: {b.prior_views.map((v) =>
              `${v.stance} ${Math.round(v.confidence * 100)}% (${ago(v.as_of)})`).join(" · ")}</p>
          )}
        </>
      )}
    </div>
  )
}

function Row({ k, v }: { k: string; v: React.ReactNode }) {
  return <div className="flex justify-between"><span className="text-muted-foreground">{k}</span><span className="tabular font-medium">{v}</span></div>
}

function ClaimList({ title, items, icon }: { title: string; items: string[]; icon: React.ReactNode }) {
  return (
    <div className="space-y-2">
      <div className="text-sm font-medium">{title}</div>
      <ul className="space-y-2">
        {items.map((t, i) => <li key={i} className="flex gap-2 text-sm text-muted-foreground"><span className="mt-0.5 shrink-0">{icon}</span>{t}</li>)}
        {items.length === 0 && <li className="text-sm text-muted-foreground">None.</li>}
      </ul>
    </div>
  )
}
