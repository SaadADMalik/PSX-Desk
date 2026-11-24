export type Stance = "bullish" | "neutral" | "bearish"

export interface Quote {
  price: number
  change_pct: number | null
  volume_avg_30d: number | null
}

export interface QuotesEvent {
  fetched_at: string
  quotes: Record<string, Quote>
  market_open?: boolean
}

export interface DeskSummary {
  stance: Stance
  confidence: number
  thesis: string
  risk: "pass" | "caution" | "veto"
  actionable: boolean
  as_of: string
  run_id: string
}

export interface WatchRow {
  symbol: string
  price: number | null
  change_pct: number | null
  spark: number[]
  desk: DeskSummary | null
}

export interface Position {
  symbol: string
  quantity: number
  avg_cost: number
  mark: number | null
  market_value: number
  weight_pct: number
}

export interface Portfolio {
  cash: number
  equity: number
  positions: Position[]
  marks_stale: string[]
}

export interface Check {
  rule: string
  ok: boolean
  detail: string
}

export interface Proposal {
  id: string
  client_order_id: string
  run_id: string | null
  proposed_by: string
  symbol: string
  side: "buy" | "sell"
  quantity: number
  ref_price: number
  rationale: string
  brief: Record<string, unknown>
  evidence_ids: string[]
  checks: Check[]
  status: "pending" | "rejected" | "expired" | "filled" | "failed"
  decided_by: string | null
  decided_at: string | null
  decision_note: string | null
  created_at: string
  expires_at: string
}

export interface Fill {
  proposal_id: string
  symbol: string
  side: "buy" | "sell"
  quantity: number
  price: number
  fees: number
  filled_at: string
}

export interface Overview {
  user: string
  market_open: boolean
  quotes_at: string | null
  watchlist: WatchRow[]
  portfolio: Portfolio
  pending: Proposal[]
  alerts: AlertRule[]
  alert_events: AlertEvent[]
  initial_capital: number
}

export interface AlertRule {
  id: string
  symbol: string
  kind: "pct" | "pkr"
  threshold: number
  ref_price: number
  ref_basis: "avg_cost" | "price_when_set"
  starts_at: string
  ends_at: string | null
  state: "armed" | "triggered"
  status: "active" | "expired" | "cancelled" | "ended"
  last_price: number | null
  last_checked_at: string | null
  times_triggered: number
  created_at: string
}

export interface AlertEvent {
  id: number
  rule_id: string
  account: string
  symbol: string
  kind: "triggered" | "rearmed"
  price: number
  drop_pct: number
  loss_pkr: number | null
  at: string
}

export interface Claim {
  text: string
  evidence_ids: string[]
}

export interface FullBrief {
  symbol: string
  id: string
  finished_at: string
  brief: {
    stance: Stance
    confidence: number
    thesis: string
    key_points: Claim[]
    risks: Claim[]
    data_gaps: string[]
  }
  risk: {
    verdict: "pass" | "caution" | "veto"
    actionable: boolean
    confidence_before: number
    confidence_after: number
    flags: { rule: string; severity: string; detail: string }[]
  }
  proposal: { action: string; reason?: string; quantity?: number; status?: string } | null
  cases: { side: "bull" | "bear"; thesis: string; confidence: number; concession: string;
           arguments: Claim[] }[]
  reports: { agent: string; stance: Stance; confidence: number; summary: string }[]
  prior_views: { as_of: string; stance: string; confidence: number }[]
  lean?: { stance: string | null; reason: string; override_reason: string }
  verification: { checked: number; verified: number }
}

export interface StockData {
  symbol: string
  info: Listing | null
  history: { as_of: string; stale: boolean; points: { date: string; close: number;
             volume: number }[] } | null
  quote: Quote | null
  position: Position | null
  cash: number
  equity: number
  brief: FullBrief | null
  watched: boolean
  alerts: AlertRule[]
}

export interface CurvePoint {
  date: string
  equity: number
  benchmark: number | null
}

export interface PortfolioData {
  portfolio: Portfolio
  fills: Fill[]
  proposals: Proposal[]
  curve: CurvePoint[]
  initial_capital: number
}

export interface Decision {
  proposal: Proposal
  fill: Fill | null
  reason: string | null
}

export interface ToolStep {
  name: string
  args?: Record<string, unknown>
}

export interface ChatMsg {
  role: "user" | "assistant"
  content: string
  tools?: string[]
  steps?: ToolStep[]
  proposals?: string[]
  streaming?: boolean
}

export interface ChatReply {
  reply: string
  tools_used: string[]
  proposals: string[]
  tokens: number
  model: string
}

export interface SystemData {
  agents: { skills?: { skill: string; agent: string; version: string; url: string }[];
            unreachable?: Record<string, string>; error?: string }
  budget: { models?: { model: string; requests_today: number; requests_per_day: number;
            tokens_today: number; tokens_per_day: number; circuit: string }[]; error?: string }
  audit_check: { checked: number; first_bad_id: number | null; ok: boolean } | { error: string }
  audit: { id: number; at: string; actor: string; action: string; subject: string;
           details: Record<string, unknown>; hash: string }[] | { error: string }
  market_open: boolean
}

export interface Listing {
  symbol: string
  name: string
  sector: string
  etf: boolean
  ex: string | null
  price: number
  change_pct: number | null
  change_1y_pct: number | null
  pe_ratio: number | null
  dividend_yield: number | null
  market_cap: number | null
  volume_avg_30d: number | null
}

export interface MarketBoard {
  fetched_at: string
  count: number
  market_open: boolean
  watchlist: string[]
  stocks: Listing[]
}
