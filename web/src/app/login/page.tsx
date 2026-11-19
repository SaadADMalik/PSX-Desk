"use client"

import { useState } from "react"
import { useRouter } from "next/navigation"
import { Activity, Loader2 } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { post } from "@/lib/api"

export default function LoginPage() {
  const router = useRouter()
  const [user, setUser] = useState("operator")
  const [password, setPassword] = useState("")
  const [error, setError] = useState("")
  const [busy, setBusy] = useState(false)

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError("")
    try {
      await post("/api/login", { user, password })
      router.push("/")
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="glow grid min-h-screen place-items-center px-4">
      <div className="w-full max-w-sm space-y-6">
        <div className="flex flex-col items-center gap-3 text-center">
          <div className="grid size-11 place-items-center rounded-xl bg-primary text-primary-foreground shadow-lg shadow-primary/20">
            <Activity className="size-5" />
          </div>
          <div>
            <h1 className="text-xl font-semibold tracking-tight">PSX Desk</h1>
            <p className="text-sm text-muted-foreground">AI research and a demo account for the Pakistan Stock Exchange</p>
          </div>
        </div>
        <Card>
          <CardHeader>
            <CardTitle>Sign in</CardTitle>
            <CardDescription>Your password is DASHBOARD_PASSWORD in .env</CardDescription>
          </CardHeader>
          <CardContent>
            <form onSubmit={submit} className="grid gap-4">
              <div className="grid gap-2">
                <Label htmlFor="user">User</Label>
                <Input id="user" value={user} onChange={(e) => setUser(e.target.value)} autoComplete="username" />
              </div>
              <div className="grid gap-2">
                <Label htmlFor="pw">Password</Label>
                <Input id="pw" type="password" value={password} autoFocus
                       onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" />
              </div>
              {error && <p className="text-sm text-loss">{error}</p>}
              <Button type="submit" disabled={busy || !password}>
                {busy && <Loader2 className="animate-spin" />}Sign in
              </Button>
            </form>
          </CardContent>
        </Card>
        <p className="text-center text-xs text-muted-foreground">
          Demo money only. Research, not licensed financial advice.
        </p>
      </div>
    </div>
  )
}
