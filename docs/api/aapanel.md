# aaPanel API — implementation reference (system / website / ssl)

Scope: only what the provisioning module (**M3**) needs — a health check, create/list/delete
nginx vhosts, and issue an origin SSL cert. aaPanel has 350+ methods; this doc documents ~10.

Roles reminder (see CLAUDE.md): aaPanel is the **provisioning engine** on the VPS (vhost + origin
SSL). DNS and origin masking stay on **Cloudflare**. We need a valid origin cert so Cloudflare can
run **Full (strict)** SSL.

---

## Purpose

Our app (`backend/app/integrations/aapanel.py`, `AaPanelClient`) calls aaPanel over HTTP to:

- `ping()` — cheap system check that the panel + API are reachable and the key is valid.
- `add_site()` — create an nginx vhost (pure-static or PHP) with a document root.
- `list_sites()` — enumerate sites for **idempotency** (check-before-create) and to resolve `id`.
- `delete_site()` — teardown for lifecycle (**M6**).
- `apply_ssl()` — issue + deploy a Let's Encrypt origin cert bound to the site.

---

## Base URL & prerequisites

- Base URL: `https://$HOST:8888` (config: `settings.AAPANEL_URL`). Port is the panel port; it may
  differ if the admin changed it, and the panel may be behind a random security-entrance path — but
  the **API endpoints below sit at the panel root**, not under the entrance path.
- Enable the API: aaPanel → **Settings (设置) → API interface** → toggle **on**. This reveals the
  **Interface key** (`api_sk`) and an **IP whitelist**.
- **IP whitelist is mandatory.** Add the IP of the host that runs our app. If the app runs on the
  same VPS as the panel, whitelist `127.0.0.1` and point `AAPANEL_URL` at `https://127.0.0.1:8888`
  (avoids the public cert/hostname mismatch too).
- All requests are **POST**. All responses are **JSON**.
- The panel serves a **self-signed cert** on :8888 → TLS verification must be disabled client-side
  (see Gotchas).

Source (auth/prereqs): official API doc PDF, p.1 — <https://www.aapanel.com/Document/api.pdf>
("Precautions": POST, save cookie, IP whitelist, JSON).

---

## Auth (exact) — token + cookies

Every request carries two extra POST fields:

| Field | Value |
|---|---|
| `request_time` | current Unix timestamp **in seconds** (string). `str(int(time.time()))` |
| `request_token` | `md5( str(request_time) + md5(api_sk) )` |

`api_sk` is the panel's **Interface key** (config: `settings.AAPANEL_API_KEY`). The md5 is **chained**:
first hash the secret key alone, then concatenate the *same* `request_time` string in front of that
hash and hash again. Both fields go in the POST body alongside the endpoint's own params.

Official signature spec (verbatim, PDF p.1):

```
api_sk        = Interface key (obtained in the panel settings page - API interface)
request_time  = Unix timestamp of current request time ( php: time() / python: time.time() )
request_token = md5(string(request_time) + md5(api_sk))
```

### Worked example (verify against your own md5)

```
api_sk        = "s2cKq9F...EXAMPLE_SECRET"
request_time  = "1751731200"                       # seconds
md5(api_sk)   = "a249bddbcd20a18fbc3da7701fc4c064"
request_token = md5("1751731200" + "a249bddbcd20a18fbc3da7701fc4c064")
              = "9c16d7111b22cfa27116ed4e97db655e"
```

Our existing stub `_auth_fields()` in `backend/app/integrations/aapanel.py` already implements this
**correctly** (`md5(t + md5(api_sk))`, seconds). Keep it.

### Seconds, not milliseconds

The current HTML docs page (`aapanel.com/docs/api/api-list.html`) says `request_time` is in
*milliseconds* — **that is wrong / misleading**. The authoritative PDF and every working client use
**seconds** (`time()` / `time.time()`). This matters: the server validates a freshness/clock-skew
window on `request_time`, so a millisecond value (~`1751731200000`) is ~1000× off and the request is
rejected. Always send seconds.

> Note on int vs float: the official Python demo uses `time.time()` (a float). The server recomputes
> the token from the *exact string you sent*, so int-seconds and float-seconds both authenticate.
> Int seconds (`str(int(time.time()))`) is the safe, canonical choice and what we use. The PHP lib
> uses `time()` (int seconds) too.

