# Wayback Machine (Internet Archive) — референс интеграции

> Бесплатные API Internet Archive для реконструкции истории домена в скоринге (**M1**,
> `integrations/wayback.py` → `services/domain.py`). **API-ключа нет, денег не стоит**, но
> сервис тяжёлый и агрессивно рейт-лимитит (429) — гонять только по доменам, прошедшим дешёвый пре-фильтр.
> Все примеры ниже проверены живым запросом 2026-07 (кроме мест, помеченных **UNVERIFIED**).

## Назначение
Из истории захватов домена вытаскиваем для скоринга (см. [docs/DONORS.md](../DONORS.md)):
- **Реальный возраст** — timestamp первого захвата (не дата регистрации, а когда реально жил сайт).
- **`prior_flags`** — что домен хостил во времени: adult / pharma / casino / gambling / warez / spam.
- ~~Topic-switch~~ — отдельного флага НЕТ (см. п.6 ниже): отравление «пекарня → казино» ловится
  самим флагом `casino`, а `topic_switch` был его строгим подмножеством и удалён.
- **Real site vs parked** — был ли это связный контентный сайт или парковка/заглушка.

Логика: взять список захватов через CDX → сэмплировать N снапшотов по таймлайну →
скачать «сырой» HTML (`id_`) → **раскодировать БАЙТЫ по charset страницы** (`_decode`) →
**вырезать разметку и оставить ВИДИМЫЙ ТЕКСТ + `<title>`** (`_visible_text`, nh3) → прогнать по
ключевикам → выставить флаги. Считать стоп-слова в сырой разметке нельзя: два «casino» в
`<script>` рекламной сети или в `alt=` картинки — это не история домена, а мебельный магазин,
отбракованный как казино (аудит 2026-07-14, F3).

Два следствия, каждое оплачено багом (ревью Задачи 3, 2026-07-14):
- **Снимок без видимого текста ≠ чистый снимок.** Редирект-заглушка (`meta refresh` /
  `location.href`), frameset, SPA-оболочка отдают 200 + `text/html` (фильтры CDX их пропускают),
  а читать на них нечего. Такой снимок НЕ засчитывается в покрытие (`MIN_TEXT_CHARS`) — история
  остаётся непроверенной (`wayback_checked=False` → вердикт `unknown` → домен вне пакетного
  одобрения). Именно в такую заглушку с редиректом на казино домен превращают перед сдачей.
- **Кодировка — со страницы, а не от httpx.** Архивные .ru-страницы 2000-х — windows-1251, а
  charset в заголовке Wayback отдаёт не всегда; `Response.text` молча раскодирует их utf-8 в
  мозаику, и русская половина словаря (несущая: портфель — .ru-дропы) не находит НИ ОДНОГО слова.
  Порядок в `_decode`: charset заголовка → `<meta charset>`/`http-equiv` страницы → utf-8 → cp1251.

## Base URLs
| Назначение | URL |
|---|---|
| **CDX Server** (список захватов) | `http://web.archive.org/cdx/search/cdx` |
| **Availability API** (есть ли снапшот / ближайший к дате) | `http://archive.org/wayback/available` |
| **Snapshot fetch** (страница с баннером/переписанными ссылками) | `http://web.archive.org/web/{timestamp}/{original}` |
| **Snapshot fetch RAW** (`id_` — исходные байты, без баннера) | `http://web.archive.org/web/{timestamp}id_/{original}` |

`http` апгрейдится в `https`. Ключей/токенов не требуется.

## CDX Server API — параметры
Обязателен только `url`. Формат по умолчанию — plain-text CDX; нам нужен `output=json`.

