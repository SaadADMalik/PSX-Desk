"use client"

import { useEffect, useState } from "react"
import { toast } from "sonner"
import { useSWRConfig } from "swr"
import { Loader2, Sparkles } from "lucide-react"
import { Button } from "@/components/ui/button"
import { fetcher, post } from "@/lib/api"

const LABELS: Record<string, string> = {
  technical_analysis: "price trends", fundamental_analysis: "valuation",
  news_analysis: "announcements", bull_case: "bull case", bear_case: "bear case",
  judgement: "weighing both sides", verification: "checking every number",
  risk_gate: "risk checks", remember: "saving", proposal: "sizing a trade",
}

/** Starts a fresh multi-agent brief and shows which step it is on. */
export function ResearchButton({ symbol, variant = "outline", size = "sm" }:
  { symbol: string; variant?: "outline" | "default" | "ghost"; size?: "sm" | "default" }) {
  const [run, setRun] = useState<string | null>(null)
  const [step, setStep] = useState("starting")
  const { mutate } = useSWRConfig()

  useEffect(() => {
    if (!run) return
    const t = setInterval(async () => {
      try {
        const r = await fetcher<{ status: string; steps: Record<string, string>; error?: string }>(
          `/api/runs/${run}`)
        const current = Object.entries(r.steps).find(([, s]) => s === "running")?.[0]
        if (current) setStep(LABELS[current] ?? current)
        if (r.status === "succeeded" || r.status === "failed") {
          clearInterval(t)
          setRun(null)
          if (r.status === "failed") toast.error(`Research on ${symbol} failed`, { description: r.error })
          mutate((k) => typeof k === "string" && k.startsWith("/api/"))
        }
      } catch {}
    }, 2000)
    return () => clearInterval(t)
  }, [run, symbol, mutate])

  async function start() {
    try {
      const r = await post<{ run_id: string }>("/api/research", { symbol })
      setRun(r.run_id)
      setStep("starting")
      toast(`Researching ${symbol}`, { description: "Five agents are on it. Usually 1–3 minutes." })
    } catch (e) {
      toast.error("Could not start research", { description: (e as Error).message })
    }
  }

  return (
    <Button variant={variant} size={size} onClick={start} disabled={!!run}>
      {run ? <Loader2 className="animate-spin" /> : <Sparkles />}
      {run ? `Researching: ${step}…` : "Research now"}
    </Button>
  )
}