### Cookies — must persist and resend

After the first authenticated request the panel sets a session cookie. You **must store the cookie
jar and resend it on every subsequent request** ("save the cookie and attach a cookie on each
request" — PDF p.1). In httpx this means reusing a single `httpx.Client`/`AsyncClient` instance (it
keeps a cookie jar) for the life of the `AaPanelClient`, not a fresh client per call. `BaseClient`
should hold one persistent client. Without cookie persistence some panel builds re-run heavier
auth per call and can rate-limit or fail intermittently.

Source (auth): PDF p.1; PHP lib `HttpClient.php` — `request_token => md5($now . md5($apiKey))`,
`request_time => $now` where `$now = time()`, and cURL `CURLOPT_COOKIEJAR`/`CURLOPT_COOKIEFILE`
persist cookies to `md5(baseUrl).'.cookie'`:
<https://github.com/AzozzALFiras/aapanel-api/blob/main/src/HttpClient.php>

---

## Endpoints

All paths are relative to the base URL. All are **POST**; append the two auth fields to every body.
Where a query string is shown (e.g. `?action=...`), those go in the URL and the auth + other fields
go in the POST body.

### SYSTEM — check (for `ping()`)

Pick the **cheapest** endpoint. Two good options:

| Purpose | URI | Params | Notes |
|---|---|---|---|
| Task count (lightest) | `/ajax?action=GetTaskCount` | none | Returns a bare integer (count of running install tasks). Cheapest; ideal for `ping()`. |
| System totals | `/system?action=GetSystemTotal` | none | Returns OS/CPU/mem/panel version JSON. Slightly heavier but confirms more. |

Recommended for `ping()`: **`/ajax?action=GetTaskCount`** — success = HTTP 200 and a parseable JSON
integer (e.g. `0`). If it 401s / returns an auth error, the key or whitelist is wrong.
`GetSystemTotal` is a fine fallback and lets you log the panel version.

**Example request (`GetSystemTotal`):**

```
POST https://127.0.0.1:8888/system?action=GetSystemTotal
Content-Type: application/x-www-form-urlencoded

request_time=1751731200&request_token=9c16d7111b22cfa27116ed4e97db655e
```

**Example response (`GetSystemTotal`, PDF p.2):**

```json
{
  "cpuRealUsed": 0.85,
  "memTotal": 1741,
  "system": "CentOS Linux 7.5.1804 (Core)",
  "memRealUsed": 691,
  "cpuNum": 6,
  "memFree": 189,
  "version": "6.8.1",
  "time": "0天23小时57分钟",
  "memCached": 722,
  "memBuffers": 139,
  "isuser": 0
}
```

`GetTaskCount` response is literally `0` (JSON integer).

Source: PDF p.2-3; PHP lib `System.php` (`/system?action=GetSystemTotal`, `/ajax?action=GetTaskCount`):
<https://github.com/AzozzALFiras/aapanel-api/blob/main/src/Modules/System.php>

---

### WEBSITE — create (`add_site()`)

**URI:** `/site?action=AddSite`

| Field | Required | Example | Meaning |
|---|---|---|---|
| `webname` | yes | `{"domain":"w1.hao.com","domainlist":[],"count":0}` | **JSON string.** `domain` = primary domain; `domainlist` = array of extra domains (strings, e.g. `["www.w1.hao.com"]`); `count` = number of extra domains (0 is accepted). |
| `path` | yes | `/www/wwwroot/w1.hao.com` | Document root (docroot). |
| `type_id` | yes | `0` | Site classification/category id. `0` = default category. |
| `type` | yes | `PHP` | Project type. Use `PHP` even for static sites. |
| `version` | yes | `72` | PHP version from `GetPHPVersion` list. **`00` = pure static** (no PHP) — use this for our static affiliate sites. |
| `port` | yes | `80` | HTTP listen port. |
| `ps` | yes | `test` | Free-text remark/note. |
| `ftp` | yes | `false` | `"true"`/`"false"` string. Create an FTP account? We use `false`. |
| `ftp_username` | if `ftp=true` | `w1_hao_com` | — |
| `ftp_password` | if `ftp=true` | `WCBZ6cH87raERzXc` | — |
| `sql` | yes | `false` | `"true"`/`"false"` string. Create a DB? We use `false`. |
| `codeing` | if `sql=true` | `utf8` | DB charset: `utf8`\|`utf8mb4`\|`gbk`\|`big5`. |
| `datauser` | if `sql=true` | `w1_hao_com` | DB username. |
| `datapassword` | if `sql=true` | `PdbNjJy5hBA346AR` | DB password. |

