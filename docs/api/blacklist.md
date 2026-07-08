# Domain Blocklists — Spamhaus DBL & SURBL (DNS-based)

> Integration reference for `integrations/` transport layer. Used in M1 Domain
> Intelligence as a **spam/risk hard-flag** in donor scoring (see `docs/DONORS.md`,
> history/risk stage).

## Purpose — spam/risk hard-flag

A candidate drop domain must have a **clean history** (project rule: not adult/pharma/
casino/spam in the past, not in RKN, not in blacklists — see `CLAUDE.md`). DNS domain
blocklists are the cheapest, near-realtime signal that a domain's *name itself* is (or
recently was) associated with spam, phishing, malware, or botnet abuse.

Rule in code: **if a candidate domain is listed on a domain blocklist → `blacklisted=true`
→ flag/reject** (hard fail of the history gate). This is a boolean risk signal, not a
quality score. Two providers, queried independently, OR'd together:

- **Spamhaus DBL** — Domain Block List (domains only).
- **SURBL** `multi` — combined phishing/malware/abuse/cracked/etc. list.

Both are **domain-name** blocklists: you query the domain directly. **No reverse-IP is
needed** (unlike IP-based DNSBLs such as Zen/SBL, which require reversing the octets).

---

## Spamhaus DBL

### Query format

Append the domain to the zone and resolve an **A record**:

```
<domain>.dbl.spamhaus.org          → A record lookup
```

- The domain is inserted **as-is**, most-specific label first (it is a normal FQDN
  prepended to the zone). DBL is a **wildcarded zone**: you can pass a full hostname
  (`www.example.com`) or the registrable domain (`example.com`) — the standard
  practice is to query the **registrable domain** (`example.com`). For our use (we
  evaluate the domain we might buy), query the bare registrable domain.
- **Do not** query IP addresses against DBL — it returns an error code (see below).

### Return-code table (verified against current Spamhaus docs)

A **listed** domain returns an A record in the `127.0.1.x` range. **NXDOMAIN = not
listed** (clean). Two families:

**Inherently-bad domains (safe to block) — range `127.0.1.2`–`127.0.1.99`:**

| A record       | Meaning                        |
|----------------|--------------------------------|
| `127.0.1.2`    | spam domain                    |
| `127.0.1.4`    | phish domain                   |
| `127.0.1.5`    | malware domain                 |
| `127.0.1.6`    | botnet C&C domain              |

**Abused-legit domains (compromised legit sites) — range `127.0.1.102`–`127.0.1.199`:**
These are *legitimate* domains observed being abused; Spamhaus says use them **only for
scoring**, not hard blocking (a legit compromised site can be de-listed once cleaned).

| A record       | Meaning                                  |
|----------------|------------------------------------------|
| `127.0.1.102`  | abused legit spam                        |
| `127.0.1.103`  | abused legit redirector / URL shortener  |
| `127.0.1.104`  | abused legit phish                       |
| `127.0.1.105`  | abused legit malware                     |
| `127.0.1.106`  | abused legit botnet C&C                  |

> The docs also present a `127.0.2.x` / `2002`–`2199` "decimal" duplicate scheme in the
> DQS documentation, but the canonical DNS answers are the `127.0.1.x` values above.
> `127.0.1.255` is **not** a listing — it means **"IP queries prohibited"** (you queried
> an IP against DBL). Treat it as an error, not a hit.

**Error / blocked-query codes — `127.255.255.x` (NOT listings, do not treat as a hit):**

| A record          | Meaning                                                          |
|-------------------|------------------------------------------------------------------|
| `127.255.255.252` | typing error in the DNSBL zone name (e.g. `dlb`/`zin` typo)      |
| `127.255.255.254` | **query blocked — came via an open/public resolver or an IP with generic rDNS** |
| `127.255.255.255` | **query blocked — excessive queries (outside Fair Use)**        |
| `127.255.255.250` | (DQS) DQS key disabled                                           |
| `127.255.255.251` | (DQS) DQS key illegally used                                    |

