"use client"

import { useEffect, useRef, useState } from "react"
import Link from "next/link"
import ReactMarkdown from "react-markdown"
import remarkGfm from "remark-gfm"
import { ArrowUp, Bot, Check, Globe, Loader2, RotateCcw, Sparkles, Square, Wrench } from "lucide-react"
import { useSWRConfig } from "swr"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { Textarea } from "@/components/ui/textarea"
import { streamPost } from "@/lib/api"
import { cn } from "@/lib/utils"
import type { ChatMsg, ToolStep } from "@/lib/types"

const KEY = "psx-desk-chat"
const SUGGESTIONS = [
  "How is my demo account doing?",
  "What does the desk think of my watchlist today?",
  "What does Meezan Bank charge for ATM withdrawals at other banks?",
  "What did SBP decide at its latest monetary policy meeting?",
]

/** gpt-oss tends to cite as 【https://…】 whatever the prompt says; show those as links. */
function linkify(md: string): string {
  return md.replace(/【\s*(https?:\/\/[^】\s]+)\s*】/g, (_, u: string) => ` ([${host(u) || "source"}](${u}))`)
           .replace(/【[^】]*】/g, "")
}

function host(url: unknown): string {
  try { return new URL(String(url)).hostname.replace(/^www\./, "") } catch { return "" }
}

/** What a tool step looks like to a person, e.g. "Searching official sources for ATM charges". */
function describe(s: ToolStep): string {
  const a = s.args ?? {}
  const sym = a.symbol ? ` ${String(a.symbol).toUpperCase()}` : ""
  switch (s.name) {
    case "search_official": return `Searched official sources for “${a.query ?? ""}”`
    case "read_official": return `Read ${host(a.url) || "an official page"}`
    case "desk_view": return "Read the desk's research"
    case "market_snapshot": return `Checked market data for${sym}`
    case "recent_news": return `Checked announcements for${sym}`
    case "live_quotes": return "Checked live prices"
    case "my_portfolio": return "Looked at your demo account"
    case "past_views": return `Checked past calls on${sym}`
    case "research": return `Asked the desk to research${sym}`
    case "suggest_trade": return "Suggested a demo trade"
    default: return s.name
  }
}

function Steps({ steps, live }: { steps: ToolStep[]; live: boolean }) {
  if (!steps.length) return null
  return (
    <div className="space-y-1 border-l-2 border-border pl-3">
      {steps.map((s, i) => {
        const running = live && i === steps.length - 1
        const web = s.name === "search_official" || s.name === "read_official"
        const Icon = running ? Loader2 : web ? Globe : s.name === "suggest_trade" ? Wrench : Check
        return (
          <div key={i} className="flex items-center gap-2 text-xs text-muted-foreground">
            <Icon className={cn("size-3.5 shrink-0", running && "animate-spin", web && !running && "text-primary")} />
            <span className="truncate">{describe(s)}</span>
          </div>
        )
      })}
    </div>
  )
}

