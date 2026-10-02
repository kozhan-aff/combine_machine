# Open-source: что можно взять готового

Проверено 2026-10-01 по GitHub API (звёзды, push, лицензия, issues) и PyPI JSON (версия, дата,
зависимости). Семь пакетов прогнаны вживую в отдельном venv.
Пометки: **[тест]** — запускал сам, **[док]** — из README или доки, **[не пров.]** — по памяти.

**Экосистемный риск.** Часть пакетов ушла на `httpx2` (pydantic/httpx2, с 2026-05): `mcp` 2.x,
`fastmcp` 4.x, `whodap` 0.2.0. Они тащат второй HTTP-стек рядом с нашим `httpx`.

## Решения

| Задача | Берём | Почему | Не берём |
|---|---|---|---|
| RDAP | **свой клиент на httpx (~50 строк)** | `whoisit` тянет и httpx, и requests; `rdap` (20c) тянет googlemaps/phonenumbers | whoisit, rdap, whodap (httpx2) |
| whois:43 (.mx/.co/.nz) | **A-Parser Net::Whois, как сейчас** | Уже работает на боксе. Если не разберёт .mx/.co — `python-whois` (★453, MIT, одна зависимость, лучший результат в живом тесте: .com.mx, .co, .co.nz) | asyncwhois (неверный whois-сервер .co, httpx2), whoisdomain (subprocess) |
| Wayback | **свой `wayback.py`** | Наш классификатор видимого текста зрелее; `wayback` (EDGI) на requests. Сверить темп: у EDGI дефолт CDX 0,4 запроса/с | waybackpy (мёртв с 2022) |
| Язык и тема | **LLM (уже есть)** — язык идёт бесплатным полем того же JSON | Если понадобится дешёвый детерминированный пре-фильтр — `fasttext-predict` + lid.176.ftz (0 зависимостей, ~10 строк) | lingua (wheel 170 MB), langdetect (недетерминирован), py3langid (numpy) |
| Ahrefs | **свой клиент на httpx** | Официальный `ahrefs-python` — alpha, ★1, нет на PyPI | ahrefs-api-python (2019, GPL) |
| Web Risk | **REST на httpx (~15 строк)** | `google-cloud-webrisk` тянет grpcio | SDK |
| Public suffix | **не нужен в подпроекте 1** (белый список зон = наш перечень) | Если понадобится — `publicsuffixlist` (0 зависимостей, офлайн, PSL вшит, режим `only_icann`) | tldextract (по умолчанию ходит в сеть и пишет кэш) |
| Регистраторы | **свои клиенты** (NameSilo, Dynadot — ~60 строк каждый) | Python-SDK мертвы (2012–2018) | dns-lexicon и libcloud — только DNS-записи, NS у регистратора не меняют |
| Cloudflare | **свой клиент (уже есть)** | SDK `cloudflare` 5.x меняет мажорную версию каждый год | — |
| aaPanel | **свой клиент (уже есть)** | Официального Python SDK нет | bt-python-sdk (это BaoTa, набор эндпоинтов другой) |
| MCP (подпроект 4) | **официальный `mcp` 1.x** с явным списком инструментов (`mcp>=1.30,<2`, если остаёмся на httpx) | — | **`FastMCP.from_fastapi`**: выставит агенту роуты подтверждения заказа и редактуры, то есть обойдёт оба хард-гейта. `fastmcp` 4.x — около 20 зависимостей и ежедневные релизы |

## Данные, которые стоит взять позже

- **blocklistproject/Lists** (★5122, Unlicense, обновляется ежедневно) — списки ДОМЕНОВ gambling (8,4 MB),
  porn (26 MB), drugs, scam и др. Дешёвый офлайн-сигнал: домен сам был в списке казино или порно →
  жёсткий флаг.
- **olbat/ut1-blacklists** (CC BY-SA 4.0, нужна атрибуция) — категории gambling/adult.
- **Wikidata Q56240391 «VPN service»** (CC0) — сид списка VPN-брендов. Там всего 27 записей, нет
  ExpressVPN и Surfshark, поэтому список дополняем руками.
- Резолвер **Cloudflare for Families 1.1.1.3** (блокирует malware/adult) — возможная бесплатная
  DNS-проверка через dnspython [не пров.].

## Готовые «охотники за доменами» — переиспользовать нечего

| Репозиторий | Что это | Вердикт |
|---|---|---|
| threatexpress/domainhunter (★1679) | red-team скрейпер ExpiredDomains + категоризация | Нарушает ToS ExpiredDomains. Берём только идею внешней категоризации |
| maelgangloff/domain-watchdog (★398, AGPL, PHP) | RDAP-мониторинг и автопокупка | Только идеи |
| AIRMASTER, dropfilter-cli | — | Мертвы с 2022 |

PBN-детекторов с репутацией нет. Наша воронка зрелее всего найденного.
