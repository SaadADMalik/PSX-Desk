import { Suspense } from "react"
import { AppShell } from "@/components/app-shell"
import { LiveProvider } from "@/lib/live"

// Everything behind sign-in is per-user and live, so the shell renders at request time.
export default function AppLayout({ children }: { children: React.ReactNode }) {
  return (
    <Suspense fallback={<div className="min-h-screen bg-background" />}>
      <LiveProvider>
        <AppShell>{children}</AppShell>
      </LiveProvider>
    </Suspense>
  )
}
