import { expect, test } from "@playwright/test"
import { mockApi } from "./mock"

test.describe("sign in", () => {
  test("a wrong password shows the error and stays on the page", async ({ page }) => {
    await mockApi(page, { signedIn: false })
    await page.goto("/login")
    await page.getByLabel("Password").fill("nope")
    await page.getByRole("button", { name: /sign in/i }).click()
    await expect(page.getByText("wrong user or password")).toBeVisible()
    await expect(page).toHaveURL(/\/login$/)
  })

  test("the right password opens Today", async ({ page }) => {
    await mockApi(page)
    await page.goto("/login")
    await page.getByLabel("Password").fill("test-password")
    await page.getByRole("button", { name: /sign in/i }).click()
    await expect(page).toHaveURL(/\/$/)
    await expect(page.getByText("Watchlist").first()).toBeVisible()
  })

  test("a signed-out visitor is sent to sign in", async ({ page }) => {
    await mockApi(page, { signedIn: false })
    await page.goto("/portfolio")
    await expect(page).toHaveURL(/\/login$/)
  })
})

test.describe("stocks", () => {
  test("search narrows the list on each letter, by name as well as ticker", async ({ page }) => {
    await mockApi(page)
    await page.goto("/stocks")
    const rows = page.locator("tbody tr")
    await expect(rows).toHaveCount(20)
    await page.getByPlaceholder(/search by ticker or company/i).fill("meez")
    await expect(rows.first()).toContainText("MEBL")
    await expect(page.getByText(/of 20 stocks/)).toBeVisible()
  })

  test("the sector filter keeps only that sector", async ({ page }) => {
    await mockApi(page)
    await page.goto("/stocks")
    const select = page.locator("select")
    const sector = await select.locator("option").nth(1).textContent()
    await select.selectOption({ label: sector! })
    const cells = page.locator("tbody tr")
    await expect(cells.first()).toBeVisible()
    expect(await cells.count()).toBeLessThan(20)
  })

  test("Ctrl+K search opens a stock page with the desk's reasoning", async ({ page }) => {
    await mockApi(page)
    await page.goto("/")
    // the shortcut is wired up once the page hydrates; retry until it focuses the box
    await expect(async () => {
      await page.keyboard.press("Control+k")
      await expect(page.getByLabel("Search stocks")).toBeFocused({ timeout: 500 })
    }).toPass()
    await page.keyboard.type("hbl")
    await page.keyboard.press("Enter")
    await expect(page).toHaveURL(/\/stocks\/HBL$/)
    await expect(page.getByText("The desk's view")).toBeVisible()
    await expect(page.getByText("How the desk decided")).toBeVisible()
  })
})

test("a price alert can be set on a stock, with no end date", async ({ page }) => {
  const calls = await mockApi(page)
  await page.goto("/stocks/HBL")
  await page.getByRole("button", { name: "Set alert" }).click()
  await page.getByLabel(/falls by/).fill("5")
  await expect(page.getByText(/Alerts at about/)).toBeVisible()
  await page.getByRole("dialog").getByRole("button", { name: "Set alert" }).click()
  await expect(page.getByText("Alert set on HBL")).toBeVisible()
  expect(calls.find((c) => c.url === "/api/alerts")?.body)
    .toEqual({ symbol: "HBL", kind: "pct", threshold: 5, days: null })
})

test("Under the hood lists every agent, including unreachable ones", async ({ page }) => {
  // Regression: `unreachable` is an object; treating it as a list crashed the page.
  await mockApi(page)
  await page.goto("/system")
  await expect(page.getByRole("heading", { name: "Under the hood" })).toBeVisible()
  await expect(page.getByText("advisor-agent")).toBeVisible()
  await expect(page.getByText("http://news-agent:8000/")).toBeVisible()
  await expect(page.getByText("unreachable")).toBeVisible()
})

test("the advisor streams its answer and shows the steps it took", async ({ page }) => {
  await mockApi(page, {
    chat: [
      { type: "tool", name: "search_official", args: { query: "Meezan ATM charges" } },
      { type: "delta", text: "Meezan charges **Rs 35** " },
      { type: "delta", text: "per withdrawal at other banks' ATMs." },
      { type: "done", reply: "Meezan charges **Rs 35** per withdrawal at other banks' ATMs.",
        tools_used: ["search_official"], proposals: [] },
    ],
  })
  await page.goto("/advisor")
  await page.getByPlaceholder(/ask about a stock/i).fill("What does Meezan charge at other ATMs?")
  await page.getByRole("button", { name: "Send" }).click()
  await expect(page.getByText(/Searched official sources for .Meezan ATM charges./)).toBeVisible()
  await expect(page.getByText("per withdrawal at other banks' ATMs.", { exact: false })).toBeVisible()
  await expect(page.locator("strong", { hasText: "Rs 35" })).toBeVisible()
})

test("the demo account page shows holdings and history", async ({ page }) => {
  await mockApi(page)
  await page.goto("/portfolio")
  await expect(page.getByRole("heading", { name: "Demo account" })).toBeVisible()
  await expect(page.getByText("Account value over time")).toBeVisible()
})
