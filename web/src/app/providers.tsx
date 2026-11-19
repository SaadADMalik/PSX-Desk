"use client"

import { ThemeProvider } from "next-themes"
import { SWRConfig } from "swr"
import { Toaster } from "@/components/ui/sonner"
import { TooltipProvider } from "@/components/ui/tooltip"
import { fetcher } from "@/lib/api"

export function Providers({ children }: { children: React.ReactNode }) {
  return (
    <ThemeProvider attribute="class" defaultTheme="dark" enableSystem={false} disableTransitionOnChange>
      <SWRConfig value={{ fetcher, revalidateOnFocus: true, dedupingInterval: 2000 }}>
        <TooltipProvider>
          {children}
          <Toaster richColors position="bottom-right" />
        </TooltipProvider>
      </SWRConfig>
    </ThemeProvider>
  )
}
