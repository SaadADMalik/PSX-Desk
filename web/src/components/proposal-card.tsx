"use client"

import { useState } from "react"
import Link from "next/link"
import { toast } from "sonner"
import { useSWRConfig } from "swr"
import { Check, X, Sparkles, Bot, User } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Badge } from "@/components/ui/badge"
import { StanceBadge } from "@/components/desk"
import { post } from "@/lib/api"
import { ago, money, price as fmtPrice, who } from "@/lib/format"
import type { Decision, Proposal, Stance } from "@/lib/types"

const STATUS: Record<string, string> = {
  pending: "Waiting for you", filled: "Done (demo)", rejected: "Rejected",
  expired: "Expired", failed: "Refused by broker",
}

export function ProposalCard({ p, compact = false }: { p: Proposal; compact?: boolean }) {
  const [note, setNote] = useState("")
  const [busy, setBusy] = useState<"" | "yes" | "no">("")
  const { mutate } = useSWRConfig()
  const b = p.brief as { stance?: Stance; confidence?: number; thesis?: string; source?: string;
                         manual?: boolean; flags?: string[] }
  const Src = p.proposed_by === "advisor-agent" ? Bot : b.manual ? User : Sparkles

  async function decide(approve: boolean) {
    setBusy(approve ? "yes" : "no")
    try {
      const d = await post<Decision>(`/api/proposals/${p.id}/decide`, { approve, note })
      if (d.fill) toast.success(`Done: ${p.side} ${p.quantity} ${p.symbol} (demo)`,
                                { description: `filled at ${fmtPrice(d.fill.price)}` })
      else if (approve) toast.error("Not filled", { description: d.reason ?? d.proposal.status })
      else toast(`Rejected ${p.symbol}`)
      mutate((k) => typeof k === "string" && k.startsWith("/api/"))
    } catch (e) {
      toast.error("Could not record the decision", { description: (e as Error).message })
    } finally {
      setBusy("")
    }
  }

  const failed = p.checks.filter((c) => !c.ok)
  return (
    <Card size="sm" className="overflow-hidden">
      <CardContent className="space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <span className={p.side === "buy" ? "font-semibold text-gain" : "font-semibold text-loss"}>
              {p.side === "buy" ? "Buy" : "Sell"}</span>
            <span className="font-semibold tabular">{p.quantity.toLocaleString()}</span>
            <Link href={`/stocks/${p.symbol}`} className="font-semibold hover:underline">{p.symbol}</Link>
            <span className="text-sm text-muted-foreground tabular">
              ≈ {money(p.quantity * p.ref_price)} at {fmtPrice(p.ref_price)}</span>
          </div>
          <Badge variant={p.status === "pending" ? "default" : "outline"}>{STATUS[p.status]}</Badge>
        </div>
        <div className="flex items-start gap-2 text-sm">
          <Src className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
          <p className="text-muted-foreground"><span className="text-foreground">{who(p.proposed_by)}</span>
            {" · "}{ago(p.created_at)} — {p.rationale}</p>
        </div>
        {!compact && b.stance && (
          <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            <StanceBadge stance={b.stance} confidence={b.confidence} />
            {b.thesis && <span className="line-clamp-2">{b.thesis}</span>}
          </div>
        )}
        {failed.length > 0 && (
          <ul className="space-y-0.5 text-xs text-loss">
            {failed.map((c) => <li key={c.rule}>✕ {c.detail}</li>)}
          </ul>
        )}
        {p.status === "pending" ? (
          <div className="flex flex-col gap-2 sm:flex-row">
            <Input value={note} onChange={(e) => setNote(e.target.value)} placeholder="Note (optional)"
                   className="h-8 sm:max-w-xs" />
            <div className="flex gap-2">
              <Button size="sm" disabled={!!busy} onClick={() => decide(true)}>
                <Check />{busy === "yes" ? "Placing…" : "Approve demo trade"}</Button>
              <Button size="sm" variant="outline" disabled={!!busy} onClick={() => decide(false)}>
                <X />Reject</Button>
            </div>
          </div>
        ) : p.decided_by && (
          <p className="text-xs text-muted-foreground">
            {STATUS[p.status]} by {who(p.decided_by)} {ago(p.decided_at)}
            {p.decision_note ? ` — ${p.decision_note}` : ""}</p>
        )}
      </CardContent>
    </Card>
  )
}
