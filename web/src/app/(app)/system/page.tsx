"use client"

import useSWR from "swr"
import { ShieldCheck, ShieldX } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Progress } from "@/components/ui/progress"
import { Skeleton } from "@/components/ui/skeleton"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { num, who } from "@/lib/format"
import type { SystemData } from "@/lib/types"

const SKILL_LABEL: Record<string, string> = {
  technical_analysis: "Reads price trends", fundamental_analysis: "Reads valuation",
  news_analysis: "Reads company announcements", bull_case: "Argues the case for",
  bear_case: "Argues the case against", judgement: "Weighs both sides and decides",
  propose_trade: "Turns a strong call into a suggested trade", chat: "Advisor chat",
}

export default function SystemPage() {
  const { data } = useSWR<SystemData>("/api/system", { refreshInterval: 15000 })
  if (!data) return <div className="grid gap-4"><Skeleton className="h-40" /><Skeleton className="h-80" /></div>

  const check = "ok" in data.audit_check ? data.audit_check : null
  const audit = Array.isArray(data.audit) ? data.audit : []

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Under the hood</h1>
        <p className="text-sm text-muted-foreground">The AI agents behind the desk, how much of today&apos;s free AI budget is used, and a tamper-proof record of every action.</p>
      </div>

      <div className="grid gap-6 lg:grid-cols-2">
        <Card>
          <CardHeader><CardTitle>Agents</CardTitle><CardDescription>Each runs as its own service and proves who it is on every call</CardDescription></CardHeader>
          <CardContent className="space-y-2">
            {data.agents.skills?.map((s) => (
              <div key={s.skill} className="flex items-center justify-between rounded-lg border px-3 py-2 text-sm">
                <div><div className="font-medium">{s.agent}</div><div className="text-xs text-muted-foreground">{SKILL_LABEL[s.skill] ?? s.skill}</div></div>
                <Badge variant="outline" className="gap-1.5"><span className="size-1.5 rounded-full bg-gain" />online · v{s.version}</Badge>
              </div>
            ))}
            {Object.keys(data.agents.unreachable ?? {}).map((u) => (
              <div key={u} className="flex items-center justify-between rounded-lg border border-loss/30 px-3 py-2 text-sm">
                <span>{u}</span><Badge variant="outline" className="text-loss">unreachable</Badge></div>
            ))}
            {data.agents.error && <p className="text-sm text-loss">{data.agents.error}</p>}
          </CardContent>
        </Card>

        <Card>
          <CardHeader><CardTitle>Today&apos;s AI budget</CardTitle><CardDescription>Free Groq tier, per model. Resets daily.</CardDescription></CardHeader>
          <CardContent className="space-y-5">
            {data.budget.models?.map((m) => {
              const used = m.tokens_per_day ? (m.tokens_today / m.tokens_per_day) * 100 : 0
              return (
                <div key={m.model} className="space-y-1.5">
                  <div className="flex justify-between text-sm">
                    <span className="font-medium">{m.model}</span>
                    <span className="text-xs text-muted-foreground tabular">{num(m.tokens_today)} / {num(m.tokens_per_day)} tokens
                      {m.circuit !== "closed" && <Badge variant="outline" className="ml-2 text-loss">{m.circuit}</Badge>}</span>
                  </div>
                  <Progress value={Math.min(used, 100)} />
                  <div className="text-xs text-muted-foreground tabular">{m.requests_today} of {m.requests_per_day} requests</div>
                </div>
              )
            })}
            {data.budget.error && <p className="text-sm text-loss">{data.budget.error}</p>}
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            {check?.ok ? <ShieldCheck className="size-5 text-gain" /> : <ShieldX className="size-5 text-loss" />}
            Audit record
          </CardTitle>
          <CardDescription>
            {check ? (check.ok ? `All ${check.checked} entries verified. Each one is chained to the last, so any edit would show.`
              : `Chain broken at entry ${check.first_bad_id}.`) : "Could not verify the record."}
          </CardDescription>
        </CardHeader>
        <CardContent className="px-0">
          <Table>
            <TableHeader><TableRow>
              <TableHead className="pl-6">#</TableHead><TableHead>When</TableHead><TableHead>Who</TableHead>
              <TableHead>What</TableHead><TableHead className="pr-6">Fingerprint</TableHead>
            </TableRow></TableHeader>
            <TableBody>
              {audit.map((a) => (
                <TableRow key={a.id}>
                  <TableCell className="pl-6 tabular text-muted-foreground">{a.id}</TableCell>
                  <TableCell className="text-muted-foreground">{new Date(a.at).toLocaleString()}</TableCell>
                  <TableCell>{who(a.actor)}</TableCell>
                  <TableCell><Badge variant="outline" className="font-mono text-xs">{a.action}</Badge></TableCell>
                  <TableCell className="pr-6 font-mono text-xs text-muted-foreground">{a.hash.slice(0, 12)}…</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
          {audit.length === 0 && <p className="px-6 pt-4 text-sm text-muted-foreground">No actions recorded yet.</p>}
        </CardContent>
      </Card>
    </div>
  )
}
