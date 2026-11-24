"use client"

import { useState } from "react"
import { Star } from "lucide-react"
import { toast } from "sonner"
import { useSWRConfig } from "swr"
import { Button } from "@/components/ui/button"
import { send } from "@/lib/api"
import { cn } from "@/lib/utils"

/** Add a stock to, or remove it from, your own watchlist. */
export function WatchButton({ symbol, watched, size = "sm", iconOnly = false, ghost = false }: {
  symbol: string; watched: boolean; size?: "sm" | "icon-sm"; iconOnly?: boolean
  /** A bare star for dense tables. */
  ghost?: boolean
}) {
  const [busy, setBusy] = useState(false)
  const { mutate } = useSWRConfig()

  async function toggle() {
    setBusy(true)
    try {
      await send(watched ? "DELETE" : "PUT", `/api/watchlist/${symbol}`)
      toast.success(watched ? `${symbol} removed from your watchlist`
                            : `${symbol} added to your watchlist`,
        { description: watched ? undefined : "The desk researches it after each trading day." })
      mutate((k) => typeof k === "string" &&
        (k.startsWith("/api/overview") || k.startsWith("/api/market") ||
         k.startsWith("/api/stocks") || k.startsWith("/api/watchlist")))
    } catch (e) {
      toast.error("Couldn't update your watchlist", { description: (e as Error).message })
    } finally {
      setBusy(false)
    }
  }

  const label = watched ? "Watching" : "Watch"
  return (
    <Button size={iconOnly ? "icon-sm" : size}
            variant={ghost ? "ghost" : watched ? "secondary" : "outline"}
            className={cn(ghost && "size-6 text-muted-foreground/50 hover:text-amber-500")}
            disabled={busy} onClick={toggle}
            aria-label={watched ? `Remove ${symbol} from watchlist` : `Add ${symbol} to watchlist`}
            title={watched ? "Remove from your watchlist" : "Add to your watchlist"}>
      <Star className={cn(watched && "fill-current text-amber-500")} />
      {!iconOnly && label}
    </Button>
  )
}
