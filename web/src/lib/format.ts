const pkr = new Intl.NumberFormat("en-PK", { maximumFractionDigits: 0 })
const pkr2 = new Intl.NumberFormat("en-PK", { minimumFractionDigits: 2, maximumFractionDigits: 2 })

export const money = (v: number | null | undefined, decimals = false) =>
  v == null ? "—" : `Rs ${(decimals ? pkr2 : pkr).format(v)}`

export const price = (v: number | null | undefined) => (v == null ? "—" : pkr2.format(v))

const short = new Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: 1 })

/** 1.2M, 340K: for big rupee amounts in tight table cells. */
export const compact = (v: number | null | undefined) => (v == null || v === 0 ? "—" : `Rs ${short.format(v)}`)

export const num = (v: number | null | undefined) => (v == null ? "—" : pkr.format(v))

export const pct = (v: number | null | undefined, sign = true) =>
  v == null ? "—" : `${sign && v > 0 ? "+" : ""}${v.toFixed(2)}%`

export const tone = (v: number | null | undefined) =>
  v == null || v === 0 ? "text-muted-foreground" : v > 0 ? "text-gain" : "text-loss"

export function ago(iso: string | null | undefined): string {
  if (!iso) return "never"
  const s = (Date.now() - new Date(iso).getTime()) / 1000
  if (s < 60) return "just now"
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`
  return `${Math.floor(s / 86400)}d ago`
}

export const who = (actor: string | null | undefined) => {
  if (!actor) return "—"
  if (actor.startsWith("dashboard:")) return `you (${actor.slice(10)})`
  if (actor === "portfolio-agent") return "research desk"
  if (actor === "advisor-agent") return "advisor (chat)"
  return actor
}
