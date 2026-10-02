# Источники доменов (discovery), международные TLD

Сбор: 2026-10-01. Пометки: **[ЖИВ]** — проверено реальным запросом, **[ОФ]** — официальный сайт или дока,
**[2-й]** — вторичный источник, **[НП]** — не подтверждено.

## Как это связано с покупкой

Источник полезен, только если найденный в нём домен можно купить нашим каналом: NameSilo,
дальше Dynadot (см. `registrars.md`). Отсюда правило: **фиды GoDaddy — только разведка**. GoDaddy
и Namecheap закрыты для РФ (по прессе [НП]) и не принимают крипту, поэтому их аукционы для нас недоступны.

## A. Аукционы и drop-catch

| Сервис | Что даёт | TLD | API / выгрузка | Цена | Метрики в фиде |
|---|---|---|---|---|---|
| **GoDaddy Auctions** | expired-аукционы, closeouts | gTLD; ccTLD (.nz/.ca/.au/.de/.uk) — 0 [ЖИВ] | публичные inventory-файлы без авторизации, `inventory.auctions.godaddy.com`, JSON/XML/CSV, ежедневно ~14:36 UTC [ЖИВ] | closeout от $5 | возраст, Majestic TF/CF/RD, трафик, isAdult [ЖИВ]. Semrush RD как RD не брать — в нём спам-агрегаторы |
| **DropCatch** | backorder на pending delete | .com .net .org .tv .cc + gTLD Identity Digital | **CSV/XLSX без логина** «Dropping today…+4 дня» [ЖИВ], ~132 тыс. строк в файле; API v2 (JWT) [ОФ] | backorder $59 | нет: только `Domain,TLD,Type,Drop Date` |
| **Dynadot** | аукционы, closeouts, backorder | широко, включая .uk; TBR-регистратор .ca | API: `get_open_auctions`, `place_auction_bid`, `add_backorder_request`, `get_expired_closeout_domains`, `buy_expired_closeout_domain` [ОФ] | backorder .com от $24.99 | Dynappraisal |
| **NameSilo** | drop-catch .com/.net, аукционы | .com/.net | `registerDomainDrop` (только через `/apibatch`, только с баланса) [ОФ, проверено 2026-10-01] | см. прайс | — |
| **Catched** | backorder и аукционы ccTLD | 82 ccTLD, включая .nz/.co.nz/.uk/.co | REST API, токен по письму на info@catched.com [ОФ] | .com $25, .co.nz $29, .co.uk £25, .co $51, платишь при успехе | — |
| **NameJet / SnapNames** | expiry и pending delete | .com/.net/.org | CSV, похоже, нужен логин; API не подтверждён | от $69 [2-й] | — |
| Park.io | hacker-TLD (.io .ly .to .me …) | — | JSON/RSS | $99 | для наших зон не нужен |

## B. Агрегаторы: только для ручной работы оператора

| Сервис | API | Цена | Заметка |
|---|---|---|---|
| **ExpiredDomains.net** | **нет**: «There is no API, so no you can't» [ОФ] | бесплатно | 676 TLD, включая ccTLD. **За скрипты, ботов и AI-агентов аккаунт закрывают** [ОФ]. Только руками |
| **SpamZilla** | нет («Not at the moment») [ОФ] | Free 25 доменов; $37/мес | Спам-скоринг + история языков. SZ Score <5 super clean, 6–15 very clean, 16–20 clean, 20+ questionable |
| DomCop | нет [2-й] | $816–1416/год | Множество метрик, свой краулер |
| Domain Hunter Gatherer | не заявлен | $27–97/мес | Десктоп и веб |
| **CatchDoms** | **есть REST API** | €39/мес (15 req/min), €79/мес при годовой оплате (60 req/min) | 22 площадки + удалённые ccTLD, Majestic/Moz/Wayback. Мелкий вендор, сначала тест |

Типичная ручная связка профи: ExpiredDomains (или DomCop) → вычистка в SpamZilla → покупка. В машину
это заходит через **источник `list`** — ручную вставку списка.

## C. Официальные списки дропов реестров ccTLD

Только зоны, открытые иностранцу (подробно — `cctld-markets.md`).

| Зона | Источник | Что в нём |
|---|---|---|
| .uk / .co.uk | `droplists.nominet.uk/current/uk.csv.gz` [ЖИВ] | ежедневно, **точное время дропа**, ~3.5–4.6 тыс. дропов в день |
| .mx / .com.mx | `registry.mx/report/domain_deleted_list.csv` [ЖИВ] | ежедневно, уже удалённые → свободны |
| .cl | `nic.cl/registry/Eliminados.do?t=1d&f=txt` [ЖИВ] | удалённые за день или неделю |
| .pl | `dns.pl/deleted_domains.txt` [ЖИВ] | ежедневно |
| .se | `data.internetstiftelsen.se/bardate_domains.txt` (+json) [ЖИВ] | с датами освобождения |
| .com.tr | `trabis.gov.tr/yenidenTahsiseAcilanAlanAdlari` [ОФ] | домен + дата, с которой принимают заявки |
| .co.nz | TBR JSON API [ОФ] | ловят только регистраторы через EPP → для нас Catched |
| .hu | parking list `info.domain.hu` [ОФ] | но регистрация условная |
| .de | публичного списка нет; после удаления 30 дней RGP | только ExpiredDomains вручную |

## D. Новорегистрации и EMD

- **EMD машина генерирует сама:** ключи × язык × TLD → проверка доступности через RDAP/whois
  или API регистратора. Фиды новорегистраций для этого не нужны.
- ICANN CZDS (зоны gTLD, включая .com) — бесплатно, по одобрению реестра. Годится только для
  анализа ниш, не как источник: diff зон не равен дропам.
- WhoisDS — бесплатный ежедневный срез NRD (~70 тыс./день), коммерческое использование разрешено [ЖИВ].
- WhoisXML NRD ($499+/мес) и DomainTools (enterprise) — не нужны.

## Риски источников

- **ToS DropCatch на использование их CSV не прочитан**: страница рендерится JS. Прочитать вручную
  до включения источника; по умолчанию источник выключен.
- Объём DropCatch ~130 тыс./день. Скринить его можно только бесплатным DR от Ahrefs, а у той лицензии
  есть риск отзыва (см. `metrics-history.md`).

## Первоисточники

expireddomains.net/faq · inventory.auctions.godaddy.com/metadata.json · dropcatch.com/hiw/faq ·
dropcatch.com/downloads · dynadot.com/domain/api-document · namesilo.com/api-reference ·
catched.com/coverage/cctld · catched.com/pricing · api.catched.com/docs · spamzilla.io/pricing ·
catchdoms.com/api · registrars.nominet.uk/drop-lists · identitydigital.au/domain-name-drop-list ·
cira.ca/en/ca-domains/tbr · docs.internetnz.nz/registry/faq/dropcatch · denic.de/en/products/de-domains/deletion ·
icann.org/resources/pages/zfa-2013-06-28-en · whoisds.com/newly-registered-domains
