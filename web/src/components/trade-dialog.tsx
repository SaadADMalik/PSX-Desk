"use client"

import { useMemo, useState } from "react"
import { toast } from "sonner"
import { useSWRConfig } from "swr"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
         DialogTrigger } from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Textarea } from "@/components/ui/textarea"
import { cn } from "@/lib/utils"
import { post } from "@/lib/api"
import { money, price as fmtPrice } from "@/lib/format"
import { useLivePrice } from "@/lib/live"
import type { Decision } from "@/lib/types"

export function TradeDialog({ symbol, held = 0, cash, defaultSide = "buy", trigger }: {
  symbol: string; held?: number; cash?: number; defaultSide?: "buy" | "sell";
  trigger: React.ReactElement
}) {
  const [open, setOpen] = useState(false)
  const [side, setSide] = useState<"buy" | "sell">(defaultSide)
  const [qty, setQty] = useState("")
  const [note, setNote] = useState("")
  const [busy, setBusy] = useState(false)
  const { mutate } = useSWRConfig()
  const { price } = useLivePrice(symbol)
  const orderId = useMemo(() => crypto.randomUUID(), [open]) // eslint-disable-line react-hooks/exhaustive-deps
  const n = Number.parseInt(qty || "0", 10)
  const value = price && n > 0 ? n * price : null

  async function submit() {
    setBusy(true)
    try {
      const d = await post<Decision>("/api/trades", { symbol, side, quantity: n, note,
                                                     client_order_id: orderId })
      if (d.fill) {
        toast.success(`${side === "buy" ? "Bought" : "Sold"} ${n} ${symbol} (demo)`,
          { description: `at ${fmtPrice(d.fill.price)} · fees ${money(d.fill.fees, true)}` })
        setOpen(false); setQty(""); setNote("")
      } else {
        toast.error("The broker refused this trade", { description: d.reason ?? d.proposal.status })
      }
      mutate((k) => typeof k === "string" && k.startsWith("/api/"))
    } catch (e) {
      toast.error("Trade failed", { description: (e as Error).message })
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger render={trigger} />
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Demo trade · {symbol}</DialogTitle>
          <DialogDescription>
            Test money at the live PSX price. Fees (0.15%) and a small slippage are simulated.
          </DialogDescription>
        </DialogHeader>
        <div className="grid grid-cols-2 gap-1 rounded-lg bg-muted p-1">
          {(["buy", "sell"] as const).map((s) => (
            <button key={s} onClick={() => setSide(s)} disabled={s === "sell" && held === 0}
              className={cn("rounded-md py-1.5 text-sm font-medium capitalize transition disabled:opacity-40",
                side === s ? (s === "buy" ? "bg-gain text-background" : "bg-loss text-background")
                           : "text-muted-foreground hover:text-foreground")}>
              {s}
            </button>
          ))}
        </div>
        <div className="grid gap-2">
          <Label htmlFor="qty">Shares</Label>
          <Input id="qty" inputMode="numeric" value={qty} autoFocus
                 onChange={(e) => setQty(e.target.value.replace(/\D/g, ""))} placeholder="e.g. 100" />
          <div className="flex justify-between text-xs text-muted-foreground">
            <span>Price {fmtPrice(price)} · {side === "sell" ? `you hold ${held}` : `cash ${money(cash)}`}</span>
            <span className="tabular">≈ {money(value)}</span>
          </div>
        </div>
        <div className="grid gap-2">
          <Label htmlFor="note">Why? <span className="text-muted-foreground">(optional, for your own record)</span></Label>
          <Textarea id="note" value={note} onChange={(e) => setNote(e.target.value)} rows={2}
                    placeholder="e.g. cement demand recovering" />
        </div>
        <DialogFooter>
          <Button disabled={busy || n < 1 || !price} onClick={submit}
                  className={side === "sell" ? "bg-loss hover:bg-loss/85" : ""}>
            {busy ? "Placing…" : `${side === "buy" ? "Buy" : "Sell"} ${n > 0 ? n : ""} ${symbol}`}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
