# Аудит M1 «поиск доноров → оценка» — находки и материал для фиксов

Дата: 2026-07-07. Метод: baseline-тесты (148 passed, pyflakes чист) + два независимых
аудита кода (discovery, scoring) + **живой функциональный прогон** с Mac (реальные
Wayback / РКН-дамп antizapret / SearXNG бокса / A-Parser бокса / backorder-фид).
Этот документ — сырьё для плана фиксов: каждая находка с file:line, конкретным входом,
следствием и рецептом.

---

## 1. Живой функциональный тест воронки: 6 реальных доменов — вердикты 6/6

Прогон: SQLite + реальные интеграции, все домены `lane=bid`, RD=30, пороги дефолтные
(approve 0.70 / manual 0.40 / min_age 3.0).

| Домен | Репутация | Вердикт | reject_reason | Примечание |
|---|---|---|---|---|
| rutracker.org | плохой (РКН) | rejected | `rkn` | пойман на T2; Wayback НЕ вызывался (ранний выход ✓) |
| thepiratebay.org | плохой (РКН) | rejected | `rkn` | то же |
| azino777.com | плохой (казино) | rejected | `history_dirty` | в antizapret-дампе его НЕТ → дошёл до T3, Wayback-классификатор поймал `['casino']` (sampled=4, age 13.14) |
| python.org | хороший | approved | — | score 0.829, возраст 29.2 (Wayback-фолбэк), echo=True |
| mozilla.org | хороший | approved | — | 0.829, 27.6 лет |
| w3.org | хороший | approved | — | 0.829, 29.5 лет |

Скор 0.829 = 0.40 (history) + 0.20 (age) + 0.129 (rd_proxy: log10(31)/log10(3001)×0.30)
+ 0.10 (echo) — сходится с ручным расчётом весов.

**Вывод:** логика вердиктов, ранний выход дёшево→дорого, РКН-дамп, SearXNG-echo и
классификатор истории Wayback — работают на реальных данных.

## 2. Живой discovery: состояние источников