| Параметр | Что делает | Пример |
|---|---|---|
| `url` | Целевой адрес. `*` в конце (`DOMAIN/*`) = `matchType=prefix`; `*.DOMAIN` = `matchType=domain` | `url=example.com/*` |
| `matchType` | Область матчинга: `exact` (деф.), `prefix`, `host`, `domain` (с сабдоменами) | `matchType=domain` |
| `output` | `json` → массив массивов, **первая строка — заголовки полей** | `output=json` |
| `fl` | Список полей через запятую. Доступны: `urlkey,timestamp,original,mimetype,statuscode,digest,length` | `fl=timestamp,original,mimetype,statuscode,digest` |
| `from` / `to` | Диапазон по timestamp, включительно. 1–14 цифр `yyyyMMddhhmmss` | `from=2010&to=2011` |
| `collapse` | Схлопывает **соседние** дубли по полю. `field:N` — по первым N символам | `collapse=digest`, `collapse=timestamp:8` (1/день), `collapse=timestamp:6` (1/мес) |
| `filter` | Regex-фильтр по полю: `[!]field:regex`. `!` инвертирует (Java-regex) | `filter=statuscode:200`, `filter=mimetype:text/html`, `filter=!statuscode:200` |
| `limit` | Первые N результатов; `-N` — последние N (медленнее) | `limit=1`, `limit=-1` |
| `offset` | Пропустить M записей (простая пагинация; на больших объёмах не масштабируется) | `offset=100` |
| `fastLatest` | Быстро отдать последние захваты (для `exact`) | `fastLatest=true&limit=-5` |
| `gzip` | Сжатие ответа, по умолчанию `true`; `false` — отключить | `gzip=false` |
| `showResumeKey` + `resumeKey` | Курсорная пагинация: ключ печатается последней строкой, скармливается в след. запрос | `showResumeKey=true&limit=1000` |
| `page` / `pageSize` / `showNumPages` | Блочная пагинация (zipnum, только большие хосты) | `showNumPages=true`, `page=0` |
| `showDupeCount` | Добавляет колонку `dupecount` (кол-во дублей по `digest`) | `showDupeCount=true` |

Порядок по умолчанию — **хронологический по возрастанию** (первый ряд = самый ранний захват).
Серверный максимум — **150 000** записей на запрос.

**Важно про `collapse`:** схлопываются только **соседние** строки. Т.к. CDX по умолчанию
отсортирован по urlkey+timestamp, `collapse=digest` для одного URL корректно убирает подряд
идущие идентичные версии (не изменившийся контент), а `collapse=timestamp:8` даёт максимум
один захват в сутки (`8` = `yyyyMMdd`). `timestamp:6` = один в месяц, `:4` = один в год.

## Примеры

### 1. Список захватов домена (для сэмплинга истории)
Один захват в день, только успешные HTML-страницы, дедуп по контенту:
```
http://web.archive.org/cdx/search/cdx?url=example.com*&output=json&fl=timestamp,original,mimetype,statuscode,digest&collapse=digest&filter=statuscode:200&filter=mimetype:text/html
```
Ответ (первая строка — заголовки):
```json
[["timestamp","original","mimetype","statuscode","digest"],
 ["20020120142510","http://example.com:80/","text/html","200","HT2DYGA5UKZCPBSFVCV3JOBXGW2G5UUA"],
 ["20020328012821","http://www.example.com:80/","text/html","200","UY3I2DT2AMWAY6DECFCFYMT5ZOTFHUCH"],
 ["20030207055228","http://www.example.com:80/","text/html","200","EF7YLJGKQUMLJFP3F7A7LBALC65T5W2O"],
 ["20100603215612","http://www.example.com/","text/html","200","COSFPXIHL6FDWTZZOQFPYN5HBTZ4Z57M"]]
```
Каждый ряд — кандидат-снапшот. `digest` — SHA1 контента (одинаковый = страница не менялась).
`length` (если запросить) — размер; крошечный размер на всех захватах ≈ парковка/заглушка.