> For our static VPN sites: `type=PHP`, `version=00` (pure static), `ftp=false`, `sql=false`,
> `type_id=0`. That yields a plain nginx vhost with a docroot and no PHP/FTP/DB — exactly what we
> want to serve rendered content and receive the Cloudflare origin cert.

**Example request:**

```
POST https://127.0.0.1:8888/site?action=AddSite
Content-Type: application/x-www-form-urlencoded

webname={"domain":"w1.hao.com","domainlist":[],"count":0}
&path=/www/wwwroot/w1.hao.com
&type_id=0&type=PHP&version=00&port=80&ps=vpn-offer-xx
&ftp=false&sql=false
&request_time=1751731200&request_token=9c16d7111b22cfa27116ed4e97db655e
```

(`webname` and each value must be URL-encoded in the actual body.)

**Example response (PDF p.6):**

```json
{
  "siteStatus": true,
  "ftpStatus": true,
  "ftpUser": "w2_hao_com",
  "ftpPass": "sRxmY6xCn6zEsFtG",
  "databaseStatus": true,
  "databaseUser": "w2_hao_com",
  "databasePass": "PdbNjJy5hBA346AR"
}
```

With `ftp=false`/`sql=false`, expect `siteStatus:true` and the ftp/database fields false/absent.

> Failure mode: if the site already exists, `AddSite` returns a status/`msg` error (Chinese text like
> "网站已存在"/site already exists). Do a list-first check (below) to stay idempotent instead of
> relying on the error. The docroot dir is created by the panel if missing.

Source: PDF p.5-6; PHP lib `Websites/PhpSite.php` `create()` — builds exactly
`webname=json_encode(['domain'=>..., 'domainlist'=>..., 'count'=>0])`, `path`, `ps`, `type_id`,
`type='PHP'`, `version`, `port`, `ftp`, `sql`, `codeing`, plus conditional ftp/db creds:
<https://github.com/AzozzALFiras/aapanel-api/blob/main/src/Modules/Websites/PhpSite.php>

---

### WEBSITE — list (`list_sites()`)

Two endpoint styles exist. **Prefer the `/v2/data` form on current panels (7.x+)**; the legacy
`/data` form still works on older builds. Same params, same response shape.

**Legacy:** `/data?action=getData&table=sites`
**Current (v2):** `/v2/data?action=getData&table=sites`

| Field | Required | Example | Meaning |
|---|---|---|---|
| `table` | yes | `sites` | Fixed. |
| `action` | yes | `getData` | Fixed. |
| `limit` | yes | `100` | Rows per page. Pass a large value to get all sites in one page for the idempotency scan. |
| `p` | no | `1` | Page number. Default 1. |
| `type` | no | `-1` | Category filter. `-1` = all, `0` = default category. |
| `order` | no | `id desc` | Sort. |
| `search` | no | `w1.hao.com` | Substring filter on site name — handy to check one domain. |
| `tojs` | no | `get_site_list` | JS pagination callback; omit for API use. |

**Example request (v2, scan-all):**

```
POST https://127.0.0.1:8888/v2/data?action=getData&table=sites
Content-Type: application/x-www-form-urlencoded

limit=1000&p=1&type=-1&order=id desc
&request_time=1751731200&request_token=9c16d7111b22cfa27116ed4e97db655e
```

**Example response (PDF p.4-5):**

```json
{
  "data": [
    {
      "id": 64,
      "name": "bbb.com",
      "status": "1",
      "ps": "bbb.com",
      "path": "/www/wwwroot/bbb.com",
      "addtime": "2018-12-14 16:14:03",
      "edate": "0000-00-00",
      "backup_count": 0,
      "domain": 1
    }
  ],
  "where": "type_id=0",
  "page": "<div>...pagination html...</div>"
}
```