| Источник | Живой статус | Детали |
|---|---|---|
| backorder | ✅ работает | 37 строк, 37/37 нормализовано; поля: `domainname/hotness/price/x_value/yandex_tic/links/visitors/registrar`; `delete_date` парсится в дедлайн |
| cctld | ❌ собирает мусор | 8 «кандидатов» = `yandex.ru`, `regcctld.ru`, `netoscope.ru`… — навигационные ссылки страницы, не реестр дропов (стабильно в двух прогонах) |
| reg_ru | ❌ тихий ноль | A-Parser-прокси вернул `596 HTTPS proxy error: 502 Bad Gateway` → `fetch_html` требует `200` → молча None → 0 строк БЕЗ лога (живое подтверждение I6). Прокси ротируются — может ожить на другом прогоне, но отказ неотличим от «на странице нет доменов» |
| sweb | ❌ ReadTimeout | честное исключение (попало бы в docker-лог warning'ом) |

Итого: из 4 источников полноценно жив 1 (backorder).

## 3. Живой A-Parser: whois-формат снят — парсер кода его не понимает

`Net::Whois` (preset default) возвращает НЕ сырой whois, а свёртку одной строкой:

```
python.org - registered: 1, expire: 28.03.2033, creation: 27.03.1995
sdjfh-nonexistent-drop-2026.ru - registered: 0, expire: none, creation: none
опрелеленно-свободный-2026.рф - registered: 0, expire: none, creation: none
```

Парсер [aparser.py:13-43](../../../backend/app/integrations/aparser.py) ищет
`created: YYYY.MM.DD` (_RE_RU), `Creation Date: YYYY-MM-DD` (_RE_GTLD), маркеры
`nserver|registrar|…` — ничего из этого в свёртке нет → `available=None, created=None`
на ЛЮБОМ домене (проверено: занятый gTLD, реальный .ru-дроп из фида, свободный .ru,
свободный кириллический .рф).

Перепроверка (по запросу пользователя, A-Parser подтверждён живым: ping True, v1.2.2940;
первоначальный «недоступен» был нетерпеливым curl -m 5 — A-Parser отвечает медленно,
холодные whois-вызовы таймаутились на 30с):

| Запрос | Сырой ответ A-Parser | Парсер кода |
|---|---|---|
| python.org | `registered: 1, expire: 28.03.2033, creation: 27.03.1995` | `None/None` ❌ |
| doctortop.ru (реальный дроп из фида) | `registered: 1, expire: 07.06.2026, creation: 07.06.2021` | `None/None` ❌ |
| free-drop-nonexistent-2026.ru | `registered: 0, expire: none, creation: none` | `None/None` ❌ |
| опрелеленно-свободный-2026.рф | `registered: 0, expire: none, creation: none` | `None/None` ❌ |

Бонус-наблюдение: `doctortop.ru` — дроп из сегодняшнего фида — показывает
`registered: 1` (дропы остаются зарегистрированными до даты удаления). Это живое
доказательство «логической бомбы» C3: после фикса C1 такие домены без deadline-aware
логики уходили бы в `not_acquirable` навсегда.

---

## 4. Находки: Critical

### C1. T1 (whois) мёртв целиком — формат A-Parser не парсится {живьём}
- Где: `backend/app/integrations/aparser.py:13-43` (`_RE_RU`, `_RE_GTLD`, `_FREE_MARKERS`, `_REG_MARKERS`).
- Вход → результат: любой домен → `{available: None, created: None}` (см. §3).
- Следствия:
  - сырые источники (cctld/reg_ru/sweb): `available=None` → `acquirability_unresolved` →
    домен вечно `discovered` → на каждом прогоне занимает слот `limit` и единицу
    `max_whois_per_run` (starvation, см. I2);
  - free-lane (optimizator-путь) недостижим: `av is True` не случается никогда;
  - возраст-гейт T1 не работает — только Wayback-фолбэк (после дорогого T3).
- Фикс: парсить реальный формат: `registered:\s*(0|1)` → available (0=True/свободен,
  1=False/занят), `creation:\s*(\d{2})\.(\d{2})\.(\d{4})` → created (+ `creation: none`).
  Старые регексы оставить фолбэком (формат зависит от пресета A-Parser). Тест на живые
  строки из §3.

### C2. Spamhaus-гейт fail-open — молча пропускает всё как «чистое» {живьём}
- Где: `backend/app/integrations/blacklist.py:30-33` (stdlib-путь), `:40-41`
  (`except OSError → None`); `backend/app/services/scoring.py:147-149` (None не
  попадает в `sig["errors"]`).
- Живое подтверждение: тест-поинт `test.dbl.spamhaus.org` с системного резолвера →
  NXDOMAIN (не сентинел `127.255.255.x` из докстринга) → `is_blacklisted` возвращает
  `False` = «чист на всех зонах». `DNS_RESOLVER` в `.env` пуст и на Mac, и на боксе →
  **в проде спам-проверка не проверяет ничего**.
- Вторая половина: `blacklisted=None` (OSError-путь) пишется в БД без записи в errors →
  risk-guard `_decide` (scoring.py:30-32) не срабатывает → возможен auto-approve без
  фактической проверки. Противоречит докстрингу модуля («never treat as clean → RAISE»).
- Третья половина: blacklist ВООБЩЕ отсутствует в `/diag` `_spec()`
  (`backend/app/services/diagnostics.py:29-47`) — мёртвость проверки не видна нигде.
- Фикс (три части):
  1. контрольный запрос тест-поинта (`test.dbl.spamhaus.org` → должен дать `127.0.1.2`)
     при первом использовании клиента; NXDOMAIN/иное → RAISE (проверка недоступна);
  2. `if sig["blacklisted"] is None: sig["errors"].append("blacklist:unavailable")` в
     `_funnel` — чтобы risk-guard уводил в manual;
  3. добавить blacklist в `/diag` (ping уже написан и честен).

### C3. cctld-источник: мусор со страницы + перманентная отбраковка не-дождавшихся дропов {живьём + логика}
- Где: `backend/app/integrations/cctld.py:16-23, 32-35`; `backend/app/services/scoring.py:130-134`; `scoring.py:249`.
- Живое подтверждение мусора: 8 «кандидатов» — `yandex.ru`, `regcctld.ru`, `netoscope.ru`
  (навигация страницы). Парсер натравлен на весь HTML, а не на реестр.
- Логическая бомба (активируется фиксом C1!): dellist — реестр *освобождающихся*
  (ещё зарегистрированных) доменов; после фикса whois они получат `available=False` →
  `rejected/not_acquirable` **навсегда** (`score_pending` берёт только `discovered`,
  rejected не перепробуется; повторный discovery пропустит — уже в БД). Реестр
  освобождающихся по построению работает только на уже освободившиеся.
- Фикс: (а) парсить именно таблицу dellist + тянуть дату освобождения в
  `acquire_deadline`; (б) в T1: `av is False` при `acquire_deadline` в будущем или
  неизвестном для сырого источника → оставлять `discovered` (unresolved), НЕ reject;
  (в) до выверки живой разметки — выключить cctld дефолтом в `sources_enabled`.
- ⚠️ C1+C3 чинить вместе: фикс одного без другого ухудшает поведение.

---

## 5. Находки: Important

### I1. `.рф` из backorder-фида молча выбрасывается
- `backend/app/services/discovery.py:13` — `_DOMAIN_RE` ASCII-only; фид отдаёт .РФ
  кириллицей (docs/api/backorder.md, gotcha 5). `normalize_row("пример.рф")` → None,
  без лога и счётчика. Теряется весь .РФ-сегмент единственного RD-несущего источника.
- Фикс: единая IDNA-канонизация на входе ВСЕХ источников (punycode через
  `encode("idna")` или расширение регекса + нормализатор). Решает и I3.

### I2. Вечно-unresolved домены: starvation очереди + сжигание whois-бюджета
- `scoring.py:130-137` + `:248-252`: домен с нераспознаваемым whois навсегда
  `discovered`, при высоком RD сортируется первым → каждый прогон скорит одних и тех же;
  свежие домены не скорятся никогда. Ошибок в run-логе нет. Сейчас усилено C1 (whois
  None на всём). После фикса C1 остаётся для незнакомых TLD.
- Фикс: счётчик попыток / `last_probe_at` + backoff, либо исключать недавно-unresolved
  из выборки; после N проб → manual `scored`.

### I3. IDN-дуализм: один .рф-домен — две строки БД
- `cctld.py:9-14`: из HTML извлекаются ОБЕ формы (`xn--…` и кириллица) как разные
  кандидаты. UNIQUE(domain) не спасает — строки разные. Двойной whois/Wayback-бюджет,
  обе могут дойти до очереди выкупа. Фикс: та же канонизация, что I1.

### I4. Wayback: 1 скачанный снапшот из 5 = «история проверена»
- `wayback.py:83-95,104`: фикс «пустой ≠ чистый» держится только для ok==0. При ok==1
  (4 из 5 задросселены — троттлинг archive.org систематический) → `wayback_checked=True`,
  упавшие снапшоты считаются чистыми. Казино-дроп с одним чистым ранним снапшотом может
  auto-approve. Поле `sampled` нигде не читается.
- Фикс: порог покрытия (`ok >= sample//2+1` → checked=True, иначе False), тащить
  `sampled` в sig/БД.

### I5. Существующая строка БД никогда не обогащается («больший RD выигрывает» — только внутри батча)
- `discovery.py:110-126`: `_insert` вставляет только новые. День 1: домен из cctld
  (RD=NULL); день 2: тот же из backorder (links=42, дедлайн, флаги) → пропущен как
  existing. RD навсегда NULL (rd_proxy=0), lane NULL (→ not_acquirable-путь вместо bid),
  дедлайна нет. CLAUDE.md обещает «больший RD выигрывает» без оговорки — код расходится.
- Фикс: для existing со статусом `discovered` — дозаполнять NULL-поля и повышать RD;
  статусы/`reject_reason` не трогать.

### I6. «Тихое пусто» источника не видно нигде
- `discovery.py:76-77` логирует только исключения; 0 строк от включённого источника —
  ноль следов. `run_discovery` репортит «собрано N», где N = вставленные НОВЫЕ (при
  полном дедупе «собрано 0» выглядит как смерть источников). Плюс `fetch_html`
  (`aparser.py:96-98`) требует `resultString.startswith("200")` и `\n\n` — статус-строка
  `HTTP/1.1 200 OK` или CRLF → молчаливый None → источник вернул `[]` без warning.
  CLAUDE.md обещает «ошибки источников в docker-логах» — этот класс туда не попадает.
- Живое подтверждение: reg_ru вернул `596 HTTPS(C) proxy error: … 502 Bad Gateway` →
  fetch_html молча None → «строк: 0» без следов. Прокси-ошибки A-Parser — регулярный
  класс отказа, не экзотика.
- Фикс: per-source INFO-счётчик строк + WARNING при нуле; в `fetch_html` различать
  «не-200» (логировать статус) от «нет тела»; принимать `HTTP/... 200` и нормализовать `\r\n`.

### I7. m1_live.py сломан
- `backend/scripts/m1_live.py:23-28` не импортирует `app.models.settings` (и autonomy) →
  `create_all` не создаёт `scoring_settings` → `score_domain` падает
  `no such table: scoring_settings` (воспроизведено). Скрипт протух при вводе
  рантайм-порогов (миграция 0002).
- Фикс: добавить импорты; заодно печатать `reject_reason`.

---

## 6. Находки: Minor (пакетом в один таск)

| # | Где | Что | Фикс |
|---|---|---|---|
| M1 | discovery.py:44 | сентинелы фида пишутся как данные: `visitors=-1`, `tic=0` неотличим от нуля | отрицательные → None |
| M2 | backorder.py:49-61 | инвариант gotcha 2 (все записи удовлетворяют фильтру) не проверяется | assert/warning на выборке |
| M3 | discovery.py:13 | `www.example.ru` проходит регекс → отдельная строка от example.ru | срезать `www.` в нормализаторе |
| M4 | discovery.py:22-24 | `_parse_deadline` теряет таймзону (`+03:00` парсится как UTC) | пока фид отдаёт голую дату — не стреляет; учесть при фиксе |
| M5 | discovery.py:122 | `acquire_price` NULL на первом прогоне (кэш тарифа пуст до «Обновить цены»); per-domain `price` из фида выбрасывается | брать `price` из строки фида |
| M6 | CLAUDE.md | enum `reject_reason` без `not_acquirable` | дописать |
| M7 | settings.py:56-68 | `approve_at < manual_review_at` записывается (инверсия порогов → зона manual становится approve) | clamp `approve_at = max(approve, manual)` |
| M8 | scoring.py:196 | `trademark_risk` из БД никогда не читается (мёртвый гейт); если оживёт — reject_reason будет ложный `low_score` | перенести `d.trademark_risk` в sig |
| M9 | panel.py:309-313 | `POST /domains/{id}/score` без гейта по статусу — рескор `purchased/live` откатывает lifecycle | скорить только discovered/scored/rejected |
| M10 | scoring.py:160-164 | history_dirty-путь теряет `wayback_checked/first_seen/age` (подтверждено в живом прогоне: у azino777 age NULL при найденных 13.14) | сохранять sig до return |
| M11 | wayback.py:15-35 | стоп-слова: нет брендов «вулкан/azino/joycasino/пин ап» (FN); `slots` ловит «time slots» (FP, режет чистое) | расширить/уточнить списки |
| M12 | scoring.py:256-261 | падение одного домена в `score_pending` роняет остаток батча (нет per-entity изоляции, в отличие от стадий оркестратора) | try/except вокруг score_domain |
| M13 | settings.py:14 | `max_whois_per_run=0` разрешён → скоринг вечно делает ноль работы без ошибок | нижняя граница 1 или явный «выкл» |

Наблюдение (не баг, помнить при калибровке): NULL-RD домен с чистой историей, age≥8,
echo=True набирает ровно 0.70 = дефолтный approve_at → auto-approve без данных об
авторитетности.

---

## 7. Что подтверждено здоровым (не трогать)

- Ранний выход воронки: регрессия `wb.calls == 0` честная (T0–T2-отказники не доходят
  до Wayback) — подтверждено и живым прогоном (у rkn-отказников flags пустые, echo None).
- Рантайм-пороги `/settings` реально правят вердикт (score_domain и авто-режим — один
  `_decide`); превью зеркалит NULL-RD-пропуск.
- «Пустой Wayback ≠ чистый» держится (ok==0 → checked=False → downgrade-гард).
- РКН fail-closed: маленький дамп без кэша → raise; ошибка → errors → risk-guard.
- Веса в сумме 1.0; DR informational-only (вес 0) держится; NULL-поля дают 0.
- Дедуп внутри батча (больший RD) и гонка вставки (IntegrityError → перечитать → досыпать).
- Гейты денег/редактуры: не затронуты аудитом, инварианты в силе.

## 8. Дыры тестового покрытия (закрыть вместе с фиксами)

1. `.рф`-строка из backorder через normalize_row (поймала бы I1).
2. `is_blacklisted() → None` сквозь score_domain до статуса (C2-проводка).
3. Сбой RKN/blacklist исключением в `_funnel` end-to-end до `scored` (есть только pure-тест).
4. Wayback partial-fetch (1 ok из 5) → checked должен стать False (I4).
5. Кросс-прогонное обогащение existing-строки (I5) + сохранность статуса при re-run.
6. Инверсия порогов approve<manual (M7).
7. Падение одного домена в score_pending — остальные выживают (M12).
8. Рескор purchased/live через роут (M9).
9. `_parse_domains` на реалистичном HTML с chrome/e-mail/суффиксами (C3/I3).
10. `fetch_html`: статус-строки `200`/`HTTP/1.1 200`, CRLF, non-200 (I6).
11. Живые строки whois-свёртки A-Parser из §3 (C1).

## 9. Рекомендуемая нарезка фиксов (для плана)

1. **whois-формат A-Parser** (C1) + тест на живые строки — разблокирует T1.
2. **blacklist fail-closed** (C2: контрольный запрос + None→errors + /diag) — закрывает молчащий гейт.
3. **cctld + not_acquirable-логика** (C3) — вместе с 1; до выверки разметки cctld выключить дефолтом.
4. **IDNA-канонизация всех источников** (I1+I3+M3).
5. **Wayback порог покрытия** (I4).
6. **Обогащение existing + m1_live + тихое пусто** (I5+I7+I6).
7. **Minor-пакет** (M1–M13) + тестовые дыры §8.

Порядок 1→2 — самые ценные: сейчас и T1, и Spamhaus-гейт фактически выключены.

## 10. Открытое / непроверяемое с Mac

- reg_ru/sweb-разметка НЕ выверена и после перепроверки: reg_ru — прокси-596 (страница
  не получена), sweb — ReadTimeout. Сами регексы `_parse_domains` на их живом HTML ещё
  не встречались; повторить после фикса I6 (когда отказы станут видимыми).
- JSON тарифа backorder (`price_ru_backorder.ru.json`) — не проверялся.
- Троттлинг archive.org на боксе (масштаб I4) — замерить на реальном прогоне.
- Сентинел/NXDOMAIN Spamhaus с резолвера бокса (Docker Desktop → Windows → ISP) —
  проверить `test.dbl.spamhaus.org` с бокса; фикс C2 закрывает оба исхода.
