import { expect, test } from "@playwright/test"

// Against the real Docker stack: nothing mocked. Nothing here needs an LLM or live
// PSX prices, so it is stable in CI.

// The operator account: OPERATOR_EMAIL (default below) and DASHBOARD_PASSWORD from .env.
async function signInAsOperator(page: import("@playwright/test").Page) {
  await page.goto("/login")
  await page.getByLabel("Email").fill(process.env.E2E_EMAIL ?? "operator@psx-desk.local")
  await page.getByLabel("Password").fill(process.env.E2E_PASSWORD ?? "")
  await page.getByRole("button", { name: /^sign in$/i }).click()
  await expect(page).toHaveURL(/\/$/)
}

test.describe("as the operator", () => {
  test.beforeEach(async ({ page }) => signInAsOperator(page))

  test("every agent is discovered and online, and the audit chain verifies", async ({ page }) => {
    await page.goto("/system")
    for (const agent of ["technical-agent", "fundamentals-agent", "news-agent", "debate-agent",
                         "portfolio-agent", "advisor-agent"]) {
      await expect(page.getByText(agent).first()).toBeVisible()
    }
    await expect(page.getByText("unreachable")).toHaveCount(0)
    await expect(page.getByText(/entries verified/)).toBeVisible()
  })

  test("the demo account opens from the real broker", async ({ page }) => {
    await page.goto("/portfolio")
    await expect(page.getByRole("heading", { name: "Demo account" })).toBeVisible()
    await expect(page.getByText("Account value over time")).toBeVisible()
  })
})

test("a new user signs up and gets their own demo account", async ({ page }) => {
  const email = `e2e-${Date.now()}@example.com`
  await page.goto("/login")
  await page.getByRole("tab", { name: "Create account" }).click()
  await page.getByLabel("Name").fill("Smoke Test")
  await page.getByLabel("Email").fill(email)
  await page.getByLabel("Password").fill("a long test password")
  await page.getByRole("button", { name: "Create account" }).last().click()
  await expect(page).toHaveURL(/\/$/)
  const me = await (await page.request.get("/api/me")).json()
  expect(me.email).toBe(email)
  expect(me.account).not.toBe("operator")
  const pf = await (await page.request.get("/api/portfolio")).json()
  expect(pf.portfolio.cash).toBe(1_000_000)   // fresh money, nobody else's trades
  expect(pf.fills).toHaveLength(0)
})

test("the API refuses a write without the CSRF header", async ({ request }) => {
  const r = await request.post("/api/research", { data: { symbol: "HBL" } })
  expect([401, 403]).toContain(r.status())
})
