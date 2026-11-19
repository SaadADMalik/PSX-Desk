# ADR 0004: PSX data via psxdata, with a last-good snapshot as fallback

**Status:** accepted. Supersedes the plan's "psx-dps as fallback".

## Context
PSX has no official API. `psxdata` scrapes the data portal (dps.psx.com.pk) with
retries and a disk cache. The plan proposed a second scraper as fallback. When
probed directly, the portal's JSON endpoint returned 404 and then 403: it blocks
plain scripted clients.

## Decision
- `psxdata` is the only live source, behind a `PriceSource` interface and a polite
  shared limiter (one request per 1.5 s).
- The fallback is our own **last-good snapshot** of each symbol, written atomically
  after every successful fetch. Anything served from it is marked `stale=true`, and
  the flag travels in the Evidence into the brief's `data_gaps`.
- We do not add a second scraper to get around the portal's blocking.

## Consequences
- Degraded but honest: a brief can still be produced when PSX is down, and says so.
- One live source is a single dependency. A licensed data feed would be the
  production answer.
- Where two sources disagree (the screener's `change_1y_pct` vs our computed 1-year
  return), we keep one source of truth per quantity and drop the other.
