"use client"

import { useEffect, useRef, useState } from "react"
import { Line, LineChart, ResponsiveContainer, YAxis } from "recharts"
import { TrendingDown, TrendingUp, Minus, ShieldCheck, ShieldAlert, ShieldX } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { cn } from "@/lib/utils"
import { pct, price as fmtPrice, tone } from "@/lib/format"
import type { Stance } from "@/lib/types"

export function StanceBadge({ stance, confidence, className }:
  { stance?: Stance | null; confidence?: number | null; className?: string }) {
  if (!stance) return <Badge variant="outline" className={cn("text-muted-foreground", className)}>
    No research yet</Badge>
  const Icon = stance === "bullish" ? TrendingUp : stance === "bearish" ? TrendingDown : Minus
  const color = stance === "bullish" ? "bg-gain/15 text-gain border-gain/25"
    : stance === "bearish" ? "bg-loss/15 text-loss border-loss/25"
    : "bg-muted text-muted-foreground border-border"
  return (
    <Badge variant="outline" className={cn("gap-1 font-medium capitalize", color, className)}>
      <Icon className="size-3" />
      {stance}
      {confidence != null && <span className="opacity-70 tabular">· {Math.round(confidence * 100)}%</span>}
    </Badge>
  )
}

export function RiskBadge({ verdict }: { verdict?: string | null }) {
  if (!verdict) return null
  const map = {
    pass: { Icon: ShieldCheck, cls: "text-gain", label: "Risk checks passed" },
    caution: { Icon: ShieldAlert, cls: "text-amber-500", label: "Caution" },
    veto: { Icon: ShieldX, cls: "text-loss", label: "Vetoed by risk rules" },
  } as const
  const m = map[verdict as keyof typeof map] ?? map.caution
  return (
    <span className={cn("inline-flex items-center gap-1 text-xs", m.cls)}>
      <m.Icon className="size-3.5" />{m.label}
    </span>
  )
}

export function Sparkline({ data, className }: { data: number[]; className?: string }) {
  if (data.length < 2) return <div className={cn("h-8 w-24", className)} />
  const up = data[data.length - 1] >= data[0]
  const points = data.map((v, i) => ({ i, v }))
  return (
    <div className={cn("h-8 w-24", className)}>
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={points}>
          <YAxis hide domain={["dataMin", "dataMax"]} />
          <Line type="monotone" dataKey="v" dot={false} strokeWidth={1.6}
                stroke={up ? "var(--gain)" : "var(--loss)"} isAnimationActive={false} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  )
}

/** A price that flashes green or red when it changes. */
export function LivePrice({ value, className }: { value: number | null; className?: string }) {
  const prev = useRef(value)
  const [flash, setFlash] = useState<"" | "flash-up" | "flash-down">("")
  useEffect(() => {
    if (value != null && prev.current != null && value !== prev.current) {
      setFlash(value > prev.current ? "flash-up" : "flash-down")
      const t = setTimeout(() => setFlash(""), 1200)
      prev.current = value
      return () => clearTimeout(t)
    }
    prev.current = value
  }, [value])
  return <span className={cn("rounded px-1 -mx-1 tabular", flash, className)}>{fmtPrice(value)}</span>
}

export function Change({ value, className }: { value: number | null; className?: string }) {
  return <span className={cn("tabular", tone(value), className)}>{pct(value)}</span>
}

export function ConfidenceMeter({ value }: { value: number }) {
  return (
    <div className="flex items-center gap-2">
      <div className="h-1.5 w-24 overflow-hidden rounded-full bg-muted">
        <div className="h-full rounded-full bg-primary transition-all" style={{ width: `${value * 100}%` }} />
      </div>
      <span className="text-xs tabular text-muted-foreground">{Math.round(value * 100)}%</span>
    </div>
  )
}

export function LiveDot({ on, label }: { on: boolean; label: string }) {
  return (
    <span className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
      <span className="relative flex size-2">
        {on && <span className="absolute inline-flex size-full animate-ping rounded-full bg-gain opacity-60" />}
        <span className={cn("relative inline-flex size-2 rounded-full", on ? "bg-gain" : "bg-muted-foreground/50")} />
      </span>
      {label}
    </span>
  )
}
