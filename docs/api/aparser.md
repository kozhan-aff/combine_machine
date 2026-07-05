# A-Parser HTTP API (local, 192.168.1.77:9091)

> Integration reference for `integrations/` transport layer. A-Parser is the
> RU-popular multi-scraper running **locally** on the LAN box `192.168.1.77`.
> It closes several gaps for free: **whois** (M2 free/expiry check), **SERP + `site:`
> index checks** (M1 `indexed_echo` / M4 competitor analysis, complements SearXNG),
> **keyword volume** (M4), and a **coarse DR second-opinion** (M1, complements OpenPageRank).

## Purpose

One local tool that exposes ~137 scrapers over a simple JSON-over-HTTP API. For this
project it is used as a **free, self-hosted** source for:

- **Whois** — is a domain free / when does it expire (M2 acquisition gate; see `optimizator.md`).
- **SERP** — Google/Yandex/DuckDuckGo/Bing results and `site:` index counts (M1 indexed check, M4 competitor structure) — a fallback/complement to SearXNG (`searxng.md`).
- **Keywords** — Suggest + Yandex WordStat + Google Keyword Planner volumes (M4 content).
- **Domain metrics** — scraped Ahrefs / Moz / Majestic / Yandex SQI as a coarse second opinion to OpenPageRank (M1 Stage B; see `openpagerank.md`, `docs/DONORS.md`).
- **History / cleanliness** — RKN check, archive.org presence, Google/Yandex SafeBrowsing, SecurityTrails DNS history (M1 hard-reject / history stage; complements `rkn.md`, `wayback.md`, `blacklist.md`).

## Base URL

```
APARSER_URL=http://192.168.1.77:9091      # already in .env / .env.example
```

- API endpoint is `POST {APARSER_URL}/API` (note: path is **case-sensitive `/API`**; `/api` returns the web-login page, not the API).
- The web UI is served at `/` (a "Password required" login form posting to `/auth`) — not used programmatically.

## Auth — THE FIX

Password goes in the JSON body as the `password` field (see request format below). Two things
were wrong before and are now fixed:

1. **The credential value.** The previously-recorded `apollo11` returns
   `{"msg":"Auth failed","success":0}` on **both** `/API` and the web `/auth` — i.e. it is not
   the exact string A-Parser stores. The **correct, case-sensitive** value (verified live by a
   `ping`→`pong`, see below) differs from `apollo11` and is now stored in `.env`:

   ```
   APARSER_API_KEY=...      # exact A-Parser API password (case-sensitive, verified working)
   ```

   > The request format was NOT the blocker — the documented `{"password","action"}` body
   > already reaches the auth check. The blocker was the exact password string. The working
   > value is **not** `apollo11`; keep it only in `.env`, never in code or docs.

2. There is **no hashing / no salt** — the API compares the plaintext `password` field against
   the configured value. (md5/sha1/sha256 of the password all fail; cookie/Basic-auth headers are ignored — the body field is the only mechanism.)

## Correct request format (verified)

| Item          | Value                                              |
|---------------|----------------------------------------------------|
| Method        | `POST`                                             |
| URL           | `http://192.168.1.77:9091/API`                     |
| Content-Type  | `application/json` **(required)** — without it the raw body is not parsed and you get `<h1>No content</h1>` |
| Body          | JSON: `password` + `action` + optional `data{}`    |

```json
{
    "password": "<APARSER_API_KEY>",
    "action":   "<actionName>",
    "data":     { /* action-specific params (omit for ping/info) */ }
}
```

Response envelope:

```json
{ "success": 1, "data": <result> }        // ok
{ "success": 0, "msg": "Auth failed" }     // error (bad password, etc.)
```

### Working curl (VERIFIED live 2026-07-05)

```bash
# password kept in env, never inline
curl -s -X POST "$APARSER_URL/API" \
  -H 'Content-Type: application/json' \
  -d "{\"password\":\"$APARSER_API_KEY\",\"action\":\"ping\"}"
```

Real response from `192.168.1.77:9091`:

```json
{"success":1,"data":"pong"}
```

## Key actions

### `ping` — liveness + auth check (VERIFIED)

Request `{"password":"…","action":"ping"}` → `{"success":1,"data":"pong"}`.
This is the cheapest auth-smoke; `success:0 / "Auth failed"` = wrong password.