**These `127.255.255.x` codes are the single most important gotcha.** If your lookups
start returning `127.255.255.254`/`.255`, you are **not** getting listing data at all —
every domain will silently look "listed" or your logic will misfire. Detect them
explicitly and treat as "lookup unavailable", never as `blacklisted=true`.

### Interpreting a lookup

- **NXDOMAIN** → not listed → clean (pass).
- **A record in `127.0.1.2`–`127.0.1.106`** → listed → `blacklisted=true` (fail the gate).
  Optionally record the sub-reason (spam/phish/malware/botnet/abused-legit) for the
  scoring log; abused-legit (`.102`–`.106`) may be treated as a softer signal.
- **A record `127.0.1.255`** → you queried an IP; fix the caller.
- **A record `127.255.255.x`** → configuration/quota error; alert, don't score.

### Example dig / interpretation

```bash
# A known permanently-listed DBL test point:
dig +short test.dbl.spamhaus.org A
# → 127.0.1.2          (listed as "spam domain")  == blacklisted

# A clean domain:
dig +short example.com.dbl.spamhaus.org A
# → (empty / NXDOMAIN)                            == not listed

# If you see this, your resolver is the problem, NOT the domain:
# → 127.255.255.254    (blocked: open/public resolver)
```

`test.dbl.spamhaus.org` is Spamhaus's permanent test entry — always listed, safe for
`ping()`.

### DQS (paid/keyed) query format

With a Data Query Service account key, the zone changes to a private host and the domain
is prepended before the key:

```
<domain>.<dqs-key>.dbl.dq.spamhaus.net      → A record lookup
```

`<dqs-key>` is a 26-character per-account code. Same return codes as above. DQS is **not**
blocked on public-resolver grounds (the key identifies you) and has its own quota.

---

## SURBL (`multi`)

### Query format

Append the domain to the `multi` zone and resolve an **A record**:

```
<domain>.multi.surbl.org           → A record lookup
```

Query the **registrable domain** (SURBL extracts the significant domain of a URI; passing
the bare registrable domain is the intended input). **NXDOMAIN = not listed** (clean).

### Return-code table — bitmasked `127.0.0.x` (verified against current SURBL docs)

SURBL `multi` is a **single bitmasked list**: one A record per domain whose **last octet
is the OR of the list bits** the domain is on. Add bits to read multiple memberships.

| Bit (last octet) | List | Meaning                          |
|------------------|------|----------------------------------|
| `2`              | —    | **reserved / legacy, currently unused** (historically `SC` SpamCop). Do not rely on it. |
| `4`              | DM   | Disposable Mail domains          |
| `8`              | PH   | Phishing sites                   |
| `16`             | MW   | Malware sites                    |
| `32`             | CT   | Click-tracker domains            |
| `64`             | ABUSE| Spam & abuse sites               |
| `128`            | CR   | Cracked sites                    |

**Bitmask reading:** the fourth octet is a sum. Examples:

- `127.0.0.8`  → PH (phishing) only.
- `127.0.0.64` → ABUSE only.
- `127.0.0.80` → `16 + 64` → MW **and** ABUSE.
- `127.0.0.72` → `8 + 64` → PH **and** ABUSE.

To test a specific list, bit-AND the last octet: `octet & 8` != 0 ⇒ phishing, etc. For our
**hard-flag** purpose we don't care which bit — **any** `127.0.0.x` answer (with x ≥ 2)
means listed → `blacklisted=true`.

> **Gotcha — `127.0.0.1` is NOT a listing.** A response of `127.0.0.1` from the public
> SURBL nameservers means **your access is blocked** (public-resolver / policy violation),
> analogous to Spamhaus's `127.255.255.254`. SURBL says the public zone should only ever
> return a `127.0.0.x` (x from the table) **or NOTHING (NXDOMAIN)**; a `127.0.0.1` is an
> error, treat as "lookup unavailable", never as a hit.

The task brief's rough mapping (`2/4/8/16/64/128 = phishing/malware/abuse/cracked`) is
**slightly outdated** — the current verified assignment is the table above (`8=PH`,
`16=MW`, `64=ABUSE`, `128=CR`, `4=DM`, `32=CT`, `2` unused). Re-verify at integration time.

