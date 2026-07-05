# Cloudflare API v4 — Provisioning Reference (M3)

Implementation-ready reference for the ~5 Cloudflare operations M3 Provisioning needs.
Scoped to: create zone → read assigned name servers → poll until active → create a
proxied A record (masks origin IP) → set SSL mode. Plus a `ping()` connectivity check.

Verified against official docs (developers.cloudflare.com/api), fetched 2026-07-05.
Anything not confirmed on an official page is marked **[UNVERIFIED]**.

---

## Purpose

Cloudflare is the DNS + origin-masking layer. For each portfolio domain we:
1. Add the domain as a **zone** and read back the two Cloudflare **name servers**.
2. (External step) Set those NS at the **registrar**. This is async and outside our control.
3. Poll the zone until `status = active` (Cloudflare has detected the NS change).
4. Create a **proxied A record** pointing at the aaPanel origin IP — the orange-cloud
   proxy hides the real origin IP.
5. Set **SSL/TLS mode** (target: `strict` = "Full (strict)") once a valid origin cert exists.

`integrations/cloudflare.py` = transport only. Orchestration/state lives in `services/` (M3).

---

## Base URL

```
https://api.cloudflare.com/client/v4/
```

Every endpoint below is relative to this base.

---

## Auth — Bearer scoped API token

Send the token on every request:

```
Authorization: Bearer <CLOUDFLARE_API_TOKEN>
Content-Type: application/json
```

Use a **scoped API token** (not the legacy global API key + email). Store in `.env`
(`CLOUDFLARE_API_TOKEN`). The `account_id` used in the create-zone body is a separate
config value (`CLOUDFLARE_ACCOUNT_ID`).

### Required token permission groups

Configure the token in dash → My Profile → API Tokens with these permission groups:

| Permission group | Scope | Why we need it |
|---|---|---|
| **Zone → Zone → Edit** | Account (all zones) | Create/manage zones; also lets you read zone status. |
| **Zone → DNS → Edit** | Account or per-zone | Create A / TXT records. |
| **Zone → SSL and Certificates → Edit** | Account or per-zone | Set SSL/TLS encryption mode (`strict`). |

Notes on **zone creation** specifically:
- `POST /zones` requires an **account-level** permission. The underlying permission is
  `com.cloudflare.api.account.zone.create` ("Create Zone"). In practice an account-scoped
  **Zone:Edit** token satisfies this; if creation fails you'll get an error like
  *"Requires permission 'com.cloudflare.api.account.zone.create' to create zones for the
  selected account"* — fix by scoping the token to the **account** (Include → your account)
  rather than to a specific existing zone. (Confirmed via error string in Cloudflare
  Community; the discrete "Create Zone" permission group may not appear separately in the
  dashboard UI — an account-scoped Zone:Edit covers it.) **[Partly UNVERIFIED — dashboard
  UI labels; behavior confirmed by the error message.]**
- Because we create *new* zones, the token **must be scoped to the account** ("All zones"
  under that account), not to an individual zone that doesn't exist yet.

`/user/tokens/verify` (the ping) works with **any** valid token — no special scope.

---

## Endpoints

For all endpoints, the response envelope is:

```json
{ "success": true, "errors": [], "messages": [], "result": { ... }, "result_info": { ... } }
```

Check `success == true`. On failure, `success == false` and `errors[]` carries
`{ "code": <int>, "message": <str> }`. `result_info` appears only on list endpoints.

---

### 1. Add a zone — `POST /zones`

Creates the zone and returns the assigned Cloudflare name servers.

**Body**

| Field | Req | Notes |
|---|---|---|
| `name` | yes | The domain, e.g. `"example.com"` (≤253 chars, RFC 1035). |
| `account.id` | yes | Our `CLOUDFLARE_ACCOUNT_ID`. Passed as an object: `{"id": "..."}`. |
| `type` | no | `"full"` (default — Cloudflare is authoritative DNS, requires NS change). Use `full` for our flow. Other values: `partial` (CNAME setup), `secondary`, `internal`. |

**Example request**

```json
POST /zones
{
  "name": "example.com",
  "account": { "id": "023e105f4ecef8ad9ca31a8372d0c353" },
  "type": "full"
}
```

**Example response (fields we consume in bold-comment)**