Key fields we use: `data[].name` (site name = primary domain, the idempotency key) and `data[].id`
(numeric id required by `DeleteSite`/`SiteStop`/SSL). `data[].domain` is a **count** of domains, not
a domain string. Note the response wraps rows in `data` and includes an HTML `page` blob — parse
`data` only.

> Caveat (aaPanel forum): `getData table=sites` historically returned only PHP/static sites and could
> omit Node/Proxy project types on some builds. Not a concern for our static vhosts, but don't assume
> it lists every project type. Ref: <https://www.aapanel.com/forum/d/23548>

Source: PDF p.4-5 (legacy `/data`); v2 path confirmed by official Postman example on
`aapanel.com/docs/api/api-list.html`
(`{{addr}}:{{port}}/v2/data?action=getData&p=1&limit=50&table=sites&search=&order=&type=-1`);
PHP lib `Websites/PhpSite.php` `getList()` uses `/data?action=getData&table=sites`.

---

### WEBSITE — delete / stop (lifecycle M6)

**Delete:** `/site?action=DeleteSite`

| Field | Required | Example | Meaning |
|---|---|---|---|
| `id` | yes | `66` | Site id (from list). |
| `webname` | yes | `w2.hao.com` | Site name. |
| `ftp` | no | `1` | Pass `1` to also delete the FTP account. **Omit** to keep it. |
| `database` | no | `1` | Pass `1` to also delete the DB. **Omit** to keep it. |
| `path` | no | `1` | Pass `1` to also delete the docroot files. **Omit** to keep files. |

**Response (PDF p.6-7):** `{"status": true, "msg": "站点删除成功!"}`

**Stop (disable):** `/site?action=SiteStop` — **Start (enable):** `/site?action=SiteStart`

| Field | Required | Example | Meaning |
|---|---|---|---|
| `id` | yes | `66` | Site id. |
| `name` | yes | `w2.hao.com` | Site name (primary domain). Note this field is `name`, **not** `webname`. |

**Response:** `{"status": true, "msg": "..."}` (Start returns "站点已启用").

Source: PDF p.6-7; PHP lib `Websites/PhpSite.php` (`DeleteSite` fields `id/webname/ftp/database/path`;
`SiteStart`/`SiteStop` fields `id/name`):
<https://github.com/AzozzALFiras/aapanel-api/blob/main/src/Modules/Websites/PhpSite.php>

---

### SSL — issue + deploy origin cert (`apply_ssl()`)

> Not in the official PDF (it predates the ACME endpoint). Sourced from the PHP lib `Ssl.php` and
> aaPanel's own `class/letsencrypt.py`. Treat exact field semantics as slightly less certain than the
> website endpoints — see **UNVERIFIED** flags.

Two steps. The library exposes them separately (`applyCertificate` then `setSSL`) and combined
(`applyAndDeploy`). For a Let's Encrypt origin cert (what Cloudflare Full-strict needs), issuing +
deploying is normally handled in one panel call, but the API models it as apply → deploy.

**Step 1 — issue LE cert:** `/acme?action=apply_cert_api`

| Field | Required | Example | Meaning |
|---|---|---|---|
| `domains` | yes | `["w1.hao.com"]` | **JSON array** of domain strings to include in the cert (SAN list). |
| `id` | yes | `66` | Site id. |
| `auth_to` | yes | `66` **(UNVERIFIED)** | Validation target. The PHP lib sets this to the **site id**; aaPanel's own code has used the **site name** or docroot path here for http-01. If issuance fails, try the site name (`w1.hao.com`) instead of the id. |
| `auth_type` | yes | `http` | Challenge type: `http` (http-01, needs the domain already resolving to this box) or `dns`. For our flow the domain is proxied through Cloudflare → use `http` **only after** DNS points at the origin, or use `dns`. |
| `auto_wildcard` | no | `0` | `1` to also request a wildcard (requires `dns`). Default `0`. |

Returns (on success) an object containing the issued `private_key` and the certificate chain
(`cert` + root). The lib then feeds those into step 2.

