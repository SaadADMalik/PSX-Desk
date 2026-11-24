"use client"

import { useState } from "react"
import { BellRing, X } from "lucide-react"
import { toast } from "sonner"
import { useSWRConfig } from "swr"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
         DialogTrigger } from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { send } from "@/lib/api"
import { ago, money, price as fmtPrice } from "@/lib/format"
import { cn } from "@/lib/utils"
import type { AlertRule, Position } from "@/lib/types"

const refresh = (k: unknown) => typeof k === "string" &&
  (k.startsWith("/api/overview") || k.startsWith("/api/stocks") || k.startsWith("/api/alerts"))

/** Set a price alert: a fall in percent (any stock) or in rupees (a stock you hold),
 *  for a number of days or with no end. */
export function AlertDialog({ symbol, position, livePrice, trigger }: {
  symbol: string; position: Position | null; livePrice: number | null
  trigger: React.ReactElement
}) {
  const [open, setOpen] = useState(false)
  const [kind, setKind] = useState<"pct" | "pkr">("pct")
  const [threshold, setThreshold] = useState("")
  const [days, setDays] = useState("")
  const [busy, setBusy] = useState(false)
  const { mutate } = useSWRConfig()

  const ref = position ? position.avg_cost : livePrice
  const t = Number.parseFloat(threshold)
  const d = days ? Number.parseInt(days, 10) : null
  const valid = t > 0 && (kind === "pct" ? t <= 90 : true) && (d === null || (d >= 1 && d <= 3650))
  const line = ref && t > 0
    ? kind === "pct" ? ref * (1 - t / 100)
      : position ? ref - t / position.quantity : null
    : null

  async function submit() {
    setBusy(true)
    try {
      await send("POST", "/api/alerts", { symbol, kind, threshold: t, days: d })
      toast.success(`Alert set on ${symbol}`, {
        description: `${describe(kind, t)}${d ? `, for ${d} days` : ", until you cancel it"}.` })
      setOpen(false); setThreshold(""); setDays("")
      mutate(refresh)
    } catch (e) {
      toast.error("Couldn't set the alert", { description: (e as Error).message })
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger render={trigger} />
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Price alert · {symbol}</DialogTitle>
          <DialogDescription>
            {position
              ? <>Measured from your average cost, {fmtPrice(position.avg_cost)} a share.</>
              : <>Measured from today&apos;s price, {fmtPrice(livePrice)}.</>}{" "}
            It alerts you once when the price falls past your line, and again only after it
            has recovered and falls again. You also get fresh research on {symbol}.
          </DialogDescription>
        </DialogHeader>
        <div className="grid grid-cols-2 gap-1 rounded-lg bg-muted p-1">
          {([["pct", "Percent fall"], ["pkr", "Rupees lost"]] as const).map(([k, label]) => (
            <button key={k} onClick={() => setKind(k)} disabled={k === "pkr" && !position}
              title={k === "pkr" && !position ? `Needs a position: you don't hold ${symbol}` : undefined}
              className={cn("rounded-md py-1.5 text-sm font-medium transition disabled:opacity-40",
                kind === k ? "bg-background text-foreground shadow-sm"
                           : "text-muted-foreground hover:text-foreground")}>
              {label}
            </button>
          ))}
        </div>
        <div className="grid gap-2">
          <Label htmlFor="threshold">
            {kind === "pct" ? "Alert me if it falls by (%)" : "Alert me if I lose (Rs)"}</Label>
          <Input id="threshold" inputMode="decimal" value={threshold} autoFocus
                 placeholder={kind === "pct" ? "e.g. 5" : "e.g. 10000"}
                 onChange={(e) => setThreshold(e.target.value.replace(/[^\d.]/g, ""))} />
          {line != null && line > 0 && (
            <p className="text-xs text-muted-foreground tabular">
              Alerts at about {fmtPrice(line)} a share
              {kind === "pkr" && position ? ` (${position.quantity} shares)` : ""}.</p>
          )}
        </div>
        <div className="grid gap-2">
          <Label htmlFor="days">For how many days? <span className="text-muted-foreground">
            (leave empty to watch until you cancel)</span></Label>
          <Input id="days" inputMode="numeric" value={days} placeholder="no end"
                 onChange={(e) => setDays(e.target.value.replace(/\D/g, ""))} />
        </div>
        <DialogFooter>
          <Button disabled={busy || !valid} onClick={submit}><BellRing />
            {busy ? "Setting…" : "Set alert"}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

export function describe(kind: "pct" | "pkr", threshold: number) {
  return kind === "pct" ? `falls ${threshold}%` : `loses ${money(threshold)}`
}

/** One line per alert, with its state and a cancel button. */
export function AlertList({ alerts, showSymbol = false }: { alerts: AlertRule[]; showSymbol?: boolean }) {
  const { mutate } = useSWRConfig()
  const [busy, setBusy] = useState<string | null>(null)

  async function cancel(a: AlertRule) {
    setBusy(a.id)
    try {
      await send("DELETE", `/api/alerts/${a.id}`)
      toast.success(`Alert on ${a.symbol} cancelled`)
      mutate(refresh)
    } catch (e) {
      toast.error("Couldn't cancel the alert", { description: (e as Error).message })
    } finally {
      setBusy(null)
    }
  }

  if (!alerts.length) return <p className="text-sm text-muted-foreground">No alerts set.</p>
  return (
    <ul className="divide-y">
      {alerts.map((a) => (
        <li key={a.id} className="flex items-center justify-between gap-3 py-2.5 text-sm">
          <div className="min-w-0 space-y-0.5">
            <div className="flex flex-wrap items-center gap-2">
              {showSymbol && <span className="font-semibold">{a.symbol}</span>}
              <span>{describe(a.kind, a.threshold)} from {fmtPrice(a.ref_price)}</span>
              <AlertState a={a} />
            </div>
            <div className="text-xs text-muted-foreground">
              {a.ref_basis === "avg_cost" ? "your average cost" : "price when set"} ·{" "}
              {a.ends_at ? `until ${new Date(a.ends_at).toLocaleDateString("en-GB",
                { day: "numeric", month: "short", year: "numeric" })}` : "no end"}
              {a.last_checked_at && ` · checked ${ago(a.last_checked_at)} at ${fmtPrice(a.last_price)}`}
              {a.times_triggered > 0 && ` · fired ${a.times_triggered}×`}
            </div>
          </div>
          {a.status === "active" && (
            <Button size="icon-sm" variant="ghost" disabled={busy === a.id} onClick={() => cancel(a)}
                    aria-label={`Cancel alert on ${a.symbol}`} title="Cancel this alert"><X /></Button>
          )}
        </li>
      ))}
    </ul>
  )
}

function AlertState({ a }: { a: AlertRule }) {
  if (a.status !== "active") return <Badge variant="outline">{a.status}</Badge>
  return a.state === "triggered"
    ? <Badge className="bg-loss/15 text-loss">triggered</Badge>
    : <Badge variant="secondary">watching</Badge>
}