### 2. Первый захват (реальный возраст)
CDX, сортировка по возрастанию, `limit=1`:
```
http://web.archive.org/cdx/search/cdx?url=example.com&output=json&fl=timestamp,original&limit=1
```
```json
[["timestamp","original"],
 ["20020120142510","http://example.com:80/"]]
```
`timestamp` первого ряда → возраст = `today − 20020120`. (Для домена целиком без учёта путей —
можно без `*`; с `url=DOMAIN*` получишь самый ранний захват любого пути.)

Альтернатива — **Availability API** (ближайший к дате снапшот, одним объектом):
```
http://archive.org/wayback/available?url=example.com&timestamp=20060101
```
```json
{"url": "example.com",
 "archived_snapshots": {"closest": {
    "status": "200", "available": true,
    "url": "http://web.archive.org/web/20060101213916/http://example.com:80/",
    "timestamp": "20060101213916"}},
 "timestamp": "20060101"}
```
Параметры: `url` (обяз.), `timestamp` (1–14 цифр `YYYYMMDDhhmmss`, деф. — самый свежий),
`callback` (JSONP). Нет снапшота → `{"archived_snapshots":{}}`.
Опц. `&direction=before|after|either` — с какой стороны от даты искать ближайший (**UNVERIFIED** —
в official help не задокументировано явно, встречается в комьюнити).
Availability удобен для «есть ли вообще / ближайший к X», но **для полной истории нужен CDX**
(availability отдаёт только один closest-снапшот).

### 3. Скачать сырой HTML снапшота (`id_`)
С баннером и переписанными ссылками (НЕ для классификации):
```
http://web.archive.org/web/20020120142510/http://example.com/
```
**RAW — исходные байты как заархивированы (`id_`), для классификации именно это:**
```
http://web.archive.org/web/20020120142510id_/http://example.com/
```
```html
<HTML>
<HEAD>
  <TITLE>Reserved Domain Names</TITLE>
</HEAD>
<BODY BGCOLOR="#ffffff">
...
```
**Когда `id_`:** всегда для текстового анализа/классификации. `id_` («identity») отдаёт
оригинальный ресурс **без** инъекции тулбара Wayback и **без** переписывания ссылок/скриптов.
Без `id_` в HTML попадает разметка баннера архива и переписанные URL — это шум и ложные
срабатывания ключевиков (напр. строки `web.archive.org`). Для рендера в браузере `id_` хуже
(CSS/картинки не переписаны на архивные копии), но нам нужен **текст**, не рендер → берём `id_`.
`{timestamp}` и `{original}` — прямо из строки CDX (`original` уже полный URL со схемой).

## Стратегия извлечения (для services/domain.py)
1. **Гейт:** запускать Wayback только для доменов, прошедших дешёвый пре-фильтр (метрики/блэклисты).
   Это самый медленный и рейт-лимитный источник — беречь.
