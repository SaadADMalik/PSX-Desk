"use client"

import { useState } from "react"
import { useRouter } from "next/navigation"
import { Activity, Loader2 } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { post } from "@/lib/api"
import { cn } from "@/lib/utils"

type Mode = "signin" | "signup"

export default function LoginPage() {
  const router = useRouter()
  const [mode, setMode] = useState<Mode>("signin")
  const [name, setName] = useState("")
  const [email, setEmail] = useState("")
  const [password, setPassword] = useState("")
  const [error, setError] = useState("")
  const [busy, setBusy] = useState(false)

  const signup = mode === "signup"
  const ready = email.includes("@") && (signup ? name.trim() && password.length >= 10 : password)

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError("")
    try {
      await post(signup ? "/api/auth/signup" : "/api/auth/login",
                 signup ? { name: name.trim(), email, password } : { email, password })
      router.push("/")
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setBusy(false)
    }
  }

  function switchTo(m: Mode) {
    setMode(m)
    setError("")
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
            <div className="grid grid-cols-2 gap-1 rounded-lg bg-muted p-1" role="tablist">
              {([["signin", "Sign in"], ["signup", "Create account"]] as const).map(([m, label]) => (
                <button key={m} type="button" role="tab" aria-selected={mode === m} onClick={() => switchTo(m)}
                  className={cn("rounded-md py-1.5 text-sm font-medium transition",
                    mode === m ? "bg-background text-foreground shadow-sm" : "text-muted-foreground hover:text-foreground")}>
                  {label}
                </button>
              ))}
            </div>
            <CardTitle className="pt-3">{signup ? "Create your demo account" : "Welcome back"}</CardTitle>
            <CardDescription>
              {signup
                ? "You get Rs 1,000,000 of demo money, your own watchlist and price alerts."
                : "Sign in with your email and password."}
            </CardDescription>
          </CardHeader>
          <CardContent>
            <form onSubmit={submit} className="grid gap-4">
              {signup && (
                <div className="grid gap-2">
                  <Label htmlFor="name">Name</Label>
                  <Input id="name" value={name} onChange={(e) => setName(e.target.value)}
                         autoComplete="name" maxLength={60} autoFocus />
                </div>
              )}
              <div className="grid gap-2">
                <Label htmlFor="email">Email</Label>
                <Input id="email" type="email" value={email} onChange={(e) => setEmail(e.target.value)}
                       autoComplete="email" autoFocus={!signup} />
              </div>
              <div className="grid gap-2">
                <Label htmlFor="pw">Password</Label>
                <Input id="pw" type="password" value={password}
                       onChange={(e) => setPassword(e.target.value)}
                       autoComplete={signup ? "new-password" : "current-password"} />
                {signup && <p className="text-xs text-muted-foreground">At least 10 characters.</p>}
              </div>
              {error && <p className="text-sm text-loss" role="alert">{error}</p>}
              <Button type="submit" disabled={busy || !ready}>
                {busy && <Loader2 className="animate-spin" />}{signup ? "Create account" : "Sign in"}
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
