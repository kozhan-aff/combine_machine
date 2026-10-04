# Провижн: aaPanel + Cloudflare + MCP

Сбор: 2026-10-01. **[✔]** — исходники или официальная дока, **[~]** — форум или блог, **[?]** — проверить вживую.
Это вход для подпроекта 2 «Купил → сайт поднят».

## Главное

- **aaPanel не тупик.** API полный, а документация к нему — исходный код (github.com/aaPanel/aaPanel).
- Чинить надо три вещи:
  1. **Транспорт:** SSH-туннель вместо публичного IP.
  2. **SSL:** Cloudflare Origin CA через `SetSSL` вместо Let's Encrypt.
  3. **Проверка после каждого шага.**
- **Origin CA Key отключён 30.09.2026.** Теперь нужен только API-токен с правом
  Zone → SSL and Certificates → Edit [✔ changelog CF].
- Надёжного готового MCP для aaPanel нет. Наш клиент лучше любого найденного.

## Готовые MCP для aaPanel и 宝塔: брать нечего

| Репозиторий | ★ | Вердикт |
|---|---|---|
| aaPanel/mcp-server (официальный, Go) | 18 | демо, заброшен с 03.2025, нет SSL и файлов |
| nipunanirmal/aapanel-mcp (Python) | 3 | 278 инструментов за один день, тестов нет, маппинг методов не выверен (`UploadFile`). Годится только как шпаргалка по именам |
| devochkaskustikom/aapanel-mcp (TS) | 0 | слишком свежий |

У 宝塔 есть официальный закрытый «宝塔 MCP 服务» (панель ≥9.6.0). У aaPanel аналога нет.

## API aaPanel 8.x: реальные методы по исходнику [✔]

- **Где искать:**
  - список action — кортежи `defs` в `BTPanel/__init__.py`;
  - параметры — `class/panelSite.py`, `class/files.py`, `class/database.py`, `class/crontab.py`,
    `class/panelRedirect.py`;
  - есть параллельный v2 (`class_v2/*`).
- **Авторизация:**
  - `request_token = md5(request_time + md5(api_sk))`;
  - IP берётся из `request.remote_addr`, X-Forwarded-For не учитывается;
  - whitelist понимает IPv4/IPv6/CIDR/диапазоны;
  - **20 неудачных проверок подряд банят IP на 1 час.** Ретраи с битой подписью запрещены.

| Задача | Метод |
|---|---|
| Сайт | `/site?action=AddSite`, `DeleteSite`, `AddDomain`, `SetPHPVersion`, `SetPath` |
| **Свой сертификат** | `/site?action=SetSSL` (`siteName`, `key`, `csr` = PEM сертификата). Так ставится Cloudflare Origin CA. Плюс `GetSSL`, `HttpToHttps` |
| Default vhost для неизвестного SNI | `/v2/site?action=set_https_mode` (самоподписанный сертификат — не светит домены по IP) |
| Редиректы 301 | `CreateRedirect`, `ModifyRedirect`, `DeleteRedirect`, `GetRedirectList` |
| Файлы | `/files?action=upload` (чанки, multipart: `f_path`, `f_name`, `f_size`, `f_start`, `blob`), `UnZip`, `DownloadFile`, `SaveFileBody`, `CreateFile` |
| WordPress | `deploy_wp` и др. (не используем, см. `sites-content-indexing.md`) |

Сейчас `apply_ssl` в `backend/app/integrations/aapanel.py` идёт через `/acme?action=apply_cert_api`,
то есть через Let's Encrypt. Его надо перевести на Origin CA + `SetSSL`.

## Cloudflare

- **Origin CA:**
  - запрос `POST /certificates` с полями `csr`, `hostnames[]`, `request_type: origin-ecc`,
    `requested_validity: 5475` (15 лет);
  - hostnames должны принадлежать зонам аккаунта;
  - работает с **Full (strict)**;
  - браузеры этому сертификату не доверяют: без прокси CF будет ошибка.
- **Лимиты:**
  - не больше **25 новых зон за 10 минут**;
  - если зон >50 и pending больше, чем active, добавление блокируется;
  - `activation_check` на Free — не чаще раза в час;
  - API: 1200 запросов за 5 минут на пользователя.
- **MCP:** официальный `cloudflare/mcp` (Code Mode, ~2500 эндпоинтов) — инструмент оператора и
  отладки, не рантайм. Токены с фильтром по IP он не поддерживает.

## Footprint

- **NS-пара:** все зоны одного CF-аккаунта получают одну пару NS. Её видно обратным поиском по NS
  (ViewDNS и т.п.). Своя пара NS — только на Business/Enterprise.
  - **Решение оператора: ~10 CF-аккаунтов**, сайт назначается на аккаунт.
- **Общий IP Google не наказывает** (Mueller: «There is no SEO advantage to using a unique IP»).
  Реальный риск — абьюз-жалоба кладёт весь CF-аккаунт, плюс утечка origin.
- **Утечка origin-IP.** Если по неизвестному SNI отдаётся сертификат сайта, Censys/Shodan свяжут IP
  с доменом. Лечится так:
  - `set_https_mode`;
  - файрвол 80/443 только для IP Cloudflare;
  - **один Origin-сертификат на домен**: multi-SAN перечислил бы весь портфель.

## Рекомендованный провижн: каждый шаг «проверить → сделать → проверить»

1. **Транспорт:** SSH-туннель от бокса до VPS, в whitelist aaPanel только `127.0.0.1` [?].
   Ретраи только на сетевые ошибки.
2. **Зона CF:** `find_zone` → `create_zone`, не быстрее 25 зон за 10 минут. Проверка: непустой `name_servers`.
3. **NS у регистратора.** Проверка: `dig NS` к TLD-серверу. Затем `activation_check` (не чаще раза в час).
4. **DNS:** `@` и `www`, `proxied=true`.
5. **Origin-сертификат:** ключ ECDSA и CSR генерируем локально (`cryptography`), `origin-ecc`, `[d, *.d]`,
   5475 дней. Ключ храним зашифрованным. Перед выпуском проверить, нет ли уже живого сертификата.
6. **Сайт:** `ensure_site`.
7. **`SetSSL`;** один раз на сервер `set_https_mode`. Проверка `openssl s_client -servername d`:
   issuer «CloudFlare Origin».
8. **CF SSL → strict**, только после шага 7, иначе 526.
9. **Выкладка:** `rsync --checksum` через SSH или `upload` + `UnZip`. Маркер `/.build-id`.
10. **Финальная проверка:** `https://d/.build-id` через CF отдаёт 200, хэш совпадает, есть `cf-ray`.
11. **Один раз на VPS:** файрвол 80/443 только для IP CF.

## Свой MCP

- FastMCP умеет `FastMCP.from_fastapi(app)` (только для прототипа) и
  `mcp.http_app(path='/mcp')` (обязательно `lifespan=mcp_app.lifespan`).
- **Решение:** тонкий адаптер над сервисным слоем, 6–10 курированных инструментов:
  `provision_site`, `site_status`, `verify_site`, `list_sites`, `issue_origin_cert`, `rollback_site`.
  - По умолчанию только чтение.
  - Всё, что тратит деньги, — только через гейт подтверждения.
  - Сырые вызовы aaPanel наружу не выставлять.
- **Надёжность даёт сервисный слой с проверками, а не протокол.** Поэтому MCP идёт последним подпроектом.

## Альтернативы aaPanel, если будет капризничать

- Голый nginx через SSH: самый предсказуемый, SSH к этому моменту уже будет в схеме.
- HestiaCP: REST API.
- 1Panel: есть официальный MCP.
- CloudPanel: только CLI.