2. **CDX-инвентаризация:** один запрос из [примера 1](#1-список-захватов-домена-для-сэмплинга-истории)
   (`collapse=digest`, `filter=statuscode:200`, `filter=mimetype:text/html`). Получаем таймлайн + `digest`.
   - Первый ряд → **реальный возраст**.
   - Кол-во уникальных `digest` и разброс `length`: мало рядов / одинаковый крошечный размер ≈ **parked**.
3. **Сэмплинг N снапшотов** (напр. N=5–10) равномерно по таймлайну: первый, последний и
   равномерные точки между — чтобы поймать смену владельца/тематики. Не тянуть все ряды.
4. **Fetch `id_` HTML** каждого сэмпла (пауза между запросами, см. рейт-лимит). Извлечь `<title>`,
   видимый текст, `lang`.
5. **Классификация → `prior_flags`:** прогнать текст по словарям/классификатору на adult / pharma /
   casino / gambling / warez / spam (мультиязычно — гео офферов разное). Любой хит на любом
   снапшоте → флаг (история отравлена, даже если сейчас чисто).
6. ~~**Topic-switch:**~~ **НЕ РЕАЛИЗОВАН и удалён из кода** (аудит 2026-07-14, F4). Флаг
   `topic_switch` считался как `(поздние категории − ранние) ∩ {adult,pharma,casino,gambling}` —
   то есть был строгим ПОДМНОЖЕСТВОМ уже сработавшего категорийного hard-reject и не мог
   добавить ни одного отказа. Смену «пекарня → казино» ловит флаг `casino`, а не он. Настоящее
   сравнение тематик (эмбеддинги) — открытая задача; пока её нет, кода, который ПРИТВОРЯЕТСЯ
   такой проверкой, в машине быть не должно.
7. **Real-vs-parked:** связный многостраничный контент с эволюцией во времени = живой сайт (плюс);
   один и тот же мусор/парковка/`for sale` = минус.
8. **Кэшировать** сырой результат CDX + скачанные снапшоты по домену (история архива меняется
   редко) — не бить API повторно на том же домене.

## Рейт-лимит и вежливость
- **Ключа/аутентификации нет.** Лимиты не опубликованы официально; Wayback известен агрессивными
  **429 Too Many Requests** и временными блокировками IP при нагрузке (**UNVERIFIED** — конкретных
  цифр IA не даёт; ниже — sane defaults, а не гарантии).
- **Последовательно, не параллельно** по одному домену; между fetch снапшотов — задержка
  (старт ~**1–2 c**, тюнить по факту). Не запускать пачку доменов конкурентно на один IP.
- **httpx + backoff:** на `429`/`5xx` — экспоненциальный retry с уважением `Retry-After`, потолок
  попыток; на устойчивый 429 — отложить домен в очередь, не долбить.
- **Минимизировать запросы:** один CDX-запрос на домен (не по каждому пути), `collapse`/`filter`
  на стороне сервера, сэмпл N снапшотов вместо всех. Кэш обязателен.
- Разумный `User-Agent` с контактом — вежливая практика для IA.
- Гейт из [стратегии](#стратегия-извлечения-для-servicesdomainpy) п.1 — главная экономия квоты.

## `ping()` (для scripts/smoke.py)
Крошечный CDX-запрос с `limit=1` по заведомо живому домену:
```
GET http://web.archive.org/cdx/search/cdx?url=example.com&output=json&limit=1
```
Возвращать `True`, если HTTP 200 и тело парсится как непустой JSON-массив (первый ряд — заголовки
`["urlkey"...]` или `["timestamp"...]`). Дёшево, не тянет контент снапшота. Альтернатива ещё легче —
Availability: `GET http://archive.org/wayback/available?url=example.com` → 200 + ключ `archived_snapshots`.

## Готчи
- **`output=json`: первая строка — заголовки**, не данные. Всегда пропускать `rows[0]`.
- `original` в CDX часто со старым портом (`:80`) и/или `www` — это норм, подставляется в `id_`-URL как есть.
- `collapse` работает по **соседним** строкам; для дедупа по контенту домена держать сортировку по умолчанию.
- Пустой ответ CDX (нет захватов) ≠ ошибка — возможно, домен молодой/никогда не индексировался (→ возраст неизвестен).
- Не все снапшоты `200`: редиректы/`404`/парковки. Фильтровать `filter=statuscode:200` для контентного анализа.
- Availability отдаёт **один** closest-снапшот — не путь к полной истории.

## Источники
- CDX Server API (official README): https://github.com/internetarchive/wayback/blob/master/wayback-cdx-server/README.md
- Wayback Availability JSON API (official help): https://archive.org/help/wayback_api.php
- IA Developer Portal — snapshot tutorial: https://archive.org/developers/tutorial-get-snapshot-wayback.html
- `id_` (identity, без тулбара/переписывания) — IA Help / форумы:
  https://help.archive.org/help/using-the-wayback-machine/ ,
  https://archive.org/post/1044859/how-do-i-retrieve-the-original-form-of-a-page-from-the-wayback-machine