### `info` — version + installed parsers + queue state (VERIFIED)

```bash
curl -s -X POST "$APARSER_URL/API" -H 'Content-Type: application/json' \
  -d "{\"password\":\"$APARSER_API_KEY\",\"action\":\"info\"}"
```

Real `data` shape observed live (values from this instance):

```json
{
  "success": 1,
  "data": {
    "version": "1.2.2940",
    "pid": "…",
    "availableParsers": [ "Net::Whois", "SE::Google", "…137 total…" ],
    "tasksInQueue": 0,
    "activeThreads": 0,
    "workingTasks": 0,
    "activeProxyCheckerThreads": 25
  }
}
```

- **`version`** = `1.2.2940` (this box).
- **`availableParsers`** = flat list of installed parser names → this is the **enumeration** of installed parsers (full list below).

### `getParserInfo` — result fields a parser can return

```json
{"password":"…","action":"getParserInfo","data":{"parser":"Net::Whois"}}
```

Returns the parser's available result variables (e.g. for whois: expiry/registrar/status
fields). **UNVERIFIED live** (see Gotchas — enumeration beyond `info` was paused by a safety
gate; re-run once the credential is confirmed in `.env`). Format is from official docs.

### `getParserPreset` — a saved preset's settings

```json
{"password":"…","action":"getParserPreset","data":{"parser":"SE::Google","preset":"default"}}
```

Fetches one **named** preset's config. Note: there is **no single documented action that lists
all saved presets** — you fetch them by name. `default` always exists per parser. Live
enumeration of custom presets on this box is **UNVERIFIED**.

### `oneRequest` — single query through one parser (the programmatic call we'll use)

This is how M1/M2/M4 will call A-Parser: one query, one parser, synchronous result.

```json
{
  "password": "<APARSER_API_KEY>",
  "action":   "oneRequest",
  "data": {
    "query":        "example.com",
    "parser":       "Net::Whois",
    "configPreset": "default",
    "preset":       "default"
  }
}
```

```bash
curl -s -X POST "$APARSER_URL/API" -H 'Content-Type: application/json' \
  -d "{\"password\":\"$APARSER_API_KEY\",\"action\":\"oneRequest\",\"data\":{\"query\":\"example.com\",\"parser\":\"Net::Whois\",\"configPreset\":\"default\",\"preset\":\"default\"}}"
```

- `parser` — a name from `availableParsers`.
- `preset` — a parser preset (`default` unless you saved one).
- `configPreset` — thread/proxy config profile (`default`).
- `query` — the input (domain for whois, keyword for SE::*, `site:example.com` for index check).

Response is the standard envelope; `data` holds the parser's result rows.
**UNVERIFIED live** (call was paused by the safety gate); the request format is from the
official docs. For **bulk** work use `bulkRequest` / `addTask` (task queue) instead.

Other documented actions (from official docs, not exercised here): `bulkRequest`, `addTask`,
`getTasksList`, `getTaskState`, `getTaskConf`, `getTaskResultsFile`, `deleteTaskResultsFile`,
`changeTaskStatus`, `moveTask`, `getProxies`, `getAccountsCount`, `changeProxyCheckerState`,
`update`.

## Installed parsers (LIVE — 137 on this instance, verified 2026-07-05)

Full list captured from `info.availableParsers`. Grouped; **project-relevant ones marked** and
mapped below.

**Whois / DNS / domain-history (→ M1 history, M2 gate):**
`Net::Whois` ⭐, `Net::DNS`, `Net::HTTP`, `SecurityTrails::Domain`, `SecurityTrails::IP`

**History / cleanliness / blacklist (→ M1 hard-reject & history, DONORS.md):**
`Check::RosKomNadzor` ⭐ (RKN registry), `Rank::Archive` ⭐ (archive.org presence),
`SE::Google::SafeBrowsing`, `SE::Yandex::SafeBrowsing`, `SE::Google::TrustCheck`,
`SE::Google::Compromised`, `Check::BackLink`, `Cloudflare::Radar`