---

## Usage limits & resolver rules (the big gotcha)

Both services are "free-ish" for low volume but have **hard restrictions that will
silently break naive lookups**. Read this section before writing the client.

### 1. No open / public resolvers

- **Spamhaus public mirrors block queries that arrive via large open resolvers** —
  **Google `8.8.8.8`, Cloudflare `1.1.1.1`, Quad9, OpenDNS, big cloud resolvers**, etc.
  A blocked query returns **`127.255.255.254`** (not real data). Rationale: Spamhaus can't
  attribute/meter usage coming through shared public resolvers, so they refuse them.
- Fair Use Policy wording: you must query **"from a recursive resolver run on your own
  network, or from a public resolver which supports ECS"** — and the network originating
  the DNS query must be **identifiable** (no generic rDNS, no shared hosting egress).
  In practice Google/Cloudflare are blocked regardless, so **assume public resolvers = no**.
- **SURBL** applies the same principle: querying its public nameservers through a
  disallowed/open resolver returns **`127.0.0.1`** = access blocked.

**Consequence:** running the lookup from a Docker container that uses the host's `8.8.8.8`/
`1.1.1.1` will return error codes for *every* domain. You **must** point the lookups at your
own resolver, or use a keyed data service.

### 2. Free-usage volume limits

- **Spamhaus free Data Query Service (DQS):** query volume **must not consistently exceed
  100,000 queries/day** (current, verified — free DQS Terms/Fair-Use). Non-commercial,
  "Know Your Client" (keep contact details current).
- **Spamhaus public mirrors (no key):** the Fair Use Policy **no longer publishes a hard
  number** — it says volume "must not exceed volumes reasonably expected in circumstances
  of non-commercial use." Historically the free ceiling was quoted as ~**300,000
  queries/day** and a limited datafeed size; that exact **300k figure is now
  historical/UNVERIFIED** — the currently documented concrete number is the **100k/day**
  free-DQS limit. High volume / commercial / ISP / shared-hosting use requires a paid
  **Datafeed** (rsync) or a **DQS** subscription.
- **SURBL:** free use is for low-volume, non-commercial querying via the public mirrors
  under the same open-resolver restriction. Exact free query-count ceiling is **not
  published as a hard number (UNVERIFIED)**; heavy/commercial use requires a paid data
  feed (SURBL offers rsync, DNS Private Query Service, RPZ, CSV, API, RTF/JSON tiers via
  resellers).

### 3. Above the limit → paid keyed service

- **Spamhaus:** switch to **DQS** (per-account 26-char key), query
  `<domain>.<dqs-key>.dbl.dq.spamhaus.net`. The key both authenticates and lifts the
  open-resolver restriction (you may then use any resolver). Free DQS tier exists
  (≤100k/day, non-commercial); paid above that.
- **SURBL:** switch to a **paid data feed / DNS Private Query Service** (keyed private
  zone) or rsync the zone locally.

---

## Implementation notes

Transport lives in `integrations/` (e.g. `integrations/blacklist.py`); the OR-ing / scoring
decision lives in `services/`. Use **`dnspython`** (already a natural fit — pure DNS, no
HTTP).