export default function AdvisorPage() {
  const [messages, setMessages] = useState<ChatMsg[]>([])
  const [input, setInput] = useState("")
  const [busy, setBusy] = useState(false)
  const endRef = useRef<HTMLDivElement>(null)
  const abortRef = useRef<AbortController | null>(null)
  const { mutate } = useSWRConfig()

  useEffect(() => {
    try {
      const saved: ChatMsg[] = JSON.parse(localStorage.getItem(KEY) ?? "[]")
      // Restored after mount on purpose: reading localStorage during render would make
      // the prerendered HTML and the first client render disagree (hydration error).
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setMessages(saved.map((m) => ({ ...m, streaming: false })))
    } catch { /* ignore */ }
  }, [])
  useEffect(() => {
    if (!busy) localStorage.setItem(KEY, JSON.stringify(messages.slice(-40)))
    endRef.current?.scrollIntoView({ behavior: busy ? "auto" : "smooth", block: "end" })
  }, [messages, busy])

  /** Apply a change to the reply being written (always the last message). */
  function patch(fn: (m: ChatMsg) => ChatMsg) {
    setMessages((ms) => [...ms.slice(0, -1), fn(ms[ms.length - 1])])
  }

  // Smooth typing. The model writes far faster than people read, and text arrives in
  // bursts. Received text goes into `target`; a frame loop reveals it a few characters
  // at a time, faster when there is a backlog, so it reads like someone typing.
  const target = useRef("")
  const shown = useRef("")
  const finished = useRef<null | (() => void)>(null)
  const frame = useRef<number | null>(null)

  function tick() {
    const t = target.current
    let s = shown.current
    if (!t.startsWith(s)) s = t.slice(0, Math.min(s.length, t.length))
    if (s.length < t.length) {
      const step = Math.max(1, Math.ceil((t.length - s.length) / 30))
      shown.current = t.slice(0, s.length + step)
      const text = shown.current
      patch((m) => ({ ...m, content: text }))
      frame.current = requestAnimationFrame(tick)
      return
    }
    frame.current = null
    if (finished.current) {
      const done = finished.current
      finished.current = null
      done()
    }
  }

  function reveal(text: string) {
    target.current = text
    if (frame.current == null) frame.current = requestAnimationFrame(tick)
  }

  /** Runs once the typing has caught up, so the reply never jumps ahead of the cursor. */
  function whenTyped(fn: () => void) {
    finished.current = fn
    if (frame.current == null) tick()
  }

  async function send(text: string) {
    const t = text.trim()
    if (!t || busy) return
    const history: ChatMsg[] = [...messages, { role: "user", content: t }]
    setMessages([...history, { role: "assistant", content: "", steps: [], streaming: true }])
    setInput("")
    setBusy(true)
    target.current = ""
    shown.current = ""
    const ctrl = new AbortController()
    abortRef.current = ctrl
    let final: Partial<ChatMsg> = {}
    try {
      await streamPost("/api/chat/stream", {
        messages: history.map(({ role, content }) => ({ role, content: content.slice(0, 4000) })),
      }, (e) => {
        switch (e.type) {
          case "delta":
            reveal(target.current + String(e.text))
            break
          case "discard": // text that was only a lead-in to a tool call
            target.current = ""
            shown.current = ""
            patch((m) => ({ ...m, content: "" }))
            break
          case "tool":
            patch((m) => ({ ...m, steps: [...(m.steps ?? []), { name: String(e.name), args: e.args as ToolStep["args"] }] }))
            break
          case "done": {
            const proposals = (e.proposals as string[]) ?? []
            if (e.reply) reveal(String(e.reply))
            final = { tools: e.tools_used as string[], proposals }
            if (proposals.length) { mutate("/api/overview"); mutate("/api/portfolio") }
            break
          }
          case "error":
            reveal(`_${String(e.message)}_`)
            break
        }
      }, ctrl.signal)
    } catch (err) {
      if ((err as Error).name === "AbortError") {
        target.current = shown.current // stop typing where it is
      } else if (!target.current) {
        reveal(`_${(err as Error).message}_`)
      }
    }
    whenTyped(() => {
      patch((m) => ({ ...m, ...final, streaming: false, content: m.content || "_Stopped._" }))
      setBusy(false)
      abortRef.current = null
    })
  }

  return (
    <div className="mx-auto flex h-[calc(100dvh-8rem)] max-w-3xl flex-col">
      <div className="flex items-center justify-between pb-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Advisor</h1>
          <p className="text-sm text-muted-foreground">Ask about a stock, a company&apos;s business, or your demo account.
            Business answers come only from official sources: PSX, regulators and the company&apos;s own site.</p>
        </div>
        {messages.length > 0 && !busy &&
          <Button variant="ghost" size="sm" onClick={() => setMessages([])}><RotateCcw />New chat</Button>}
      </div>

      <div className="flex-1 space-y-6 overflow-y-auto pr-1 pb-4">
        {messages.length === 0 && (
          <div className="glow grid h-full place-items-center">
            <div className="space-y-6 text-center">
              <div className="mx-auto grid size-12 place-items-center rounded-2xl bg-primary/15 text-primary"><Sparkles className="size-6" /></div>
              <p className="text-muted-foreground">Try one of these</p>
              <div className="grid gap-2 sm:grid-cols-2">
                {SUGGESTIONS.map((s) => (
                  <button key={s} onClick={() => send(s)}
                    className="rounded-xl border bg-card px-4 py-3 text-left text-sm transition hover:border-primary/40 hover:bg-accent">{s}</button>
                ))}
              </div>
            </div>
          </div>
        )}
        {messages.map((m, i) => m.role === "user" ? (
          <div key={i} className="flex justify-end">
            <div className="max-w-[85%] rounded-2xl rounded-br-sm bg-primary px-4 py-2.5 text-sm whitespace-pre-wrap text-primary-foreground">{m.content}</div>
          </div>
        ) : (
          <div key={i} className="flex gap-3">
            <div className="grid size-8 shrink-0 place-items-center rounded-full bg-muted"><Bot className="size-4" /></div>
            <div className="min-w-0 flex-1 space-y-3">
              <Steps steps={m.steps ?? (m.tools ?? []).map((name) => ({ name }))} live={!!m.streaming && !m.content} />
              {m.streaming && !m.content && !(m.steps?.length) && (
                <div className="flex items-center gap-1 pt-2">
                  {[0, 150, 300].map((d) => <span key={d} className="size-1.5 animate-bounce rounded-full bg-muted-foreground" style={{ animationDelay: `${d}ms` }} />)}
                </div>
              )}
              {m.content && (
                <div className={cn("prose-chat text-sm leading-relaxed", m.streaming && "typing")}>
                  <ReactMarkdown remarkPlugins={[remarkGfm]}
                    components={{ a: (p) => <a {...p} target="_blank" rel="noopener noreferrer" /> }}>
                    {linkify(m.content)}
                  </ReactMarkdown>
                </div>
              )}
              {!!m.proposals?.length && (
                <Card size="sm" className="border-primary/30 bg-primary/5 px-4 text-sm">
                  <span>The suggested trade is waiting for your approval. <Link className="font-medium text-primary underline-offset-4 hover:underline" href="/">Review it on Today</Link>.</span>
                </Card>
              )}
            </div>
          </div>
        ))}
        <div ref={endRef} />
      </div>

      <form onSubmit={(e) => { e.preventDefault(); send(input) }}
            className="relative rounded-2xl border bg-card shadow-sm focus-within:border-primary/40">
        <Textarea value={input} onChange={(e) => setInput(e.target.value)} rows={2} maxLength={4000}
          placeholder="Ask about a stock, a company's business, or your account…"
          className="resize-none border-0 bg-transparent pr-14 shadow-none focus-visible:ring-0 dark:bg-transparent"
          onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(input) } }} />
        {busy ? (
          <Button type="button" size="icon" variant="secondary" className="absolute right-2 bottom-2 rounded-full"
                  onClick={() => abortRef.current?.abort()} aria-label="Stop"><Square className="fill-current" /></Button>
        ) : (
          <Button type="submit" size="icon" disabled={!input.trim()} className="absolute right-2 bottom-2 rounded-full"
                  aria-label="Send"><ArrowUp /></Button>
        )}
      </form>
      <p className="pt-2 text-center text-xs text-muted-foreground">Research on a demo account, not licensed financial advice. It can suggest trades; only you can approve them.</p>
    </div>
  )
}