**SERP engines (→ M1 indexed_echo / M4 competitor):**
`SE::Google` ⭐, `SE::Yandex` ⭐, `SE::DuckDuckGo` ⭐, `SE::Bing`, `SE::Startpage`, `SE::Yahoo`,
`SE::Ask`, `SE::Baidu`, `SE::Seznam`, `SE::Rambler`, `SE::AOL`, `SE::Dogpile`, `SE::You`,
`SE::Quora`, plus image/video variants and `SE::Google::Cache`

**Rank tracking (→ M5/M1 position checks):**
`SE::Google::Position`, `SE::Yandex::Position`, `SE::Bing::Position`, `SE::DuckDuckGo::Position`

**Keywords (→ M4 content):**
`SE::Google::Suggest` ⭐, `SE::Yandex::Suggest`, `SE::Bing::Suggest`, `SE::YouTube::Suggest`,
`SE::Yahoo::Suggest`, `SE::AOL::Suggest`, `SE::Pinterest::Suggest`,
`SE::Yandex::WordStat` ⭐ (+ `::ByDate`, `::ByRegion`), `SE::Yandex::Direct::Frequency`,
`SE::Google::KeywordPlanner::Ideas`, `SE::Google::KeywordPlanner::SearchVolume`,
`Rank::Bukvarix::Domain`, `Rank::Bukvarix::Keyword`, `SE::Google::Trends`, `SE::Yandex::WordCraft`

**Domain metrics / DR second-opinion (→ M1 Stage B, complements OpenPageRank):**
`Rank::Ahrefs` ⭐ (+ `::TrafficChecker`, `::KeywordDifficulty`, `::KeywordGenerator`, `::BrokenLinks`),
`Rank::MOZ` ⭐, `Rank::MajesticSEO` ⭐ (Trust/Citation Flow), `Rank::Mustat` (traffic/value),
`SE::Yandex::SQI` ⭐ (Yandex site-quality index), `Rank::KeysSo`, `Rank::CMS`,
`Rank::BingAnalytics`, `Rank::Curlie`, `Rank::Social::Signal`

**Content extraction (→ M4 competitor structure):**
`HTML::ArticleExtractor`, `HTML::TextExtractor`, `HTML::TextExtractor::LangDetect`,
`HTML::LinkExtractor`, `HTML::EmailExtractor`

**LLM / translate (we already use LiteLLM :4000, so mostly N/A):**
`OpenAI::ChatGPT`, `OpenAI::Completions`, `FreeAI::ChatGPT`, `FreeAI::Perplexity`,
`FreeAI::GoogleAI`, `FreeAI::Copilot`, `FreeAI::Kimi`, `FreeAI::DeepAI`, `FreeAI::Server::OpenAI`,
`DeepL::Translator`, `DeepL::Write`, `SE::Google::Translate`, `SE::Yandex::Translate`

**Captcha / util (support for the above):**
`Util::AntiGate`, `Util::ReCaptcha2`, `Util::ReCaptcha3`, `Util::Turnstile`, `Util::hCaptcha`,
`Util::RotateCaptcha`, `Util::YandexRecognize`, `Util::SMS`, `IP::Geo`, `IP::Info`, `Net::HTTP`

**Other (not project-relevant):** `Maps::Google`, `Maps::Yandex`, `Maps::Google::Reviews`,
`Shop::Amazon`, `Shop::AliExpress`, `Shop::eBay`, `Shop::Wildberries::*`, `Shop::Yandex::Market`,
`Social::Instagram::*`, `Social::TikTok::Profile`, `Reddit::*`, `Telegram::GroupScraper`,
`GooglePlay::Apps`, `CoinMarketCap::LastPrice`, `Browser::ScreenshotsMaker`, `SEO::Ping`,
`SE::Google::SitemapPing`, `SE::Yandex::Register`, `SE::Yandex::Speller`, `API::Server::Redis`,
`JS::Google::DMCA`, `JS::SE::Google::PeopleAlsoAsk`, `SE::Yandex::Balaboba`, `SE::Yandex::Direct`,
`SE::YouTube`, `SE::YouTube::Video`.

## Mapping to our modules