**Step 2 — deploy cert to the vhost:** `/site?action=SetSSL`

| Field | Required | Example | Meaning |
|---|---|---|---|
| `type` | yes | `1` | `1` = deploy a provided key/cert pair. |
| `siteName` | yes | `w1.hao.com` | Site name (primary domain). |
| `key` | yes | `-----BEGIN PRIVATE KEY-----...` | PEM private key (from step 1). |
| `csr` | yes | `-----BEGIN CERTIFICATE-----...` | **Full certificate chain** (leaf + intermediate/root). Despite the name `csr`, aaPanel puts the **certificate PEM** here, not a signing request. |

**Response:** `{"status": true, ...}` on success.

**Related helpers:**
- Read current SSL: `/site?action=GetSSL` (field: `siteName`). Use to fetch the cert `index` and to
  confirm a cert is bound (idempotency for SSL). 
- Turn SSL off: `/site?action=CloseSSLConf` (fields: `updateOf`, `siteName`). **UNVERIFIED** field names.
- Renew: `/acme?action=renew_cert` (field: `index`, from `GetSSL`).

**For Cloudflare Full (strict):** the origin only needs a *valid* (non-expired, hostname-matching)
cert. A Let's Encrypt cert issued here satisfies strict mode. Alternatively you can `SetSSL` a
**Cloudflare Origin CA** cert (15-year, only trusted by Cloudflare) via the same step-2 call
(`type=1`, paste key+cert) and skip ACME entirely — simpler when the domain is already CF-proxied
and http-01 to the origin would fail. Decide per-domain; document the choice in the site record.

Source: PHP lib `Ssl.php` — `applyCertificate()` posts `domains=json_encode([$domain]), id, auth_to,
auth_type='http', auto_wildcard`; `setSSL()` posts `type=1, siteName, key, csr=<full chain>`:
<https://github.com/AzozzALFiras/aapanel-api/blob/main/src/Modules/Ssl.php> ·
aaPanel `class/letsencrypt.py`:
<https://github.com/aaPanel/aaPanel/blob/master/class/letsencrypt.py>

---

## Idempotency notes

The provisioning module (M3) must be **idempotent** (CLAUDE.md convention). aaPanel's `AddSite`,
`DeleteSite`, and SSL calls are not naturally idempotent, so guard them:

1. **Before `AddSite`:** call website-list (`/v2/data?action=getData&table=sites`, `search=<domain>`)
   and check whether a row with `name == domain` exists. If it does, **skip create** and reuse it.
2. **Store `aapanel_site_name` and `aapanel_site_id`** on our site/domain record. `name` (primary
   domain) is the stable idempotency key aaPanel identifies sites by; `id` is needed for
   `DeleteSite`/`SiteStop`/SSL. Persist both after a successful create (re-read them from the list to
   be safe, since `AddSite`'s response doesn't return the id).
3. **Before `apply_ssl`:** call `/site?action=GetSSL` (`siteName`) and skip if a valid cert is already
   bound.
4. **Before `DeleteSite`:** confirm the site is present in the list; treat "not found" as success
   (already deleted).
5. Match domains **case-insensitively** and without trailing dots when comparing.

---

## Gotchas

- **Self-signed :8888 cert.** The panel's TLS cert on port 8888 is self-signed, so a default httpx
  client raises `SSLCertVerificationError`. Two options, **preferred first**:
  1. **Pin the panel's cert (recommended).** Copy the panel cert
     (`/www/server/panel/ssl/certificate.pem` on the VPS) to our host and pass it as httpx's CA:
     `verify="/path/to/aapanel-ca.pem"`. Keeps MITM protection. Works cleanly when we also fix the
     hostname (talk to the panel by the CN/SAN the cert was issued for, or add a host alias).
  2. **`verify=False`** — only acceptable when reaching the panel over a trusted local path
     (`https://127.0.0.1:8888` on the same VPS, no network in between). The reference PHP lib disables
     `CURLOPT_SSL_VERIFYPEER` by default for this reason, but over a public network it exposes the
     `api_sk`-authenticated session to MITM. Do **not** use `verify=False` against a remote panel.
  This is transport-to-the-panel only; unrelated to the origin certs we issue for sites.
