# OpenPageRank API (DomCop / Keywords Everywhere)

> Integration reference for `integrations/` transport layer. Used in M1 Domain
> Intelligence, Stage B (metrics pre-filter, see `docs/DONORS.md`).

## Purpose — free DR-proxy

We have **no paid backlink-metrics API** (Ahrefs/DataForSEO cost per row). OpenPageRank
gives a **free, coarse 0–10 domain-authority number** we use as a cheap stand-in for
Ahrefs DR in the Stage B pre-filter. It is a **gate signal only** ("dead vs has-something"),
not a real link-profile metric. See [What it can't do](#what-it-cant-do).

- **Score:** Open PageRank, scale **0–10**, modelled to replace the original Google
  PageRank metric.
- **Data source:** the [Common Crawl](https://commoncrawl.org/) open link graph — free
  for everyone. (Same underlying graph is why it can *never* be as rich as Ahrefs.)
- **Freshness:** rebuilt from **monthly crawls**; the (new) version keeps full monthly
  history per domain back to Jan 2018. Every response carries a `last_updated` date.

## Auth — `API-OPR` header

Every request must send the API key in an HTTP header:

```
API-OPR: <YOUR_API_KEY>
```

### Getting a key (free)

> **Migration note (verify at integration time):** OpenPageRank was acquired by
> **Keywords Everywhere**. There are effectively two registration paths right now, and
> which one is authoritative for *new* keys is **UNVERIFIED** — confirm before coding.

- **Legacy (DomCop):** sign up at `https://www.domcop.com/openpagerank/auth/signup`,
  then copy the generated key from the dashboard. Free, no card.
- **New (Keywords Everywhere):** go to `https://openpagerank.keywordseverywhere.com` and
  sign in with a **Keywords Everywhere API key** (create a free KE account if needed).

Store the key in `.env` (e.g. `OPENPAGERANK_API_KEY=...`) per project convention — secrets
never in code.

## Endpoint

### `GET /getPageRank`

```
GET https://openpagerank.com/api/v1.0/getPageRank?domains[]=example.com&domains[]=foo.com
```

| Item        | Value                                                           |
|-------------|-----------------------------------------------------------------|
| Method      | `GET`                                                            |
| Base URL    | `https://openpagerank.com/api/v1.0/`                            |
| Path        | `getPageRank`                                                    |
| Auth        | header `API-OPR: <key>`                                          |
| Param       | `domains[]` — repeated query param, one per domain (bare host, no scheme) |
| Batch limit | **max 100 domains per call** (confirmed in official docs)        |

Domains should be sent as the registrable host only (`example.com`), no `http://`,
no path. URL-encode each `domains[]` value.

> **Base-URL caveat (UNVERIFIED):** the official documentation still shows the
> `openpagerank.com/api/v1.0/getPageRank` host + `API-OPR` header. Whether the
> Keywords-Everywhere-era endpoint stays on this exact host is not independently
> confirmed post-migration — smoke-test the live endpoint before relying on it.

### Example request (curl)

```bash
curl -s 'https://openpagerank.com/api/v1.0/getPageRank?domains%5B%5D=google.com&domains%5B%5D=apple.com&domains%5B%5D=unknowndomain.com' \
  -H 'API-OPR: <YOUR_API_KEY>'
```

### Example response (verbatim from official docs)

```json
{
   "status_code": 200,
   "response": [
      {
         "status_code": 200,
         "error": "",
         "page_rank_integer": 10,
         "page_rank_decimal": 10,
         "rank": "6",
         "domain": "google.com"
      },
      {
         "status_code": 200,
         "error": "",
         "page_rank_integer": 8,
         "page_rank_decimal": 7.63,
         "rank": "40",
         "domain": "apple.com"
      },
      {
         "status_code": 404,
         "error": "Domain not found",
         "page_rank_integer": 0,
         "page_rank_decimal": 0,
         "rank": null,
         "domain": "unknowndomain.com"
      }
   ],
   "last_updated": "28th Mar 2026"
}
```

## Response fields we consume

**Top level:**

| Field          | Type        | Meaning                                                    |
|----------------|-------------|------------------------------------------------------------|
| `status_code`  | int         | HTTP-style status of the overall call (`200` = OK).        |
| `response`     | array       | One object per requested domain (order matches request).   |
| `last_updated` | string      | Human date of the crawl snapshot, e.g. `"28th Mar 2026"`. |

**Per-domain object (`response[]`):**

| Field               | Type          | We use it as                                                                 |
|---------------------|---------------|------------------------------------------------------------------------------|
| `domain`            | string        | Echo of the queried host — key results back to input.                        |
| `status_code`       | int           | `200` = found, `404` = "Domain not found" (no data in the graph).            |
| `error`             | string        | `""` on success, else message (e.g. `"Domain not found"`).                   |
| `page_rank_decimal` | float (0–10)  | **Primary DR-proxy.** Finer-grained score, e.g. `7.63`. Feed the gate.       |
| `page_rank_integer` | int (0–10)    | Rounded score, e.g. `8`. Convenience bucket.                                 |
| `rank`              | string \| null| Global position of the domain in the whole ranking (`"6"` = 6th overall); `null` if not found. Lower = stronger. Optional signal. |

**Pre-filter mapping (M1 Stage B):** treat `status_code == 404` **or**
`page_rank_decimal == 0` as **"dead / no footprint"** → fail the gate (or route to a
harder look). A non-zero `page_rank_decimal` above a tunable `min_opr` threshold
(kept in `scoring_config`) → **"has something"** → passes to the more expensive stages.
Do **not** treat the number as DR-equivalent for ranking quality — it only clears the floor.

## Limits / cost

> The two eras report limits differently. Confirm which applies to your key.

- **Legacy (DomCop) free tier:** **1,000 requests/day**, with a burst rate limit of
  **10,000 requests/hour**. Batch of 100 domains/request → up to ~100k domain-lookups/day.
- **New (Keywords Everywhere) free plan:** **30,000 domains/month**.
- **Paid (Keywords Everywhere):** scale from **100,000 up to 4,000,000 domains/month**,
  bundled with a Keywords Everywhere subscription. (Exact prices **UNVERIFIED** — quoted
  per KE plan tier, not on the OpenPageRank docs.)

For our volumes (batch pre-filter of drop candidates) the free tier is comfortable.
Batch aggressively (100/call) and cache — data only changes monthly, so re-querying the
same domain within a month is wasted quota. Use httpx + backoff per project convention.

## What it can't do

This is the important boundary — it is **NOT** a backlink API:

- ❌ **No referring-domains count (RD).** Our Stage B methodology says *RD matters more
  than backlinks* — OpenPageRank gives neither RD nor backlink counts.
- ❌ **No anchor-text distribution**, no dofollow/nofollow split, no sitewide/footer ratio.
- ❌ **No live-vs-lost link status** — it is a static monthly graph score, not a live
  crawl of the domain's current backlinks.
- ❌ **No topical relevance** of linking sites, no per-donor breakdown.
- ❌ **No traffic estimate.**
- ⚠️ Coarse & laggy: 0–10 granularity, monthly Common Crawl snapshot; Common Crawl misses
  a lot of the web, so a low/zero score can be a false negative for a real-but-obscure site.

Conclusion: use it **only** as the cheap first floor ("is this domain completely dead or
does it have some link footprint?"). All real donor-quality judgement (RD, anchors,
live links, history) still needs Ahrefs/DataForSEO + Wayback in later stages — see
`docs/DONORS.md` Stages B→D.

## `ping()` suggestion

Cheapest liveness check = a **1-domain getPageRank call** for a known-good domain and
assert the top-level `status_code == 200`. Costs 1 request against the daily quota.

```python
# integrations/openpagerank.py  (transport only)
def ping(self) -> bool:
    r = self._client.get(
        "https://openpagerank.com/api/v1.0/getPageRank",
        params={"domains[]": "google.com"},
        headers={"API-OPR": self.api_key},
        timeout=10,
    )
    r.raise_for_status()
    return r.json().get("status_code") == 200
```

A 401/403 here means a bad/expired key; a 200 with a populated `response[0]` confirms
both auth and data path. Good enough for `scripts/smoke.py`.

## Alternatives (coarse-authority, free/cheap)

OpenPageRank stays **primary** (truly free, simple, batchable). Others considered if we
ever need referring-domain counts cheaply:

| Source | Gives | Free/cost | Pros | Cons |
|--------|-------|-----------|------|------|
| **Moz Links API** | Domain Authority (0–100) **+ linking root domains count** | Free tier historically limited rows/month (quota **UNVERIFIED**, verify current); paid beyond | Actually includes an RD-style count + a respected DA; single API | Low free quota, per-URL not big batches, own crawl (differs from Ahrefs) |
| **DataForSEO Backlinks API** | Real RD, backlinks, anchors, ~2T live-link index; bulk up to 1000 domains/call | **Paid, but cheap** (~cents/lookup) | Closest to Ahrefs depth at low cost; the paid provider we'd graduate to for Stages B–D anyway | Not free; needs billing |
| **Common Crawl webgraph (direct)** | Domain-level harmonic-centrality / rank | Free (raw data) | Same source as OPR, no rate limit, self-hosted | Heavy to process; you'd basically rebuild OpenPageRank — not worth it |
| **Majestic** (Trust Flow / Citation Flow) | TF/CF + RD | Paid, no real free API | Good link-quality signal | No free tier |

**Recommendation:** keep OpenPageRank as the free floor gate; when budget allows, add
**DataForSEO** for the real RD/anchor/live-link data the later donor-quality stages need
(it's already a candidate provider in `docs/SERVICES.md`). Moz's free tier is the only
notable option that bundles a *referring-domains count* for free, but its quota is tiny —
evaluate only if we specifically need free RD.

## Source URLs

- OpenPageRank home / free key: https://www.domcop.com/openpagerank/
- API documentation (endpoint + example response): https://www.domcop.com/openpagerank/documentation
- FAQ (registration, limits, data source, score meaning): https://www.domcop.com/openpagerank/frequently-asked-questions
- What is Open PageRank: https://www.domcop.com/openpagerank/what-is-openpagerank
- Signup: https://www.domcop.com/openpagerank/auth/signup
- Keywords Everywhere portal (new): https://openpagerank.keywordseverywhere.com
- Third-party schema cross-check: https://publicapi.dev/open-page-rank-api
- Common Crawl (data source): https://commoncrawl.org/
- DataForSEO Backlinks API (alt): https://dataforseo.com/apis/backlinks-api

---
*Fields marked **UNVERIFIED** need a live check at integration time. Response schema and
the 100-domain batch limit are confirmed against the official documentation
(example response quoted verbatim). Verified 2026-07-05.*