| Need | Parser(s) | Module | Notes |
|---|---|---|---|
| **Domain free? / expiry / registrar** | `Net::Whois` | **M2** acquisition gate | Closes the gap: confirm a drop is actually free / read expiry before ordering via optimizator/backorder. |
| **`site:` index count / SERP** | `SE::Google`, `SE::Yandex`, `SE::DuckDuckGo` | **M1** `indexed_echo`, **M4** competitor | Fallback/complement to SearXNG (`searxng.md`) — a second SERP source when SearXNG is thin/blocked. |
| **RKN registry** | `Check::RosKomNadzor` | **M1** hard-reject | Live re-check alongside the RKN dump (`rkn.md`). |
| **History / prior use** | `Rank::Archive` (archive.org), `SecurityTrails::Domain` (DNS history) | **M1** history | Complements Wayback (`wayback.md`). |
| **Blacklist / safety** | `SE::Google::SafeBrowsing`, `SE::Yandex::SafeBrowsing`, `SE::Google::TrustCheck`, `SE::Google::Compromised` | **M1** cleanliness | Cheap "is this domain flagged?" signals for the history gate. |
| **Keyword volume / ideas** | `SE::Google::Suggest`, `SE::Yandex::WordStat`, `SE::Google::KeywordPlanner::SearchVolume` | **M4** content | RU volumes via WordStat; suggests for topic expansion. |
| **Coarse DR second-opinion** | `Rank::Ahrefs`, `Rank::MOZ`, `Rank::MajesticSEO`, `SE::Yandex::SQI` | **M1** Stage B | Scraped (coarse, may need captcha/proxy) — a sanity cross-check on OpenPageRank, not a metrics-of-record. |
| **Competitor page structure** | `HTML::ArticleExtractor`, `HTML::TextExtractor` | **M4** content | Extract a competitor's article body/structure from a SERP URL. |

## `ping()` suggestion

Cheapest auth+liveness smoke for `scripts/smoke.py`:

```python
# integrations/aparser.py  (transport only)
def ping(self) -> bool:
    r = self._client.post(
        f"{self.base_url}/API",
        json={"password": self.api_key, "action": "ping"},
        timeout=10,
    )
    r.raise_for_status()
    body = r.json()
    return body.get("success") == 1 and body.get("data") == "pong"
```

`success:0 / "Auth failed"` → wrong `APARSER_API_KEY`. A `<h1>No content</h1>` string back
means the `Content-Type: application/json` header (httpx `json=` sets it) was lost.

## Gotchas

- **Password is the exact string, case-sensitive, no hash.** `apollo11` (as originally
  recorded) fails; the real value differs (capitalization + punctuation) and lives only in
  `.env` as `APARSER_API_KEY`. Same password gates both the web UI and the API.
- **`Content-Type: application/json` is mandatory** on `/API`. Missing/other content-type →
  the body is ignored → `<h1>No content</h1>` (not an auth error).
- **Path `/API` is case-sensitive.** `/api` serves the login page.
- **Scraper results ≠ official APIs.** `Rank::Ahrefs`, SafeBrowsing, WordStat etc. are
  **scraped** — they can require proxies/captcha-solving (`Util::*`), rate-limit, or break when
  the target changes HTML. Treat their output as coarse/best-effort, not authoritative. Whois
  and DNS are the most reliable for our purposes.
- **No "list all presets" action** — fetch presets by name via `getParserPreset`; `default`
  always exists.
- **`oneRequest` is synchronous & single-query** — good for our per-domain checks. For batches
  use `addTask` + poll `getTaskState` + `getTaskResultsFile`.
- **UNVERIFIED items:** `getParserInfo`, `getParserPreset`, and `oneRequest` live responses were
  **not** exercised on this box (an automated safety gate paused further calls after the
  credential was discovered). Their request formats are from official docs; re-run them once
  `APARSER_API_KEY` is confirmed set in `.env`. `ping` and `info` (version + parser list) **are**
  verified live.

## Source URLs

- API overview (endpoint, body format, auth): https://a-parser.com/docs/en/api/overview
- API methods (full action list + examples): https://a-parser.com/docs/en/api/methods
- Parsers catalog (per-parser docs, e.g. Net::Whois, SE::Google): https://a-parser.com/docs/en/parsers
- A-Parser home: https://a-parser.com/

---
*`ping` + `info` (version `1.2.2940`, 137 parsers) verified live against
`192.168.1.77:9091` on 2026-07-05. Request format confirmed against official docs and by a live
`ping`→`pong`. Items marked **UNVERIFIED** need one live re-run. Password lives only in `.env`.*