- **Cookie persistence is required.** Reuse ONE httpx client (single cookie jar) across all calls in
  an `AaPanelClient`; don't build a client per request. See Auth.
- **Seconds, not milliseconds** for `request_time` (the HTML docs are wrong). See Auth.
- **`request_time` reused verbatim.** Compute `request_time` once per request and use the *same*
  string in both the token and the field. Recomputing between the two (e.g. across a second boundary)
  produces a token that won't validate.
- **Endpoint drift between versions.** Newer panels expose `/v2/data?...`; older ones use `/data?...`.
  Same params/response. The `/site?action=...` and `/system?action=...` and `/acme?action=...` action
  endpoints have been stable across 6.x/7.x. If a `/v2/...` call 404s, retry the legacy path.
- **Field-name traps:** `AddSite` uses `webname` (JSON) but `SiteStop`/`SiteStart` use `name`, while
  `DeleteSite` uses `webname` — don't mix them up. In `SetSSL` the certificate goes in the field
  named `csr` (misnomer), not a CSR.
- **Chinese response text.** `msg`/status text is often Chinese and unicode-escaped
  (`站点...`). Branch on the boolean `status`/`siteStatus`/`databaseStatus` fields, not on
  message strings.
- **Ports/security entrance.** If the admin changed the panel port or set a security-entrance path,
  update `AAPANEL_URL`; the action endpoints still live at the panel root regardless of the entrance
  path.
- **`GetTaskCount` returns a bare int** (`0`), not an object — handle that in `ping()`.

---

## Source URLs

Official:
- API doc (authoritative for auth/system/website; predates SSL): <https://www.aapanel.com/Document/api.pdf>
- API docs portal (v2 endpoints, Postman examples): <https://www.aapanel.com/docs/api/api-list.html>
- aaPanel source — Let's Encrypt: <https://github.com/aaPanel/aaPanel/blob/master/class/letsencrypt.py>
- Forum — `getData table=sites` project-type caveat: <https://www.aapanel.com/forum/d/23548>

PHP reference library (AzozzALFiras/aapanel-api, branch `main`) — exact field construction:
- HTTP/auth/cookies: <https://github.com/AzozzALFiras/aapanel-api/blob/main/src/HttpClient.php>
- System: <https://github.com/AzozzALFiras/aapanel-api/blob/main/src/Modules/System.php>
- Website (create/list/delete/start/stop): <https://github.com/AzozzALFiras/aapanel-api/blob/main/src/Modules/Websites/PhpSite.php>
- Website facade: <https://github.com/AzozzALFiras/aapanel-api/blob/main/src/Modules/Website.php>
- SSL (apply/deploy/renew): <https://github.com/AzozzALFiras/aapanel-api/blob/main/src/Modules/Ssl.php>
- Repo root: <https://github.com/AzozzALFiras/aapanel-api>

---

## Mapping to our client (`backend/app/integrations/aapanel.py`)

| Method | Endpoint(s) | Notes |
|---|---|---|
| `ping()` | `POST /ajax?action=GetTaskCount` | 200 + parseable int ⇒ healthy. Fallback `GetSystemTotal`. |
| `add_site(domain, doc_root, php_version)` | `POST /site?action=AddSite` | `webname` JSON, `type=PHP`, `version=00` for static (else e.g. `74`), `ftp=false`, `sql=false`. Then re-list to capture `id`. |
| `list_sites()` | `POST /v2/data?action=getData&table=sites` | `limit=1000`, `type=-1`; return `data[]`. Legacy `/data?...` fallback. |
| `delete_site(site_name)` | `POST /site?action=DeleteSite` | needs `id`+`webname`; pass `path=1`/`ftp=1`/`database=1` per teardown policy. |
| `apply_ssl(domain)` | `POST /acme?action=apply_cert_api` → `POST /site?action=SetSSL` | or SetSSL a Cloudflare Origin CA cert directly (`type=1`, key+cert). |

Auth helper `_auth_fields()` is already correct — keep it, and ensure the underlying `BaseClient`
uses **one persistent httpx client** (shared cookie jar) whose TLS trust is set per the self-signed
gotcha above (pin the panel CA when remote; `verify=False` only for a same-host `127.0.0.1` panel).