```json
{
  "success": true,
  "errors": [],
  "messages": [],
  "result": {
    "id": "023e105f4ecef8ad9ca31a8372d0c353",          // <- zone_id (store this)
    "name": "example.com",
    "name_servers": [                                    // <- SET THESE AT REGISTRAR
      "bob.ns.cloudflare.com",
      "lola.ns.cloudflare.com"
    ],
    "original_name_servers": [                           // registrar's old NS (informational)
      "ns1.originaldnshost.com",
      "ns2.originaldnshost.com"
    ],
    "status": "pending",                                 // <- pending until NS change detected
    "type": "full",
    "paused": false,
    "created_on": "2014-01-01T05:20:00.12345Z",
    "activated_on": null
  }
}
```

**We consume:** `result.id` (zone_id), `result.name_servers` (the 2 NS to set at the
registrar — this is the whole point of this call), `result.status`. Right after creation
`status` is normally `pending`.

> A zone already existing in another Cloudflare account returns an error (code 1061,
> "zone already exists") — handle idempotently in M3. **[UNVERIFIED — exact code string]**

---

### 2. Check zone status / get NS — `GET /zones/{zone_id}` and `GET /zones?name=`

Used to (a) re-read `name_servers` and (b) **poll `status` until `active`** after the
registrar NS change propagates.

#### 2a. By zone_id (preferred — we stored it at creation)

```
GET /zones/023e105f4ecef8ad9ca31a8372d0c353
```

**Example response**

```json
{
  "success": true,
  "result": {
    "id": "023e105f4ecef8ad9ca31a8372d0c353",
    "account": { "id": "023e105f4ecef8ad9ca31a8372d0c353", "name": "Example Account" },
    "name": "example.com",
    "name_servers": ["bob.ns.cloudflare.com", "lola.ns.cloudflare.com"],
    "status": "active",          // <- poll this: pending -> active
    "paused": false,
    "type": "full"
  }
}
```

**Status values:** `initializing`, `pending`, `active`, `moved`. We wait for `active`.
Cloudflare only flips `pending → active` after it detects the NS delegation at the
registrar (can take minutes to 24h+; it is external and async — see Gotchas).

#### 2b. By name (fallback / dedupe before creating)

```
GET /zones?name=example.com
```

- `name` defaults to an **exact** match. Operators available: `equal` (default),
  `contains`, `starts_with`, `ends_with` (append as `name=<op>:<value>` per docs).
- Also filterable: `status` (e.g. `?status=active`), `account.id`, `page`, `per_page`,
  `order`, `match`.

**Example response (array + result_info)**

```json
{
  "success": true,
  "result": [
    {
      "id": "023e105f4ecef8ad9ca31a8372d0c353",
      "name": "example.com",
      "status": "active",
      "name_servers": ["bob.ns.cloudflare.com", "lola.ns.cloudflare.com"]
    }
  ],
  "result_info": { "count": 1, "page": 1, "per_page": 20, "total_count": 1 }
}
```

Use 2b to look up a zone_id if we lost it, or to check "does this domain already have a
zone" before `POST /zones`. Empty `result: []` ⇒ no zone yet.

**Polling suggestion:** poll `GET /zones/{zone_id}` every ~60s (or a backoff), cap total
wait (e.g. 24h) — do **not** hammer; the whole account shares 1200 req / 5 min.

---

### 3. Create DNS record — proxied A (masks origin) — `POST /zones/{zone_id}/dns_records`

Points the apex (or host) at the aaPanel origin IP, proxied so the real IP is hidden.

**Body (A record)**

| Field | Req | Notes |
|---|---|---|
| `type` | yes | `"A"`. |
| `name` | yes | Full record name incl. zone. For apex use the domain itself, e.g. `"example.com"`. (`"@"` also resolves to apex.) Punycode for IDNs. |
| `content` | yes | Origin IPv4, e.g. aaPanel server IP. |
| `proxied` | yes* | `true` = orange cloud → **origin IP hidden**, Cloudflare in front. |
| `ttl` | no | `1` = "automatic" (required/forced when `proxied: true`). Otherwise 60–86400. |
| `comment` | no | Freeform, e.g. `"portfolio origin"`. |

**Example request**

```json
POST /zones/023e105f4ecef8ad9ca31a8372d0c353/dns_records
{
  "type": "A",
  "name": "example.com",
  "content": "198.51.100.4",
  "proxied": true,
  "ttl": 1,
  "comment": "aaPanel origin (proxied)"
}
```