```python
# integrations/blacklist.py  (transport only)
import dns.resolver
import dns.exception

# Point at OUR OWN resolver (unbound/bind on the app network), NOT 8.8.8.8/1.1.1.1.
_resolver = dns.resolver.Resolver(configure=False)
_resolver.nameservers = ["127.0.0.1"]      # local unbound; from config
_resolver.timeout = 3.0                     # per-try
_resolver.lifetime = 5.0                    # total budget per lookup

# Any of these mean "lookup failed / blocked", NOT "listed".
_SPAMHAUS_ERRORS = {
    "127.255.255.252", "127.255.255.254", "127.255.255.255",
    "127.255.255.250", "127.255.255.251", "127.0.1.255",
}
_SURBL_ERRORS = {"127.0.0.1"}

def _lookup(qname: str) -> list[str]:
    """Return list of A-record strings, or [] for NXDOMAIN (not listed)."""
    try:
        answers = _resolver.resolve(qname, "A")
        return [r.address for r in answers]
    except dns.resolver.NXDOMAIN:
        return []                           # not listed = clean
    except dns.resolver.NoAnswer:
        return []                           # no A record = not listed
    except (dns.resolver.NoNameservers, dns.exception.Timeout) as e:
        raise LookupUnavailable(str(e))     # infra problem — do NOT score as clean

def check_dbl(domain: str) -> BlacklistResult:
    ips = _lookup(f"{domain}.dbl.spamhaus.org")
    if not ips:
        return BlacklistResult(listed=False, source="dbl")
    if any(ip in _SPAMHAUS_ERRORS or ip.startswith("127.255.255.") for ip in ips):
        raise LookupUnavailable(f"DBL returned error code(s) {ips} — resolver/quota issue")
    listed = [ip for ip in ips if ip.startswith("127.0.1.") and ip != "127.0.1.255"]
    return BlacklistResult(listed=bool(listed), source="dbl", codes=listed)

def check_surbl(domain: str) -> BlacklistResult:
    ips = _lookup(f"{domain}.multi.surbl.org")
    if not ips:
        return BlacklistResult(listed=False, source="surbl")
    if any(ip in _SURBL_ERRORS for ip in ips):
        raise LookupUnavailable(f"SURBL returned {ips} — access blocked (open resolver?)")
    listed = [ip for ip in ips if ip.startswith("127.0.0.") and ip != "127.0.0.1"]
    return BlacklistResult(listed=bool(listed), source="surbl", codes=listed)
```

Key points:

- **NXDOMAIN / NoAnswer = not listed = clean.** This is the normal, dominant path — most
  domains are not on the list. Do not treat NXDOMAIN as an error.
- **Distinguish "not listed" from "lookup failed".** A timeout, `NoNameservers`, or any
  `127.255.255.x`/`127.0.0.1` error code must **not** be silently read as "clean" — raise
  `LookupUnavailable` so the scoring layer can retry/park the domain rather than passing a
  possibly-dirty domain. (Fail-closed for a risk gate is safer than fail-open.)
- **Never treat an error code as a hit either.** Only `127.0.1.2`–`127.0.1.106` (DBL) and
  `127.0.0.x` with x ≥ 2 (SURBL) are real listings.
