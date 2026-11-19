"use client"

import Link from "next/link"
import { usePathname, useRouter } from "next/navigation"
import { useTheme } from "next-themes"
import useSWR from "swr"
import { Activity, Bot, Briefcase, Cpu, LayoutDashboard, LogOut, Menu, Moon, Sun,
         CandlestickChart } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { Sheet, SheetContent, SheetTitle, SheetTrigger } from "@/components/ui/sheet"
import { LiveDot } from "@/components/desk"
import { StockSearch } from "@/components/stock-search"
import { cn } from "@/lib/utils"
import { fetcher, post } from "@/lib/api"
import { useLive } from "@/lib/live"
import { ago } from "@/lib/format"

const NAV = [
  { href: "/", label: "Today", icon: LayoutDashboard, hint: "Your watchlist and what needs you" },
  { href: "/advisor", label: "Advisor", icon: Bot, hint: "Ask anything about PSX or your account" },
  { href: "/portfolio", label: "Demo account", icon: Briefcase, hint: "Your test money and trades" },
  { href: "/stocks", label: "Stocks", icon: CandlestickChart, hint: "Charts and research per stock" },
  { href: "/system", label: "Under the hood", icon: Cpu, hint: "Agents, budget, audit log" },
]

function Nav({ onNavigate }: { onNavigate?: () => void }) {
  const path = usePathname()
  const { data } = useSWR<{ pending: unknown[] }>("/api/overview", fetcher)
  const pending = data?.pending?.length ?? 0
  return (
    <nav className="grid gap-1">
      {NAV.map(({ href, label, icon: Icon, hint }) => {
        const active = href === "/" ? path === "/" : path.startsWith(href)
        return (
          <Link key={href} href={href} onClick={onNavigate} title={hint}
            className={cn("group flex items-center gap-3 rounded-lg px-3 py-2 text-sm transition-colors",
              active ? "bg-sidebar-accent text-sidebar-accent-foreground font-medium"
                     : "text-muted-foreground hover:bg-sidebar-accent/60 hover:text-foreground")}>
            <Icon className={cn("size-4", active && "text-primary")} />
            {label}
            {href === "/" && pending > 0 && (
              <Badge className="ml-auto h-5 min-w-5 justify-center px-1.5">{pending}</Badge>
            )}
          </Link>
        )
      })}
    </nav>
  )
}

function Brand() {
  return (
    <Link href="/" className="flex items-center gap-2.5 px-2">
      <div className="grid size-8 place-items-center rounded-lg bg-primary text-primary-foreground shadow-sm">
        <Activity className="size-4" />
      </div>
      <div className="leading-tight">
        <div className="text-sm font-semibold tracking-tight">PSX Desk</div>
        <div className="text-[11px] text-muted-foreground">AI research · demo trading</div>
      </div>
    </Link>
  )
}

export function AppShell({ children }: { children: React.ReactNode }) {
  const { theme, setTheme } = useTheme()
  const router = useRouter()
  const { connected, marketOpen, quotes } = useLive()

  async function logout() {
    await post("/api/logout").catch(() => {})
    router.push("/login")
  }

  return (
    <div className="flex min-h-screen">
      <aside className="sticky top-0 hidden h-screen w-60 shrink-0 flex-col gap-6 border-r bg-sidebar px-3 py-5 md:flex">
        <Brand />
        <Nav />
        <div className="mt-auto space-y-3 px-2">
          <div className="rounded-lg border bg-background/60 p-3 text-xs text-muted-foreground">
            <span className="font-medium text-foreground">Demo account.</span> Test money, real PSX
            prices. Nothing here touches a real brokerage.
          </div>
        </div>
      </aside>
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-30 flex h-14 items-center gap-3 border-b bg-background/80 px-4 backdrop-blur md:px-6">
          <Sheet>
            <SheetTrigger render={<Button variant="ghost" size="icon" className="md:hidden" />}>
              <Menu />
            </SheetTrigger>
            <SheetContent side="left" className="w-64 gap-6 p-4">
              <SheetTitle className="sr-only">Navigation</SheetTitle>
              <Brand />
              <Nav />
            </SheetContent>
          </Sheet>
          <div className="flex items-center gap-4">
            <LiveDot on={connected && !!marketOpen}
                     label={marketOpen == null ? "Connecting…" : marketOpen ? "PSX open · live" : "PSX closed"} />
            {quotes?.fetched_at && (
              <span className="hidden text-xs text-muted-foreground sm:inline">
                prices {ago(quotes.fetched_at)}</span>
            )}
          </div>
          <div className="ml-auto flex flex-1 justify-end md:mr-2"><StockSearch /></div>
          <div className="flex items-center gap-1">
            <Badge variant="outline" className="hidden border-amber-500/30 bg-amber-500/10 text-amber-600 dark:text-amber-400 sm:inline-flex">
              Demo money</Badge>
            <Button variant="ghost" size="icon" aria-label="Toggle theme"
                    onClick={() => setTheme(theme === "dark" ? "light" : "dark")}>
              <Sun className="hidden dark:block" /><Moon className="dark:hidden" />
            </Button>
            <Button variant="ghost" size="icon" aria-label="Sign out" onClick={logout}><LogOut /></Button>
          </div>
        </header>
        <main className="glow flex-1 px-4 py-6 md:px-8 md:py-8">
          <div className="mx-auto w-full max-w-6xl">{children}</div>
        </main>
      </div>
    </div>
  )
}