**Example response**

```json
{
  "success": true,
  "result": {
    "id": "372e67954025e0ba6aaa6d586b9e0b59",   // dns_record id (store if we'll update it)
    "type": "A",
    "name": "example.com",
    "content": "198.51.100.4",
    "proxied": true,
    "ttl": 1,
    "zone_id": "023e105f4ecef8ad9ca31a8372d0c353",
    "created_on": "2014-01-01T05:20:00.12345Z",
    "modified_on": "2014-01-01T05:20:00.12345Z"
  }
}
```

**We consume:** `result.id` (to update/delete later), and confirm `proxied: true`.

> To make M3 idempotent: `GET /zones/{zone_id}/dns_records?type=A&name=example.com` first;
> if it exists, `PATCH`/`PUT /zones/{zone_id}/dns_records/{id}` instead of re-POSTing (a
> duplicate identical record 400s). **[UNVERIFIED — exact dup-record error code]**

---

### 4. Create TXT record (future domain verification) — `POST /zones/{zone_id}/dns_records`

Same endpoint, `type: "TXT"`. We'll use this later for GSC / provider verification tokens.

**Example request**

```json
POST /zones/023e105f4ecef8ad9ca31a8372d0c353/dns_records
{
  "type": "TXT",
  "name": "example.com",
  "content": "google-site-verification=abc123def456",
  "ttl": 1,
  "comment": "GSC verification"
}
```

- `content`: RFC 1035 quoted character-string(s), each ≤255 bytes. For a plain token the
  API accepts the raw string as shown; long values are split into multiple strings.
- `proxied` is not applicable to TXT (ignore / omit).

**Example response**

```json
{
  "success": true,
  "result": {
    "id": "9a7806061c88ada191ed06f989cc3dac",
    "type": "TXT",
    "name": "example.com",
    "content": "google-site-verification=abc123def456",
    "ttl": 1,
    "zone_id": "023e105f4ecef8ad9ca31a8372d0c353",
    "created_on": "2014-01-01T05:20:00.12345Z",
    "modified_on": "2014-01-01T05:20:00.12345Z"
  }
}
```

---

### 5. Set SSL/TLS mode — `PATCH /zones/{zone_id}/settings/ssl`

Set once a valid origin certificate is in place. Target mode for us: `strict`
("Full (strict)"). This is a zone-setting edit under the generic
`PATCH /zones/{zone_id}/settings/{setting_id}` (here `setting_id = ssl`).

**Values:** `"off"`, `"flexible"`, `"full"`, `"strict"`.
`strict` requires a CA-signed (or Cloudflare Origin CA) cert on the origin.

**Example request**

```json
PATCH /zones/023e105f4ecef8ad9ca31a8372d0c353/settings/ssl
{ "value": "strict" }
```

**Example response**

```json
{
  "success": true,
  "result": {
    "id": "ssl",
    "value": "strict",
    "editable": true,
    "modified_on": "2014-01-01T05:20:00.12345Z"
  }
}
```

> If the origin has no valid cert yet, use `full` first (encrypts CF↔origin but doesn't
> validate the cert), then upgrade to `strict` after installing a Cloudflare Origin CA
> cert on aaPanel. `flexible` is insecure (CF↔origin plaintext) — avoid.

---

### 6. `ping()` — auth/connectivity check — `GET /user/tokens/verify`

Cheap, scope-free way to confirm the token is valid and Cloudflare is reachable. Ideal for
`integrations/cloudflare.py::ping()` and `scripts/smoke.py`.

**Request**

```
GET /user/tokens/verify
Authorization: Bearer <CLOUDFLARE_API_TOKEN>
```

**Example response**

```json
{
  "success": true,
  "result": {
    "id": "ed17574386854bf78a67040be0a770b0",
    "status": "active",
    "expires_on": "2020-01-01T00:00:00Z"
  }
}
```

`ping()` = HTTP 200 **and** `success == true` **and** `result.status == "active"`.
`status` may also be `disabled` or `expired`. `expires_on` is absent for non-expiring tokens.

---

## Provisioning order (M3, idempotent)