- **Timeouts:** DNS is fast; a 3s per-try / 5s total budget is plenty. Back off and cache.
  Blocklist membership changes but re-querying the same drop candidate repeatedly is waste —
  cache the result for hours (respect that data can change, so don't cache for days).
- **Resolver config is the whole ballgame:** set `nameservers` explicitly to our local
  resolver from `.env` — do **not** inherit the container's `/etc/resolv.conf` if that
  points at `8.8.8.8`/`1.1.1.1`.

---

## Recommendation for our volume

**Реализовано (2026-07-08):** `unbound` поднят в `docker-compose.yml` (сервис `unbound`,
сеть `combine`, статический `172.28.0.53` — dnspython принимает только IP-литерал, не имя
сервиса). `DNS_RESOLVER=172.28.0.53` в `.env.example`. `integrations/blacklist.py` уже читал
`settings.DNS_RESOLVER` (и `ping()`, и `is_blacklisted()`/`_resolve()` — единый путь). На
боксе: раскатать compose-изменения, задать `DNS_RESOLVER` в `.env`, `↻ перепроверить` на
`/diag`. Free DQS-ключ (вариант 2 ниже) не подключён — не нужен, пока `unbound` работает.

Our M1 volume is **modest** (we score a stream of drop candidates, not mail-server-scale
millions/day). Two viable setups:

1. **Own local recursive resolver (recommended to start): run `unbound` (or `bind`) in the
   Compose stack**, and point both DBL and SURBL lookups at it. This satisfies the
   "identifiable network / not an open public resolver" rule for free, keeps us under the
   free volume ceiling comfortably, and costs nothing. This is the natural fit for the
   project (Docker Compose already present). One caveat: our container's public egress IP
   must have sane rDNS / not be a flagged shared-hosting range.
2. **Spamhaus DQS free key (`≤100k/day`):** if we ever query from an environment where we
   can't guarantee a compliant resolver (or want to use any resolver), get a **free DQS
   account** and query `<domain>.<key>.dbl.dq.spamhaus.net`. Cleanest, key-authenticated,
   no open-resolver headache. Non-commercial only; graduate to paid DQS/Datafeed if volume
   or commercial status grows. SURBL's equivalent is a paid/keyed feed — for SURBL, the
   local-resolver route is the free option.

**Concrete recommendation:** deploy a local **unbound** and query the free public zones
(`dbl.spamhaus.org`, `multi.surbl.org`) through it; keep a **free Spamhaus DQS key** in
`.env` as the fallback/alternative path. Cache results; alert on any `127.255.255.x` /
`127.0.0.1` error code (means our resolver setup regressed). Store the boolean +
sub-reason in the domain's history/risk record.

---

## `ping()` suggestion

Cheapest liveness check = resolve a **known permanently-listed test point** and assert we
get the expected listing code (this proves both DNS reachability **and** that we are not
being blocked as an open resolver — a blocked query would return an error code instead).

```python
# integrations/blacklist.py
def ping(self) -> bool:
    # Spamhaus DBL permanent test entry — always listed as 127.0.1.2.
    ips = _lookup("test.dbl.spamhaus.org")
    return "127.0.1.2" in ips        # False if empty (unreachable) or an error code
```

- `test.dbl.spamhaus.org` → expect `127.0.1.2`. Empty/NXDOMAIN or a `127.255.255.x`
  answer ⇒ resolver misconfig or open-resolver block ⇒ `ping()` fails (correctly).
- SURBL provides `test.multi.surbl.org` as a permanent test point (returns a `127.0.0.x`
  listing) — **UNVERIFIED** exact octet; confirm at integration and add a parallel SURBL
  ping if desired. Good enough for `scripts/smoke.py` to prove the DNS-blocklist path.

---

## Source URLs

**Spamhaus:**
- DBL — what it is / how it works: https://www.spamhaus.org/blocklists/domain-blocklist/
- DBL return-code changes (code table): https://www.spamhaus.org/resource-hub/dnsbl/changes-in-spamhaus-dbl-dnsbl-return-codes/
- Return codes reference (datasets docs): https://docs.spamhaus.com/datasets/docs/source/10-data-type-documentation/datasets/030-datasets.html
- Zones / DQS zone names: https://docs.spamhaus.com/datasets/docs/source/10-data-type-documentation/datasets/040-zones.html
- DQS query format: https://docs.spamhaus.com/datasets/docs/source/70-access-methods/data-query-service/040-dqs-queries.html
- Free DNSBL usage FAQ (public-resolver rule): https://www.spamhaus.org/faqs/dnsbl-usage/
- DNSBL Fair Use Policy: https://www.spamhaus.org/blocklists/dnsbl-fair-use-policy/
- Free DQS Terms / Fair Use (100k/day): https://www.spamhaus.com/terms-of-use-fair-use-policy-for-free-data-query-service/
- Error return codes explainer (`127.255.255.x`): https://www.spamhaus.org/resource-hub/dnsbl/using-our-public-mirrors-check-your-return-codes-now/
- DQS FAQ: https://www.spamhaus.com/faqs/data-query-service/

**SURBL:**
- SURBL lists / query format: https://www.surbl.org/lists
- SURBL usage guidelines (blocked-access `127.0.0.1`, data feeds): https://www.surbl.org/guidelines
- SURBL MULTI return codes (reseller docs): https://www.securityzones.net/surbl/surbl-multi/
- New MULTI return codes announcement: https://www.securityzones.net/new-return-codes-added-to-surbl-multi/

---
*DBL `127.0.1.x` code table and Spamhaus `127.255.255.x` error codes verified against
current Spamhaus docs (2026-07-05). SURBL bitmask table verified against SURBL/SecurityZones
docs. Items marked **UNVERIFIED** (historical 300k public-mirror figure, SURBL exact free
ceiling, SURBL test-point octet) need a live check at integration time. Free DQS limit
(100k/day) is the current published concrete number.*
