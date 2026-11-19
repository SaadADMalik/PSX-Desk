import { expect, test } from "@playwright/test"

// Against the real Docker stack: nothing mocked. Nothing here needs an LLM or live
// PSX prices, so it is stable in CI.

test.beforeEach(async ({ page }) => {
  await page.goto("/login")
  await page.getByLabel("Password").fill(process.env.E2E_PASSWORD ?? "")
  await page.getByRole("button", { name: /sign in/i }).click()
  await expect(page).toHaveURL(/\/$/)
})

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

test("the API refuses a write without the CSRF header", async ({ request }) => {
  const r = await request.post("/api/research", { data: { symbol: "HBL" } })
  expect([401, 403]).toContain(r.status())
})