```
1. POST /zones                      { name, account:{id}, type:"full" }
   -> store result.id (zone_id) and result.name_servers[]     (status: pending)

2. [REGISTRAR STEP — external, manual/async]
   Set the two result.name_servers[] as the domain's NS at the registrar.
   Cloudflare cannot do this; it happens at the registrar and propagates on its own.

3. Poll GET /zones/{zone_id} until result.status == "active"
   (every ~60s w/ backoff; cap total wait; do not exceed rate limit)

4. POST /zones/{zone_id}/dns_records   A record, proxied:true, ttl:1, content=origin_ip
   (requires the zone to be active — see Gotchas)     -> origin IP now masked

5. (optional now / later) POST TXT records for verification (GSC, etc.)

6. PATCH /zones/{zone_id}/settings/ssl  { value: "full" } then "strict"
   after a valid origin cert (Cloudflare Origin CA) is installed on aaPanel.
```

Make each step idempotent: check-then-act (GET before POST), tolerate "already exists".

---

## Rate limits

- **Global:** **1200 requests per 5 minutes per user**, cumulative across dashboard +
  API key + API token. Exceeding it blocks **all** calls for the rest of the 5-min window
  with **HTTP 429 – Too Many Requests**.
- Implication: our polling loop (step 3) and any batch provisioning must be paced. Use
  httpx + backoff; on 429, back off for the remainder of the window. Some product-specific
  endpoints have tighter secondary limits, but the 1200/5min global is what governs our
  zone/DNS calls.

---

## Gotchas

- **NS change is async & external.** Step 2 happens at the *registrar*, not via this API.
  The zone stays `pending` until Cloudflare independently detects the delegation — minutes
  to 24h+, occasionally longer. Never assume `active` right after create. This is exactly
  why M3 must persist state and poll rather than run straight through.
- **Proxied A record wants an active zone.** Records can be *created* while pending, but
  proxying (and SSL) only actually take effect once the zone is `active` and traffic flows
  through Cloudflare. Order it after the poll to avoid a window where the origin IP is
  exposed via a grey-cloud (DNS-only) record. Ensure `proxied: true` — a grey-cloud record
  publishes the real origin IP.
- **`ttl` with proxied records.** When `proxied: true`, TTL is forced to automatic; send
  `ttl: 1`. Sending another value may error or be ignored.
- **SSL `strict` needs a valid origin cert.** "Full (strict)" validates the origin
  certificate. Without a CA-signed / Cloudflare Origin CA cert on aaPanel you'll get 526
  errors. Sequence: `full` → install Origin CA cert → `strict`.
- **Token must be account-scoped for zone creation.** A token scoped only to an existing
  zone cannot create *new* zones (`com.cloudflare.api.account.zone.create`). Scope Include
  to the **account**.
- **Zone already exists elsewhere.** If the domain is active in another Cloudflare account,
  `POST /zones` fails; the takeover/verification flow differs. Treat as a handled error.
- **Envelope, not HTTP status, is truth.** Always check `success` + `errors[]`; a 200 can
  still carry `success:false` on some paths. **[UNVERIFIED — behavior varies by endpoint]**
- **Apex naming.** For the apex A record, `name` = the domain (`example.com`) or `@`; both
  target the apex. Don't send a bare `@` where the API expects the FQDN if it rejects it —
  prefer the full domain string.

---

## Source URLs (official, fetched 2026-07-05)

- Create Zone — https://developers.cloudflare.com/api/resources/zones/methods/create/
- Get Zone Details — https://developers.cloudflare.com/api/resources/zones/methods/get/
- List Zones — https://developers.cloudflare.com/api/resources/zones/methods/list/
- Create DNS Record — https://developers.cloudflare.com/api/resources/dns/subresources/records/methods/create/
- Edit Zone Setting (SSL) — https://developers.cloudflare.com/api/resources/zones/subresources/settings/methods/edit/
- Verify Token — https://developers.cloudflare.com/api/resources/user/subresources/tokens/methods/verify/
- API token permissions — https://developers.cloudflare.com/fundamentals/api/reference/permissions/
- Create API token — https://developers.cloudflare.com/fundamentals/api/get-started/create-token/
- API rate limits — https://developers.cloudflare.com/fundamentals/api/reference/limits/
- Zone-create permission (community, error-string confirmation) —
  https://community.cloudflare.com/t/requires-permission-com-cloudflare-api-account-zone-create-to-create-zones-for-the-selected-account/576969
