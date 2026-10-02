# План реализации — подпроект 1: M1 международные discovery и скоринг

> **Для исполнителя-агента:** ОБЯЗАТЕЛЬНЫЙ навык — `superpowers:subagent-driven-development`
> (рекомендуется) или `superpowers:executing-plans`. Шаги — чекбоксы `- [ ]`. Ревью каждой задачи —
> агент `combine-reviewer` (`.claude/agents/combine-reviewer.md`).

**Цель:** машина сама находит международные домены (pending delete с DropCatch и Nominet, удалённые
.mx, EMD-новореги, ручной список), отсекает мусор на входе по бесплатному DR Ahrefs и оценивает
выживших воронкой из 6 волн (фильтры → доступность → риск → ссылки → история+тема → анкоры). В
инбокс к оператору приходит домен с языком и темой прошлого сайта. Машина ничего не одобряет сама:
`approved` ставит только человек — кнопкой в строке или пакетом.

**Архитектура:** эволюция существующего кода. Волновой конвейер `_run_waves` остаётся и становится
циклом по таблице волн. РФ-источники и РФ-проверки удаляются. Новые клиенты — тонкие httpx-модули в
`integrations/`, логика — в `services/`. Оба гейта (деньги, редактура) не трогаются.

**Стек:** Python 3.12 в Docker, код совместим с 3.10 (локальная `.venv`). FastAPI, SQLAlchemy 2,
Alembic, httpx, Jinja. **Новых зависимостей нет.**

**Спека:** `docs/v2/02-m1-discovery-scoring-spec.md` (ревизия 3) + `docs/v2/01-master-spec.md`.
Решения оператора и находки ревью плана — `docs/v2/04-plan-review.md` (§0 обязателен; номера находок
вида «1.8», «Р3» в задачах ссылаются на него). Факты о сервисах — `docs/v2/research/*.md`. Читать до начала.

---

## Глобальные ограничения

Действуют для каждой задачи:

- **Ветка:** `feat/v2-m1-international` от `main`. Коммит после каждой задачи в этой ветке. Пуш и
  мерж — только по слову оператора.
- **Python 3.10-совместимо:** без `datetime.UTC`, `typing.Self`, `except*`. Время — `timezone.utc`.
- **Зависимости:** `requirements.txt` не меняется.
- **Слои:** `integrations/` — только транспорт и парсинг; логика — `services/`.
- **Тесты герметичны:** autouse `_no_live_network` в `backend/tests/conftest.py` не трогать.
  HTTP подменяется на инстансе: `monkeypatch.setattr(client, "request", fake)` (платные методы Ahrefs
  `batch`/`anchors`/`metrics_history` ходят мимо ретрая — у них `_request_once`, Задача 2).
- **Команды** (из корня репо):
  - тесты: `cd backend && ../.venv/bin/python -m pytest tests -q` (на старте: **777 passed** за ~11 с);
  - один тест: `cd backend && ../.venv/bin/python -m pytest tests/test_x.py::test_y -v`;
  - линт: `.venv/bin/python -m pyflakes backend/app backend/tests` — должен быть пустой вывод.
- **Пары «найти → заменить»** применяются по порядку, каждая — к файлу в состоянии после предыдущих.
  Блок НАЙТИ ищется дословно, с переносами и отступами, и встречается в файле ровно один раз. Если его
  последняя строка — только начало строки файла (например, `"wayback": wayback,`), заменяется ровно этот
  текст, хвост строки остаётся.
- **Сьют зелёный и pyflakes чистый** в конце КАЖДОЙ задачи. Если задача меняет поведение, которое
  проверял старый тест, тест обновляется под новое поведение — по ЯВНОМУ списку задачи («переписать
  так / удалить»). Инвариант (денежный гейт, «вслепую», правила пакета, «перескор не отмывает») при этом
  не ослабляется — такой тест переписывается, а не удаляется.
- **`approved` ставит только человек** (инвариант 9 мастер-спеки, решение оператора Р2). Скоринг даёт
  максимум `scored` (`_decide` — только `scored`/`rejected`). Всё, что раньше держали гарды
  авто-одобрения (история не проверена, whois/Web Risk/блэклист/Ahrefs упали, возраст неизвестен,
  анкоры не проверены), держит `scoring.bulk_ok` — единый предикат пакетного одобрения и подписи
  «история чистая»; плюс «прошлая тема далека от VPN» и пустой балл (EMD). `approve_at` — «порог
  сильного кандидата» (значение по умолчанию для пакета и счётчик в `/settings`), в решении скоринга не
  участвует. Гард-тесты v1 («never auto-approves …») переписываются в «не попадает в пакет».
- **Язык:** комментарии, сообщения задач, UI — по-русски. Комментарий объясняет «почему», в стиле
  окружающего кода.
- **Секреты** не попадают в URL, логи и текст ошибок. Ошибка сигнала пишется как
  `f"{префикс}:{type(e).__name__}"`. Ключ Web Risk — заголовок `X-Goog-Api-Key`.
- **Гейты:** `confirm_order`/`execute_confirmed_order`/`mark_caught`/`mark_edited` не трогать;
  оркестратор их не зовёт.
- **Дизайн панели** — `docs/DESIGN.md`. Только существующие классы `base.html` (`station`, `plate`,
  `details.what`, `what-body`, `go`, `hint`, `f`, `btn`, `btn-acc`, `btn-sm`, `flag`, `blind`, `src-badge`,
  `chips`/`chip`, `card`, `empty`, `flash err`/`tag`, `k-thr`/`k-dirt`/`k-taken`). Новых CSS-правил не изобретать.
- **Миграции:** `backend/alembic/versions/NNNN_description.py`, `revision`/`down_revision` — строки.
  Следующая — `0025_v2_m1`, `down_revision = "0024_domain_score_log"`.
- **Значения по умолчанию** (из спеки, дословно):

  | Параметр | Значение |
  |---|---|
  | `min_dr` | 5 |
  | `max_links_per_run` | 500 |
  | `max_deep_per_run` | 20 (0 = W6 выключен) |
  | `units_floor` | 300 000 — пол остатка units Ahrefs: один раз в начале прогона (до W2/W3) и перед W6 (0 = пола нет) |
  | `spam_anchor_max` | 0.2 |
  | `approve_at` / `manual_review_at` | 0.70 — «порог сильного кандидата» / 0.40 — ниже `rejected/low_score` |
  | «прошлая тема далека от VPN» | `topical_relevance < 0.3` (`TOPIC_FAR_BELOW`) — вне пакета |
  | PBN | `refips_subnets / refdomains < 0.3` при `refdomains ≥ 20` → компонент `rd` × 0.5 |
  | `tld_allowlist` | `com net org online xyz site co.uk mx co si nl in` |
  | `sources_enabled` | `{"dropcatch": false, "nominet": true, "mx": true, "emd": true}` |
  | веса | `history_cleanliness .25, topical_fit .15, age .12, rd .18, authority .10, anchor_quality .12, traffic_history .08` |
  | NORM | `DR_FULL 30, AGE_FULL 8, RD_FULL 3000, TRAFFIC_FULL 5000` |
  | Nominet | окно 3 дня |
  | DropCatch | файл `DaysOut2` |
  | пачки Ahrefs | DR 1000; batch 100 |
  | цена Ahrefs | DR-free 0 units; batch **25 units за домен** (живой замер); W6 ≈ 1,1 тыс. units за домен |
  | `dr_seen` | спрошенный DR не спрашивается **4 суток**; старше — чистится в начале discovery |
  | W6 | анкоров 50; история трафика 5 лет |
  | предохранители | RDAP, A-Parser whois, Web Risk, LLM темы: 3 сбоя подряд → без сети до конца прогона |
  | LLM темы (W5) | `LLM_CLASSIFY_MODEL` (пусто → `LLM_MODEL`), таймаут 30 с, одна попытка |

## Фокус ревью

Восемь входов, которые спека подразумевает, а пользователь встретит первым. Каждый закреплён тестом в
своей задаче.

1. **Повторный прогон discovery на тех же файлах.** Не дублирует строки, не трогает уже решённые
   статусы, не тратит DR ни на известные домены, ни на спрошенные за 4 суток (`dr_seen`) → Задача 6,
   `test_rerun_is_idempotent_and_skips_dr_for_known_and_remembered`.
2. **Домен в смешанном регистре или IDN из CSV** (`WerKleittechnik.com` из живого файла). Канонизируется
   и совпадает с уже известным → Задачи 4 и 6.
3. **Ahrefs вернул пачку не в том порядке, не все цели или IDN в Юникоде.** Метрики не приписываются
   чужому домену и не теряются: сопоставление только по `index` → Задача 2,
   `test_batch_maps_by_index_not_by_url_or_position`, `test_batch_idn_gets_metrics_by_index_though_url_is_unicode`.
4. **Нет ключа Web Risk; зона без RDAP.** Без ключа Web Risk домен приходит «вслепую»
   (`webrisk:not_configured`) и в пакет не попадает. Зона без RDAP (.mx/.co …) — штатный путь, не «вслепую»:
   доступность и возраст даёт whois:43 через A-Parser под капом `max_whois_per_run`. «Вслепую» — только при
   сбое канала (`whois:<Исключение>`): домен `bid` идёт дальше вне пакета, остальные ждут следующего
   прогона (`whois_failed`). Волна не падает → Задачи 9 и 10.
5. **«Стоп» посреди пачечной волны (W4/W6).** Задача закрыта как `cancelled`, домены, вышедшие на прошлых
   волнах, уже в БД → Задача 11, `test_links_wave_cancel_between_batches`. **Принятое ограничение**
   (находка 2.6, ponytail): оплаченные сигналы W4/W6 у доменов, ещё не вышедших из конвейера, пишутся в
   БД только при их выходе — отмена посреди W5/W6 или рестарт воркера (git-pull перезапускает его через
   watchfiles) теряет их, до потолка прогона (~12,5 тыс. units W4 + ~22 тыс. W6). Сохранять сигналы сразу —
   когда потери станут заметны.
6. **Сильный чистый домен — машина его не одобряет.** `scored`, с подписью «история чистая», и попадает в
   пакетное одобрение; непроверенный (Wayback/Web Risk/анкоры), с далёкой прошлой темой или EMD — в пакет
   не попадает → Задачи 8 и 13, `test_e2e_clean_strong_domain_is_scored_and_lands_in_bulk`,
   `test_far_past_topic_and_emd_stay_out_of_bulk`.
7. **Перескор, на котором проверка упала, не отмывает грязь** (спам-анкоры, угроза Web Risk) → Задачи 10 и
   13, `test_rescore_with_ahrefs_down_does_not_launder_spam_anchors`,
   `test_rescore_with_webrisk_failure_does_not_launder_the_threat`.
8. **Чужой сбой не даёт окончательного отказа** (второе ревью). Wayback ответил 429/503 — домен не
   `too_young` по одной дате RDAP (R2-1); LLM упал — японские анкоры не вечная грязь `spam_anchors` (R2-2);
   платные волны не пойдут (нет ключа Ahrefs, остаток ниже пола) — не-EMD домены не тратят W2/W3 и ждут
   следующего прогона, причина — в сообщении задачи (R2-10) → Задачи 9, 11, 13,
   `test_history_wayback_down_does_not_judge_age_by_rdap_alone`, `test_llm_down_japanese_anchors_are_not_dirt`,
   `test_links_no_key_skips_avail_and_risk_for_non_emd`.

## Карта файлов

| Файл | Действие | Ответственность |
|---|---|---|
| `backend/app/services/domain_filters.py` | создать | канон-форма домена, белый список зон, бренд-токены, генератор EMD (чистые функции) |
| `backend/app/integrations/ahrefs.py` | переписать заглушку | Ahrefs API v3: DR-free, batch, anchors, metrics-history, остаток units |
| `backend/app/integrations/rdap.py` | создать | бутстрап IANA (сбой -> статичная карта зон) + RDAP-lookup |
| `backend/app/integrations/webrisk.py` | создать | Google Web Risk `uris:search` |
| `backend/app/integrations/dropcatch.py`, `nominet.py`, `registry_mx.py` | создать | скачать и распарсить списки дропов |
| `backend/app/services/discovery.py` | переписать | v2-конвейер: источники → зоны → известные → DR → вставка; ручной список |
| `backend/app/services/history_llm.py` | создать | промпт и строгий разбор ответа LLM (язык/тема) |
| `backend/app/services/link_signals.py` | создать | доля спам-анкоров, пик трафика (чистые функции) |
| `backend/app/services/whois.py` | переписать | доступность: RDAP, иначе A-Parser whois:43 |
| `backend/app/services/scoring.py` | изменить | волны v2, `compute_score` v2, правила пакета (`bulk_ok`/`blind_reason`/`topic_far`), EMD |
| `backend/app/services/scoring_config.py`, `settings.py` | изменить | новые дефолты и ключи настроек |
| `backend/app/models/domain.py`, `models/settings.py` | изменить | новые колонки, модель `DrSeen` (память DR) |
| `backend/alembic/versions/0025_v2_m1.py` | создать | колонки, таблица `dr_seen`, новые дефолты, архив РФ-пула |
| `backend/app/integrations/wayback.py` | изменить | отдавать тексты прочитанных снимков |
| `backend/app/integrations/llm.py` | изменить | `LlmClassifyClient`: таймаут 30 с, одна попытка, модель `LLM_CLASSIFY_MODEL` |
| `backend/app/integrations/{aparser,searxng}.py` | изменить | удалить `ahrefs_probe`/`safebrowsing_check`/`archive_probe`, `indexed_echo` (SURBL в `blacklist.py` выключен ещё в v1 — ветки нет) |
| `backend/app/integrations/backorder.py` | изменить | удалить `list_dropping` (discovery v2 его не зовёт; выкуп M2 остаётся до подпроекта 2) — Задача 6 |
| `backend/app/integrations/blacklist.py` | изменить | докстринги без RKN (Задачи 10, 16); Spamhaus — только с DQS |
| `backend/app/api/panel.py`, `templates/{settings,domains,pool,autopilot,queue}.html`, `services/labels.py`, `services/diagnostics.py`, `services/diag_cache.py`, `services/transitions.py` | изменить | UI, подписи, диагностика (остаток units в кэше, баннер только от критичных), грязные причины, зона вне белого списка |
| `backend/app/api/domains.py` | изменить | докстринг «API внутренний» (DR без подписи) — Задача 16 |
| `backend/app/models/domain_score_log.py` | изменить | комментарии под волны v2 — Задача 16 |
| `backend/app/config.py`, `.env.example` | изменить | `WEBRISK_API_KEY`, `LLM_CLASSIFY_MODEL`; без `RKN_SOURCE_URL`, `CHECKTRUST_API_KEY` |
| `backend/tests/conftest.py` | изменить | autouse `_no_paid_keys`, `real_rdap_bootstrap` (Задача 3), офлайн-гвард источников v2 (Задача 6); `_no_live_network` не трогать |
| `backend/tests/fixtures/v2/` | создать | живые образцы форматов (DropCatch, Nominet, registry.mx, IANA, RDAP, Ahrefs); Web Risk и `metrics-history` — снимаются в Задаче 17 |
| `backend/tests/test_{domain_filters,ahrefs_api,rdap_webrisk,drop_sources,migration_0025,settings_v2,discovery_v2,compute_score_v2,whois,waves_v2,history_llm,link_signals,panel_settings_v2,panel_inbox_v2,diag_v2}.py` | создать | тесты задач 1–16 |
| существующие тесты (`test_funnel.py`, `test_scoring_waves.py`, `test_transitions.py`, `test_inbox.py` …) | изменить / удалить | только по явным спискам «Старые тесты» в задачах; тесты гейтов и инвариантов — переписываются, не удаляются |
| `README.md`, `docs/DONORS.md`, `docs/DEPLOY.md`, `docs/v2/CLAUDE.md` | изменить | карта модулей v2, указатель на методику v2, строка отката (только дамп), состояние |
| удалить | — | `integrations/{rkn,whois_tci,cctld,regru_drops,sweb_drops,checktrust,metrics}.py`; тесты `test_whois_tci.py` (тесты предохранителя A-Parser — в `test_whois.py`), `test_aparser.py`; методы A-Parser `ahrefs_probe`/`safebrowsing_check`/`archive_probe` |

---

### Задача 1: Чистые фильтры доменов

**Files:**
- Create: `backend/app/services/domain_filters.py`
- Modify: `backend/app/services/discovery.py` (перенести оттуда `_LABEL`, `_TLD`, `_DOMAIN_RE`, `canonical_domain`)
- Test: `backend/tests/test_domain_filters.py`

**Interfaces:**
- Produces:
  - `canonical_domain(raw) -> str | None` (переезжает, поведение то же; `discovery.canonical_domain`
    остаётся доступен реэкспортом);
  - `tld_match(domain: str, allowlist) -> str | None`;
  - `brand_hit(domain: str, tokens) -> str | None` — подстрокой ищутся только токены с `vpn` внутри
    или длиной от 7 символов, остальные — только целой частью имени между дефисами;
  - `emd_candidates(sets: list[dict], tokens) -> list[dict]`, где каждый элемент —
    `{"domain": str, "market": str, "lang": str}`; строка вместо списка в `keywords`/`tlds` набора —
    это один ключ/одна зона.

- [ ] **Шаг 1: Создать ветку**

```bash
git checkout main && git pull --ff-only && git checkout -b feat/v2-m1-international
```

- [ ] **Шаг 2: Написать падающий тест** — `backend/tests/test_domain_filters.py`

```python
"""Чистые фильтры v2: белый список зон, чужие VPN-бренды, генератор EMD."""
from app.services.domain_filters import brand_hit, canonical_domain, emd_candidates, tld_match


def test_tld_match_takes_registrable_zone_only():
    assert tld_match("foo.co.uk", ["uk", "co.uk"]) == "co.uk"
    assert tld_match("foo.co.uk", ["uk"]) is None        # зона foo.co.uk — co.uk, не uk
    assert tld_match("x.com.mx", ["mx"]) is None          # .com.mx NameSilo не продаёт — не наша
    assert tld_match("x.mx", ["mx"]) == "mx"
    assert tld_match("fooco.uk", ["co.uk"]) is None       # сравнение по полной метке
    assert tld_match("a.com", [" .COM "]) == "com"        # мусор в списке оператора нормализуется
    assert tld_match("a.com", []) is None


def test_brand_hit_substring_only_for_vpn_or_long_tokens():
    tokens = ["nordvpn", "surfshark", "pia", "avast", "norton"]
    # с «vpn» внутри или от 7 символов — подстрокой в имени без дефисов
    assert brand_hit("bestnordvpndeals.com", tokens) == "nordvpn"
    assert brand_hit("mynordvpn.com", tokens) == "nordvpn"
    assert brand_hit("nordvpn-deals.com", tokens) == "nordvpn"
    assert brand_hit("my-surf-shark.net", tokens) == "surfshark"   # дефисы не спасают
    # короткие без «vpn» — только целой частью между дефисами
    assert brand_hit("best-avast-deal.com", tokens) == "avast"
    assert brand_hit("pia-vpn.com", tokens) == "pia"
    assert brand_hit("javastudio.com", tokens) is None             # j-AVAST-udio — обычное слово
    assert brand_hit("nortonville.com", tokens) is None            # топоним, не бренд
    assert brand_hit("utopia.com", tokens) is None
    assert brand_hit("clean-vpn.com", tokens) is None


def test_emd_candidates_order_dedupe_brands_and_idn():
    sets = [{"market": "es-MX", "lang": "es",
             "keywords": ["mejor vpn", "  ", "nordvpn gratis", "vpn"],
             "tlds": ["com", ".MX"]},
            {"market": "es-CO", "lang": "es", "keywords": ["vpn"], "tlds": ["com"]}]
    out = emd_candidates(sets, ["nordvpn"])
    assert [c["domain"] for c in out] == [
        "mejorvpn.com", "mejorvpn.mx", "mejor-vpn.com", "mejor-vpn.mx", "vpn.com", "vpn.mx"]
    assert out[0] == {"domain": "mejorvpn.com", "market": "es-MX", "lang": "es"}
    idn = emd_candidates([{"keywords": ["vpn grátis"], "tlds": ["com"]}], [])
    assert idn[0]["domain"].startswith("xn--") and idn[0]["domain"].endswith(".com")


def test_emd_candidates_string_instead_of_list_is_one_item():
    # строка вместо списка — ОДИН ключ и ОДНА зона, а не набор букв (m.com, e.com, j.com…)
    out = emd_candidates([{"market": "es-MX", "lang": "es", "keywords": "mejor vpn", "tlds": "com"}], [])
    assert [c["domain"] for c in out] == ["mejorvpn.com", "mejor-vpn.com"]


def test_canonical_domain_still_reexported_from_discovery():
    from app.services import discovery
    assert discovery.canonical_domain is canonical_domain
    assert canonical_domain("WerKleittechnik.com") == "werkleittechnik.com"
```

- [ ] **Шаг 3: Запустить — убедиться, что падает**

Запуск: `cd backend && ../.venv/bin/python -m pytest tests/test_domain_filters.py -v`
Ожидание: FAIL — `ModuleNotFoundError: No module named 'app.services.domain_filters'`

- [ ] **Шаг 4: Реализовать**

Создать `backend/app/services/domain_filters.py` целиком (блок `_LABEL`/`_TLD`/`_DOMAIN_RE` с
комментарием и функция `canonical_domain` — дословно из `discovery.py`):

```python
"""Чистые фильтры доменов v2: канон-форма, белый список зон, чужие VPN-бренды, генератор EMD.

Без I/O. Отдельным модулем, потому что их зовут ДВА места: discovery (зоны на входе, EMD как
источник) и воронка (W0 — тот же белый список и бренды для ручного списка). Две копии правила
«какая зона наша» разъехались бы молча.
"""
import re

# Проверяем punycode-форму (ASCII), метка-за-меткой (аудит 2026-07-14, F30): старый
# `[a-z0-9-]+` пропускал мусор, который потом платно бьётся о whois/Ahrefs —
# ведущий/хвостовой дефис в метке ("-foo.ru"/"foo-.ru"), голый IP ("1.2.3.4" — цифровая
# последняя метка ловится тем же правилом, что и числовой TLD) и однобуквенный TLD
# ("foo.a"). Метка — не более 63 симв., не начинается/не кончается дефисом (RFC 1035);
# TLD — та же форма МЕТКИ, но с минимум двумя символами и без права быть числом целиком
# (punycode "xn--..." проходит: начинается/кончается буквой/цифрой, дефисы только внутри).
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_TLD = r"(?!\d+$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])"     # >=2 симв. и не чисто цифровой
_DOMAIN_RE = re.compile(rf"^(?:{_LABEL}\.)+{_TLD}$")


def canonical_domain(raw) -> str | None:
    """Единая канон-форма домена для ВСЕХ источников: lower, без www./точки, IDN→punycode.
    None если не домен (мусор, e-mail, пустое, недопустимые метки)."""
    s = (raw or "").strip().lower().rstrip(".")
    if s.startswith("www."):
        s = s[4:]
    if not s or len(s) > 253 or "@" in s or " " in s:
        return None
    try:
        puny = s.encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        return None                       # пустая метка, >63, недопустимый символ
    return puny if _DOMAIN_RE.match(puny) else None


def tld_match(domain: str, allowlist) -> str | None:
    """Зона из белого списка, в которой домен — РЕГИСТРИРУЕМОЕ имя, или None.

    Матч только когда перед зоной ровно одна метка: `foo.co.uk` — зона `co.uk`, а не `uk`;
    `x.com.mx` при списке [mx] — не наш (его зона com.mx, NameSilo её не продаёт). Без PSL:
    источники отдают регистрируемые имена, а белый список и есть наш перечень зон.
    """
    for raw in allowlist or ():
        t = str(raw).strip().strip(".").lower()
        if t and domain.endswith("." + t) and "." not in domain[: -len(t) - 1]:
            return t
    return None


_SUBSTRING_MIN = 7


def brand_hit(domain: str, tokens) -> str | None:
    """Чужой VPN-бренд в имени (первая метка) — или None.

    Подстрокой в имени без дефисов ищем только токены, которые в обычных словах не встречаются:
    с `vpn` внутри (`nordvpn` в `mynordvpn`) или длиной от 7 символов (`surfshark` в
    `my-surf-shark`). Остальные (`avast`, `norton`, `hideme`, `pia`) — только ЦЕЛОЙ частью между
    дефисами: подстрокой они бьют по обычным словам (`javastudio` содержит avast, `nortonville` —
    norton), а отказ по бренду жёсткий.
    ponytail: склейку с коротким брендом без дефиса (`bestavastdeal`) не ловим; понадобится —
    словарь брендов с границами слов.
    """
    label = domain.split(".", 1)[0].lower()
    parts = label.split("-")
    flat = label.replace("-", "")
    for raw in tokens or ():
        t = str(raw).strip().lower()
        if not t:
            continue
        if "vpn" in t or len(t) >= _SUBSTRING_MIN:
            if t in flat:
                return t
        elif t in parts:
            return t
    return None


_SPLIT = re.compile(r"[\s_\-]+")


def _as_list(v) -> list:
    """Строка вместо списка — это ОДИН элемент: перебор строки дал бы буквы (m.com, e.com…)."""
    return [v] if isinstance(v, str) else list(v or ())


def emd_candidates(sets, tokens) -> list[dict]:
    """Наборы оператора {market, lang, keywords[], tlds[]} -> EMD-кандидаты без сети.

    Ключ -> слова -> склейка и (если слов > 1) через дефис -> × каждый TLD набора. Имена с чужим
    брендом не генерируются (W0 их всё равно отклонил бы). Дубли между наборами убираются, порядок
    стабилен: ключ за ключом, склейка раньше дефиса, TLD в порядке набора.
    ponytail: без модификаторов best-/top- — оператор впишет их в ключи сам.
    """
    out, seen = [], set()
    for s in sets or ():
        market, lang = str(s.get("market") or "")[:16], str(s.get("lang") or "")[:8]
        for kw in _as_list(s.get("keywords")):
            words = [w for w in _SPLIT.split(str(kw).strip().lower()) if w]
            if not words:
                continue
            names = ["".join(words)] + (["-".join(words)] if len(words) > 1 else [])
            for name in names:
                for tld in _as_list(s.get("tlds")):
                    d = canonical_domain(f"{name}.{str(tld).strip().strip('.').lower()}")
                    if d and d not in seen and not brand_hit(d, tokens):
                        seen.add(d)
                        out.append({"domain": d, "market": market, "lang": lang})
    return out
```

В `backend/app/services/discovery.py`:
1. удалить строку `import re` — после переноса регулярное выражение в модуле не используется;
2. удалить блок от комментария `# Проверяем punycode-форму (ASCII), метка-за-меткой (аудит 2026-07-14, F30)…`
   до строки `_DOMAIN_RE = re.compile(rf"^(?:{_LABEL}\.)+{_TLD}$")` включительно;
3. удалить функцию `canonical_domain` целиком (от `def canonical_domain(raw)` до её `return`);
4. сразу после строки `from datetime import datetime, timezone` добавить:

```python
from app.services.domain_filters import canonical_domain   # реэкспорт: discovery.canonical_domain жив
```

`canonical_domain` дальше используется в `discovery.py` (`normalize_row`, `run_discovery`, блок
`__main__`), поэтому pyflakes импорт не помечает.

- [ ] **Шаг 5: Запустить тесты**

Запуск: `cd backend && ../.venv/bin/python -m pytest tests/test_domain_filters.py tests/test_sources.py tests/test_fresh_install.py -v`
Ожидание: PASS. Старые тесты `canonical_domain` идут через реэкспорт.

- [ ] **Шаг 6: Весь сьют + линт**

Запуск: `cd backend && ../.venv/bin/python -m pytest tests -q && cd .. && .venv/bin/python -m pyflakes backend/app backend/tests`
Ожидание: 782 passed (777 + 5 новых), pyflakes пуст.

- [ ] **Шаг 7: Коммит**

```bash
git add backend/app/services/domain_filters.py backend/app/services/discovery.py backend/tests/test_domain_filters.py
git commit -m "feat(v2): domain_filters — канон, белый список зон, VPN-бренды, генератор EMD"
```

---

### Задача 2: Клиент Ahrefs API v3

**Files:**
- Modify (переписать заглушку целиком): `backend/app/integrations/ahrefs.py`
- Delete: `backend/app/integrations/metrics.py`, `backend/app/integrations/checktrust.py`
- Modify:
  - `backend/app/config.py` — убрать `METRICS_PROVIDER`, `CHECKTRUST_API_KEY`;
  - `backend/app/services/diagnostics.py` — убрать `"CHECKTRUST_API_KEY"` из `_SECRET_FIELDS`;
  - `.env.example` — убрать строки `METRICS_PROVIDER=…` и `CHECKTRUST_API_KEY=`.
- Test: `backend/tests/test_ahrefs_api.py`, фикстуры `backend/tests/fixtures/v2/ahrefs_*.json`

**Interfaces:**
- Produces: `AhrefsClient(api_key: str | None = None)` (без аргумента — ключ из `settings.AHREFS_API_KEY`)
  с методами:
  - `.dr_free(domains: list[str]) -> dict[str, float]` (≤1000; ключ — канон-форма ASCII/punycode
    спрошенного домена, хотя IDN в ответе приходит в Юникоде; строки о доменах, которых не спрашивали,
    отбрасываются; домена нет в ответе — DR неизвестен);
  - `.batch(domains: list[str]) -> dict[str, dict]` (≤100) с ключами `BATCH_FIELDS =
    ("domain_rating","refdomains","refdomains_dofollow","refips_subnets","backlinks","org_traffic")`.
    Строка ответа сопоставляется с доменом ТОЛЬКО по `index` (0-based, порядок `domains`); домена без
    своей строки в ответе нет (вызывающий решает). Цена — 25 units за строку;
  - `.anchors(domain, limit=50) -> list[dict]`;
  - `.metrics_history(domain, years=5, today=None) -> list[dict]` — ответ без списка `metrics` →
    `ValueError` (формат снят по документации, живьём — Задача 17; W6 пишет `deep_history:ValueError`);
  - `.units_left() -> int | None`;
  - `.ping() -> bool`.
  - Платные `batch`/`anchors`/`metrics_history` идут через `BaseClient._request_once` — ОДНА попытка,
    без ретрая tenacity (находка R2-6: ретрай таймаута списал бы units трижды). Бесплатные `dr_free`/
    `units_left` — через `request` с ретраем. В тестах платные методы подменяются на инстансе через
    `monkeypatch.setattr(c, "_request_once", fake)`.

- [ ] **Шаг 0: Живая проверка DR пачкой — перенесена в Задачу 17, шаг 3а** (решение оператора 2026-10-02:
  проверить сейчас нельзя, план идёт без неё). Исполнитель на этом шаге ничего не делает и оператора не ждёт.

Живые снимки `batch-analysis` (Ahrefs MCP, 3 цели, 75 units: `select` с `index` и `url`) и
`public/domain-rating-free` (Ahrefs MCP, 3 цели в одном запросе, 0 units, с IDN) уже сняты 2026-10-01 —
это фикстуры `ahrefs_batch.json` и `ahrefs_dr_free.json` шага 1. Не проверено только, что СВОЙ ключ
приложения (не MCP-коннектор) принимает бесплатный DR пачкой. Код задач 2–16 от исхода не зависит: если
пачку не примут, `_dr_filter` (Задача 6) не сохранит доменов автоматических источников и напишет
«DR недоступен — N пропущено». Задача 17 проверяет это до первого discovery.

- [ ] **Шаг 1: Фикстуры** — живые ответы 2026-10-01 (кроме `ahrefs_metrics_history.json` — по документации)

`backend/tests/fixtures/v2/ahrefs_batch.json` — живой ответ `batch-analysis` (Ahrefs MCP, 75 units) на
цели `["pharmaindustrie.com", "nordvpn.com", "xn--e1afmkfd.xn--p1ai"]`. Обратить внимание: `url` — со
слэшем на конце, а IDN вернулся в Юникоде (`пример.рф/`), хотя отправлен punycode:

```json
{"targets": [
  {"index": 0, "url": "pharmaindustrie.com/", "domain_rating": 0.0, "refdomains": 719, "refdomains_dofollow": 358,
   "refips_subnets": 197, "backlinks": 803, "org_traffic": 0},
  {"index": 1, "url": "nordvpn.com/", "domain_rating": 88.0, "refdomains": 52520, "refdomains_dofollow": 40675,
   "refips_subnets": 12145, "backlinks": 9977459, "org_traffic": 7619254},
  {"index": 2, "url": "пример.рф/", "domain_rating": 10.0, "refdomains": 575, "refdomains_dofollow": 66,
   "refips_subnets": 205, "backlinks": 657, "org_traffic": 0}
]}
```

`backend/tests/fixtures/v2/ahrefs_anchors_spam.json` — живой ответ для pharmaindustrie.com, сокращён:

```json
{"anchors": [
  {"anchor": "Honestly, back when pharmaindustrie.com was stagnant with minimal exposure online, I thought about giving up", "refdomains": 302, "is_spam": true, "first_seen": "2026-04-19T17:56:55Z"},
  {"anchor": "Expert SEO Links and Backlinks for pharmaindustrie.com Ranking Growth", "refdomains": 278, "is_spam": true, "first_seen": "2026-09-15T21:23:11Z"},
  {"anchor": "pharmaindustrie.com", "refdomains": 94, "is_spam": true, "first_seen": "2024-08-12T12:33:45Z"},
  {"anchor": "High Quality Dofollow Backlinks DA 50 PA 40 Premium PBN Network Service pharmaindustrie.com", "refdomains": 42, "is_spam": true, "first_seen": "2026-08-04T20:25:11Z"}
]}
```

`backend/tests/fixtures/v2/ahrefs_metrics_history.json` — **по документации, НЕ живой ответ** (схема
`site-explorer/metrics-history`; ключа приложения 2026-10-01 не было). Живой образец снимается в
Задаче 17 и заменяет эту фикстуру; до того парсер строгий — ответ без списка `metrics` бросает
исключение, а не возвращает «трафика не было»:

```json
{"metrics": [
  {"date": "2022-01-01", "org_traffic": 120}, {"date": "2023-04-01", "org_traffic": 1850},
  {"date": "2024-06-01", "org_traffic": 40}, {"date": "2026-09-01", "org_traffic": 0}
]}
```

`backend/tests/fixtures/v2/ahrefs_limits.json` (живой ответ `subscription-info`):

```json
{"limits_and_usage": {"subscription": "Enterprise 2022, billed monthly", "usage_reset_date": "2026-10-04T00:00:00Z",
  "units_limit_workspace": 2000000, "units_usage_workspace": 84961, "units_limit_api_key": null,
  "units_usage_api_key": 52472, "api_key_expiration_date": "2036-08-14T10:52:29Z"}}
```

`backend/tests/fixtures/v2/ahrefs_dr_free.json` — живой ответ `public/domain-rating-free` (Ahrefs MCP,
0 units) на запрос `{"targets": ["xn--e1afmkfd.xn--p1ai", "pharmaindustrie.com", "NordVPN.com"]}`.
Обратить внимание: регистр понижен, слэш на конце, `index` нет, а IDN вернулся в Юникоде
(`пример.рф/`), хотя отправлен punycode:

```json
{"domain_rating": {"targets": [{"target": "пример.рф/", "domain_rating": 10.0},
  {"target": "pharmaindustrie.com/", "domain_rating": 0.0},
  {"target": "nordvpn.com/", "domain_rating": 88.0}],
  "license": "http://ahrefs.com/legal/domain-rating-license"}}
```

- [ ] **Шаг 2: Написать падающий тест** — `backend/tests/test_ahrefs_api.py`

```python
"""Ahrefs API v3: разбор живых ответов (фикстуры 2026-10-01), форма запросов, без сети."""
import json
import pathlib
from datetime import date

import httpx
import pytest

from app.integrations.ahrefs import AhrefsClient, BATCH_FIELDS

FX = pathlib.Path(__file__).parent / "fixtures" / "v2"


def _fx(name):
    return json.loads((FX / name).read_text(encoding="utf-8"))


def _fake(payload, calls):
    """Подмена HTTP на инстансе. Бесплатные dr_free/units_left идут через `request` (с ретраем),
    платные batch/anchors/metrics_history — через `_request_once` (одна попытка): подменяется тот
    метод, которым ходит проверяемый вызов."""
    def request(method, url, **kw):
        calls.append({"method": method, "url": url, **kw})
        return httpx.Response(200, json=payload, request=httpx.Request(method, url))
    return request


def test_dr_free_keys_by_punycode_strips_slash_and_takes_only_asked(monkeypatch):
    # живой ответ 2026-10-01: слэш на конце, IDN в Юникоде (`пример.рф/`), хотя спрошен punycode.
    # Без IDNA в `_host` каждый IDN-домен молча ушёл бы в «DR недоступен».
    c, calls = AhrefsClient(api_key="k"), []
    monkeypatch.setattr(c, "request", _fake(_fx("ahrefs_dr_free.json"), calls))
    out = c.dr_free(["xn--e1afmkfd.xn--p1ai", "pharmaindustrie.com"])
    assert out == {"xn--e1afmkfd.xn--p1ai": 10.0, "pharmaindustrie.com": 0.0}  # nordvpn.com не спрашивали
    assert calls[0]["method"] == "POST" and calls[0]["url"].endswith("/public/domain-rating-free")
    assert calls[0]["json"] == {"targets": ["xn--e1afmkfd.xn--p1ai", "pharmaindustrie.com"]}
    assert calls[0]["headers"]["Authorization"] == "Bearer k"


def test_dr_free_empty_makes_no_call_and_rejects_over_1000(monkeypatch):
    c, calls = AhrefsClient(api_key="k"), []
    monkeypatch.setattr(c, "request", _fake({}, calls))
    assert c.dr_free([]) == {} and calls == []
    with pytest.raises(ValueError):
        c.dr_free([f"d{i}.com" for i in range(1001)])


def test_batch_maps_by_index_not_by_url_or_position(monkeypatch):
    c, calls = AhrefsClient(api_key="k"), []
    rows = list(reversed(_fx("ahrefs_batch.json")["targets"]))     # строки пришли в обратном порядке
    monkeypatch.setattr(c, "_request_once", _fake({"targets": rows}, calls))
    out = c.batch(["pharmaindustrie.com", "nordvpn.com", "xn--e1afmkfd.xn--p1ai", "missing.com"])
    assert out["pharmaindustrie.com"]["refdomains"] == 719
    assert out["nordvpn.com"]["refips_subnets"] == 12145
    assert "missing.com" not in out                                 # строки index=3 нет — метрик нет
    assert set(out["nordvpn.com"]) == set(BATCH_FIELDS)
    body = calls[0]["json"]
    assert body["select"][:2] == ["index", "url"] and set(BATCH_FIELDS) <= set(body["select"])
    assert body["targets"][0] == {"url": "pharmaindustrie.com", "mode": "subdomains", "protocol": "both"}


def test_batch_idn_gets_metrics_by_index_though_url_is_unicode(monkeypatch):
    # живой факт 2026-10-01: punycode в запросе, Юникод в ответе — по `url` IDN потерялся бы
    c = AhrefsClient(api_key="k")
    monkeypatch.setattr(c, "_request_once", _fake(_fx("ahrefs_batch.json"), []))
    out = c.batch(["pharmaindustrie.com", "nordvpn.com", "xn--e1afmkfd.xn--p1ai"])
    assert out["xn--e1afmkfd.xn--p1ai"]["refdomains"] == 575
    assert out["xn--e1afmkfd.xn--p1ai"]["domain_rating"] == 10.0
    assert "пример.рф" not in out


def test_batch_skips_rows_without_index_or_out_of_range(monkeypatch):
    c = AhrefsClient(api_key="k")
    row = _fx("ahrefs_batch.json")["targets"][1]
    no_index = {k: v for k, v in row.items() if k != "index"}
    monkeypatch.setattr(c, "_request_once", _fake(
        {"targets": [no_index, {**row, "index": 2}, {**row, "index": -1}]}, []))
    assert c.batch(["nordvpn.com", "other.com"]) == {}              # по позиции и по url не гадаем


def test_anchors_and_metrics_history_request_shape(monkeypatch):
    c, calls = AhrefsClient(api_key="k"), []
    monkeypatch.setattr(c, "_request_once", _fake(_fx("ahrefs_anchors_spam.json"), calls))
    assert c.anchors("pharmaindustrie.com")[1]["refdomains"] == 278
    p = calls[0]["params"]
    assert calls[0]["url"].endswith("/site-explorer/anchors")
    assert p["target"] == "pharmaindustrie.com" and p["limit"] == 50 and p["order_by"] == "refdomains:desc"
    monkeypatch.setattr(c, "_request_once", _fake(_fx("ahrefs_metrics_history.json"), calls))
    hist = c.metrics_history("x.com", years=5, today=date(2026, 10, 1))
    assert hist[1]["org_traffic"] == 1850
    assert calls[-1]["params"]["date_from"] == "2021-10-02"
    assert calls[-1]["url"].endswith("/site-explorer/metrics-history")


def test_metrics_history_without_metrics_list_raises(monkeypatch):
    # находка R2-21: формат metrics-history снят по документации, не живьём (живой образец —
    # Задача 17). Ответ без списка `metrics` — незнакомая форма: исключение (W6 запишет
    # `deep_history:ValueError`), а не тихий [] — «трафика не было» по ответу, который не поняли.
    c = AhrefsClient(api_key="k")
    for payload in ({}, {"error": "unexpected"}, {"metrics": None}, [{"date": "2026-01-01"}]):
        monkeypatch.setattr(c, "_request_once", _fake(payload, []))
        with pytest.raises(ValueError):
            c.metrics_history("x.com")
    monkeypatch.setattr(c, "_request_once", _fake({"metrics": []}, []))
    assert c.metrics_history("x.com") == []                     # пустая история — законный ответ


@pytest.mark.parametrize("paid", [
    lambda c: c.batch(["a.com"]),
    lambda c: c.anchors("a.com"),
    lambda c: c.metrics_history("a.com"),
], ids=["batch", "anchors", "metrics_history"])
def test_paid_call_is_sent_once_on_timeout(monkeypatch, paid):
    # находка R2-6: таймаут после того, как Ahrefs принял запрос, — уже списанные units; ретрай
    # tenacity списал бы их трижды. Подменяется httpx-клиент ПОД ретраем: через `request` этот
    # же таймаут ушёл бы 3 раза.
    c, calls = AhrefsClient(api_key="k"), []

    def timeout(method, url, **kw):
        calls.append(url)
        raise httpx.ReadTimeout("timed out", request=httpx.Request(method, url))
    monkeypatch.setattr(c._client, "request", timeout)
    with pytest.raises(httpx.ReadTimeout):
        paid(c)
    assert len(calls) == 1


def test_units_left_and_ping(monkeypatch):
    c = AhrefsClient(api_key="k")
    monkeypatch.setattr(c, "request", _fake(_fx("ahrefs_limits.json"), []))
    assert c.units_left() == 2000000 - 84961
    assert c.ping() is True
```

- [ ] **Шаг 3: Запустить — убедиться, что падает**

Запуск: `cd backend && ../.venv/bin/python -m pytest tests/test_ahrefs_api.py -v`
Ожидание: FAIL — `ImportError: cannot import name 'BATCH_FIELDS' from 'app.integrations.ahrefs'`

- [ ] **Шаг 4: Реализовать** — заменить содержимое `backend/app/integrations/ahrefs.py` целиком

```python
"""Ahrefs API v3 — транспорт. Base https://api.ahrefs.com/v3, Bearer AHREFS_API_KEY.

Живые замеры 2026-10-01 (docs/v2/research/metrics-history.md):
  · public/domain-rating-free — 0 units, до 1000 целей. ЛИЦЕНЗИЯ: DR показывать только с подписью
    «Domain Rating by Ahrefs» и ссылкой на ahrefs.com; «систематический» массовый сбор запрещён —
    поэтому discovery зовёт его ОДИН раз на домен и только для новых доменов;
  · batch-analysis — 25 units за строку (6 полей + index + url), до 100 целей;
  · site-explorer/anchors — ~9 units/строку;
  · subscription-info/limits-and-usage — бесплатно.
Платные методы (batch, anchors, metrics_history) ходят через `_request_once` — ОДНА попытка, без
ретрая BaseClient: таймаут после того, как Ahrefs принял запрос, — уже списанные units, и ретрай
списал бы их трижды. Сбой — исключение; вызывающий (W4/W6) пишет его в errors, домен — следующим
прогоном. Бесплатные dr_free/units_left ретраятся как обычно.
MCP-коннектор Ahrefs в чате Claude — НЕ это: приложению нужен свой ключ.
"""
from datetime import date, timedelta

from app.config import settings
from app.integrations.base import BaseClient

BATCH_FIELDS = ("domain_rating", "refdomains", "refdomains_dofollow", "refips_subnets",
                "backlinks", "org_traffic")


def _host(target) -> str:
    """'https://Example.com/' | 'пример.рф/' -> 'example.com' | 'xn--e1afmkfd.xn--p1ai'.

    Ahrefs возвращает target в нижнем регистре, со слэшем на конце и IDN — в Юникоде, хотя спрошен
    punycode (живой ответ 2026-10-01). Ключ — канон-форма ASCII, как домен хранится у нас; имя,
    которое IDNA не кодирует, остаётся как есть (тогда оно просто не совпадёт со спрошенным).
    """
    s = str(target or "").strip().lower()
    for p in ("https://", "http://"):
        if s.startswith(p):
            s = s[len(p):]
    s = s.rstrip("/")
    try:
        return s.encode("idna").decode("ascii")
    except UnicodeError:
        return s


class AhrefsClient(BaseClient):
    def __init__(self, api_key: str | None = None):
        super().__init__("https://api.ahrefs.com/v3", timeout=60.0)
        self.api_key = settings.AHREFS_API_KEY if api_key is None else api_key

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"}

    def dr_free(self, domains: list[str]) -> dict[str, float]:
        """{домен: DR} для ≤1000 доменов. Домена нет в ответе — DR неизвестен (вызывающий решает).

        Ключ — `_host` спрошенного имени. Строку о домене, которого не спрашивали, не берём: DR не
        приписывается чужому имени, а спрошенный домен честно остаётся «DR неизвестен».
        """
        if not domains:
            return {}
        if len(domains) > 1000:
            raise ValueError("dr_free: не больше 1000 доменов за запрос")
        r = self.request("POST", f"{self.base_url}/public/domain-rating-free",
                         headers=self._headers(), json={"targets": list(domains)})
        asked = {_host(d) for d in domains}
        out = {}
        for t in (r.json().get("domain_rating") or {}).get("targets") or []:
            name, dr = _host(t.get("target")), t.get("domain_rating")
            if name in asked and dr is not None:
                out[name] = float(dr)
        return out

    def batch(self, domains: list[str]) -> dict[str, dict]:
        """{домен: {поля BATCH_FIELDS}} для ≤100 доменов.

        Сопоставление — ТОЛЬКО по `index` строки ответа (0-based, порядок `targets` запроса). Не по
        `url`: живой ответ 2026-10-01 вернул IDN в Юникоде (`пример.рф/` на отправленный punycode) —
        по `url` терялись бы все IDN. И не по позиции строки: метрики, приписанные соседу, — это
        одобренный спам-домен с чужим DR. Строка без `index` или с индексом вне диапазона
        пропускается — домен остаётся без метрик, вызывающий решает. `url` в `select` оставлен:
        так снят живой образец и замерена цена (25 units за строку).
        """
        if not domains:
            return {}
        if len(domains) > 100:
            raise ValueError("batch: не больше 100 доменов за запрос")
        body = {"select": ["index", "url", *BATCH_FIELDS],
                "targets": [{"url": d, "mode": "subdomains", "protocol": "both"} for d in domains]}
        r = self._request_once("POST", f"{self.base_url}/batch-analysis/batch-analysis",
                               headers=self._headers(), json=body)
        out = {}
        for row in r.json().get("targets") or []:
            i = row.get("index")
            if isinstance(i, int) and not isinstance(i, bool) and 0 <= i < len(domains):
                out[domains[i]] = {k: row.get(k) for k in BATCH_FIELDS}
        return out

    def anchors(self, domain: str, limit: int = 50) -> list[dict]:
        """Анкоры по убыванию refdomains, вся история (all_time) — спам-волна после дропа видна и
        в уже потерянных ссылках."""
        r = self._request_once("GET", f"{self.base_url}/site-explorer/anchors",
                               headers=self._headers(),
                               params={"target": domain, "mode": "subdomains", "limit": limit,
                                       "order_by": "refdomains:desc", "history": "all_time",
                                       "select": "anchor,refdomains,is_spam,first_seen"})
        return r.json().get("anchors") or []

    def metrics_history(self, domain: str, years: int = 5, today: date | None = None) -> list[dict]:
        """Помесячный органический трафик за `years` лет: [{"date", "org_traffic"}].

        Формат снят по документации, НЕ живьём (живой образец — Задача 17). Поэтому ответ без
        списка `metrics` — ValueError (W6 пишет `deep_history:ValueError`), а не []: тихий пустой
        список читался бы как «трафика не было» по ответу, который мы не поняли.
        """
        start = (today or date.today()) - timedelta(days=365 * years)
        r = self._request_once("GET", f"{self.base_url}/site-explorer/metrics-history",
                               headers=self._headers(),
                               params={"target": domain, "mode": "subdomains",
                                       "date_from": start.isoformat(), "history_grouping": "monthly",
                                       "select": "date,org_traffic"})
        data = r.json()
        rows = data.get("metrics") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise ValueError("metrics_history: в ответе нет списка 'metrics'")
        return rows

    def units_left(self) -> int | None:
        """Остаток units в месяце (запрос бесплатный). None — лимит не сообщён."""
        r = self.request("GET", f"{self.base_url}/subscription-info/limits-and-usage",
                         headers=self._headers())
        lu = r.json().get("limits_and_usage") or {}
        lim, used = lu.get("units_limit_workspace"), lu.get("units_usage_workspace")
        return None if lim is None or used is None else int(lim) - int(used)

    def ping(self) -> bool:
        return self.units_left() is not None
```

Удалить `backend/app/integrations/metrics.py` и `backend/app/integrations/checktrust.py` (других
импортёров у них нет).

`backend/app/config.py` — блок

```python
    # metrics
    METRICS_PROVIDER: str = "ahrefs"
    AHREFS_API_KEY: str = ""
    CHECKTRUST_API_KEY: str = ""
```

заменить на

```python
    # Ahrefs API v3 (integrations/ahrefs.py): DR-free, batch-analysis, анкоры, история трафика
    AHREFS_API_KEY: str = ""
```

`backend/app/services/diagnostics.py` — кортеж `_SECRET_FIELDS` заменить целиком (убран
`CHECKTRUST_API_KEY`, поля больше нет в настройках):

```python
_SECRET_FIELDS = (
    "AHREFS_API_KEY", "DATAFORSEO_LOGIN", "DATAFORSEO_PASSWORD",
    "SERPAPI_KEY", "YANDEX_WORDSTAT_TOKEN", "BACKORDER_LOGIN", "BACKORDER_PASSWORD",
    "OPTIMIZATOR_API_KEY", "REGRU_PASSWORD", "CLOUDFLARE_API_TOKEN", "AAPANEL_API_KEY",
    "LLM_API_KEY", "APARSER_API_KEY", "GITHUB_TOKEN", "PANEL_PASS", "SPAMHAUS_DQS_KEY",
)
```

`.env.example` — удалить ровно две строки: `METRICS_PROVIDER=ahrefs           # ahrefs(free via A-Parser) | dataforseo | checktrust | none`
и `CHECKTRUST_API_KEY=`. Остальной блок «Метрики» не трогать (тексты `.env.example` чистит Задача 16).

Проверка, что ссылок не осталось:
`grep -rnE "integrations\.(metrics|checktrust)|get_metrics_provider|CheckTrustClient|METRICS_PROVIDER|CHECKTRUST" backend/app backend/tests .env.example`
— пустой вывод.

- [ ] **Шаг 5: Тесты, весь сьют, линт**

Запуск: `cd backend && ../.venv/bin/python -m pytest tests/test_ahrefs_api.py -v && ../.venv/bin/python -m pytest tests -q && cd .. && .venv/bin/python -m pyflakes backend/app backend/tests`
Ожидание: всё зелёное — 793 passed (782 + 11 новых: 9 функций, одна из них параметризована на 3
метода), pyflakes пуст.

- [ ] **Шаг 6: Коммит**

```bash
git add -A backend/app/integrations backend/app/config.py backend/app/services/diagnostics.py .env.example backend/tests/test_ahrefs_api.py backend/tests/fixtures/v2
git commit -m "feat(v2): клиент Ahrefs API v3 (DR-free, batch по index, anchors, metrics-history, units)"
```

---

### Задача 3: Клиенты RDAP и Google Web Risk

**Files:**
- Create: `backend/app/integrations/rdap.py`, `backend/app/integrations/webrisk.py`
- Modify:
  - `backend/app/config.py` — добавить `WEBRISK_API_KEY: str = ""`;
  - `.env.example` — строка `WEBRISK_API_KEY=`;
  - `backend/app/services/diagnostics.py` — `"WEBRISK_API_KEY"` в `_SECRET_FIELDS`;
  - `backend/tests/conftest.py` — autouse-фикстура `_no_paid_keys` и фикстура `real_rdap_bootstrap`.
- Test: `backend/tests/test_rdap_webrisk.py`, фикстуры `backend/tests/fixtures/v2/{iana_dns.json,rdap_pending_delete.json}`

**Interfaces:**
- Produces:
  - `RdapClient()` с методами `.has_rdap(domain) -> bool`, `.lookup(domain) -> {"exists": bool,
    "status": list[str], "registered_at": datetime | None}` (`registered_at` всегда aware, UTC при
    отсутствии смещения), `.ping() -> bool` (живой запрос в IANA мимо запомненной карты);
  - бутстрап IANA — не больше одного `request` на жизнь клиента: сбой запоминается (один warning,
    дальше статичная карта `rdap._FALLBACK` зон белого списка: com, net, org, uk, online, site, xyz,
    si, nl, in);
  - исключение `NoRdap(Exception)`;
  - `WebRiskClient(api_key=None)` с `.configured: bool`, `.threats(domain) -> list[str]`, `.ping() -> bool`.
    Формат ответа Web Risk **НЕ снят живьём** (ключа нет; JSON в тестах — по документации, живые образцы —
    Задача 17). Поэтому `threats` строгий: ответ не словарь, `threat` не словарь или без списка
    `threatTypes` → `ValueError` (W3 запишет `webrisk:ValueError`, домен «вслепую»), а не `[]` = «чисто»;
  - **тестовый харнесс** (`backend/tests/conftest.py`):
    - autouse `_no_paid_keys`: на каждый тест `settings.AHREFS_API_KEY`, `WEBRISK_API_KEY`,
      `SPAMHAUS_DQS_KEY` = `""`, а `RdapClient._bootstrap` возвращает `{}` — ни одна зона не имеет
      RDAP (`has_rdap` → False, `lookup` → `NoRdap`), в IANA никто не ходит. Тест, которому нужен
      RDAP в воронке, передаёт фейк через `clients["rdap"]`; тест, которому нужен ключ, ставит его
      сам через `monkeypatch.setattr(settings, "…", "…")`;
    - `real_rdap_bootstrap` (не autouse): возвращает настоящий `RdapClient._bootstrap` — для
      юнит-тестов самого клиента, которые подменяют HTTP на инстансе (`monkeypatch.setattr(c, "request", …)`).

- [ ] **Шаг 1: Фикстуры** — сокращённые живые ответы 2026-10-01

`backend/tests/fixtures/v2/iana_dns.json`:

```json
{"version": "1.0", "publication": "2026-09-30T23:00:03Z", "services": [
  [["com"], ["https://rdap.verisign.com/com/v1/"]],
  [["net"], ["https://rdap.verisign.com/net/v1/"]],
  [["uk"], ["https://rdap.nominet.uk/uk/"]],
  [["online", "site"], ["https://rdap.radix.host/rdap/"]]
]}
```

`backend/tests/fixtures/v2/rdap_pending_delete.json`:

```json
{"objectClassName": "domain", "ldhName": "PHARMAINDUSTRIE.COM",
 "status": ["client hold", "pending delete"],
 "events": [{"eventAction": "registration", "eventDate": "1998-07-29T04:00:00Z"},
            {"eventAction": "expiration", "eventDate": "2026-07-28T04:00:00Z"},
            {"eventAction": "last changed", "eventDate": "2026-09-27T09:15:09Z"}]}
```

- [ ] **Шаг 2: Написать падающий тест** — `backend/tests/test_rdap_webrisk.py`

```python
"""RDAP (бутстрап IANA, 404 = свободен, сбой IANA -> статичная карта) и Web Risk (ключ в
заголовке). Без сети: HTTP подменяется на инстансе."""
import json
import logging
import pathlib
from datetime import datetime, timezone

import httpx
import pytest

from app.integrations.rdap import NoRdap, RdapClient, _iso
from app.integrations.webrisk import WebRiskClient

FX = pathlib.Path(__file__).parent / "fixtures" / "v2"


def _router(routes, calls):
    """routes: {подстрока URL: payload | int-статус}."""
    def request(method, url, **kw):
        calls.append(url)
        req = httpx.Request(method, url)
        for key, val in routes.items():
            if key in url:
                if isinstance(val, int):
                    raise httpx.HTTPStatusError(str(val), request=req,
                                                response=httpx.Response(val, request=req))
                return httpx.Response(200, json=val, request=req)
        raise AssertionError(f"неожиданный URL {url}")
    return request


def _iana():
    return json.loads((FX / "iana_dns.json").read_text(encoding="utf-8"))


def _pending():
    return json.loads((FX / "rdap_pending_delete.json").read_text(encoding="utf-8"))


def test_rdap_pending_delete_keeps_original_registration(monkeypatch, real_rdap_bootstrap):
    c, calls = RdapClient(), []
    monkeypatch.setattr(c, "request", _router({
        "data.iana.org": _iana(),
        "rdap.verisign.com/com/v1/domain/pharmaindustrie.com": _pending()}, calls))
    r = c.lookup("pharmaindustrie.com")
    assert r["exists"] is True and "pending delete" in r["status"]
    assert r["registered_at"] == datetime(1998, 7, 29, 4, 0, tzinfo=timezone.utc)
    c.lookup("pharmaindustrie.com")
    assert sum("data.iana.org" in u for u in calls) == 1        # бутстрап — один раз на клиент


def test_rdap_404_means_free_and_missing_zone_raises(monkeypatch, real_rdap_bootstrap):
    c = RdapClient()
    monkeypatch.setattr(c, "request", _router({
        "data.iana.org": _iana(),
        "/domain/free-name.com": 404}, []))
    assert c.lookup("free-name.com") == {"exists": False, "status": [], "registered_at": None}
    assert c.has_rdap("x.co.uk") is True and c.has_rdap("x.mx") is False
    with pytest.raises(NoRdap):
        c.lookup("x.mx")


def test_rdap_5xx_propagates(monkeypatch, real_rdap_bootstrap):
    c = RdapClient()
    monkeypatch.setattr(c, "request", _router({
        "data.iana.org": _iana(),
        "/domain/x.com": 503}, []))
    with pytest.raises(httpx.HTTPStatusError):
        c.lookup("x.com")


def test_rdap_iana_down_falls_back_once_per_client(monkeypatch, real_rdap_bootstrap, caplog):
    # IANA 503: ОДИН запрос в IANA и один warning на жизнь клиента, дальше статичная карта —
    # не шторм ретраев под общим локом на каждый домен волны
    c, calls = RdapClient(), []
    monkeypatch.setattr(c, "request", _router({
        "data.iana.org": 503,
        "rdap.verisign.com/com/v1/domain/pharmaindustrie.com": _pending()}, calls))
    with caplog.at_level(logging.WARNING, logger="app.integrations.rdap"):
        for _ in range(5):
            assert c.lookup("pharmaindustrie.com")["exists"] is True    # .com — через _FALLBACK
        assert c.has_rdap("x.mx") is False
        with pytest.raises(NoRdap):
            c.lookup("x.mx")                                             # .mx нет и в карте
    assert sum("data.iana.org" in u for u in calls) == 1
    assert len([r for r in caplog.records if r.name == "app.integrations.rdap"]) == 1


def test_rdap_ping_asks_iana_directly(monkeypatch):
    # /diag не зеленеет на статичной карте: пинг ходит в IANA сам, мимо запомненного бутстрапа
    c, calls = RdapClient(), []
    monkeypatch.setattr(c, "request", _router({"data.iana.org": 503}, calls))
    with pytest.raises(httpx.HTTPStatusError):
        c.ping()
    monkeypatch.setattr(c, "request", _router({"data.iana.org": _iana()}, calls))
    assert c.ping() is True
    assert sum("data.iana.org" in u for u in calls) == 2


def test_iso_pads_fraction_and_assumes_utc_for_naive():
    # живой CentralNic (.xyz): '.0Z' — fromisoformat на Python 3.10 берёт дробь только из 3 или 6 цифр
    assert _iso("2014-03-20T12:59:17.0Z") == datetime(2014, 3, 20, 12, 59, 17, tzinfo=timezone.utc)
    assert _iso("2014-03-20T12:59:17.1234567Z") == datetime(2014, 3, 20, 12, 59, 17, 123456,
                                                           tzinfo=timezone.utc)
    naive = _iso("2014-03-20T12:59:17")                    # без смещения -> UTC, не наивная дата
    assert naive.tzinfo is not None and naive == datetime(2014, 3, 20, 12, 59, 17, tzinfo=timezone.utc)
    assert _iso("1998-07-29T04:00:00+02:00") == datetime(1998, 7, 29, 2, 0, tzinfo=timezone.utc)
    assert _iso(None) is None and _iso("") is None and _iso("not a date") is None


def test_default_harness_has_no_rdap_and_no_paid_keys():
    # autouse _no_paid_keys (conftest): боевые ключи из .env не видны, бутстрап IANA не зовётся,
    # ни у одной зоны нет RDAP — сети нет даже у теста, который забыл подменить клиент
    from app.config import settings
    assert settings.AHREFS_API_KEY == settings.WEBRISK_API_KEY == settings.SPAMHAUS_DQS_KEY == ""
    c = RdapClient()
    assert c.has_rdap("x.com") is False
    with pytest.raises(NoRdap):
        c.lookup("x.com")
    assert WebRiskClient().configured is False


# Web Risk: JSON ниже — ПО ДОКУМЕНТАЦИИ (uris:search), живьём не снят — ключа нет. Живые ответы
# (тестовая malware-страница Google и example.com) снимаются в Задаче 17 (находка R2-3).
def test_webrisk_key_in_header_not_in_url(monkeypatch):
    c, seen = WebRiskClient(api_key="SECRET"), {}

    def request(method, url, **kw):
        seen.update(kw, url=url)
        return httpx.Response(200, json={}, request=httpx.Request(method, url))
    monkeypatch.setattr(c, "request", request)
    assert c.threats("example.com") == []
    assert seen["headers"]["X-Goog-Api-Key"] == "SECRET"
    assert "SECRET" not in seen["url"] and all("SECRET" not in str(v) for _, v in seen["params"])
    assert ("uri", "http://example.com/") in seen["params"]


def test_webrisk_threat_and_configured(monkeypatch):
    c = WebRiskClient(api_key="k")
    monkeypatch.setattr(c, "request", lambda m, u, **kw: httpx.Response(
        200, json={"threat": {"threatTypes": ["MALWARE"], "expireTime": "2026-10-02T00:00:00Z"}},
        request=httpx.Request(m, u)))
    assert c.threats("bad.com") == ["MALWARE"]
    assert WebRiskClient(api_key="").configured is False


def test_webrisk_unknown_shape_raises_not_clean(monkeypatch):
    # находка R2-3: формат не снят живьём. Незнакомый ответ -> исключение (W3 пишет `webrisk:`,
    # домен «вслепую», вне пакета), а не [] — тихое «чисто» пустило бы угрозу в пакет
    c = WebRiskClient(api_key="k")
    for payload in (["MALWARE"], "ok", {"threat": ["MALWARE"]}, {"threat": "MALWARE"},
                    {"threat": {"types": ["MALWARE"]}}):
        monkeypatch.setattr(c, "request", lambda m, u, _p=payload, **kw: httpx.Response(
            200, json=_p, request=httpx.Request(m, u)))
        with pytest.raises(ValueError):
            c.threats("x.com")


def test_webrisk_key_is_scrubbed_on_diag(monkeypatch):
    from app.config import settings
    from app.services import diagnostics
    monkeypatch.setattr(settings, "WEBRISK_API_KEY", "WR-SECRET-123")
    assert diagnostics._scrub("403 for uris:search?key=WR-SECRET-123") == "403 for uris:search?key=***"
```

- [ ] **Шаг 3: Запустить — убедиться, что падает**

Запуск: `cd backend && ../.venv/bin/python -m pytest tests/test_rdap_webrisk.py -v`
Ожидание: FAIL — `ModuleNotFoundError: No module named 'app.integrations.rdap'`

- [ ] **Шаг 4: Реализовать**

`backend/app/integrations/rdap.py`:

```python
"""RDAP — транспорт. Сервер зоны берётся из бутстрапа IANA (data.iana.org/rdap/dns.json).

Живой факт 2026-10-01: у домена в pending delete RDAP отдаёт статус 'pending delete' и
`registration` = дата ПЕРВОЙ регистрации (pharmaindustrie.com -> 1998) — возраст до дропа виден.
404 = домена нет (свободен). В бутстрапе нет .mx/.co/.cl/.nz/.de… — для них NoRdap, вызывающий
идёт whois:43 через A-Parser (services/whois.py).

Сбой бутстрапа запоминается на жизнь клиента (клиент живёт один прогон): один warning и статичная
карта `_FALLBACK`. Без этого каждый has_rdap/lookup снова шёл бы в IANA (3 попытки × 20 с) под
общим локом — 12 потоков волны в очереди, и вставали бы даже .mx, которым IANA не нужна.
"""
import logging
import re
import threading
from datetime import datetime, timezone

import httpx

from app.integrations.base import BaseClient

logger = logging.getLogger(__name__)

BOOTSTRAP_URL = "https://data.iana.org/rdap/dns.json"

# Серверы зон белого списка, сняты живьём 2026-10-01. Только запасной путь, когда IANA не ответила:
# карта может устареть, основной источник — бутстрап.
_FALLBACK = {
    "com": "https://rdap.verisign.com/com/v1/",
    "net": "https://rdap.verisign.com/net/v1/",
    "org": "https://rdap.publicinterestregistry.org/rdap/",
    "uk": "https://rdap.nominet.uk/uk/",
    "online": "https://rdap.radix.host/rdap/",
    "site": "https://rdap.radix.host/rdap/",
    "xyz": "https://rdap.centralnic.com/xyz/",
    "si": "https://rdap.register.si/",
    "nl": "https://rdap.sidn.nl/",
    "in": "https://rdap.nixiregistry.in/rdap/",
}


class NoRdap(Exception):
    """У зоны нет RDAP-сервера в бутстрапе IANA."""


_FRACTION = re.compile(r"\.(\d+)")


def _iso(s) -> datetime | None:
    """RDAP-дата -> aware datetime (UTC, если смещения нет) или None.

    Python 3.10 `fromisoformat` не понимает `Z` и берёт дробь секунд только из 3 или 6 цифр, а живой
    CentralNic (.xyz) отдаёт `2014-03-20T12:59:17.0Z` — дробь дополняется (или режется) до 6 цифр.
    Дата без смещения считается UTC: наивная дата дальше уронила бы `now - registered_at` TypeError'ом.
    """
    if not s:
        return None
    txt = str(s).strip().replace("Z", "+00:00")
    txt = _FRACTION.sub(lambda m: "." + (m.group(1) + "000000")[:6], txt, count=1)
    try:
        dt = datetime.fromisoformat(txt)
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


class RdapClient(BaseClient):
    def __init__(self):
        super().__init__("", timeout=20.0)
        self._servers: dict | None = None
        self._lock = threading.Lock()       # волна avail зовёт клиент из 12 потоков

    def _bootstrap(self) -> dict:
        """{tld: base_url}. В IANA — не больше одного запроса на жизнь клиента: запоминается и
        удачный ответ, и сбой (тогда статичная карта `_FALLBACK`)."""
        with self._lock:
            if self._servers is None:
                try:
                    data = self.request("GET", BOOTSTRAP_URL).json()
                    self._servers = {t.lower(): urls[0] for tlds, urls in data.get("services") or []
                                     for t in tlds if urls}
                except Exception as e:  # noqa: BLE001 — любой сбой IANA: карта, а не шторм повторов
                    logger.warning("RDAP: бутстрап IANA недоступен (%s) — до конца прогона "
                                   "статичная карта зон белого списка", type(e).__name__)
                    self._servers = dict(_FALLBACK)
            return self._servers

    def has_rdap(self, domain: str) -> bool:
        return domain.rsplit(".", 1)[-1].lower() in self._bootstrap()

    def lookup(self, domain: str) -> dict:
        base = self._bootstrap().get(domain.rsplit(".", 1)[-1].lower())
        if base is None:
            raise NoRdap(domain)
        try:
            r = self.request("GET", base.rstrip("/") + "/domain/" + domain,
                             headers={"Accept": "application/rdap+json"})
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return {"exists": False, "status": [], "registered_at": None}
            raise
        d = r.json()
        reg = next((e.get("eventDate") for e in d.get("events") or []
                    if e.get("eventAction") == "registration"), None)
        return {"exists": True, "status": [str(s).lower() for s in d.get("status") or []],
                "registered_at": _iso(reg)}

    def ping(self) -> bool:
        """Живой пинг — напрямую в IANA, мимо запомненной карты: иначе /diag зеленел бы на
        `_FALLBACK` при лежащем бутстрапе."""
        return bool(self.request("GET", BOOTSTRAP_URL).json().get("services"))
```

`backend/app/integrations/webrisk.py`:

```python
"""Google Web Risk Lookup API — транспорт. Замена Safe Browsing: тот «for non-commercial use
only», наш affiliate-бизнес коммерческий (docs/v2/research/metrics-history.md).

Один URL на запрос. Чистый -> {}, угроза -> {"threat": {"threatTypes": [...]}}. Бесплатно до
100 тыс. вызовов в месяц. КЛЮЧ — В ЗАГОЛОВКЕ X-Goog-Api-Key, не в query: ключ в URL утекал бы в
текст HTTPStatusError, в логи и в /diag.

Формат ответа — ПО ДОКУМЕНТАЦИИ, живьём не снят (ключа нет; живые образцы — Задача 17, инвариант 7).
Поэтому разбор строгий: незнакомая форма — ValueError (W3 пишет `webrisk:ValueError`, домен
«вслепую»), а не [] — тихое «чисто» отправило бы непроверенный домен в пакет.
"""
from app.config import settings
from app.integrations.base import BaseClient

URL = "https://webrisk.googleapis.com/v1/uris:search"
THREATS = ("MALWARE", "SOCIAL_ENGINEERING", "UNWANTED_SOFTWARE")


class WebRiskClient(BaseClient):
    def __init__(self, api_key: str | None = None):
        super().__init__("", timeout=20.0)
        self.api_key = settings.WEBRISK_API_KEY if api_key is None else api_key

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def threats(self, domain: str) -> list[str]:
        params = [("threatTypes", t) for t in THREATS] + [("uri", f"http://{domain}/")]
        r = self.request("GET", URL, params=params, headers={"X-Goog-Api-Key": self.api_key})
        data = r.json()
        if not isinstance(data, dict):
            raise ValueError("webrisk: ответ не объект")
        threat = data.get("threat")
        if threat is None:
            return []                                   # {} — чистый URL
        if not isinstance(threat, dict) or not isinstance(threat.get("threatTypes"), list):
            raise ValueError("webrisk: незнакомая форма 'threat'")
        return [str(t) for t in threat["threatTypes"]]

    def ping(self) -> bool:
        return self.configured and self.threats("example.com") == []
```

В `backend/app/config.py` сразу после строки `SPAMHAUS_DQS_KEY: str = ""` добавить:

```python
    WEBRISK_API_KEY: str = ""          # Google Web Risk (замена Safe Browsing); пусто -> W3 «не настроено»
```

В `.env.example` сразу после строки `SPAMHAUS_DQS_KEY=…` добавить строку `WEBRISK_API_KEY=`.

В `backend/app/services/diagnostics.py` кортеж `_SECRET_FIELDS` заменить целиком (добавлен
`WEBRISK_API_KEY` — ключ Web Risk не должен всплыть в тексте ошибки на `/diag`):

```python
_SECRET_FIELDS = (
    "AHREFS_API_KEY", "DATAFORSEO_LOGIN", "DATAFORSEO_PASSWORD",
    "SERPAPI_KEY", "YANDEX_WORDSTAT_TOKEN", "BACKORDER_LOGIN", "BACKORDER_PASSWORD",
    "OPTIMIZATOR_API_KEY", "REGRU_PASSWORD", "CLOUDFLARE_API_TOKEN", "AAPANEL_API_KEY",
    "LLM_API_KEY", "APARSER_API_KEY", "GITHUB_TOKEN", "PANEL_PASS", "SPAMHAUS_DQS_KEY",
    "WEBRISK_API_KEY",
)
```

В `backend/tests/conftest.py`:
1. после блока импортов моделей и кортежа `_REGISTER_TABLES` — сразу после его последней строки
   `                    app.models.domain_score_log)` и ПЕРЕД пустыми строками над
   `@compiles(JSONB, "sqlite")` — добавить (с пустой строкой перед блоком):

```python
from app.integrations.rdap import RdapClient

# настоящий бутстрап — для фикстуры real_rdap_bootstrap (autouse _no_paid_keys его подменяет)
_REAL_RDAP_BOOTSTRAP = RdapClient._bootstrap
```

2. сразу после фикстуры `_no_panel_auth` добавить:

```python
@pytest.fixture(autouse=True)
def _no_paid_keys(monkeypatch):
    """Тесты герметичны к .env оператора и к сети реестров.

    Ключи: на боксе тесты гоняются в контейнере, где заданы БОЕВЫЕ AHREFS_API_KEY/WEBRISK_API_KEY/
    SPAMHAUS_DQS_KEY, а config.py читает .env относительно cwd — из корня репо ключ виден, из
    backend/ нет, и тест зеленел бы или краснел в зависимости от каталога. Клиент с ключом сам
    идёт в сеть, а рубильник _no_live_network роняет такой тест BaseException'ом. Ключи пусты на
    время теста; тест, которому ключ нужен, ставит его сам через monkeypatch.setattr(settings, …).

    RDAP: по умолчанию НИ ОДНА зона не имеет RDAP (`_bootstrap` -> {}), в IANA никто не ходит —
    иначе настоящий RdapClient из _make_clients()/recheck_acquirability() полез бы в IANA из
    фонового потока. Тест, которому нужен RDAP в воронке, передаёт фейк через clients["rdap"];
    юнит-тесты самого клиента берут фикстуру real_rdap_bootstrap."""
    from app.config import settings
    for key in ("AHREFS_API_KEY", "WEBRISK_API_KEY", "SPAMHAUS_DQS_KEY"):
        monkeypatch.setattr(settings, key, "")
    monkeypatch.setattr(RdapClient, "_bootstrap", lambda self: {})
    yield


@pytest.fixture
def real_rdap_bootstrap(_no_paid_keys, monkeypatch):
    """Настоящий RdapClient._bootstrap — для юнит-тестов клиента (HTTP они подменяют на инстансе:
    monkeypatch.setattr(c, "request", …)). Зависит от _no_paid_keys, чтобы встать ПОСЛЕ его подмены."""
    monkeypatch.setattr(RdapClient, "_bootstrap", _REAL_RDAP_BOOTSTRAP)
```

Ни один существующий тест фикстура не ломает: `RdapClient` до Задачи 9 никто не создаёт, а
единственные тесты с `SPAMHAUS_DQS_KEY` (`test_m1_fixes.py`) ставят его сами через `monkeypatch`.

- [ ] **Шаг 5: Тесты, сьют, линт**

Запуск: `cd backend && ../.venv/bin/python -m pytest tests/test_rdap_webrisk.py -v && ../.venv/bin/python -m pytest tests -q && cd .. && .venv/bin/python -m pyflakes backend/app backend/tests`
Ожидание: всё зелёное — 804 passed (793 + 11 новых), pyflakes пуст.

- [ ] **Шаг 6: Коммит**

```bash
git add backend/app/integrations/rdap.py backend/app/integrations/webrisk.py backend/app/config.py backend/app/services/diagnostics.py .env.example backend/tests/conftest.py backend/tests/test_rdap_webrisk.py backend/tests/fixtures/v2
git commit -m "feat(v2): клиенты RDAP (бутстрап IANA + запасная карта) и Google Web Risk; харнесс без платных ключей и RDAP"
```

---

### Задача 4: Источники дропов — DropCatch, Nominet, registry.mx

**Files:**
- Create: `backend/app/integrations/dropcatch.py`, `nominet.py`, `registry_mx.py`
- Test: `backend/tests/test_drop_sources.py`, фикстуры `backend/tests/fixtures/v2/{dropcatch_head.csv,nominet_head.csv,registry_mx_head.csv}`

**Interfaces:**
- Produces: каждый клиент — `.list_dropping() -> list[dict]` (строки `{"domain", "source", "lane",
  "acquire_deadline"}`) и `.ping() -> bool`. Чистые парсеры для тестов:
  - `dropcatch.parse_dropping_zip(raw: bytes) -> list[dict]`;
  - `nominet.parse_droplist(text: str, now: datetime, lookahead_days: int) -> list[dict]`;
  - `registry_mx.parse_deleted(text: str) -> list[dict]`.

- [ ] **Шаг 1: Фикстуры** — живые первые строки 2026-10-01; строки, помеченные «сконструировано»,
  добавлены для граничных случаев

`backend/tests/fixtures/v2/dropcatch_head.csv`:

```
Domain,TLD,Type,Drop Date
WerKleittechnik.com,com,PendingDelete,2026-10-03
PharmAinDustRie.com,com,PendingDelete,2026-10-03
HuntingThugs.com,com,PendingDelete,2026-10-03
goodname.cc,cc,PendingDelete,2026-10-03
broken-date.com,com,PendingDelete,not-a-date
```

(последние две строки сконструированы: чужая зона и битая дата)

`backend/tests/fixtures/v2/nominet_head.csv`:

```
roid,domain,drop_time
D_87724817-UK,extrusionuk.co.uk,2026-06-13T11:31:21Z
D_86130058-UK,5thjuly.uk,2026-10-01T03:01:55Z
D_86130061-UK,mandemgpt.co.uk,2026-10-01T03:03:29Z
D_90000001-UK,laterdrop.co.uk,2026-10-03T23:00:00Z
D_90000002-UK,toofar.co.uk,2026-12-05T10:00:00Z
D_90000003-UK,badtime.co.uk,yesterday
```

(первые три строки живые, остальные сконструированы под границы окна)

`backend/tests/fixtures/v2/registry_mx_head.csv`:

```
01/10/26 03:00:16 GMT-6
Dominio,Disponible
dibanhi.com.mx,true
ric77.com.mx,true
Lennoxind.MX,true
taken-again.mx,false
```

(первые четыре строки живые, последние две сконструированы: регистр и `false`)

- [ ] **Шаг 2: Написать падающий тест** — `backend/tests/test_drop_sources.py`

```python
"""Списки дропов: живые форматы 2026-10-01 (фикстуры), без сети."""
import io
import pathlib
import zipfile
from datetime import datetime, timezone

import httpx

from app.integrations import dropcatch, nominet, registry_mx

FX = pathlib.Path(__file__).parent / "fixtures" / "v2"


def _zip(csv_text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Dropping_Domains_2026-10-03.csv", csv_text)
    return buf.getvalue()


def test_dropcatch_parse_lowercases_and_skips_bad_dates():
    rows = dropcatch.parse_dropping_zip(_zip((FX / "dropcatch_head.csv").read_text()))
    names = [r["domain"] for r in rows]
    assert names == ["werkleittechnik.com", "pharmaindustrie.com", "huntingthugs.com", "goodname.cc"]
    assert rows[0] == {"domain": "werkleittechnik.com", "source": "dropcatch", "lane": "bid",
                       "acquire_deadline": datetime(2026, 10, 3, tzinfo=timezone.utc)}


def test_dropcatch_list_dropping_asks_file_url_then_downloads(monkeypatch):
    c, calls = dropcatch.DropCatchClient(), []
    zipped = _zip((FX / "dropcatch_head.csv").read_text())

    def request(method, url, **kw):
        calls.append((url, kw.get("params")))
        req = httpx.Request(method, url)
        if "GetFileUrl" in url:
            return httpx.Response(200, request=req, json={
                "result": {"fileUrl": "https://dropcatch-downloads.s3.amazonaws.com/production/x.csv.zip?sig=1",
                           "fileName": "x.csv.zip"}, "statusCode": "OK", "success": True})
        return httpx.Response(200, request=req, content=zipped)
    monkeypatch.setattr(c, "request", request)
    assert len(c.list_dropping()) == 4
    assert calls[0][1] == {"FileType": "csv", "RequestType": "Dropping", "BackorderDay": "DaysOut2"}
    assert calls[1][0].startswith("https://dropcatch-downloads.s3.amazonaws.com/")


def test_dropcatch_no_file_url_is_an_error(monkeypatch):
    c = dropcatch.DropCatchClient()
    monkeypatch.setattr(c, "request", lambda m, u, **kw: httpx.Response(
        200, request=httpx.Request(m, u), json={"result": None, "success": False, "statusCode": "Error"}))
    try:
        c.list_dropping()
        raise AssertionError("ожидали RuntimeError")
    except RuntimeError as e:
        assert "DropCatch" in str(e)


def test_nominet_window_only_future_three_days():
    now = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)
    rows = nominet.parse_droplist((FX / "nominet_head.csv").read_text(), now, lookahead_days=3)
    assert [r["domain"] for r in rows] == ["5thjuly.uk", "mandemgpt.co.uk", "laterdrop.co.uk"]
    assert rows[0]["lane"] == "bid" and rows[0]["source"] == "nominet"
    assert rows[0]["acquire_deadline"] == datetime(2026, 10, 1, 3, 1, 55, tzinfo=timezone.utc)


def test_registry_mx_skips_preamble_and_unavailable():
    rows = registry_mx.parse_deleted((FX / "registry_mx_head.csv").read_text())
    assert [r["domain"] for r in rows] == ["dibanhi.com.mx", "ric77.com.mx", "lennoxind.mx"]
    assert rows[0] == {"domain": "dibanhi.com.mx", "source": "mx", "lane": "free", "acquire_deadline": None}
```

- [ ] **Шаг 3: Запустить — убедиться, что падает**

Запуск: `cd backend && ../.venv/bin/python -m pytest tests/test_drop_sources.py -v`
Ожидание: FAIL — `ImportError: cannot import name 'dropcatch'`

- [ ] **Шаг 4: Реализовать**

`backend/app/integrations/dropcatch.py`:

```python
"""DropCatch — список pending delete (.com/.net/.org/.cc/…), транспорт.

Снято 2026-10-01 (браузер + curl, без авторизации и капчи): страница загрузок — SPA, файл отдаётся
так: GET client.dropcatch.com/GetFileUrl?FileType=csv&RequestType=Dropping&BackorderDay=DaysOutN
-> {"result": {"fileUrl": <подписанная ссылка S3>}, "success": true} -> ZIP с одним CSV
`Domain,TLD,Type,Drop Date` (~134 тыс. строк/день, имена в смешанном регистре).
Берём DaysOut2: каждый домен виден ОДИН раз, за 2 дня до дропа (время на скоринг и решение).
ponytail: пропущенный день — потерянный день; понадобится — добавить DaysOut3.
ToS на автоматическое скачивание НЕ прочитан (страница — JS) -> источник выключен по умолчанию.
"""
import csv
import io
import zipfile
from datetime import datetime, timezone

from app.integrations.base import BaseClient

API = "https://client.dropcatch.com/GetFileUrl"


def parse_dropping_zip(raw: bytes) -> list[dict]:
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        name = next(n for n in z.namelist() if n.lower().endswith(".csv"))
        text = z.read(name).decode("utf-8-sig", errors="replace")
    out = []
    for row in csv.DictReader(io.StringIO(text)):
        d = (row.get("Domain") or "").strip().lower()
        try:
            dl = datetime.strptime((row.get("Drop Date") or "").strip(), "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if d:
            out.append({"domain": d, "source": "dropcatch", "lane": "bid", "acquire_deadline": dl})
    return out


class DropCatchClient(BaseClient):
    def __init__(self, days_out: int = 2):
        super().__init__("", timeout=120.0)
        self.days_out = days_out

    def file_url(self) -> str:
        j = self.request("GET", API, params={"FileType": "csv", "RequestType": "Dropping",
                                             "BackorderDay": f"DaysOut{self.days_out}"}).json()
        url = (j.get("result") or {}).get("fileUrl")
        if not j.get("success") or not url:
            raise RuntimeError(f"DropCatch не отдал ссылку на файл: {j.get('statusCode')}")
        return url

    def list_dropping(self) -> list[dict]:
        return parse_dropping_zip(self.request("GET", self.file_url()).content)

    def ping(self) -> bool:
        return bool(self.file_url())
```

`backend/app/integrations/nominet.py`:

```python
"""Nominet — официальный список дропов .uk/.co.uk, транспорт.

Живой факт 2026-10-01: это ВСЁ расписание (~266 тыс. строк, drop_time на 65 дней вперёд,
~4 тыс. в день), колонки `roid,domain,drop_time` (ISO с Z). Берём окно [сейчас, сейчас+N дней]:
дальние дропы ещё не время скорить, прошедшие — уже не поймать.
"""
import csv
import gzip
import io
from datetime import datetime, timedelta, timezone

from app.integrations.base import BaseClient

URL = "https://droplists.nominet.uk/current/uk.csv.gz"


def parse_droplist(text: str, now: datetime, lookahead_days: int) -> list[dict]:
    hi = now + timedelta(days=lookahead_days)
    out = []
    for row in csv.DictReader(io.StringIO(text)):
        d = (row.get("domain") or "").strip().lower()
        try:
            dt = datetime.fromisoformat((row.get("drop_time") or "").strip().replace("Z", "+00:00"))
        except ValueError:
            continue
        if d and now <= dt <= hi:
            out.append({"domain": d, "source": "nominet", "lane": "bid", "acquire_deadline": dt})
    return out


class NominetClient(BaseClient):
    def __init__(self, lookahead_days: int = 3):
        super().__init__("", timeout=120.0)
        self.lookahead_days = lookahead_days

    def list_dropping(self) -> list[dict]:
        raw = self.request("GET", URL).content
        return parse_droplist(gzip.decompress(raw).decode("utf-8", errors="replace"),
                              datetime.now(timezone.utc), self.lookahead_days)

    def ping(self) -> bool:
        return self.request("HEAD", URL).status_code == 200
```

`backend/app/integrations/registry_mx.py`:

```python
"""registry.mx — ежедневный список УЖЕ удалённых .mx/.com.mx/.org.mx, транспорт.

Живой формат 2026-10-01: первая строка — штамп времени ("01/10/26 03:00:16 GMT-6"), затем
заголовок `Dominio,Disponible`, ~620 строк. Удалённый = свободен к регистрации -> лейн free.
"""
import csv

from app.integrations.base import BaseClient

URL = "https://www.registry.mx/report/domain_deleted_list.csv"


def parse_deleted(text: str) -> list[dict]:
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.lower().startswith("dominio,")), None)
    if start is None:
        return []
    out = []
    for row in csv.DictReader(lines[start:]):
        d = (row.get("Dominio") or "").strip().lower()
        if d and (row.get("Disponible") or "").strip().lower() == "true":
            out.append({"domain": d, "source": "mx", "lane": "free", "acquire_deadline": None})
    return out


class RegistryMxClient(BaseClient):
    def __init__(self):
        super().__init__("", timeout=60.0)

    def list_dropping(self) -> list[dict]:
        return parse_deleted(self.request("GET", URL).text)

    def ping(self) -> bool:
        return self.request("GET", URL).status_code == 200
```

- [ ] **Шаг 5: Тесты, сьют, линт**

Запуск: `cd backend && ../.venv/bin/python -m pytest tests/test_drop_sources.py -v && ../.venv/bin/python -m pytest tests -q && cd .. && .venv/bin/python -m pyflakes backend/app backend/tests`
Ожидание: всё зелёное — 809 passed (804 + 5 новых), pyflakes пуст.

- [ ] **Шаг 6: Коммит**

```bash
git add backend/app/integrations/dropcatch.py backend/app/integrations/nominet.py backend/app/integrations/registry_mx.py backend/tests/test_drop_sources.py backend/tests/fixtures/v2
git commit -m "feat(v2): источники дропов DropCatch, Nominet, registry.mx (живые форматы)"
```

---

### Задача 5: Данные — миграция 0025, новые колонки и ключи настроек

Источники и веса здесь ещё не переключаются: это Задачи 6 и 8. Задача добавляет данные, настройки,
грязные причины `legacy_ru`/`tld_closed` и гард зоны на ручной перевод `→ approved` (находка R2-19);
поведение воронки не меняется.

**Files:**
- Create: `backend/alembic/versions/0025_v2_m1.py`
- Modify:
  - `backend/app/models/domain.py` — колонки `market_lang`, `topic`; модель `DrSeen` (таблица `dr_seen`);
  - `backend/app/models/settings.py` — 8 колонок;
  - `backend/app/services/scoring_config.py` — v2-дефолты;
  - `backend/app/services/settings.py` — новые ключи + валидация списков;
  - `backend/app/services/transitions.py` — `DIRTY_REASONS` += `legacy_ru`, `tld_closed`; гард зоны в
    `check` (находка R2-19) и самопроверка `__main__`.
- Test: `backend/tests/test_migration_0025.py`, `backend/tests/test_settings_v2.py`
- Старые тесты (шаг 4, только переписать): `test_transitions.py`, `test_pipeline.py`, `test_inbox.py`,
  `test_history_verdict.py`

**Interfaces:**
- Produces:
  - `get_settings()` дополнительно отдаёт `min_dr: float`, `tld_allowlist: list[str]`,
    `brand_tokens: list[str]`, `emd_sets: list[dict]`, `max_links_per_run: int`,
    `max_deep_per_run: int`, `spam_anchor_max: float`, `units_floor: int` (дефолт 300 000, кламп
    0…2 000 000; 0 = пола нет);
  - `update_settings(...)` принимает их же; невалидный JSON в `emd_sets` → `ValueError`, сохранённое
    не трогается; строка вместо списка в `keywords` набора EMD — один ключ;
  - `scoring_config`: `MIN_DR`, `TLD_ALLOWLIST`, `BRAND_TOKENS`, `MAX_LINKS_PER_RUN`, `MAX_DEEP_PER_RUN`,
    `SPAM_ANCHOR_MAX`, `UNITS_FLOOR`;
  - модель `app.models.domain.DrSeen` — таблица `dr_seen(domain String(253) PK, dr Numeric NULL,
    checked_at DateTime(timezone=True) NOT NULL)`: память бесплатного DR для discovery (Задача 6);
  - колонки `Domain.market_lang String(8)`, `Domain.topic String(120)`;
  - `transitions.DIRTY_REASONS` содержит `legacy_ru` и `tld_closed`: такой домен не вернуть в
    `approved` руками, только перескором;
  - `transitions.check(d, target, *, allowlist=None)`: перевод `→ approved` (любой ручной, и из
    `rejected`, и из `scored`, и пакетом) запрещён домену, чья зона не в белом списке —
    `domain_filters.tld_match(d.domain, allowlist)`, где `allowlist=None` → `get_settings()["tld_allowlist"]`
    (находка R2-19). Грязь проверяется раньше зоны. Отдельно — `transitions.refuse_closed_zone(d, allowlist=None)`.
    Вызывающие (`set_status` → панель: кнопка, пакет; `pipeline.mark_purchased`; `acquisition.create_order`)
    не меняются: `set_status` зовёт `check(d, target)`, настройки читаются своей сессией;
  - в миграции константы `ARCHIVE_SQL`, `UNARCHIVE_SQL`, `SOURCES_V2`.

- [ ] **Шаг 1: Написать падающие тесты**

`backend/tests/test_migration_0025.py`:

```python
"""Миграция 0025: цепочка ревизий, архив РФ-пула (SQL исполняется на SQLite), единый дефолт
источников, таблица dr_seen = модель DrSeen, грязные причины legacy_ru/tld_closed, зона вне белого
списка не возвращается в approved."""
import importlib.util
import pathlib
from datetime import datetime, timezone
from types import SimpleNamespace as NS

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

import app.db as db
from app.models.domain import Domain


def _mig():
    p = pathlib.Path(__file__).parents[1] / "alembic" / "versions" / "0025_v2_m1.py"
    spec = importlib.util.spec_from_file_location("m0025", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class _RecOp:
    """Подмена alembic.op: записывает вызовы, ничего не исполняет (миграция постгресовая —
    `::jsonb`, SQLite её не прогонит)."""
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        return lambda *a, **kw: self.calls.append((name, a, kw))


def test_revision_chain():
    m = _mig()
    assert m.revision == "0025_v2_m1" and m.down_revision == "0024_domain_score_log"


def test_archive_sql_hits_only_unbought_ru_candidates_and_is_reversible():
    rows = [("a.ru", "discovered"), ("b.xn--p1ai", "scored"), ("c.ru", "purchased"),
            ("d.com", "discovered"), ("e.su", "approved"), ("f.com.ru", "rejected")]
    with db.SessionLocal() as s:
        for name, st in rows:
            s.add(Domain(domain=name, status=st, reject_reason="rkn" if st == "rejected" else None))
        s.commit()
        s.execute(text(_mig().ARCHIVE_SQL))
        s.commit()
        s.expire_all()          # expire_on_commit=False: иначе читались бы объекты сессии, а не строки после UPDATE
        got = {d.domain: (d.status, d.reject_reason) for d in s.query(Domain)}
    for name in ("a.ru", "b.xn--p1ai", "e.su"):
        assert got[name] == ("rejected", "legacy_ru"), name
    assert got["c.ru"] == ("purchased", None)          # купленный — не трогаем
    assert got["d.com"] == ("discovered", None)        # международный — не трогаем
    assert got["f.com.ru"] == ("rejected", "rkn")      # уже отклонённый — причину не перетираем
    with db.SessionLocal() as s:
        s.execute(text(_mig().UNARCHIVE_SQL))
        s.commit()
        assert s.query(Domain).filter_by(domain="a.ru").one().status == "discovered"


def test_sources_default_single_source_of_truth():
    # миграция сеет то же, что код считает дефолтом (Задача 6 переключит cfg.SOURCES_ENABLED)
    assert set(_mig().SOURCES_V2) == {"dropcatch", "nominet", "mx", "emd"}


def test_dr_seen_table_in_migration_matches_model(monkeypatch):
    from app.models.domain import DrSeen
    m, rec = _mig(), _RecOp()
    monkeypatch.setattr(m, "op", rec)
    m.upgrade()
    made = [a for name, a, _ in rec.calls if name == "create_table" and a[0] == "dr_seen"]
    assert len(made) == 1
    cols = {c.name: c for c in made[0][1:]}
    model = DrSeen.__table__.c
    assert set(cols) == set(model.keys()) == {"domain", "dr", "checked_at"}
    for name, col in cols.items():                     # живой PG получит ровно то, что видят тесты
        assert (col.primary_key, col.nullable, type(col.type)) == (
            model[name].primary_key, model[name].nullable, type(model[name].type)), name
    assert cols["domain"].type.length == model["domain"].type.length == 253
    assert cols["checked_at"].type.timezone is True and model["checked_at"].type.timezone is True
    added = {(a[0], a[1].name) for name, a, _ in rec.calls if name == "add_column"}
    assert ("scoring_settings", "units_floor") in added
    rec.calls.clear()
    m.downgrade()
    assert ("drop_table", ("dr_seen",), {}) in rec.calls
    assert ("drop_column", ("scoring_settings", "units_floor"), {}) in rec.calls


def test_dr_seen_model_roundtrip_and_checked_at_required():
    from app.models.domain import DrSeen
    now = datetime.now(timezone.utc)
    with db.SessionLocal() as s:
        s.add_all([DrSeen(domain="low-dr.com", dr=0, checked_at=now),
                   DrSeen(domain="no-dr.com", dr=None, checked_at=now)])     # Ahrefs не вернул DR
        s.commit()
        assert float(s.get(DrSeen, "low-dr.com").dr) == 0.0
        assert s.get(DrSeen, "no-dr.com").dr is None
        s.add(DrSeen(domain="no-time.com", dr=3))
        with pytest.raises(IntegrityError):
            s.commit()


def test_legacy_ru_and_closed_zone_never_back_to_approved():
    # архив РФ-пула и чужая зона — факт о домене, не наш порог: к кассе руками не вернуть
    from app.services import transitions
    for reason in ("legacy_ru", "tld_closed"):
        d = NS(domain="old.ru", status="rejected", reject_reason=reason, rkn_listed=None,
               blacklisted=None, prior_flags={}, wayback_checked=True, score_breakdown=None)
        assert transitions.dirty_reason(d) == reason
        with pytest.raises(transitions.TransitionDenied):
            transitions.check(d, "approved")


def test_threshold_reject_outside_allowlist_cannot_return_to_approved():
    # находка R2-19: v1-домен .ru, отклонённый ПОРОГОМ, не грязный — но его зоны нет в белом
    # списке: «↩ вернуть в approved» повела бы его в очередь backorder, который .ru покупает
    from app.services import transitions
    from app.services.settings import update_settings

    def d(name):
        return NS(domain=name, status="rejected", reject_reason="low_score", rkn_listed=None,
                  blacklisted=None, prior_flags={}, wayback_checked=True, score_breakdown=None)
    with pytest.raises(transitions.TransitionDenied, match="белом списке"):
        transitions.check(d("weak.ru"), "approved")
    transitions.check(d("weak.com"), "approved")      # зона в списке — порог возвращается руками
    update_settings(tld_allowlist="com ru")
    transitions.check(d("weak.ru"), "approved")       # список — из /settings, не из кода
```

`backend/tests/test_settings_v2.py`:

```python
"""Настройки v2: дефолты, нормализация списков, валидация наборов EMD, пол остатка units."""
import pytest

from app.services import scoring_config as cfg
from app.services.settings import get_settings, update_settings


def test_v2_defaults():
    s = get_settings()
    assert s["min_dr"] == 5.0 and s["max_links_per_run"] == 500 and s["max_deep_per_run"] == 20
    assert s["spam_anchor_max"] == 0.2 and s["emd_sets"] == []
    assert s["tld_allowlist"] == cfg.TLD_ALLOWLIST and "nordvpn" in s["brand_tokens"]
    assert s["units_floor"] == cfg.UNITS_FLOOR == 300000


def test_lists_from_textarea_are_normalized():
    s = update_settings(tld_allowlist=" .CO.UK, com\nmx com ;", brand_tokens="NordVPN\n\nsurfshark")
    assert s["tld_allowlist"] == ["co.uk", "com", "mx"]
    assert s["brand_tokens"] == ["nordvpn", "surfshark"]


def test_emd_sets_valid_json_saved_invalid_rejected_without_losing_old():
    ok = '[{"market":"es-MX","lang":"ES","keywords":["mejor vpn"," "],"tlds":["com",".MX"]},{"keywords":[]}]'
    s = update_settings(emd_sets=ok)
    assert s["emd_sets"] == [{"market": "es-MX", "lang": "es", "keywords": ["mejor vpn"], "tlds": ["com", "mx"]}]
    with pytest.raises(ValueError):
        update_settings(emd_sets="{not json")
    assert get_settings()["emd_sets"] == s["emd_sets"]


def test_emd_sets_string_instead_of_list_is_one_keyword():
    # строка вместо списка: ключ — ОДИН (не m, e, j…), зоны строкой разбираются как textarea
    s = update_settings(emd_sets='[{"market":"es-MX","lang":"es","keywords":"mejor vpn","tlds":"com .MX"}]')
    assert s["emd_sets"] == [{"market": "es-MX", "lang": "es", "keywords": ["mejor vpn"], "tlds": ["com", "mx"]}]


def test_numeric_bounds():
    s = update_settings(min_dr=500, max_links_per_run=0, max_deep_per_run=9999, spam_anchor_max=3)
    assert s["min_dr"] == 100.0 and s["max_links_per_run"] == 1
    assert s["max_deep_per_run"] == 500 and s["spam_anchor_max"] == 1.0


def test_units_floor_bounds():
    assert update_settings(units_floor=5_000_000)["units_floor"] == 2_000_000
    assert update_settings(units_floor=-1)["units_floor"] == 0               # 0 = пола нет
    assert update_settings(units_floor=150000)["units_floor"] == 150000
```

- [ ] **Шаг 2: Запустить — убедиться, что падает**

Запуск: `cd backend && ../.venv/bin/python -m pytest tests/test_migration_0025.py tests/test_settings_v2.py -v`
Ожидание: FAIL — 13 failed: тесты миграции — `FileNotFoundError` (нет `0025_v2_m1.py`), `DrSeen` —
`ImportError`, `test_legacy_ru_and_closed_zone_never_back_to_approved` — `AssertionError`
(`dirty_reason` вернул `None`), `test_threshold_reject_outside_allowlist_cannot_return_to_approved` —
`Failed: DID NOT RAISE`; тесты настроек — `KeyError` на новых ключах (`min_dr`,
`tld_allowlist`, `emd_sets`, `units_floor`).

- [ ] **Шаг 3: Реализовать**

`backend/app/services/scoring_config.py` — добавить в конец файла:

```python

# ---- v2: международные домены (docs/v2/02-m1-discovery-scoring-spec.md) ----
MIN_DR = 5.0                 # фильтр по DR на входе discovery: основная масса дропов — DR 0–4 со спамом
TLD_ALLOWLIST = ["com", "net", "org", "online", "xyz", "site", "co.uk", "mx", "co", "si", "nl", "in"]
BRAND_TOKENS = ["nordvpn", "expressvpn", "surfshark", "protonvpn", "cyberghost", "ipvanish",
                "privateinternetaccess", "mullvad", "windscribe", "hotspotshield", "tunnelbear",
                "purevpn", "vyprvpn", "hidemyass", "atlasvpn", "privadovpn", "hideme", "strongvpn",
                "zenmate", "avast", "kaspersky", "norton"]
MAX_LINKS_PER_RUN = 500      # W4 Ahrefs batch-analysis: 25 units/домен
MAX_DEEP_PER_RUN = 20        # W6 анкоры + история трафика: ~1,1 тыс. units/домен; 0 = выключить
SPAM_ANCHOR_MAX = 0.2        # доля спам-анкоров (по refdomains), выше — отказ spam_anchors
UNITS_FLOOR = 300_000        # пол остатка units Ahrefs в месяце: ниже — W4/W6 не тратят (автопилот — раз в час)
```

`backend/app/models/domain.py`:
1. в блок history сразу после строки `indexed_echo: Mapped[bool | None] = mapped_column(Boolean)       # old content still indexed` добавить:

```python
    # v2: язык и тема прошлого сайта — W5 (LLM по видимому тексту снимков Wayback); у EMD язык
    # берётся из набора ключей. Мягкий сигнал для оператора и рынка, не гейт.
    market_lang: Mapped[str | None] = mapped_column(String(8))
    topic: Mapped[str | None] = mapped_column(String(120))
```

2. в конец файла добавить модель (импорты `String`, `Numeric`, `DateTime`, `datetime` в модуле уже есть;
   conftest импортирует `app.models.domain`, поэтому таблица в тестовой БД появится сама):

```python


class DrSeen(Base):
    """Память бесплатного DR Ahrefs (решение оператора Р4, миграция 0025).

    Без неё DR одних и тех же не вставленных доменов (ниже порога) спрашивался бы заново на каждом
    прогоне discovery — живьём 123 запроса на 122 тыс. доменов при повторе в тот же день, а лицензия
    Ahrefs запрещает систематический сбор: DR один раз на домен. discovery (Задача 6) перед запросом
    отсекает домены со свежей записью (4 суток), после успешного ответа пишет ВСЕ спрошенные домены
    (`dr` None — Ahrefs DR не вернул), в начале прогона удаляет записи старше 4 суток."""
    __tablename__ = "dr_seen"

    domain: Mapped[str] = mapped_column(String(253), primary_key=True)
    dr: Mapped[float | None] = mapped_column(Numeric)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
```

`backend/app/models/settings.py` — заменить содержимое файла целиком:

```python
"""Рантайм-настройки скоринга (single-row, id=1). Дефолты — в scoring_config.py.

Пороги воронки редактируются на /settings; сервис settings.py читает/пишет эту строку.
"""
from datetime import datetime
from sqlalchemy import Integer, Numeric, DateTime, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.db import Base


class ScoringSettings(Base):
    __tablename__ = "scoring_settings"

    id: Mapped[int] = mapped_column(primary_key=True)                # всегда 1
    min_referring_domains: Mapped[int] = mapped_column(Integer, default=1)
    min_age_years: Mapped[float] = mapped_column(Numeric, default=3.0)
    approve_at: Mapped[float] = mapped_column(Numeric, default=0.70)
    manual_review_at: Mapped[float] = mapped_column(Numeric, default=0.40)
    max_whois_per_run: Mapped[int] = mapped_column(Integer, default=200)  # кап whois-вызовов за прогон
    # ЛЕГАСИ v1 (капча A-Parser Rank::Ahrefs): в v2 W4 — Ahrefs API с капом max_links_per_run, см. 0025
    max_ahrefs_per_run: Mapped[int] = mapped_column(Integer, default=50)
    sources_enabled: Mapped[dict] = mapped_column(JSONB, default=dict)
    # веса критериев оценки донора (history_cleanliness/age/rd_proxy/indexed_echo/authority).
    # Были зашиты в scoring_config.WEIGHTS — оператор видел, ПО ЧЕМУ его судят, но не мог
    # изменить НИ ОДИН вес (жалоба 2026-07-13). Сумма не обязана быть 1.0: compute_score
    # нормирует её сам, иначе один сдвинутый ползунок ломал бы шкалу 0..1.
    weights: Mapped[dict] = mapped_column(JSONB, default=dict)
    # v2 (миграция 0025)
    min_dr: Mapped[float] = mapped_column(Numeric, default=5)
    tld_allowlist: Mapped[list] = mapped_column(JSONB, default=list)
    brand_tokens: Mapped[list] = mapped_column(JSONB, default=list)
    emd_sets: Mapped[list] = mapped_column(JSONB, default=list)
    max_links_per_run: Mapped[int] = mapped_column(Integer, default=500)
    max_deep_per_run: Mapped[int] = mapped_column(Integer, default=20)
    spam_anchor_max: Mapped[float] = mapped_column(Numeric, default=0.2)
    # пол остатка units Ahrefs (решение оператора Р3): ниже — W4/W6 не тратят units
    units_floor: Mapped[int] = mapped_column(Integer, default=300000)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),
                                                        server_default=func.now(), onupdate=func.now())
```

`backend/app/services/settings.py` — заменить содержимое файла целиком:

```python
"""Рантайм-настройки воронки: читать/писать single-row scoring_settings.

get_settings() возвращает effective-словарь (сидит дефолтами из scoring_config при
отсутствии строки). Пороги валидируются по диапазонам, чтобы UI не записал мусор.
"""
import json
import re

from app.services import scoring_config as cfg

_KEYS_NUM = ("min_referring_domains", "min_age_years", "approve_at", "manual_review_at",
             "max_whois_per_run", "max_ahrefs_per_run",
             "min_dr", "max_links_per_run", "max_deep_per_run", "spam_anchor_max", "units_floor")
_BOUNDS = {                       # (min, max) для валидации ползунков
    "min_referring_domains": (0, 100000),
    "min_age_years": (0.0, 30.0),
    "approve_at": (0.0, 1.0),
    "manual_review_at": (0.0, 1.0),
    "max_whois_per_run": (1, 5000),
    "max_ahrefs_per_run": (0, 1000),
    "min_dr": (0.0, 100.0),
    "max_links_per_run": (1, 5000),
    "max_deep_per_run": (0, 500),        # 0 = W6 выключен: анкоры не проверены, в пакет домен не попадёт
    "spam_anchor_max": (0.0, 1.0),
    "units_floor": (0, 2_000_000),       # 0 = пола нет; 2 млн — месячный лимит units
}
_LIST_MAX = 200


def _defaults() -> dict:
    return {
        "min_referring_domains": cfg.PREFILTER["min_referring_domains"],
        "min_age_years": cfg.MIN_AGE_YEARS,
        "approve_at": cfg.DECISION["approve_at"],
        "manual_review_at": cfg.DECISION["manual_review_at"],
        "max_whois_per_run": cfg.MAX_WHOIS_PER_RUN,
        "max_ahrefs_per_run": cfg.MAX_AHREFS_PER_RUN,
        "sources_enabled": dict(cfg.SOURCES_ENABLED),
        "weights": dict(cfg.WEIGHTS),
        "min_dr": cfg.MIN_DR,
        "tld_allowlist": list(cfg.TLD_ALLOWLIST),
        "brand_tokens": list(cfg.BRAND_TOKENS),
        "emd_sets": [],
        "max_links_per_run": cfg.MAX_LINKS_PER_RUN,
        "max_deep_per_run": cfg.MAX_DEEP_PER_RUN,
        "spam_anchor_max": cfg.SPAM_ANCHOR_MAX,
        "units_floor": cfg.UNITS_FLOOR,
    }


def _clean_weights(raw, base: dict | None = None) -> dict:
    """Веса с UI -> валидный словарь. Ключи — только известные компоненты (чужие игнорим:
    неизвестный ключ не с чем перемножать, compute_score упал бы на KeyError).

    `base` — на что опираться для НЕ переданных ключей. Из UI приходят все пять, но частичный
    POST (API, скрипт) не должен молча ронять остальные веса к дефолтам: база — то, что сейчас
    записано, а не то, что зашито в коде.

    Вырожденный набор (всё по нулю / мусор) НЕ записываем: он обнулил бы score всем доменам
    разом и тихо превратил бы воронку в «всё отклонено». В таком случае — дефолты."""
    b = {**cfg.WEIGHTS, **(base or {})}
    if not isinstance(raw, dict):
        return {k: b[k] for k in cfg.WEIGHTS}
    out = {}
    for k in cfg.WEIGHTS:                       # порядок и состав ключей задаёт код, не форма
        try:
            out[k] = max(0.0, min(1.0, float(raw.get(k, b[k]))))
        except (TypeError, ValueError):
            out[k] = b[k]
    return out if sum(out.values()) > 0 else dict(cfg.WEIGHTS)


def _clean_list(raw) -> list[str]:
    """Список с UI (textarea: строки/запятые/пробелы) или готовый список -> нижний регистр, без
    точек по краям, без пустых и дублей. Порядок оператора сохраняется."""
    if isinstance(raw, str):
        raw = re.split(r"[\s,;]+", raw)
    out = []
    for x in raw or ():
        t = str(x).strip().strip(".").lower()
        if t and t not in out:
            out.append(t)
    return out[:_LIST_MAX]


def _clean_emd_sets(raw) -> list[dict]:
    """Наборы EMD (JSON-текст с UI или список) -> валидный список. Невалидный JSON — ValueError:
    панель покажет ошибку, а сохранённые наборы НЕ затрутся пустотой.

    Строка вместо списка в `keywords` — это ОДИН ключ: перебор строки дал бы буквы
    (`"mejor vpn"` -> m.com, e.com, j.com…). `tlds` строкой разбирает `_clean_list`, как textarea."""
    if isinstance(raw, str):
        raw = json.loads(raw) if raw.strip() else []       # JSONDecodeError — подкласс ValueError
    if not isinstance(raw, list):
        raise ValueError("наборы EMD: ожидается JSON-список")
    out = []
    for s in raw[:50]:
        if not isinstance(s, dict):
            continue
        kw_raw = s.get("keywords") or []
        if isinstance(kw_raw, str):
            kw_raw = [kw_raw]
        kws = [str(k).strip()[:60] for k in kw_raw if str(k).strip()][:50]
        tlds = _clean_list(s.get("tlds") or [])[:20]
        if kws and tlds:
            out.append({"market": str(s.get("market") or "")[:16],
                        "lang": str(s.get("lang") or "")[:8].lower(),
                        "keywords": kws, "tlds": tlds})
    return out


def _row(db):
    """Вернуть (создав при отсутствии) строку scoring_settings id=1, засеянную дефолтами."""
    from app.models.settings import ScoringSettings
    row = db.get(ScoringSettings, 1)
    if row is None:
        d = _defaults()
        row = ScoringSettings(id=1, **d)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def get_settings() -> dict:
    from app.db import SessionLocal
    with SessionLocal() as db:
        r = _row(db)
        return {
            "min_referring_domains": int(r.min_referring_domains),
            "min_age_years": float(r.min_age_years),
            "approve_at": float(r.approve_at),
            "manual_review_at": float(r.manual_review_at),
            "max_whois_per_run": int(r.max_whois_per_run),
            "max_ahrefs_per_run": int(r.max_ahrefs_per_run),
            "sources_enabled": dict(r.sources_enabled or cfg.SOURCES_ENABLED),
            # пусто (миграция 0009 засеяла {}) -> дефолты из кода, а не нулевая шкала
            "weights": _clean_weights(r.weights or cfg.WEIGHTS),
            "min_dr": float(r.min_dr),
            "tld_allowlist": list(r.tld_allowlist or cfg.TLD_ALLOWLIST),   # пусто -> дефолт: пустой список убил бы всё
            "brand_tokens": list(r.brand_tokens or cfg.BRAND_TOKENS),
            "emd_sets": list(r.emd_sets or []),
            "max_links_per_run": int(r.max_links_per_run),
            "max_deep_per_run": int(r.max_deep_per_run),
            "spam_anchor_max": float(r.spam_anchor_max),
            "units_floor": int(r.units_floor),
        }


def update_settings(**kw) -> dict:
    """Записать переданные ключи с валидацией диапазонов. Неизвестные ключи игнор."""
    from app.db import SessionLocal
    with SessionLocal() as db:
        r = _row(db)
        for k in _KEYS_NUM:
            if k in kw and kw[k] is not None:
                lo, hi = _BOUNDS[k]
                v = max(lo, min(hi, type(lo)(kw[k])))
                setattr(r, k, v)
        for k in ("tld_allowlist", "brand_tokens"):
            if kw.get(k) is not None:
                setattr(r, k, _clean_list(kw[k]))
        if kw.get("emd_sets") is not None:
            r.emd_sets = _clean_emd_sets(kw["emd_sets"])   # ValueError -> выходим ДО commit
        if "sources_enabled" in kw and isinstance(kw["sources_enabled"], dict):
            r.sources_enabled = {s: bool(kw["sources_enabled"].get(s, False))
                                 for s in cfg.SOURCES_ENABLED}
        if "weights" in kw and kw["weights"] is not None:
            r.weights = _clean_weights(kw["weights"], base=dict(r.weights or {}))
        if r.max_whois_per_run < 1:
            r.max_whois_per_run = 1                 # 0 глушил бы скоринг целиком
        if r.approve_at < r.manual_review_at:
            r.approve_at = r.manual_review_at       # инверсия порогов -> approve не ниже manual
        db.commit()
    return get_settings()


def reset_settings() -> dict:
    from app.db import SessionLocal
    with SessionLocal() as db:
        r = _row(db)
        for k, v in _defaults().items():
            setattr(r, k, v)
        db.commit()
    return get_settings()
```

`backend/app/services/transitions.py` — блок от комментария `# Причины отказа, за которыми стоит ФАКТ О ДОМЕНЕ`
до строки `DIRTY_REASONS = frozenset({...})` включительно заменить на:

```python
# Причины отказа, за которыми стоит ФАКТ О ДОМЕНЕ, а не наш порог. Порог («мало доноров»,
# «молодой», «низкий скор») крутится на /settings, и вернуть такой домен в оборот руками —
# законное решение оператора. Эти не крутятся ничем: РКН — реестр государства, блэклист —
# внешний вердикт, грязная история и флаг фида — прошлое домена. Для портфеля, который держится
# на ЧИСТОЙ ИСТОРИИ (CLAUDE.md), они значат «никогда».
#
# v2 (миграция 0025): `legacy_ru` — архив РФ-пула (РФ из v2 исключена), `tld_closed` — зона вне
# белого списка. Без них «↩ вернуть в approved» открывала бы ручной путь к кассе домену, которого
# машина больше не судит (в 0025 `legacy_ru` перезаписывает и «отмытые» v1-домены с `rkn`). Зону
# добавили в белый список — путь назад тот же, что у грязи: перескор, а не кнопка.
#
# `not_acquirable` здесь НЕТ намеренно: «домен занят» — это не грязь, а чужая покупка. Оператор,
# знающий, что домен всё-таки дропнулся, вправе вернуть его руками.
DIRTY_REASONS = frozenset({"rkn", "blacklist", "history_dirty", "feed_flag", "safebrowsing",
                           "legacy_ru", "tld_closed"})
```

Там же функцию `check` (от `def check(d, target: str) -> None:` до строки `        refuse_dirty(d)`
включительно) заменить на две функции — гард зоны (находка R2-19) и `check` с ним:

```python
def refuse_closed_zone(d, allowlist=None) -> None:
    """Зона вне белого списка — в `approved` домен не вернуть даже руками. Бросает TransitionDenied.

    v2 судит и выкупает только зоны белого списка (/settings). Отказ `legacy_ru`/`tld_closed` —
    в DIRTY_REASONS, но v1-домен .ru, отклонённый ПОРОГОМ (`low_score`, `too_young`), грязным не
    считается, а миграция 0025 архивирует только ещё не решённые домены. Без этого гарда «↩ вернуть
    в approved» вела бы такой домен в очередь backorder, который .ru всё ещё покупает (находка
    R2-19). Зону добавили в белый список — домен возвращается той же кнопкой.
    `allowlist=None` — список из /settings; самопроверка без БД передаёт его явно.
    """
    from app.services.domain_filters import tld_match
    if allowlist is None:
        from app.services.settings import get_settings
        allowlist = get_settings()["tld_allowlist"]
    if not tld_match(d.domain, allowlist):
        raise TransitionDenied(
            f"домен «{d.domain}»: его зоны нет в белом списке зон (/settings) — v2 не судит и не "
            "выкупает такие домены, в approved его не вернуть")


def check(d, target: str, *, allowlist=None) -> None:
    """Разрешён ли РУЧНОЙ перевод домена `d` в `target`. Бросает TransitionDenied.

    Грязь проверяется раньше зоны: у грязного домена вне списка оператор увидит причину-грязь."""
    src = d.status
    if target not in MANUAL_TRANSITIONS.get(src, frozenset()):
        raise TransitionDenied(
            f"домен «{d.domain}» в статусе {src!r}: ручной перевод в {target!r} не разрешён")
    if target in TOWARD_MONEY:
        refuse_dirty(d)
    if target == "approved":
        refuse_closed_zone(d, allowlist)
```

Там же в блоке `if __name__ == "__main__":` (самопроверка без БД: с гардом зоны `check` без `allowlist`
полез бы в БД за настройками) строки

```python
    weak = NS(domain="weak.ru", status="rejected", reject_reason="low_score",
              rkn_listed=False, blacklisted=None, prior_flags={}, wayback_checked=True)
    assert dirty_reason(rkn) == "rkn" and dirty_reason(weak) is None
    try:
        check(rkn, "approved")
        raise AssertionError("грязь обязана быть отвергнута")
    except TransitionDenied:
        pass
    check(weak, "approved")                       # отсеянный ПОРОГОМ домен возвращается руками
```

заменить на

```python
    weak = NS(domain="weak.com", status="rejected", reject_reason="low_score",
              rkn_listed=False, blacklisted=None, prior_flags={}, wayback_checked=True)
    assert dirty_reason(rkn) == "rkn" and dirty_reason(weak) is None
    try:
        check(rkn, "approved", allowlist=["com"])
        raise AssertionError("грязь обязана быть отвергнута")
    except TransitionDenied:
        pass
    check(weak, "approved", allowlist=["com"])    # отсеянный ПОРОГОМ домен возвращается руками
    try:
        check(NS(**{**vars(weak), "domain": "weak.ru"}), "approved", allowlist=["com"])
        raise AssertionError("зона вне белого списка обязана быть отвергнута")
    except TransitionDenied:
        pass
```

`backend/alembic/versions/0025_v2_m1.py`:

```python
"""v2 M1: язык/тема домена, настройки международной воронки, память DR, архив РФ-пула"""
import json

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0025_v2_m1"
down_revision = "0024_domain_score_log"
branch_labels = None
depends_on = None

TLD_DEFAULT = ["com", "net", "org", "online", "xyz", "site", "co.uk", "mx", "co", "si", "nl", "in"]
BRANDS_DEFAULT = ["nordvpn", "expressvpn", "surfshark", "protonvpn", "cyberghost", "ipvanish",
                  "privateinternetaccess", "mullvad", "windscribe", "hotspotshield", "tunnelbear",
                  "purevpn", "vyprvpn", "hidemyass", "atlasvpn", "privadovpn", "hideme", "strongvpn",
                  "zenmate", "avast", "kaspersky", "norton"]
SOURCES_V2 = {"dropcatch": False, "nominet": True, "mx": True, "emd": True}
SOURCES_V1 = {"backorder": True, "cctld": False, "reg_ru": False, "sweb": False}

# РФ-кандидаты больше не нужны (v2 ушёл из РФ). Купленные и дальше — не трогаем; уже
# отклонённые — тоже (их причина — улика, а не мусор).
ARCHIVE_SQL = ("UPDATE domains SET status='rejected', reject_reason='legacy_ru' "
               "WHERE status IN ('discovered','scored','approved') "
               "AND (domain LIKE '%.ru' OR domain LIKE '%.su' OR domain LIKE '%.xn--p1ai')")
UNARCHIVE_SQL = ("UPDATE domains SET status='discovered', reject_reason=NULL "
                 "WHERE reject_reason='legacy_ru'")


def _jsonb(v):
    return sa.text("'" + json.dumps(v) + "'::jsonb")


def upgrade():
    op.add_column("domains", sa.Column("market_lang", sa.String(8), nullable=True))
    op.add_column("domains", sa.Column("topic", sa.String(120), nullable=True))
    op.add_column("scoring_settings", sa.Column("min_dr", sa.Numeric(), nullable=False, server_default="5"))
    op.add_column("scoring_settings", sa.Column("tld_allowlist", postgresql.JSONB(), nullable=False,
                                                server_default=_jsonb(TLD_DEFAULT)))
    op.add_column("scoring_settings", sa.Column("brand_tokens", postgresql.JSONB(), nullable=False,
                                                server_default=_jsonb(BRANDS_DEFAULT)))
    op.add_column("scoring_settings", sa.Column("emd_sets", postgresql.JSONB(), nullable=False,
                                                server_default=_jsonb([])))
    op.add_column("scoring_settings", sa.Column("max_links_per_run", sa.Integer(), nullable=False,
                                                server_default="500"))
    op.add_column("scoring_settings", sa.Column("max_deep_per_run", sa.Integer(), nullable=False,
                                                server_default="20"))
    op.add_column("scoring_settings", sa.Column("spam_anchor_max", sa.Numeric(), nullable=False,
                                                server_default="0.2"))
    # пол остатка units Ahrefs (решение оператора Р3): автопилот гоняет скоринг раз в час, капы
    # «на прогон» месячный бюджет не держат
    op.add_column("scoring_settings", sa.Column("units_floor", sa.Integer(), nullable=False,
                                                server_default="300000"))
    # память бесплатного DR (решение оператора Р4): DR один раз на домен, записи живут 4 суток
    op.create_table(
        "dr_seen",
        sa.Column("domain", sa.String(253), primary_key=True),
        sa.Column("dr", sa.Numeric(), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
    )
    # источники v2 и сброс весов: старые ключи (rd_proxy/indexed_echo) больше не существуют,
    # а смесь старых значений с новыми дефолтами дала бы шкалу, которую никто не задавал
    op.execute(sa.text("UPDATE scoring_settings SET sources_enabled = CAST(:s AS JSONB), "
                       "weights = CAST('{}' AS JSONB)").bindparams(s=json.dumps(SOURCES_V2)))
    op.execute(ARCHIVE_SQL)


def downgrade():
    op.execute(UNARCHIVE_SQL)
    op.execute(sa.text("UPDATE scoring_settings SET sources_enabled = CAST(:s AS JSONB)")
               .bindparams(s=json.dumps(SOURCES_V1)))
    op.drop_table("dr_seen")
    for col in ("units_floor", "spam_anchor_max", "max_deep_per_run", "max_links_per_run", "emd_sets",
                "brand_tokens", "tld_allowlist", "min_dr"):
        op.drop_column("scoring_settings", col)
    op.drop_column("domains", "topic")
    op.drop_column("domains", "market_lang")
```

- [ ] **Шаг 4: Старые тесты, которые ломает задача — явный список**

Гард зоны (R2-19) отвергает `→ approved` для `.ru`. Без правок ниже падают ровно 5 тестов: домен, который
тест переводит в `approved` (кнопкой, пакетом или `check`), остаётся в прежнем статусе или ловит
`TransitionDenied`. Это тесты гейтов — только переписать: домен, который должен дойти до `approved`,
переезжает в зону белого списка (`.com`), смысл и ассерты не меняются. Остальные `.ru`-литералы в этих
файлах не трогать (их домены в `approved` не идут; `.ru` → `.com` в воронке — Задача 9).

1. `test_transitions.py::test_manual_transition_checks_source_status_not_only_target` — в помощнике
   `d(status, **kw)` строку

```python
        return NS(**{"domain": "x.ru", "status": status, "reject_reason": None,
```

   заменить на

```python
        return NS(**{"domain": "x.com", "status": status, "reject_reason": None,
```

   (помощник `d(**kw)` соседнего `test_dirty_reason_sees_verdict_and_raw_signals` — со строкой
   `"domain": "x.ru", "status": "rejected"` — не трогать: он в `approved` не переводит).

2. `test_transitions.py::test_threshold_reject_is_still_returnable` — заменить функцию целиком:

```python
def test_threshold_reject_is_still_returnable(client):
    """ЧТО ЛОМАЕТСЯ, когда запрет срабатывает: НИЧЕГО у законно отклонённых.

    Домен, отсеянный ПОРОГОМ (низкий скор), — не грязь: порог крутится на /settings, и вернуть
    такой домен в оборот руками оператор вправе. Запрет, который заодно запер бы и его, был бы
    не фиксом, а новой поломкой. v2: домен — в зоне белого списка; .ru с тем же порогом не
    вернуть (находка R2-19, test_migration_0025).
    """
    did = _add(domain="weak.com", status="rejected", reject_reason="low_score", score=0.35)
    client.post(f"/domains/{did}/set-status", data={"status": "approved"}, follow_redirects=False)
    assert _status(did) == "approved"
```

3. `test_pipeline.py::test_panel_actions` — строку

```python
    did = _add(Domain(domain="curate-me.ru", source="backorder", status="scored"))
```

   заменить на

```python
    did = _add(Domain(domain="curate-me.com", source="backorder", status="scored"))
```

4. `test_inbox.py::test_bulk_approve_skips_blind_domains` — заменить функцию целиком (`clean.ru` →
   `clean.com`; `blind.ru`/`weak.ru` в пакет не идут и остаются):

```python
def test_bulk_approve_skips_blind_domains(client):
    """Пакет — решение человека, но НЕ обход гейта: непроверенное в него не попадает.

    `clean.com` несёт wayback_checked=True НЕ для красоты: «чистый» домен без реально
    прочитанной истории — это и был баг F2 (пустой Wayback ошибки не бросает), и фикстура,
    молчавшая об этом поле, ровно его и покрывала собой."""
    _add(domain="clean.com", status="scored", score=0.9, wayback_checked=True,
         prior_flags={}, score_breakdown={"errors": []})
    _add(domain="blind.ru", status="scored", score=0.9,
         score_breakdown={"errors": ["wayback:ConnectError"]})
    _add(domain="weak.ru", status="scored", score=0.5, wayback_checked=True,
         prior_flags={}, score_breakdown={"errors": []})
    r = client.post("/domains/bulk-approve", data={"min_score": 0.8}, follow_redirects=False)
    assert r.status_code == 303
    with SessionLocal() as db:
        st = {d.domain: d.status for d in db.query(Domain).all()}
    assert st == {"clean.com": "approved", "blind.ru": "scored", "weak.ru": "scored"}
```

   (`test_bulk_preview_counts` в том же файле не трогать: он только считает, его `clean.ru` остаётся).

5. `test_history_verdict.py::test_unchecked_history_stays_out_of_bulk` — заменить функцию целиком
   (`ok.ru` → `ok.com`; `ghost.ru` в пакет не идёт):

```python
def test_unchecked_history_stays_out_of_bulk(client):
    """РЕПРО АУДИТА: score 0.825, errors пуст, снимков не было — домен уходил в пакет как чистый."""
    _add(domain="ghost.ru", status="scored", score=0.825, wayback_checked=False,
         prior_flags={}, score_breakdown={"errors": []})
    _add(domain="ok.com", status="scored", score=0.825, wayback_checked=True,
         prior_flags=_CLEAN_FLAGS, score_breakdown={"errors": []})
    assert client.get("/domains/bulk-preview?min_score=0.8").json() == {"n": 1, "skipped": 1}
    r = client.post("/domains/bulk-approve", data={"min_score": 0.8}, follow_redirects=False)
    assert r.status_code == 303
    with db.SessionLocal() as s:
        st = {d.domain: d.status for d in s.query(Domain).all()}
    assert st == {"ghost.ru": "scored", "ok.com": "approved"}   # непроверенный НЕ одобрен пакетом
```

   (`ok.ru` в `test_verdict_clean_only_when_wayback_really_checked` того же файла не трогать: он не
   переводится в `approved`).

- [ ] **Шаг 5: Тесты, сьют, самопроверка, линт**

Запуск: `cd backend && ../.venv/bin/python -m pytest tests/test_migration_0025.py tests/test_settings_v2.py tests/test_settings.py tests/test_transitions.py -v && ../.venv/bin/python -m pytest tests -q && ../.venv/bin/python -m app.services.transitions && cd .. && .venv/bin/python -m pyflakes backend/app backend/tests`
Ожидание: всё зелёное — 822 passed (809 + 13 новых), самопроверка печатает `transitions policy ok`,
pyflakes пуст.

- [ ] **Шаг 6: Коммит**

```bash
git add backend/alembic/versions/0025_v2_m1.py backend/app/models backend/app/services/scoring_config.py backend/app/services/settings.py backend/app/services/transitions.py backend/tests/test_migration_0025.py backend/tests/test_settings_v2.py backend/tests/test_transitions.py backend/tests/test_pipeline.py backend/tests/test_inbox.py backend/tests/test_history_verdict.py
git commit -m "feat(v2): миграция 0025 — язык/тема домена, настройки v2, пол units, память DR, архив РФ-пула; зона вне списка не возвращается в approved"
```

---

### Задача 6: Discovery v2 — источники, фильтр зон и DR на входе (с памятью `dr_seen`), ручной список

**Files:**
- Rewrite: `backend/app/services/discovery.py`
- Modify:
  - `backend/app/services/scoring_config.py` — `SOURCES_ENABLED`;
  - `backend/app/services/settings.py` — нормализация `sources_enabled` к ключам кода;
  - `backend/app/services/diagnostics.py` — убрать запись `backorder` (это больше не discovery);
  - `backend/app/integrations/backorder.py` — удалить `list_dropping`;
  - `backend/tests/conftest.py` — офлайн-гвард источников;
  - `backend/app/api/panel.py` — `settings_save` (тумблеры) + новый `POST /domains/add-list`;
  - `backend/app/templates/settings.html` — станция «Источники»;
  - `backend/app/templates/pool.html` — форма списка.
- Delete: `backend/app/integrations/{cctld,regru_drops,sweb_drops}.py`.
- Test: `backend/tests/test_discovery_v2.py`. Старые тесты — явный список в шаге 5.

**Interfaces:**
- Consumes:
  - `domain_filters.canonical_domain/tld_match/emd_candidates` (Задача 1);
  - `AhrefsClient().dr_free` (Задача 2): ключ ответа — канон-форма спрошенного домена;
  - `DropCatchClient/NominetClient/RegistryMxClient().list_dropping()` (Задача 4);
  - ключи настроек и модель `DrSeen` из Задачи 5.
- Produces:
  - `run_discovery() -> int` (сколько вставлено, контракт тот же; при отмене — `None`, как и раньше);
  - `add_list(text: str) -> {"added": int, "known": int, "bad": int, "cut": int}` (`cut` — сколько
    отброшено сверх `_LIST_MAX` за раз);
  - константа `AUTO_SOURCES = ("dropcatch", "nominet", "mx")`;
  - `discovery.canonical_domain` — реэкспорт из `domain_filters` (его проверяет
    `test_domain_filters.py::test_canonical_domain_still_reexported_from_discovery`; импорт не убирать);
  - `discovery._sleep` — подменяемая функция сна (пауза между пачками DR и ожидание после 429).
- Поведение, на которое опираются задачи 11–17: стадии задачи `discovery` — включённые источники +
  `dr` + `save`; сообщение задачи — водопад по источникам, затем `· DR из памяти (4 сут) — N`,
  `· DR недоступен — N пропущено`, `· <Источник>: упал (<Исключение>)`; без кандидатов —
  `нет кандидатов` (+ упавшие источники через ` · `).
- «DR недоступен» в `_dr_filter` (в `dr_seen` не пишется): пачка, на которой Ahrefs упал; пачка, на
  которую пришёл `200`, но без единого спрошенного домена (пустое или чужое тело — находка R2-7);
  `401`/`403` — опрос ОСТАВШИХСЯ пачек прекращается, весь остаток пропущен (находка R2-8); пустой
  ключ у клиента (`ahrefs.api_key == ""`) — в Ahrefs не ходим вовсе. Отдельный домен, которого нет в
  непустом ответе, — по-прежнему пропущен и запомнен с `dr=None`.

- [ ] **Шаг 1: Написать падающий тест** — `backend/tests/test_discovery_v2.py`

```python
"""Discovery v2: зоны -> известные -> DR на входе (с памятью dr_seen) -> вставка. Сеть подменена."""
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote

import httpx
import pytest

import app.db as db
from app.models.domain import Domain, DrSeen
from app.services import discovery, jobs
from app.services.settings import update_settings

DL = datetime(2026, 10, 3, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def slept(monkeypatch):
    """Пауза между пачками DR и ожидание после 429 — без настоящего сна; что «проспали» — видно."""
    out = []
    monkeypatch.setattr(discovery, "_sleep", out.append)
    return out


class _Src:
    rows: list = []

    def list_dropping(self):
        return list(self.rows)


def _sources(monkeypatch, **rows):
    classes = {}
    for name in ("dropcatch", "nominet", "mx"):
        classes[name] = type(name, (_Src,), {"rows": rows.get(name, [])})
    monkeypatch.setattr(discovery, "_clients", lambda: classes)


class _Ahrefs:
    calls: list = []
    drs: dict = {}
    fail = False
    missing: set = set()          # домены, которых «нет в ответе»
    errors: list = []             # исключения первых вызовов, по очереди

    def dr_free(self, domains):
        _Ahrefs.calls.append(list(domains))
        if _Ahrefs.errors:
            raise _Ahrefs.errors.pop(0)
        if _Ahrefs.fail:
            raise RuntimeError("ahrefs down")
        return {d: _Ahrefs.drs.get(d, 0.0) for d in domains if d not in _Ahrefs.missing}


def _ahrefs(monkeypatch, drs, fail=False, missing=(), errors=()):
    import app.integrations.ahrefs as ah
    _Ahrefs.calls, _Ahrefs.drs, _Ahrefs.fail = [], drs, fail
    _Ahrefs.missing, _Ahrefs.errors = set(missing), list(errors)
    monkeypatch.setattr(ah, "AhrefsClient", _Ahrefs)


def _http_error(code, headers=None):
    req = httpx.Request("POST", "https://api.ahrefs.com/v3/public/domain-rating-free")
    return httpx.HTTPStatusError(str(code), request=req,
                                 response=httpx.Response(code, headers=headers or {}, request=req))


def _row(d, src="nominet", lane="bid", dl=DL):
    return {"domain": d, "source": src, "lane": lane, "acquire_deadline": dl}


def _all():
    with db.SessionLocal() as s:
        return {d.domain: d for d in s.query(Domain)}


def _seen():
    with db.SessionLocal() as s:
        return {r.domain: (None if r.dr is None else float(r.dr)) for r in s.query(DrSeen)}


def test_zone_filter_dr_filter_and_save(monkeypatch):
    update_settings(sources_enabled={"nominet": True, "mx": True})
    _sources(monkeypatch,
             nominet=[_row("Good.co.uk"), _row("junk.co.uk"), _row("x.org.uk")],
             mx=[_row("a.mx", "mx", "free", None), _row("b.com.mx", "mx", "free", None)])
    _ahrefs(monkeypatch, {"good.co.uk": 12.0, "a.mx": 6.0})
    assert discovery.run_discovery() == 2
    got = _all()
    assert set(got) == {"good.co.uk", "a.mx"}                  # junk: DR 0; x.org.uk, b.com.mx: не наши зоны
    assert float(got["good.co.uk"].dr) == 12.0 and got["good.co.uk"].lane == "bid"
    assert got["good.co.uk"].acquire_deadline.replace(tzinfo=timezone.utc) == DL
    assert got["a.mx"].source == "mx" and got["a.mx"].lane == "free"
    assert sorted(_Ahrefs.calls[0]) == ["a.mx", "good.co.uk", "junk.co.uk"]   # DR только для своих зон


def test_rerun_is_idempotent_and_skips_dr_for_known_and_remembered(monkeypatch):
    # Р4: домен ниже порога в `domains` не попадает — без памяти dr_seen его DR спрашивался бы на
    # каждом прогоне, а лицензия DR-free запрещает систематический сбор
    update_settings(sources_enabled={"nominet": True})
    _sources(monkeypatch, nominet=[_row("good.co.uk"), _row("junk.co.uk")])
    _ahrefs(monkeypatch, {"good.co.uk": 12.0})                   # junk.co.uk -> DR 0.0
    assert discovery.run_discovery() == 1
    assert _seen() == {"good.co.uk": 12.0, "junk.co.uk": 0.0}    # запомнены ВСЕ спрошенные
    with db.SessionLocal() as s:
        s.query(Domain).filter_by(domain="good.co.uk").update({"status": "scored"})
        s.commit()
    _Ahrefs.calls = []
    assert discovery.run_discovery() == 0
    assert _Ahrefs.calls == []                                   # известный и запомненный — DR не тратим
    assert _all()["good.co.uk"].status == "scored"               # решённый статус не откатился
    assert set(_all()) == {"good.co.uk"}
    assert "DR из памяти (4 сут) — 1" in jobs.last("discovery")["message"]


def test_dr_memory_older_than_4_days_is_purged_and_asked_again(monkeypatch):
    old = datetime.now(timezone.utc) - timedelta(days=5)
    with db.SessionLocal() as s:
        s.add_all([DrSeen(domain="junk.co.uk", dr=0.0, checked_at=old),
                   DrSeen(domain="gone.co.uk", dr=1.0, checked_at=old)])
        s.commit()
    update_settings(sources_enabled={"nominet": True})
    _sources(monkeypatch, nominet=[_row("junk.co.uk")])
    _ahrefs(monkeypatch, {})
    assert discovery.run_discovery() == 0
    assert _Ahrefs.calls == [["junk.co.uk"]]                     # память протухла — спросили снова
    assert _seen() == {"junk.co.uk": 0.0}                        # gone.co.uk вычищен в начале прогона
    with db.SessionLocal() as s:
        fresh = s.get(DrSeen, "junk.co.uk").checked_at.replace(tzinfo=timezone.utc)
    assert fresh > old + timedelta(days=4)


def test_dr_unavailable_saves_nothing_and_remembers_nothing(monkeypatch):
    update_settings(sources_enabled={"nominet": True})
    _sources(monkeypatch, nominet=[_row("good.co.uk")])
    _ahrefs(monkeypatch, {}, fail=True)
    assert discovery.run_discovery() == 0 and _all() == {}
    assert "DR недоступен — 1 пропущено" in jobs.last("discovery")["message"]
    assert _seen() == {}                                         # сбой не запоминаем: следующий прогон спросит


def test_domain_missing_from_dr_answer_is_skipped_not_below_threshold(monkeypatch):
    # частичный ответ — аномалия (на несуществующий домен Ahrefs отдаёт 0.0), а не «DR ниже порога»
    update_settings(sources_enabled={"nominet": True})
    _sources(monkeypatch, nominet=[_row("good.co.uk"), _row("lost.co.uk")])
    _ahrefs(monkeypatch, {"good.co.uk": 12.0}, missing={"lost.co.uk"})
    assert discovery.run_discovery() == 1
    assert set(_all()) == {"good.co.uk"}
    assert "DR недоступен — 1 пропущено" in jobs.last("discovery")["message"]
    assert _seen() == {"good.co.uk": 12.0, "lost.co.uk": None}   # не переспрашиваем его каждый час


def test_dr_batches_pause_between_and_wait_retry_after_on_429(monkeypatch, slept):
    monkeypatch.setattr(discovery, "_DR_BATCH", 2)
    update_settings(sources_enabled={"nominet": True})
    names = [f"d{i}.co.uk" for i in range(5)]
    _sources(monkeypatch, nominet=[_row(n) for n in names])
    _ahrefs(monkeypatch, {n: 9.0 for n in names}, errors=[_http_error(429, {"Retry-After": "7"})])
    assert discovery.run_discovery() == 5
    assert _Ahrefs.calls == [names[0:2], names[0:2], names[2:4], names[4:5]]   # после 429 — один повтор
    assert slept == [7.0, 1.0, 1.0]                              # Retry-After, затем пауза ≥1 с между пачками


def test_second_429_skips_the_batch_and_does_not_remember_it(monkeypatch, slept):
    update_settings(sources_enabled={"nominet": True})
    _sources(monkeypatch, nominet=[_row("good.co.uk")])
    _ahrefs(monkeypatch, {"good.co.uk": 12.0}, errors=[_http_error(429), _http_error(429)])
    assert discovery.run_discovery() == 0
    assert len(_Ahrefs.calls) == 2 and slept == [60.0]           # без Retry-After — минута; повтор один
    assert "DR недоступен — 1 пропущено" in jobs.last("discovery")["message"]
    assert _seen() == {}


def test_dr_answer_without_any_asked_domain_is_a_failed_batch(monkeypatch):
    # находка R2-7: 200, но в ответе нет НИ ОДНОГО спрошенного домена (пустое или чужое тело) — это
    # сбой пачки, а не «DR неизвестен у всех»: запомни мы её, пачка была бы похоронена на 4 суток
    update_settings(sources_enabled={"nominet": True})
    _sources(monkeypatch, nominet=[_row("good.co.uk"), _row("lost.co.uk")])
    _ahrefs(monkeypatch, {"good.co.uk": 12.0}, missing={"good.co.uk", "lost.co.uk"})
    assert discovery.run_discovery() == 0 and _all() == {}
    assert "DR недоступен — 2 пропущено" in jobs.last("discovery")["message"]
    assert _seen() == {}                                         # следующий прогон спросит снова


@pytest.mark.parametrize("code", [401, 403])
def test_dr_auth_error_stops_asking_remaining_batches(monkeypatch, code):
    # находка R2-8: ключ не принят — остальные пачки ответят так же; не тратим на них запросы
    # (DropCatch — ~134 пачки за прогон), весь остаток — «DR недоступен»
    monkeypatch.setattr(discovery, "_DR_BATCH", 1)
    update_settings(sources_enabled={"nominet": True})
    names = ["a.co.uk", "b.co.uk", "c.co.uk"]
    _sources(monkeypatch, nominet=[_row(n) for n in names])
    _ahrefs(monkeypatch, {n: 9.0 for n in names}, errors=[_http_error(code)])
    assert discovery.run_discovery() == 0 and _all() == {}
    assert _Ahrefs.calls == [["a.co.uk"]]                        # после отказа в доступе — ни одной пачки
    assert "DR недоступен — 3 пропущено" in jobs.last("discovery")["message"]
    assert _seen() == {}


def test_empty_ahrefs_key_asks_nothing(monkeypatch):
    # находка R2-8: пустой AHREFS_API_KEY (autouse _no_paid_keys) — настоящий клиент в сеть не ходит
    # (иначе рубильник _no_live_network уронил бы тест), домены — «DR недоступен», в память не пишутся
    update_settings(sources_enabled={"nominet": True})
    _sources(monkeypatch, nominet=[_row("good.co.uk")])
    assert discovery.run_discovery() == 0 and _all() == {}
    assert "DR недоступен — 1 пропущено" in jobs.last("discovery")["message"]
    assert _seen() == {}


def test_cancel_between_dr_batches_then_remembered_dr_saves_without_asking(monkeypatch):
    import app.integrations.ahrefs as ah
    monkeypatch.setattr(discovery, "_DR_BATCH", 1)
    update_settings(sources_enabled={"nominet": True})
    _sources(monkeypatch, nominet=[_row("a.co.uk"), _row("b.co.uk")])
    _ahrefs(monkeypatch, {"a.co.uk": 9.0, "b.co.uk": 9.0})

    class _StopDuringFirst(_Ahrefs):
        def dr_free(self, domains):
            jobs.request_cancel("discovery")                     # «стоп» во время первой пачки DR
            return super().dr_free(domains)
    monkeypatch.setattr(ah, "AhrefsClient", _StopDuringFirst)
    discovery.run_discovery()
    assert jobs.progress("discovery")["status"] == "cancelled"
    assert _Ahrefs.calls == [["a.co.uk"]] and _all() == {}       # вторую пачку не спросили, записи не было
    monkeypatch.setattr(ah, "AhrefsClient", _Ahrefs)
    _Ahrefs.calls = []
    assert discovery.run_discovery() == 2
    assert _Ahrefs.calls == [["b.co.uk"]]                        # a.co.uk — из памяти (DR 9), не теряется


def test_cancel_between_save_chunks_keeps_written_chunk(monkeypatch):
    monkeypatch.setattr(discovery, "_CHUNK", 1)
    update_settings(sources_enabled={"nominet": True})
    _sources(monkeypatch, nominet=[_row("a.co.uk"), _row("b.co.uk")])
    _ahrefs(monkeypatch, {"a.co.uk": 9.0, "b.co.uk": 9.0})
    real = discovery._new_domain

    def new_domain(name, c, dr):
        jobs.request_cancel("discovery")                         # «стоп» во время записи первого чанка
        return real(name, c, dr)
    monkeypatch.setattr(discovery, "_new_domain", new_domain)
    discovery.run_discovery()
    assert jobs.progress("discovery")["status"] == "cancelled"
    assert set(_all()) == {"a.co.uk"}                            # записанное остаётся, второй чанк не начат


def test_dr_and_save_stages_report_progress(monkeypatch):
    monkeypatch.setattr(discovery, "_DR_BATCH", 2)
    monkeypatch.setattr(discovery, "_CHUNK", 2)
    update_settings(sources_enabled={"nominet": True})
    names = [f"d{i}.co.uk" for i in range(3)]
    _sources(monkeypatch, nominet=[_row(n) for n in names])
    _ahrefs(monkeypatch, {n: 9.0 for n in names})
    real, seen = jobs.report, []

    def spy(run_id, **kw):
        seen.append(kw.get("current"))
        return real(run_id, **kw)
    monkeypatch.setattr(jobs, "report", spy)
    assert discovery.run_discovery() == 3
    assert [c for c in seen if c and c.startswith(("DR:", "запись:"))] == [
        "DR: 0 из 3", "DR: 2 из 3", "запись: 0 из 3", "запись: 2 из 3"]


def test_emd_saved_without_dr_with_market_lang(monkeypatch):
    update_settings(sources_enabled={"emd": True},
                    emd_sets='[{"market":"es-MX","lang":"es","keywords":["mejor vpn"],"tlds":["com"]}]')
    _sources(monkeypatch)
    _ahrefs(monkeypatch, {})
    assert discovery.run_discovery() == 2
    got = _all()
    assert set(got) == {"mejorvpn.com", "mejor-vpn.com"}
    assert got["mejorvpn.com"].source == "emd" and got["mejorvpn.com"].market_lang == "es"
    assert got["mejorvpn.com"].lane == "free" and _Ahrefs.calls == []


def test_emd_name_also_found_by_auto_source_bypasses_dr_filter(monkeypatch):
    # у свободного EMD ссылок и не должно быть: DR 0 — не повод терять имя, найденное и registry.mx
    update_settings(sources_enabled={"mx": True, "emd": True},
                    emd_sets='[{"market":"es-MX","lang":"es","keywords":["mejor vpn"],"tlds":["mx"]}]')
    _sources(monkeypatch, mx=[_row("mejorvpn.mx", "mx", "free", None)])
    _ahrefs(monkeypatch, {})                                     # спроси мы DR — был бы 0.0
    assert discovery.run_discovery() == 2
    got = _all()
    assert _Ahrefs.calls == []
    assert got["mejorvpn.mx"].source == "mx" and got["mejorvpn.mx"].market_lang == "es"
    assert got["mejor-vpn.mx"].source == "emd"


def test_known_discovered_row_gets_missing_lane_and_deadline(monkeypatch):
    with db.SessionLocal() as s:
        s.add(Domain(domain="good.co.uk", source="list", status="discovered"))
        s.commit()
    update_settings(sources_enabled={"nominet": True})
    _sources(monkeypatch, nominet=[_row("good.co.uk")])
    _ahrefs(monkeypatch, {})
    discovery.run_discovery()
    d = _all()["good.co.uk"]
    assert d.lane == "bid" and d.acquire_deadline is not None and d.source == "list"


def test_chunked_lookup_and_insert(monkeypatch):
    monkeypatch.setattr(discovery, "_CHUNK", 2)
    update_settings(sources_enabled={"nominet": True})
    names = [f"d{i}.co.uk" for i in range(5)]
    _sources(monkeypatch, nominet=[_row(n) for n in names])
    _ahrefs(monkeypatch, {n: 9.0 for n in names})
    assert discovery.run_discovery() == 5 and set(_all()) == set(names)


def test_failing_source_does_not_sink_others(monkeypatch, caplog):
    update_settings(sources_enabled={"nominet": True, "mx": True})

    class Boom:
        def list_dropping(self):
            raise RuntimeError("nominet down")
    classes = {"dropcatch": _Src, "nominet": Boom,
               "mx": type("mx", (_Src,), {"rows": [_row("a.mx", "mx", "free", None)]})}
    monkeypatch.setattr(discovery, "_clients", lambda: classes)
    _ahrefs(monkeypatch, {"a.mx": 7.0})
    assert discovery.run_discovery() == 1
    assert "nominet" in caplog.text
    assert "Nominet (.uk): упал (RuntimeError)" in jobs.last("discovery")["message"]


def test_failed_source_visible_even_when_no_candidates(monkeypatch):
    # «все источники упали» не должно выглядеть как пустой день
    update_settings(sources_enabled={"nominet": True})

    class Boom:
        def list_dropping(self):
            raise RuntimeError("nominet down")
    monkeypatch.setattr(discovery, "_clients", lambda: {"nominet": Boom})
    _ahrefs(monkeypatch, {})
    assert discovery.run_discovery() == 0
    assert jobs.last("discovery")["message"] == "нет кандидатов · Nominet (.uk): упал (RuntimeError)"


def test_stale_v1_source_keys_in_db_fall_back_to_code_defaults(monkeypatch):
    # в БД бокса лежат ключи v1 (backorder/cctld/…): они не должны молча выключить источники v2
    from app.models.settings import ScoringSettings
    from app.services import scoring_config as cfg
    from app.services.settings import get_settings
    monkeypatch.setattr(cfg, "SOURCES_ENABLED",
                        {"dropcatch": False, "nominet": True, "mx": True, "emd": True})
    get_settings()
    with db.SessionLocal() as s:
        s.get(ScoringSettings, 1).sources_enabled = {"backorder": True, "nominet": False}
        s.commit()
    assert get_settings()["sources_enabled"] == {"dropcatch": False, "nominet": False,
                                                 "mx": True, "emd": True}


def test_add_list_counts_bad_known_and_added():
    with db.SessionLocal() as s:
        s.add(Domain(domain="known.com", status="scored"))
        s.commit()
    out = discovery.add_list("New.com\nknown.com, new.com ;;  not_a_domain  x.ru")
    assert out == {"added": 2, "known": 1, "bad": 1, "cut": 0}   # x.ru принят: W0 скажет tld_closed
    got = _all()
    assert got["new.com"].source == "list" and got["new.com"].status == "discovered"


def test_add_list_reports_what_was_cut_over_max(monkeypatch, client):
    monkeypatch.setattr(discovery, "_LIST_MAX", 2)
    assert discovery.add_list("a.com b.com c.com") == {"added": 2, "known": 0, "bad": 0, "cut": 1}
    r = client.post("/domains/add-list", data={"domains": "d.com e.com f.com"}, follow_redirects=False)
    assert r.status_code == 303
    assert "сверх 2 за раз отброшено 1" in unquote(r.headers["location"])
```

- [ ] **Шаг 2: Запустить — убедиться, что падает**

Запуск: `cd backend && ../.venv/bin/python -m pytest tests/test_discovery_v2.py -v`
Ожидание: `23 errors` — на каждом тесте `AttributeError: <module 'app.services.discovery' …> has no
attribute '_sleep'` (падает autouse-фикстура `slept` на этапе setup, поэтому ERROR, а не FAILED).

- [ ] **Шаг 3: Реализовать** — `backend/app/services/discovery.py` переписать целиком

```python
"""M1a — discovery v2: международные источники -> `domains` (status='discovered').

Конвейер АВТОМАТИЧЕСКИХ источников (dropcatch/nominet/mx): канон-форма -> белый список зон ->
отсев известных -> бесплатный DR Ahrefs только для НОВЫХ, не спрошенных за 4 суток (`dr_seen`) ->
вставка тех, у кого DR >= min_dr.

Почему DR-фильтр здесь, а не волной скоринга (живой замер 2026-10-01): Nominet отдаёт всё
расписание (~266 тыс. строк), DropCatch — ~134 тыс. в день, и почти всё — DR 0, засыпанный
автоматическим SEO-спамом (RD 700+ из спам-анкоров). Хранить их, чтобы потом отклонить, — раздувать
базу на десятки тысяч строк в день и делать столько же коммитов. EMD и ручной список фильтр по DR
не проходят: это выбор оператора / новореги, у которых ссылок и не должно быть.
"""
import logging
import re
import time
from datetime import datetime, timedelta, timezone

import httpx

from app.services.domain_filters import canonical_domain, emd_candidates, tld_match

logger = logging.getLogger(__name__)

AUTO_SOURCES = ("dropcatch", "nominet", "mx")
_SOURCE_RU = {"dropcatch": "DropCatch", "nominet": "Nominet (.uk)", "mx": "registry.mx", "emd": "EMD"}
_CHUNK = 5000        # psycopg: не больше 65 535 параметров на запрос — IN и вставку режем чанками
_DR_BATCH = 1000     # public/domain-rating-free: до 1000 целей за запрос
_DR_PAUSE = 1.0      # с между пачками DR: лимит Ahrefs — 60 запросов в минуту
_DR_MEMORY = timedelta(days=4)   # спрошенный домен не спрашиваем снова 4 суток (Р4, лицензия DR-free)
_LIST_MAX = 5000     # ручной список за раз
_sleep = time.sleep  # пауза между пачками DR и ожидание после 429; тесты подменяют, чтобы не спать


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _clients() -> dict:
    from app.integrations.dropcatch import DropCatchClient
    from app.integrations.nominet import NominetClient
    from app.integrations.registry_mx import RegistryMxClient
    return {"dropcatch": DropCatchClient, "nominet": NominetClient, "mx": RegistryMxClient}


def _collect(enabled: dict, st: dict, run=None) -> tuple[dict, dict]:
    """({источник: строки}, {упавший источник: имя исключения}). Сбой одного источника не топит
    остальные, но и не молчит: вызывающий пишет его в сообщение задачи — иначе «все источники
    упали» неотличимо от пустого дня. Стоп проверяется между источниками."""
    from app.services import jobs
    clients, out, failed = _clients(), {}, {}
    for name in (*AUTO_SOURCES, "emd"):
        if not enabled.get(name):
            continue
        if jobs.cancelled(run):
            raise jobs.Cancelled()
        jobs.report(run, stage=name, current=f"собираю: {_SOURCE_RU[name]}")
        try:
            if name == "emd":
                rows = [{"domain": c["domain"], "source": "emd", "lane": "free",
                         "acquire_deadline": None, "market_lang": c["lang"] or None}
                        for c in emd_candidates(st["emd_sets"], st["brand_tokens"])]
            else:
                rows = clients[name]().list_dropping()
        except Exception as e:  # noqa: BLE001 — один источник упал, остальные идут
            logger.warning("discovery source %s failed: %s", name, e)
            failed[name] = type(e).__name__
            continue
        if not rows:
            logger.warning("discovery source %s дал 0 строк (пусто/сменился формат?)", name)
        out[name] = rows
    return out, failed


def _known(db, names: list) -> dict:
    """{домен: Domain} для уже известных — чанками (предел параметров psycopg)."""
    from sqlalchemy import select
    from app.models.domain import Domain
    out = {}
    for i in range(0, len(names), _CHUNK):
        part = names[i:i + _CHUNK]
        out.update({d.domain: d for d in db.execute(
            select(Domain).where(Domain.domain.in_(part))).scalars()})
    return out


def _dr_purge() -> None:
    """Удалить записи dr_seen старше 4 суток — в начале прогона, иначе таблица растёт без края."""
    from sqlalchemy import delete
    from app.db import SessionLocal
    from app.models.domain import DrSeen
    with SessionLocal() as db:
        db.execute(delete(DrSeen).where(DrSeen.checked_at < _now() - _DR_MEMORY))
        db.commit()


def _dr_memo(names: list, since: datetime) -> dict:
    """{домен: DR | None} спрошенных не раньше `since` — чанками (предел параметров psycopg)."""
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models.domain import DrSeen
    out = {}
    with SessionLocal() as db:
        for i in range(0, len(names), _CHUNK):
            part = names[i:i + _CHUNK]
            out.update({r.domain: r.dr for r in db.execute(
                select(DrSeen).where(DrSeen.domain.in_(part), DrSeen.checked_at >= since)).scalars()})
    return out


def _dr_remember(part: list, drs: dict, now: datetime) -> None:
    """Запомнить ВСЕ спрошенные домены пачки (`dr` None — Ahrefs его не вернул). Upsert как
    delete+insert: одинаково работает на SQLite тестов и PostgreSQL бокса."""
    from sqlalchemy import delete, insert
    from app.db import SessionLocal
    from app.models.domain import DrSeen
    with SessionLocal() as db:
        db.execute(delete(DrSeen).where(DrSeen.domain.in_(part)))
        db.execute(insert(DrSeen), [{"domain": d, "dr": drs.get(d), "checked_at": now} for d in part])
        db.commit()


def _retry_after(e: httpx.HTTPStatusError) -> float:
    """Секунды из Retry-After ответа 429; нет заголовка или в нём дата — 60 (окно лимита — минута)."""
    try:
        return float(e.response.headers.get("Retry-After") or 60)
    except ValueError:
        return 60.0


def _dr_once(ahrefs, part: list) -> dict:
    """dr_free с одним повтором на 429. Ретрай BaseClient (3 попытки за ~3 с) короче минутного
    окна лимита Ahrefs (60 запросов в минуту) — после него ждём, сколько просит сервер."""
    try:
        return ahrefs.dr_free(part)
    except httpx.HTTPStatusError as e:
        if e.response.status_code != 429:
            raise
        _sleep(_retry_after(e))
        return ahrefs.dr_free(part)


def _dr_filter(names: list, min_dr: float, ahrefs, run=None) -> tuple[dict, int, set]:
    """({домен: DR} прошедших порог, сколько ПРОПУЩЕНО, имена, взятые из памяти dr_seen).

    Память (решение оператора Р4): домен, спрошенный за последние 4 суток, в Ahrefs не идёт.
    Отсеянные домены в `domains` не попадают, и без памяти их DR спрашивался бы на каждом прогоне
    (живьём: 123 запроса на 122 тыс. доменов при повторе в тот же день), а лицензия DR-free
    запрещает систематический сбор. Запомненный DR не выбрасывается: домен с DR >= порога из
    памяти проходит без запроса — иначе отмена или рестарт воркера между DR и записью похоронили
    бы ценный дроп на 4 суток.

    Пропущено («DR недоступен»):
      · пачка, на которой Ahrefs упал, — её домены не сохраняем и НЕ запоминаем: без DR нечем
        отличить ценный дроп от спам-мусора, а следующий прогон спросит снова;
      · пачка, на которую пришёл 200 без ЕДИНОГО спрошенного домена (пустое или чужое тело), — тоже
        сбой пачки, не запоминаем: иначе она похоронена на 4 суток (находка R2-7);
      · 401/403 — ключ не принят, остальные пачки ответят так же: опрос прекращается, весь остаток
        пропущен; пустой ключ у клиента — в Ahrefs не ходим вовсе (находка R2-8);
      · домен, которого нет в непустом ответе (аномалия: на несуществующий домен Ahrefs отдаёт 0.0), —
        он запоминается с dr=None, чтобы не переспрашивать его каждый час.
    Между пачками — пауза и проверка «стопа»: DropCatch даёт ~134 пачки за прогон.
    """
    from app.services import jobs
    now = _now()
    memo = _dr_memo(names, now - _DR_MEMORY)
    kept = {d: float(dr) for d, dr in memo.items() if dr is not None and float(dr) >= min_dr}
    ask = [n for n in names if n not in memo]
    if ask and getattr(ahrefs, "api_key", None) == "":     # у фейков тестов атрибута нет
        logger.warning("DR-фильтр: AHREFS_API_KEY пуст — %d доменов без DR пропущены", len(ask))
        return kept, len(ask), set(memo)
    skipped = 0
    for i in range(0, len(ask), _DR_BATCH):
        if jobs.cancelled(run):
            raise jobs.Cancelled()
        jobs.report(run, done=i, total=len(ask), current=f"DR: {i} из {len(ask)}")
        if i:
            _sleep(_DR_PAUSE)
        part = ask[i:i + _DR_BATCH]
        try:
            got = _dr_once(ahrefs, part)
        except Exception as e:  # noqa: BLE001 — пачка без DR пропускается, прогон идёт дальше
            code = e.response.status_code if isinstance(e, httpx.HTTPStatusError) else None
            if code in (401, 403):
                logger.warning("DR-фильтр: Ahrefs %s — ключ не принят, остаток %d пропущен",
                               code, len(ask) - i)
                skipped += len(ask) - i
                break
            logger.warning("DR-фильтр: пачка из %d пропущена (%s)", len(part), type(e).__name__)
            skipped += len(part)
            continue
        want = set(part)
        drs = {d: float(dr) for d, dr in got.items() if d in want}
        if not drs:
            logger.warning("DR-фильтр: в ответе нет ни одного из %d спрошенных — пачка пропущена",
                           len(part))
            skipped += len(part)
            continue
        skipped += len(want - set(drs))
        kept.update({d: dr for d, dr in drs.items() if dr >= min_dr})
        _dr_remember(part, drs, now)
    return kept, skipped, set(memo)


def _new_domain(name: str, c: dict, dr):
    from app.models.domain import Domain
    return Domain(domain=name, source=c.get("source"), lane=c.get("lane"),
                  acquire_deadline=c.get("acquire_deadline"), market_lang=c.get("market_lang"),
                  dr=dr)


def _insert(names: list, cand: dict, drs: dict, run=None) -> int:
    """Вставка чанками; прогресс и «стоп» — между чанками (записанное остаётся). Гонка с
    параллельным прогоном (unique на domain) — откат чанка, перечитать известные и досыпать
    остаток (одной повторной попытки хватает)."""
    from sqlalchemy.exc import IntegrityError
    from app.db import SessionLocal
    from app.services import jobs
    n = 0
    for i in range(0, len(names), _CHUNK):
        if jobs.cancelled(run):
            raise jobs.Cancelled()
        jobs.report(run, done=i, total=len(names), current=f"запись: {i} из {len(names)}")
        part = names[i:i + _CHUNK]
        with SessionLocal() as db:
            db.add_all([_new_domain(x, cand[x], drs.get(x)) for x in part])
            try:
                db.commit()
                n += len(part)
            except IntegrityError:
                db.rollback()
                seen = _known(db, part)
                rest = [x for x in part if x not in seen]
                db.add_all([_new_domain(x, cand[x], drs.get(x)) for x in rest])
                db.commit()
                n += len(rest)
    return n


def _enrich(known: dict, cand: dict) -> None:
    """Уже известный, ещё НЕ обработанный домен дозаполняется тем, чего у него не было (лейн,
    дедлайн, язык EMD). Статус и прочее не трогаем: повторный прогон не откатывает решённое."""
    for name, d in known.items():
        if d.status != "discovered":
            continue
        c = cand[name]
        for attr in ("lane", "acquire_deadline", "market_lang"):
            if getattr(d, attr) is None and c.get(attr) is not None:
                setattr(d, attr, c[attr])


def _line(src: str, s: dict, min_dr: float) -> str:
    if src == "emd":
        return f"EMD: {s['rows']} вариантов → новых {s['new']}"
    return (f"{_SOURCE_RU[src]}: {s['rows']} строк → наши зоны {s['zone']} → новых {s['new']}"
            f" → DR≥{min_dr:g}: {s['saved']}")


def run_discovery() -> int:
    """Собрать включённые источники и записать новых кандидатов. Прогресс — через jobs.track
    (видно и когда зовёт оркестратор из воркера). Возвращает, сколько доменов вставлено."""
    from app.db import SessionLocal
    from app.integrations.ahrefs import AhrefsClient
    from app.services import jobs
    from app.services.settings import get_settings

    st = get_settings()
    enabled, min_dr = st["sources_enabled"], float(st["min_dr"])
    on = [k for k in (*AUTO_SOURCES, "emd") if enabled.get(k)]
    stages = ([{"key": k, "label": _SOURCE_RU[k]} for k in on]
              + [{"key": "dr", "label": "DR-фильтр"}, {"key": "save", "label": "запись"}])
    with jobs.track("discovery", stages=stages) as run:
        _dr_purge()
        by_src, failed = _collect(enabled, st, run)
        fails = [f"{_SOURCE_RU[k]}: упал ({v})" for k, v in failed.items()]
        cand, stats, emd = {}, {}, set()
        for src, rows in by_src.items():
            s = stats.setdefault(src, {"rows": len(rows), "zone": 0, "new": 0, "saved": 0})
            for r in rows:
                d = canonical_domain(r.get("domain"))
                if not d or (src in AUTO_SOURCES and not tld_match(d, st["tld_allowlist"])):
                    continue
                s["zone"] += 1
                c = cand.setdefault(d, {**r, "domain": d})
                if src == "emd":
                    emd.add(d)
                    c.setdefault("market_lang", r.get("market_lang"))
        if not cand:
            jobs.report(run, done=0, total=0, current="",
                        message=" · ".join(["нет кандидатов", *fails]))
            return 0
        with SessionLocal() as db:
            known = _known(db, list(cand))
            _enrich(known, cand)
            db.commit()
        fresh = [n for n in cand if n not in known]
        # EMD-имя, которое нашёл и автоматический источник, DR-фильтр не проходит: у новорега и
        # свободного EMD ссылок и не должно быть, DR 0 — не повод его терять
        auto = [n for n in fresh if cand[n]["source"] in AUTO_SOURCES and n not in emd]
        jobs.report(run, stage="dr", current=f"DR для {len(auto)} новых")
        drs, skipped, remembered = (_dr_filter(auto, min_dr, AhrefsClient(), run)
                                    if auto else ({}, 0, set()))
        for n in fresh:
            if n not in remembered:              # «новых» = не известных и не спрошенных за 4 суток
                stats[cand[n]["source"]]["new"] += 1
        keep = [n for n in fresh if n not in auto or n in drs]
        jobs.report(run, stage="save", current=f"запись {len(keep)}")
        inserted = _insert(keep, cand, drs, run)
        for n in keep:
            stats[cand[n]["source"]]["saved"] += 1
        msg = " · ".join(_line(src, s, min_dr) for src, s in stats.items())
        if remembered:
            msg += f" · DR из памяти (4 сут) — {len(remembered)}"
        if skipped:
            msg += f" · DR недоступен — {skipped} пропущено"
        jobs.report(run, done=1, total=1, current="", message=" · ".join([msg, *fails]))
        return inserted


def add_list(text: str) -> dict:
    """Ручной список оператора (то, что он нашёл в ExpiredDomains/SpamZilla — их автоматизировать
    запрещено их же правилами) -> discovered, source='list'. Зону и бренды здесь НЕ режем: домен
    дойдёт до W0 и получит tld_closed/trademark — оператор увидит причину, а не тихую потерю.
    Больше _LIST_MAX за раз не берём, но и не молчим: сколько отброшено — в `cut`."""
    from app.db import SessionLocal
    tokens = [t for t in re.split(r"[\s,;]+", text or "") if t]
    bad, names = 0, []
    for x in tokens[:_LIST_MAX]:
        d = canonical_domain(x)
        if d is None:
            bad += 1
        elif d not in names:
            names.append(d)
    with SessionLocal() as db:
        known = _known(db, names)
    fresh = [n for n in names if n not in known]
    added = _insert(fresh, {n: {"source": "list"} for n in fresh}, {})
    return {"added": added, "known": len(names) - len(fresh), "bad": bad,
            "cut": max(0, len(tokens) - _LIST_MAX)}
```

В `backend/app/services/scoring_config.py` строку `SOURCES_ENABLED = {"backorder": True, …}` заменить на:

```python
SOURCES_ENABLED = {"dropcatch": False, "nominet": True, "mx": True, "emd": True}  # dropcatch — после проверки ToS оператором
```

В `backend/app/services/settings.py`, в `get_settings()`, строку
`"sources_enabled": dict(r.sources_enabled or cfg.SOURCES_ENABLED),` заменить так, чтобы устаревшие
ключи в БД (backorder/cctld/…) не выключали молча новые источники:

```python
            "sources_enabled": {k: bool((r.sources_enabled or {}).get(k, v))
                                for k, v in cfg.SOURCES_ENABLED.items()},
```

В `backend/tests/conftest.py` фикстуру `_default_sources_backorder_only` заменить целиком:

```python
@pytest.fixture(autouse=True)
def _default_sources_offline(sqlite_db, monkeypatch):
    """Структурный офлайн-гвард (финальное ревью v1, Finding 4): по умолчанию сид настроек видит
    ВСЕ источники v2 выключенными — все они сетевые (DropCatch, Nominet, registry.mx), а EMD без
    наборов пуст, — чтобы тест run_discovery() не мог тихо уйти в живую сеть. Достигается
    монки-патчем самого дефолта в scoring_config (не отдельным update_settings-вызовом), поэтому
    test_settings.py::test_get_settings_seeds_defaults (сверяет seed с cfg.SOURCES_ENABLED)
    остаётся верным — обе стороны сравнения видят один и тот же патченный дефолт. Тесты, которым
    нужны источники, сами зовут update_settings(sources_enabled=...) и подменяют
    discovery._clients. Зависимость от sqlite_db — только порядок фикстур."""
    from app.services import scoring_config as cfg
    monkeypatch.setattr(cfg, "SOURCES_ENABLED",
                        {"dropcatch": False, "nominet": False, "mx": False, "emd": False})
    yield
```

**Диагностика.** В `backend/app/services/diagnostics.py`, в `_spec()`, удалить две строки записи
`backorder` (роль «M1 · discovery»):

```python
        ("backorder", "Backorder", "M1 · discovery", "1", "M1", True,  # публичный фид, кред не нужен
         lambda: __import__("app.integrations.backorder", fromlist=["x"]).BackorderClient().ping()),
```

Фид backorder больше не источник discovery, поэтому из `/diag` его убираем. `BackorderClient.ping()`
самостоятельный (ходит в фид сам, не через `list_dropping`) и остаётся в клиенте — клиент нужен для
выкупа (подпроект 2).

**Панель.** В `panel.py::settings_save` строки параметров

```python
                  backorder: str = Form(""), cctld: str = Form(""),
                  reg_ru: str = Form(""), sweb: str = Form(""),
```

заменить на

```python
                  dropcatch: str = Form(""), nominet: str = Form(""),
                  mx: str = Form(""), emd: str = Form(""),
```

а в вызове `st.update_settings(...)` аргумент `sources_enabled={"backorder": …, "sweb": bool(sweb)}` —
на `sources_enabled={"dropcatch": bool(dropcatch), "nominet": bool(nominet), "mx": bool(mx), "emd": bool(emd)},`.

В `settings.html` заменить целиком блок `<div class="station">…</div>` с плашкой
`Источники дропов — <b>какие списки собирает Discovery</b>` (он стоит перед станцией «Применить»;
внутри четыре чекбокса `backorder`/`cctld`/`reg_ru`/`sweb`) на:

```html
  <div class="station">
    <div class="plate">Источники доменов — <b>что собирает Discovery</b></div>
    <details class="what"><summary>зачем это</summary>
      <div class="what-body">Автоматические источники (DropCatch, Nominet, registry.mx) проходят
      фильтр зон и бесплатный DR Ahrefs ещё на входе: домен с DR ниже порога в базу не попадает.
      EMD генерируется из твоих наборов ключей и DR-фильтр не проходит. Выключенный источник не
      опрашивается; уже найденные с него домены остаются.</div></details>
    <div class="go">
      <label title="pending delete .com/.net/.org — ~134 тыс. строк в день"><input type="checkbox" name="dropcatch" {{ 'checked' if s.sources_enabled.dropcatch }}> DropCatch <span class="hint">(включать после проверки ToS)</span></label>
      <label title="официальный список дропов .uk/.co.uk"><input type="checkbox" name="nominet" {{ 'checked' if s.sources_enabled.nominet }}> Nominet</label>
      <label title="удалённые .mx — свободны к регистрации"><input type="checkbox" name="mx" {{ 'checked' if s.sources_enabled.mx }}> registry.mx</label>
      <label title="генератор EMD по наборам ключей ниже"><input type="checkbox" name="emd" {{ 'checked' if s.sources_enabled.emd }}> EMD</label>
    </div>
  </div>
```

**Ручной список.** В `panel.py` добавить роут сразу после `run_discovery_action` (`@router.post("/run/discovery")`):

```python
@router.post("/domains/add-list")
def domains_add_list(domains: str = Form("")):
    from app.services import discovery
    r = discovery.add_list(domains)
    cut = f", сверх {discovery._LIST_MAX} за раз отброшено {r['cut']}" if r["cut"] else ""
    return _back("/domains/pool", msg=f"Добавлено {r['added']}, уже были {r['known']}, "
                                      f"не домены {r['bad']}{cut} — новые оценятся при «Оценить домены»")
```

В `pool.html` перед строкой `<div class="wrap">` (над таблицей) вставить форму в языке станций:

```html
<div class="station">
  <div class="plate">Добавить домены списком — <b>то, что нашёл руками</b></div>
  <details class="what"><summary>зачем это</summary>
    <div class="what-body">ExpiredDomains.net и SpamZilla запрещают ботов — там ищешь сам и
    вставляешь сюда (по одному в строке или через запятую, до 5000 за раз). Домены попадут в пул как
    «найден» и пройдут обычную воронку: чужая зона или бренд в имени — честный отказ с причиной.</div></details>
  <form class="go" method="post" action="/domains/add-list">
    <textarea name="domains" rows="4" style="flex:1 1 320px" placeholder="example.com&#10;mejorvpn.mx"></textarea>
    <button class="btn-acc" title="добавить в пул со статусом «найден»">Добавить</button>
  </form>
</div>
```

**Удаление РФ-источников.**
- Удалить файлы `backend/app/integrations/cctld.py`, `regru_drops.py`, `sweb_drops.py`.
- В `backend/app/integrations/backorder.py` удалить метод `list_dropping` целиком вместе со строкой-
  разделителем над ним `# -- discovery (публичный фид, без auth) ---…`. Других потребителей у него
  нет (старый discovery и тесты из списка ниже); `ping()` не трогать.
- `test_fresh_install.py` не трогать: он уже сравнивает сид миграции 0002 с литералом v1, а не с
  `cfg.SOURCES_ENABLED`.

- [ ] **Шаг 4: Запустить тесты задачи — проходят**

Запуск: `cd backend && ../.venv/bin/python -m pytest tests/test_discovery_v2.py -v`
Ожидание: 23 passed.

- [ ] **Шаг 5: Старые тесты, которые ломает задача — явный список**

Удалить (сценарии удалённого РФ-кода):
- `backend/tests/test_sources.py`: `test_run_discovery_dedups_across_sources`,
  `test_normalize_row_captures_acquirability`, `test_run_discovery_persists_acquirability`,
  `test_normalize_row_keeps_rf`, `test_existing_discovered_row_enriched_on_rediscovery`,
  `test_existing_discovered_row_feed_flags_refreshed_on_rediscovery`,
  `test_existing_discovered_row_upgraded_to_backorder_source_and_price`,
  `test_backorder_deadline_overrides_stale_whois_projection`,
  `test_fresh_deadline_not_overwritten_by_second_rediscovery`,
  `test_existing_backorder_source_not_touched_by_upgrade_branch`,
  `test_normalize_row_sentinels_and_price`, `test_list_dropping_string_links_below_min_warns`,
  `test_collect_logs_and_survives_source_failure`, `test_regru_drops_extracts_only_domain_cells`,
  `test_regru_drops_ping`, `test_sweb_drops_excludes_dates_sharing_class_with_domain`,
  `test_sweb_drops_ping`, `test_cctld_downloads_both_zips_and_lists_domains`,
  `test_cctld_partial_zip_failure_logs_and_returns_other`, `test_cctld_ping_true_when_zip_href_found`,
  `test_cctld_ping_false_when_no_zip_href`, `test_cctld_carries_drop_deadline_from_zip_name`,
  `test_cctld_unparsable_zip_name_yields_no_deadline` (23 теста). Удалить и помощник `_make_zip`
  (функция верхнего уровня перед `test_cctld_downloads_both_zips_and_lists_domains`, от
  `def _make_zip(filename: str, lines: list[str]) -> bytes:` до её `return buf.getvalue()`): его звали
  только удалённые cctld-тесты, а pyflakes мёртвую функцию не покажет. Импорты файла
  (`timezone`, `pytest`, `_parse_whois_created`) остаются — ими пользуются оставшиеся тесты. Тесты
  разбора whois A-Parser (`test_whois_*`, `test_parse_whois_available`) и `test_canonical_domain`
  **оставить** (11 тестов): A-Parser whois остаётся для зон без RDAP.
- `backend/tests/test_pipeline.py::test_discovery_upsert_idempotent` — покрыт
  `test_rerun_is_idempotent_and_skips_dr_for_known_and_remembered`.
- `backend/tests/test_pricing.py::test_discovery_insert_uses_zone_matched_cached_price` — цена .РФ при
  вставке backorder-кандидата, в v2 такого пути нет.

Переписать (смысл ассерта сохраняется):

`backend/tests/test_cancel_coverage.py::test_discovery_stops_between_sources_on_cancel` — заменить
функцию целиком:

```python
def test_discovery_stops_between_sources_on_cancel(monkeypatch):
    """Cancel во время первого источника -> второй источник не опрашивается, статус cancelled.

    РЕГРЕССИЯ F18: до фикса `_collect` не звала `jobs.cancelled()` вовсе — второй источник был бы
    опрошен, несмотря на нажатую кнопку, а прогон закрылся бы как `done`.
    """
    from app.services.settings import update_settings
    update_settings(sources_enabled={"dropcatch": True, "nominet": True, "mx": False, "emd": False})

    nominet_calls = []

    class _First:
        def list_dropping(self):
            jobs.request_cancel("discovery")       # человек нажал «стоп» во время первого источника
            return []

    class _Second:
        def list_dropping(self):
            nominet_calls.append(True)             # не должно случиться
            return []

    monkeypatch.setattr(discovery, "_clients", lambda: {"dropcatch": _First, "nominet": _Second})

    discovery.run_discovery()

    assert nominet_calls == []                      # второй источник даже не начали
    p = jobs.progress("discovery")
    assert p["status"] == "cancelled"
    assert p["stage"] == "dropcatch"                 # застыли на источнике, где нажали «стоп»
```

`backend/tests/test_job_stages.py::test_discovery_stages_are_sources` — заменить функцию целиком:

```python
def test_discovery_stages_are_sources(monkeypatch):
    """Чипы discovery — включённые источники + DR-фильтр + запись."""
    from app.services.settings import update_settings
    update_settings(sources_enabled={"dropcatch": False, "nominet": True, "mx": False, "emd": False})

    class _Empty:
        def list_dropping(self):
            return []
    monkeypatch.setattr(discovery, "_clients", lambda: {"nominet": _Empty})
    assert discovery.run_discovery() == 0
    p = jobs.progress("discovery")
    assert p["status"] == "done" and p["error"] is None
    assert [s["key"] for s in p["stages"]] == ["nominet", "dr", "save"]
    assert p["message"] == "нет кандидатов"
```

`backend/tests/test_m1_fixes.py::test_discovery_survives_insert_race` — заменить функцию целиком
(гонка двух discovery на v2-источнике; `_EmptyResult` получает `__iter__`, потому что `_known`
итерирует `.scalars()` напрямую):

```python
def test_discovery_survives_insert_race(monkeypatch):
    from sqlalchemy import select
    from sqlalchemy.orm import Session
    from app.services import discovery
    from app.services.settings import update_settings
    import app.db as db
    from app.models.domain import Domain

    # офлайн-тест бьёт только nominet — остальные источники выключаем, иначе _collect уйдёт в
    # реальную сеть. Прогреваем settings ДО патча Session.execute, чтобы get_settings() внутри
    # run_discovery() не занял "первый" перехваченный вызов случайной строкой настроек.
    update_settings(sources_enabled={"dropcatch": False, "nominet": True, "mx": False, "emd": False})

    # как будто параллельный запуск уже вставил race.co.uk (до нашего COMMIT)
    with db.SessionLocal() as s:
        s.add(Domain(domain="race.co.uk", source="nominet", referring_domains=1))
        s.commit()

    rows = [{"domain": "race.co.uk", "source": "nominet", "lane": "bid", "acquire_deadline": None},
            {"domain": "fresh.co.uk", "source": "nominet", "lane": "bid", "acquire_deadline": None}]

    class _Src:
        def list_dropping(self):
            return list(rows)

    class _Ahrefs:
        def dr_free(self, domains):
            return {d: 10.0 for d in domains}
    monkeypatch.setattr(discovery, "_clients", lambda: {"nominet": _Src})
    monkeypatch.setattr("app.integrations.ahrefs.AhrefsClient", _Ahrefs)

    # ПЕРВЫЙ SELECT именно по domains (known) отдаём пустым (устаревшее чтение) ->
    # код попробует вставить дубль race.co.uk -> IntegrityError; остальные (включая
    # get_settings() внутри run_discovery, dr_seen и повторное чтение known) — настоящие.
    real_execute = Session.execute
    state = {"fired": False}

    class _EmptyResult:
        def scalars(self):
            return self

        def __iter__(self):
            return iter(())

        def all(self):
            return []

    def flaky_execute(self, statement, *a, **k):
        is_domains_select = "FROM domains" in str(statement)
        if is_domains_select and not state["fired"]:
            state["fired"] = True
            return _EmptyResult()
        return real_execute(self, statement, *a, **k)

    monkeypatch.setattr(Session, "execute", flaky_execute)
    inserted = discovery.run_discovery()
    monkeypatch.undo()   # снять патчи перед проверками

    assert inserted == 1   # досыпан только fresh.co.uk — батч не потерян
    with db.SessionLocal() as s:
        names = set(s.execute(select(Domain.domain)).scalars().all())
    assert names == {"race.co.uk", "fresh.co.uk"}
```

`backend/tests/test_settings.py` — две функции, каждая заменяется на своём месте (порядок тестов в
файле не меняется):

1. `test_update_and_reset` — заменить функцию целиком:

```python
def test_update_and_reset():
    st.update_settings(min_age_years=5, approve_at=0.8,
                       sources_enabled={"dropcatch": False, "nominet": True, "mx": False, "emd": False})
    s = st.get_settings()
    assert s["min_age_years"] == 5.0 and s["approve_at"] == 0.8
    assert s["sources_enabled"]["dropcatch"] is False and s["sources_enabled"]["nominet"] is True
    st.reset_settings()
    assert st.get_settings()["min_age_years"] == cfg.MIN_AGE_YEARS
```

2. `test_default_test_sources_are_backorder_only_offline_guard` — заменить функцию целиком на том же
   месте (между `test_update_clamps_out_of_range` и `test_max_whois_per_run_default_and_clamp`); имя
   меняется на `test_default_test_sources_are_off_offline_guard`:

```python
def test_default_test_sources_are_off_offline_guard():
    """Finding 4 (финальное ревью, структурный офлайн-гвард в conftest): без единого явного
    update_settings() дефолт, который видят тесты, — ВСЕ источники v2 выключены (они сетевые),
    чтобы тест discovery.run_discovery() не мог тихо уйти в живую сеть. Ожидание захардкожено
    (не сверяется с cfg.SOURCES_ENABLED), чтобы тест реально проверял конкретный безопасный
    дефолт, а не совпадение с самим патчем."""
    s = st.get_settings()
    assert s["sources_enabled"] == {"dropcatch": False, "nominet": False,
                                    "mx": False, "emd": False}
```

`backend/tests/test_web_fixes.py::test_settings_render_and_save` — заменить функцию целиком:

```python
def test_settings_render_and_save(client):
    assert client.get("/settings").status_code == 200
    r = client.post("/settings/save", data={
        "min_referring_domains": 2, "min_age_years": 4, "approve_at": 0.75,
        "manual_review_at": 0.4, "mx": "on"}, follow_redirects=False)
    assert r.status_code == 303
    from app.services import settings as st
    s = st.get_settings()
    assert s["min_age_years"] == 4.0
    assert s["sources_enabled"]["mx"] is True and s["sources_enabled"]["dropcatch"] is False
```

Проверка, что ссылок на удалённое не осталось (комментарии с упоминанием v1 чистит Задача 16):
`grep -rnE "integrations\.(cctld|regru_drops|sweb_drops)|normalize_row|BackorderClient\.list_dropping|_parse_deadline\(" backend/app backend/tests`
— пустой вывод.

- [ ] **Шаг 6: Весь сьют, линт**

Запуск: `cd backend && ../.venv/bin/python -m pytest tests -q && cd .. && .venv/bin/python -m pyflakes backend/app backend/tests`
Ожидание: **820 passed** (822 + 23 новых − 23 из `test_sources.py` − 2 удалённых), pyflakes пуст.

- [ ] **Шаг 7: Коммит**

```bash
git add -A backend/app backend/tests
git commit -m "feat(v2): discovery — DropCatch/Nominet/registry.mx/EMD, фильтр зон и DR на входе с памятью dr_seen, ручной список"
```

---

### Задача 7: `_run_waves` — цикл по таблице волн (рефакторинг без смены поведения)

Сейчас каждая волна — копипаста блока «волна → отмена → checkpoint → водопад → report». Волн станет 6,
поэтому сначала цикл, потом новые волны. **Поведение не меняется**, существующие тесты проходят без
правок. Одно сознательное изменение текста: подпись последней волны в водопаде была `ahrefs: N решено`
(считала только выживших) — становится `ahrefs: N → M`, как у остальных волн (находка 4.11): в
Задаче 13 последней станет W6, которая отклоняет `spam_anchors`, и «решено» врало бы.

**Files:**
- Modify: `backend/app/services/scoring.py` (функция `_run_waves`)
- Test: `backend/tests/test_scoring_waves.py` (добавить один тест)

**Interfaces:**
- Produces: `_run_waves(states, clients, st, whois_budget, ahrefs_budget, run) -> list` — сигнатура
  прежняя. Таблица волн — список кортежей `(stage_key, waterfall_label, fn(alive))`, порядок
  совпадает с `FUNNEL_STAGES`. После КАЖДОЙ волны (включая последнюю) — `jobs.report(run,
  message=…, stage_key=key, stage_before=before, stage_after=after)`, подпись водопада
  `f"{label}: {before} → {after}"`.
- Тест `test_run_waves_reports_every_funnel_stage_in_order` не зависит от набора волн: подменяет
  пустышками ВСЕ `scoring._wave_*` и `_commit_result`, зовёт `_run_waves` позиционно
  `(states, clients, st, None, None, None)` с клиентами-пустышками (любой метод → `None`) и
  `st = {**get_settings(), "units_floor": 0}`, сравнивает последовательность `stage_key` из
  `jobs.report` с `FUNNEL_STAGES`. Задачам 9–13 его править не нужно, пока `_run_waves` принимает
  первые шесть аргументов в этом порядке, а `FunnelState` — поля
  `domain_id, domain, lane, referring_domains, acquire_deadline, feed_flags`.

- [ ] **Шаг 1: Написать тест на единый источник порядка** — дописать в конец
  `backend/tests/test_scoring_waves.py`

```python
def test_run_waves_reports_every_funnel_stage_in_order(monkeypatch):
    """Порядок чипов (FUNNEL_STAGES) и порядок волн — одно и то же: стадия, о которой волна не
    отчиталась, висела бы на панели «ожидает» вечно. Порядок проверяется по последовательности
    stage_key в jobs.report. Волны и финализация подменены пустышками — тест про конвейер, а не
    про сигналы, поэтому переживает смену набора волн (Задачи 9–13)."""
    from collections import defaultdict
    from app.services import jobs
    from app.services.settings import get_settings

    class _Quiet:
        """Клиент-пустышка: любой метод отвечает None (волны подменены, сеть не нужна)."""
        def __getattr__(self, name):
            return lambda *a, **k: None

    for name in [n for n in dir(scoring) if n.startswith("_wave_")]:
        monkeypatch.setattr(scoring, name, lambda alive, *a, **k: None)
    monkeypatch.setattr(scoring, "_commit_result", lambda s, run, st: {"domain": s.domain})
    real, keys = jobs.report, []

    def spy(run_id, **kw):
        if kw.get("stage_key"):
            keys.append(kw["stage_key"])
        return real(run_id, **kw)
    monkeypatch.setattr(jobs, "report", spy)
    states = [scoring.FunnelState(domain_id=i, domain=f"ord{i}.com", lane="bid", referring_domains=5,
                                  acquire_deadline=None, feed_flags=None) for i in range(2)]
    st = {**get_settings(), "units_floor": 0}
    out = scoring._run_waves(states, defaultdict(_Quiet), st, None, None, None)
    assert keys == [s["key"] for s in scoring.FUNNEL_STAGES]
    assert [r["domain"] for r in out] == ["ord0.com", "ord1.com"]
```

- [ ] **Шаг 2: Запустить — тест должен ПРОЙТИ уже сейчас** (фиксирует текущее поведение)

Запуск: `cd backend && ../.venv/bin/python -m pytest tests/test_scoring_waves.py -v`
Ожидание: PASS (всё в файле). Если FAIL — понять, чего не хватает, до рефакторинга.

- [ ] **Шаг 3: Рефакторинг** — в `_run_waves` заменить всё от строки `results = []` до `return results`
  включительно (пять развёрнутых блоков волн) на:

```python
    # (ключ чипа, подпись в водопаде, волна). Порядок = порядок FUNNEL_STAGES — один источник
    # правды: новая волна добавляется ОДНОЙ строкой здесь и одной в FUNNEL_STAGES.
    waves = [
        ("rd", "RD", lambda alive: _wave_t0(alive, st)),
        ("whois", "whois", lambda alive: _wave_whois(alive, clients, whois_b, st, run)),
        ("risk", "risk", lambda alive: _wave_risk(alive, clients, run)),
        ("history", "history", lambda alive: _wave_history(alive, clients, st, run)),
        ("ahrefs", "ahrefs", lambda alive: _wave_ahrefs(alive, clients, ahrefs_b, run)),
    ]
    results, waterfall, alive = [], [], list(states)
    for i, (key, label, wave) in enumerate(waves):
        before = len(alive)
        wave(alive)
        if jobs.cancelled(run):
            raise jobs.Cancelled()
        if i < len(waves) - 1:
            results += _checkpoint(alive, run, st)
            alive = [s for s in alive if s.alive]
            after = len(alive)
        else:
            # ПОСЛЕДНЯЯ волна: финализируем ВСЕХ, кто в неё вошёл. _commit_result сам различает
            # отказ / unresolved / скор — вышедшие на ней и выжившие решаются одним путём.
            results += [_commit_result(s, run, st) for s in alive]
            after = sum(1 for s in alive if s.alive)
        # та же подпись и у последней волны: «N решено» считало бы только выживших
        waterfall.append(f"{label}: {before} → {after}")
        jobs.report(run, message=" · ".join(waterfall),
                    stage_key=key, stage_before=before, stage_after=after)
    return results
```

Докстринг `_run_waves` заменить целиком:

```python
    """Оркестратор: волны по порядку дёшево->дорого, между каждой — checkpoint (коммит
    вышедших, отчёт волновой истории), отмена проверяется между волнами (внутри волны —
    в _run_concurrent). Волны — таблица `waves` (ключ чипа, подпись водопада, функция) в
    порядке FUNNEL_STAGES; цикл один на всех. Выжившие после ПОСЛЕДНЕЙ волны финализируются
    как решённые — см. _commit_result. Возвращает результаты в порядке завершения (порядок
    не важен вызывающим — score_pending считает только длину, score_domain — единственный
    элемент списка)."""
```

- [ ] **Шаг 4: Весь сьют + линт** — без единой правки старых тестов

Запуск: `cd backend && ../.venv/bin/python -m pytest tests -q && cd .. && .venv/bin/python -m pyflakes backend/app backend/tests`
Ожидание: **821 passed** (820 + 1), pyflakes пуст; в том числе
`test_run_waves_shrinks_pool_across_stages_and_writes_wave_history`.

- [ ] **Шаг 5: Коммит**

```bash
git add backend/app/services/scoring.py backend/tests/test_scoring_waves.py
git commit -m "refactor(scoring): _run_waves — цикл по таблице волн, поведение без изменений"
```

---

### Задача 8: `compute_score` v2 — новые компоненты, веса, штраф PBN; авто-одобрения нет

Чистая функция переходит на компоненты v2 раньше волн. Отсутствующий сигнал даёт нейтральные
0.5 (тема, анкоры, трафик), поэтому старые волны продолжают работать: просто без этих сигналов.

**Авто-одобрения нет (решение оператора Р2, инвариант 9 мастер-спеки).** `_decide` отдаёт только
`scored`/`rejected`; `approved` ставит только человек (кнопка или пакет). Гарды, которые жили в
`_decide` (Wayback не проверен, whois/РКН/блэклист упали, возраст неизвестен), переезжают в
пакетное одобрение: `bulk_ok` их уже видит через `blind_reason`, кроме «возраст неизвестен» — эту
строку `blind_reason` получает в ЭТОЙ же задаче, чтобы не было окна, когда такой домен проходит в
пакет. `approve_at` остаётся в БД и в `/settings` (новый смысл — «порог сильного кандидата», подпись
меняет Задача 14), в `_decide` не участвует.

**Files:**
- Modify:
  - `backend/app/services/scoring.py` — `_decide`, `compute_score`, вызов `_decide` в `_commit_result`,
    `blind_reason` (строка про возраст), комментарии про авто-одобрение, self-check в `__main__`;
  - `backend/app/services/scoring_config.py` — `WEIGHTS`, `NORM`, `PBN_*`, комментарий о жёстких отказах;
  - `backend/app/api/panel.py` — `settings_save`, веса;
  - `backend/app/templates/settings.html` — станция весов и `W_KEYS`.
- Test: `backend/tests/test_compute_score_v2.py`. Старые тесты — явный список в шаге 5.

**Interfaces:**
- Produces:
  - `_decide(score: float, sig: dict, manual_review_at: float) -> str` — `"scored"` если
    `score >= manual_review_at`, иначе `"rejected"`. Параметра `approve_at` больше нет;
  - `compute_score(sig, weights=None) -> {"score", "status", "breakdown"}`, где `status` ∈
    {`scored`, `rejected`}, `breakdown = {"components": {7 ключей}, "weights", "pbn_suspect": bool}`
    (жёсткий отказ — `{"hard_reject": [коды]}`, коды `blacklisted`/`webrisk`/`trademark`/`prior_<кат>`);
  - читаемые ключи `sig`: `prior_flags, wayback_checked, blacklisted, webrisk_threats,
    trademark_risk, topical_relevance, age_years, referring_domains, ref_subnets, dr,
    spam_anchor_ratio, peak_traffic`. `rkn_listed` compute_score больше не читает (РКН-отказ пока
    ставит волна risk, саму проверку удаляет Задача 10);
  - `blind_reason(d)` — новая последняя ветка: `d.whois_created`, `d.first_seen` и `d.age_years` все
    `None` -> `"возраст НЕ проверен: возраста нет ни из RDAP/whois, ни из архива"` (значит
    `bulk_ok(d)` ложно). Задача 13 дописывает в `bulk_ok` анкоры и тему; эту ветку не дублировать;
  - `cfg.WEIGHTS` (7 ключей: `history_cleanliness, topical_fit, age, rd, authority, anchor_quality,
    traffic_history`), `cfg.NORM["TRAFFIC_FULL"]`, `cfg.PBN_SUBNET_RATIO = 0.3`, `cfg.PBN_MIN_RD = 20`;
  - форма `/settings/save` принимает `w_history_cleanliness, w_topical_fit, w_age, w_rd, w_authority,
    w_anchor_quality, w_traffic_history`.
- Тесты, которым нужен «чистый» домен в пакете (`bulk_ok` истинно), обязаны давать ему возраст
  (`age_years`, `whois_created` или `first_seen`) — фикстуры задач 9–15 это учитывают.

- [ ] **Шаг 1: Написать падающий тест** — `backend/tests/test_compute_score_v2.py`

```python
"""compute_score v2: компоненты, нейтральные значения, жёсткие отказы, PBN, живой спам-дроп;
авто-одобрения нет (Р2) — гард «возраст неизвестен» переехал в пакет."""
from datetime import datetime, timezone

import pytest

from app.models.domain import Domain
from app.services import scoring_config as cfg
from app.services.scoring import _decide, blind_reason, bulk_ok, compute_score

BASE = {"wayback_checked": True, "prior_flags": {}, "age_years": 10.0, "deep_checked": True}


def test_weights_sum_to_one_and_keys():
    assert set(cfg.WEIGHTS) == {"history_cleanliness", "topical_fit", "age", "rd", "authority",
                                "anchor_quality", "traffic_history"}
    assert abs(sum(cfg.WEIGHTS.values()) - 1.0) < 1e-9


@pytest.mark.parametrize("sig,code", [
    ({"blacklisted": True}, "blacklisted"),
    ({"webrisk_threats": ["MALWARE"]}, "webrisk"),
    ({"trademark_risk": True}, "trademark"),
    ({"prior_flags": {"casino": True}}, "prior_casino"),
])
def test_hard_rejects(sig, code):
    r = compute_score({**BASE, **sig})
    assert r["status"] == "rejected" and r["score"] == 0.0 and code in r["breakdown"]["hard_reject"]


def test_missing_soft_signals_are_neutral():
    comp = compute_score(dict(BASE))["breakdown"]["components"]
    assert comp["topical_fit"] == 0.5 and comp["anchor_quality"] == 0.5 and comp["traffic_history"] == 0.5
    assert comp["rd"] == 0.0 and comp["authority"] == 0.0


def test_pbn_suspect_halves_rd():
    clean = compute_score({**BASE, "referring_domains": 717, "ref_subnets": 600})
    pbn = compute_score({**BASE, "referring_domains": 717, "ref_subnets": 198})
    assert pbn["breakdown"]["pbn_suspect"] is True and clean["breakdown"]["pbn_suspect"] is False
    assert abs(pbn["breakdown"]["components"]["rd"] * 2 - clean["breakdown"]["components"]["rd"]) < 1e-9


def test_pbn_penalty_keeps_live_spam_drop_below_strong_threshold():
    """Живой профиль 2026-10-01 (pharmaindustrie.com): RD 717, подсети 198 — спам-сетка. DR 5 —
    ровно такой домен проходит фильтр DR на входе discovery. Без штрафа PBN он взял бы порог
    сильного кандидата approve_at (0.7095), со штрафом — нет (0.6356)."""
    sig = {**BASE, "dr": 5.0, "referring_domains": 717}
    pbn = compute_score({**sig, "ref_subnets": 198})
    clean = compute_score({**sig, "ref_subnets": 600})
    assert pbn["breakdown"]["pbn_suspect"] is True
    assert pbn["score"] == pytest.approx(0.6356, abs=1e-4)
    assert clean["score"] == pytest.approx(0.7095, abs=1e-4)
    assert pbn["score"] < cfg.DECISION["approve_at"] <= clean["score"]


def test_strong_clean_domain_scores_high_but_is_only_scored():
    r = compute_score({**BASE, "dr": 35.0, "referring_domains": 900, "ref_subnets": 700,
                       "topical_relevance": 0.9, "spam_anchor_ratio": 0.02, "peak_traffic": 4000})
    assert r["score"] >= 0.85 and r["status"] == "scored"           # одобряет только человек (Р2)


def test_decide_never_approves_only_scored_or_rejected():
    """Р2: авто-одобрения нет вообще — даже максимальный балл со всеми проверками даёт `scored`."""
    assert _decide(1.0, {"wayback_checked": True, "errors": [], "age_years": 20}, 0.4) == "scored"
    assert _decide(0.4, {}, 0.4) == "scored"
    assert _decide(0.39, {}, 0.4) == "rejected"


def test_domain_without_any_age_is_blind_and_out_of_bulk():
    """Гард «возраст неизвестен» жил в _decide; авто-одобрения больше нет (Р2), и его держит
    пакет: возраста не дал никто (ни RDAP/whois, ни архив) — домен «вслепую», в пакет не идёт."""
    kw = dict(domain="noage.com", wayback_checked=True, prior_flags={},
              score_breakdown={"errors": [], "history_evidence": []})
    d = Domain(**kw)
    assert blind_reason(d) == "возраст НЕ проверен: возраста нет ни из RDAP/whois, ни из архива"
    assert bulk_ok(d) is False
    reg = datetime(2010, 1, 1, tzinfo=timezone.utc)
    assert blind_reason(Domain(**kw, whois_created=reg)) is None     # возраст из RDAP/whois
    assert blind_reason(Domain(**kw, first_seen=reg)) is None        # возраст из архива
    assert bulk_ok(Domain(**kw, age_years=9.0)) is True
```

- [ ] **Шаг 2: Запустить — убедиться, что падает**

Запуск: `cd backend && ../.venv/bin/python -m pytest tests/test_compute_score_v2.py -v`
Ожидание: импорт проходит, `9 failed, 2 passed`: `test_weights_sum_to_one_and_keys` (старые ключи),
`test_hard_rejects[sig1/sig2]` (нет отказов `webrisk`/`trademark`), `test_missing_soft_signals_are_neutral`,
`test_pbn_*` (KeyError/нет `pbn_suspect`), `test_strong_clean_…` (`approved`), `test_decide_…`
(`TypeError`: старая сигнатура), `test_domain_without_any_age_…` (`blind_reason` — `None`). Проходят
`test_hard_rejects[sig0]` и `test_hard_rejects[sig3]` (эти отказы были и в v1).

- [ ] **Шаг 3: Реализовать**

`scoring_config.py`:

1) Комментарий под `HARD_REJECT_FLAGS` — четыре строки от `# also hard-reject on: rkn_listed, blacklisted is True.`
до `# одного производителя. Проверка, которой нет, не должна выглядеть работающей — см. compute_score.`
включительно — заменить на:

```python
# also hard-reject on: blacklisted is True, webrisk_threats (Google Web Risk), trademark_risk
# (бренд-токен в имени — его ставит W0 по domain_filters.brand_hit). В v1 `trademark_risk` был
# призраком (ни одного производителя, аудит 2026-07-14), в v2 производитель есть. `topic_switch`
# удалён насовсем: подмножество категорий выше, не мог добавить ни одного отказа.
```

2) Всё от строки `# Stage F — composite weights (positives; sum = 1.0). …` до строки
`# Decision thresholds on final score (0..1). …` (не включая её) — то есть старые `WEIGHTS` и `NORM`
с комментариями — заменить на:

```python
# Stage F v2 — сумма 1.0 (docs/v2/02-m1-discovery-scoring-spec.md §3.2). Нет сигнала у
# topical_fit/anchor_quality/traffic_history -> 0.5 (нейтрально), см. compute_score.
WEIGHTS = {
    "history_cleanliness": 0.25,  # Wayback: проверена и чиста
    "topical_fit": 0.15,          # W5 LLM: близость прошлой темы к VPN/приватности/софту (expired domain abuse)
    "age": 0.12,
    "rd": 0.18,                   # W4 refdomains, лог-шкала, ×0.5 при подозрении на PBN
    "authority": 0.10,            # DR — главный честный сигнал на спам-дропах
    "anchor_quality": 0.12,       # W6: 1 − доля спам-анкоров
    "traffic_history": 0.08,      # W6: пик органического трафика за 5 лет
}
NORM = {"DR_FULL": 30.0, "AGE_FULL": 8.0, "RD_FULL": 3000.0, "TRAFFIC_FULL": 5000.0}
# PBN/спам-сетка: живые спам-дропы дают refips_subnets/refdomains ≈ 0.27 (2026-10-01).
# ponytail: порог стартовый — калибровать по водопаду первого живого прогона.
PBN_SUBNET_RATIO = 0.3
PBN_MIN_RD = 20

```

`scoring.py`:

1) В докстринге модуля строку `-> composite score + breakdown -> status approved | scored(manual) | rejected.`
заменить на `-> composite score + breakdown -> status scored | rejected (\`approved\` ставит только человек).`

2) Три строки комментария над `_BLIND_RU`, от `# Проверки, чей отказ означает «домен судили ВСЛЕПУЮ». Гарды в _decide не дают авто-approve`
до `# проверенного. Человек штампует непроверенное, думая, что машина посмотрела историю.` включительно,
заменить на:

```python
# Проверки, чей отказ означает «домен судили ВСЛЕПУЮ». Авто-одобрения нет (Р2): любой домен
# уходит в scored, то есть В ИНБОКС К ЧЕЛОВЕКУ, и без пометки там неотличим от честно
# проверенного. Человек штампует непроверенное, думая, что машина посмотрела историю, — поэтому
# пометка «вслепую» ещё и исключает домен из пакета (bulk_ok).
```

3) В `blind_reason` после цикла `for e in errors: …` (перед финальным `return None`) вставить:

```python
    # Возраста не дал никто: ни RDAP/whois (`whois_created`), ни архив (`first_seen`/`age_years`)
    # — гейт «слишком молодой» не применялся ни разу. Раньше это держал гард _decide; авто-
    # одобрения больше нет (Р2), и единственная защита — пакет такой домен не берёт.
    if d.whois_created is None and d.first_seen is None and d.age_years is None:
        return "возраст НЕ проверен: возраста нет ни из RDAP/whois, ни из архива"
```

4) В докстринге `history_note` две строки

```
    авто-approve по-прежнему гардится по `sig` ТЕКУЩЕГО прогона (`_decide`) — машина такой домен
    сама не одобрит. Запирать его от пакетного одобрения из-за ТРАНЗИЕНТНОГО сбоя архива значило
```

заменить на

```
    сама машина не одобряет ничего (Р2) — решает человек, видя эту пометку. Запирать домен от
    пакетного одобрения из-за ТРАНЗИЕНТНОГО сбоя архива значило
```

5) Функцию `_decide` заменить целиком:

```python
def _decide(score: float, sig: dict, manual_review_at: float) -> str:
    """Pure: score -> 'scored' | 'rejected'. АВТО-ОДОБРЕНИЯ НЕТ (решение оператора Р2,
    инвариант 9 мастер-спеки): `approved` ставит только человек — кнопкой или пакетом.

    Гарды, которые раньше жили здесь (Wayback не проверен, whois/РКН/блэклист упали, возраст
    неизвестен), переехали в bulk_ok через blind_reason: домен с такой дырой доезжает до
    инбокса как `scored` с пометкой «оценён вслепую», и пакет его не берёт. `sig` в сигнатуре
    оставлен: compute_score и _commit_result решают одним вызовом."""
    return "scored" if score >= manual_review_at else "rejected"
```

6) Функцию `compute_score` заменить целиком (вместе с её комментарием о «призраках» `trademark_risk`/
`topic_switch` — он устарел):

```python
def compute_score(sig: dict, weights: dict | None = None) -> dict:
    """Pure: signals -> {score, status, breakdown}. No I/O. v2 — docs/v2/02-…-spec.md §3.2.

    `weights` — рантайм-веса с /settings (None -> scoring_config.WEIGHTS); нормируем на сумму,
    чтобы шкала 0..1 и пороги не «плыли» от сдвига одного ползунка. Сигнал, которого нет
    (W5/W6 не дошли или не смогли), — нейтральные 0.5, а не 0: «не знаем» не равно «плохо».
    Статус — только `scored`/`rejected` (_decide): одобряет человек, а домен с непроверенным
    сигналом пакет не возьмёт (bulk_ok/blind_reason).
    """
    pf = sig.get("prior_flags") or {}
    reasons = []
    if sig.get("blacklisted") is True:
        reasons.append("blacklisted")
    if sig.get("webrisk_threats"):
        reasons.append("webrisk")
    if sig.get("trademark_risk"):
        reasons.append("trademark")
    reasons += [f"prior_{c}" for c in cfg.HARD_REJECT_FLAGS if pf.get(c)]
    if reasons:
        return {"score": 0.0, "status": "rejected", "breakdown": {"hard_reject": reasons}}

    n = cfg.NORM

    def _log(v, full):
        return _clamp(math.log10((v or 0) + 1) / math.log10(full + 1))

    rd = sig.get("referring_domains") or 0
    subnets = sig.get("ref_subnets")
    pbn = subnets is not None and rd >= cfg.PBN_MIN_RD and subnets / rd < cfg.PBN_SUBNET_RATIO
    tr, spam, peak = sig.get("topical_relevance"), sig.get("spam_anchor_ratio"), sig.get("peak_traffic")
    comp = {
        "history_cleanliness": 1.0 if sig.get("wayback_checked") else 0.5,
        "topical_fit": _clamp(float(tr)) if tr is not None else 0.5,
        "age": _clamp((sig.get("age_years") or 0.0) / n["AGE_FULL"]),
        "rd": _log(rd, n["RD_FULL"]) * (0.5 if pbn else 1.0),
        "authority": _clamp(float(sig.get("dr") or 0.0) / n["DR_FULL"]),
        "anchor_quality": 1.0 - _clamp(float(spam)) if spam is not None else 0.5,
        "traffic_history": _log(peak, n["TRAFFIC_FULL"]) if peak is not None else 0.5,
    }
    w = {k: float(v) for k, v in (weights or cfg.WEIGHTS).items() if k in comp}
    norm = sum(w.values()) or 1.0
    score = round(_clamp(sum(w[k] * comp[k] for k in w) / norm), 4)
    status = _decide(score, sig, cfg.DECISION["manual_review_at"])
    return {"score": score, "status": status,
            "breakdown": {"components": comp, "weights": w, "pbn_suspect": pbn}}
```

7) В `_commit_result` вызов

```python
                result = {**result, "status": _decide(result["score"], sig,
                                                      st["approve_at"], st["manual_review_at"])}
```

заменить на

```python
                result = {**result, "status": _decide(result["score"], sig, st["manual_review_at"])}
```

8) Блок `if __name__ == "__main__":` в конце `scoring.py` заменить целиком:

```python
if __name__ == "__main__":  # pure-function self-check (no I/O)
    # чистый старый домен с хорошими ссылками -> scored (одобряет только человек, Р2)
    clean = compute_score({"wayback_checked": True, "prior_flags": {}, "dr": 20.0,
                           "age_years": 10, "referring_domains": 800, "ref_subnets": 600})
    assert clean["status"] == "scored", clean
    # казино в истории -> жёсткий отказ
    dirty = compute_score({"wayback_checked": True, "prior_flags": {"casino": True},
                           "dr": 9.0, "age_years": 15, "referring_domains": 500})
    assert dirty["status"] == "rejected" and dirty["score"] == 0.0, dirty
    # угроза Web Risk -> жёсткий отказ при любом качестве
    risky = compute_score({"webrisk_threats": ["MALWARE"], "dr": 40.0, "age_years": 12,
                           "referring_domains": 2000, "wayback_checked": True, "prior_flags": {}})
    assert risky["status"] == "rejected", risky
    # пусто/неизвестно -> низкий балл, отказ
    empty = compute_score({})
    assert empty["status"] == "rejected", empty
    # ИНВАРИАНТ (Р2): никакой сигнал не даёт `approved` — даже непроверенная история с огромным RD
    unverified = compute_score({"referring_domains": 5000, "wayback_checked": False,
                                "prior_flags": {}})
    assert unverified["status"] != "approved", unverified
    # веса в сумме 1.0
    assert abs(sum(cfg.WEIGHTS.values()) - 1.0) < 1e-9
    print("scoring compute_score ok:", clean["score"], dirty["score"], risky["score"], empty["score"])
```

**Панель.** В `panel.py::settings_save` пять строк параметров весов

```python
                  w_history_cleanliness: float | None = Form(None),
                  w_rd_proxy: float | None = Form(None), w_age: float | None = Form(None),
                  w_indexed_echo: float | None = Form(None),
                  w_authority: float | None = Form(None)):
```

заменить на

```python
                  w_history_cleanliness: float | None = Form(None),
                  w_topical_fit: float | None = Form(None), w_age: float | None = Form(None),
                  w_rd: float | None = Form(None), w_authority: float | None = Form(None),
                  w_anchor_quality: float | None = Form(None),
                  w_traffic_history: float | None = Form(None)):
```

а сборку `weights = {k: v for k, v in (("history_cleanliness", …), …) if v is not None}` — на

```python
    weights = {k: v for k, v in (("history_cleanliness", w_history_cleanliness),
                                 ("topical_fit", w_topical_fit), ("age", w_age), ("rd", w_rd),
                                 ("authority", w_authority), ("anchor_quality", w_anchor_quality),
                                 ("traffic_history", w_traffic_history)) if v is not None}
```

**`settings.html`.** Заголовок цикла весов — шесть строк от `    {% for key, ru, note in [` до строки,
которая кончается на `балл = 0')] %}` включительно (кортежи `history_cleanliness`, `rd_proxy`, `age`,
`indexed_echo`, `authority`), — заменить целиком на:

```
    {% for key, ru, note in [
        ('history_cleanliness', 'чистота истории',     'Wayback: адалт/фарма/казино/спам в прошлом'),
        ('topical_fit',         'тема прошлого сайта', 'LLM: близость к VPN/приватности/софту — против «expired domain abuse»'),
        ('age',                 'возраст домена',      'RDAP или первый снимок Wayback, полный балл к 8 годам'),
        ('rd',                  'доноры (RD)',         'Ahrefs refdomains, лог-шкала; ×0.5 при подозрении на спам-сетку'),
        ('authority',           'DR (Ahrefs)',         'Domain Rating by Ahrefs — главный честный сигнал на спам-дропах'),
        ('anchor_quality',      'чистота анкоров',     'доля спам-анкоров (проверяется только у финалистов)'),
        ('traffic_history',     'был трафик',          'пик органического трафика за 5 лет (только у финалистов)')] %}
```

Тело цикла (`<div class="go">…</div>` и `{% endfor %}`) не трогать. Строку
`const W_KEYS = ['history_cleanliness','rd_proxy','age','indexed_echo','authority'];` заменить на
`const W_KEYS = ['history_cleanliness','topical_fit','age','rd','authority','anchor_quality','traffic_history'];`.
В тексте станции «Итоговый скор — взвешенная сумма пяти сигналов.» заменить «пяти» на «семи».

- [ ] **Шаг 4: Запустить тесты задачи — проходят**

Запуск: `cd backend && ../.venv/bin/python -m pytest tests/test_compute_score_v2.py -v`
Ожидание: 11 passed.

- [ ] **Шаг 5: Старые тесты, которые ломает задача — явный список (удалять нечего, только переписать)**

Гарды v1 «never auto-approves …» не удаляются: их новый смысл — «скоринг даёт `scored`, а пакет
домен не берёт», на сильном сигнале (балл не ниже `approve_at`), чтобы исключало правило, а не балл.

**`backend/tests/test_aparser_envelope.py`.**
- После функции `_sig` добавить помощник:

```python
def _in_bulk(sig: dict, out: dict, **breakdown) -> bool:
    """Положить домен с этим итогом скоринга в БД как `scored` и спросить пакет: возьмёт ли.
    Авто-одобрения нет (Р2) — правило «whois упал / возраста нет» живёт в bulk_ok/blind_reason,
    его и проверяем, на пороге 0.0, чтобы исключал именно гард, а не балл."""
    from app.api.panel import _bulk_candidates
    with db.SessionLocal() as s:
        d = Domain(domain="bulk-probe.ru", source="backorder", status="scored", score=out["score"],
                   wayback_checked=sig.get("wayback_checked"), prior_flags=sig.get("prior_flags"),
                   age_years=sig.get("age_years"),
                   score_breakdown={"errors": sig.get("errors", []), "history_evidence": [],
                                    **breakdown})
        s.add(d); s.commit()
        ok, _ = _bulk_candidates(s, 0.0)
        return d.id in {x.id for x in ok}
```

- `test_whois_down_never_auto_approves_even_with_archive_age`, `test_whois_down_without_any_age_never_auto_approves`,
  `test_known_age_still_auto_approves` — заменить три функции целиком на (третья переименована):

```python
def test_whois_down_never_auto_approves_even_with_archive_age():
    """РЕГРЕССИЯ (ревью Задачи 4). Живой clara-c.ru: whois лежит, но Wayback дал возраст 16 лет —
    и до фикса домен набирал 0.87 и уезжал в `approved`. Авто-одобрения теперь нет вовсе (Р2), а
    гард переехал в пакет: занятость домена (`available`) не сверял никто — её из архива не
    добрать, поэтому пакетное одобрение такой домен не берёт."""
    sig = _sig(errors=["whois:RuntimeError"], age_years=16.0, referring_domains=2219)
    out = scoring.compute_score(sig)
    assert out["score"] >= 0.70, out           # балл сильного кандидата — исключает гард, а не балл
    assert out["status"] == "scored", out      # одобряет только человек
    assert _in_bulk(sig, out, age_source="wayback") is False


def test_whois_down_without_any_age_never_auto_approves():
    """Второй достижимый вид того же отказа: архив ПУСТ, возраста нет ни у кого. Сигнал нарочно
    сильный (DR 30, тема 1.0, анкоры чистые, трафик за потолком -> 0.88), чтобы пакет исключал
    домен ПРАВИЛОМ (whois упал, возраста нет), а не низким баллом (находка 1.11)."""
    sig = _sig(errors=["whois:RuntimeError"], dr=30.0, topical_relevance=1.0,
               spam_anchor_ratio=0.0, peak_traffic=5000)
    out = scoring.compute_score(sig)
    assert out["score"] >= 0.85, out
    assert out["status"] == "scored", out
    assert _in_bulk(sig, out) is False


def test_known_age_is_scored_and_lands_in_bulk():
    """Контроль: гард бьёт ТОЛЬКО по отказу whois / пустому возрасту. Домен, чей whois ответил,
    приходит `scored` (одобряет человек, Р2), и пакет его берёт — иначе «фикс» просто заморозил
    бы весь пул на поштучном разборе."""
    sig = _sig(age_years=16.0)
    out = scoring.compute_score(sig)
    assert out["status"] == "scored", out
    assert _in_bulk(sig, out) is True
```

- `test_funnel_whois_alive_domain_still_auto_approves` — переименовать в
  `test_funnel_whois_alive_domain_is_scored_and_bulk_ok`; в докстринге «проходит как раньше — до
  `approved`» заменить на «проходит воронку до `scored` (одобряет человек, Р2) — без пометки
  «вслепую» и с местом в пакете»; строку `assert out["status"] == "approved", out` заменить на
  `assert out["status"] == "scored", out`. Остальные ассерты (`blind_reason is None`,
  `bulk_ok is True`) не трогать.

**`backend/tests/test_funnel.py`.**
- `test_whois_none_falls_through_to_wayback_age`: `assert out["status"] in ("approved", "scored")`
  -> `assert out["status"] == "scored"`.
- `test_clean_strong_domain_approved` — заменить функцию целиком:

```python
def test_clean_strong_domain_is_scored_and_bulk_ok():
    """Чистый сильный домен: скоринг ставит максимум `scored` (одобряет только человек, Р2), а
    без единой дыры в проверках пакет его берёт."""
    did = _mk(domain="good.ru", referring_domains=3000, lane="bid")
    wb = _Wayback()
    old = datetime.now(timezone.utc) - timedelta(days=365 * 9)
    out = scoring.score_domain(did, clients=_clients(old, wb))
    assert wb.calls == 1 and out["status"] == "scored" and out["reject_reason"] is None
    with db.SessionLocal() as s:
        assert scoring.bulk_ok(s.get(Domain, did)) is True
```

- `test_blacklist_none_downgrades_via_funnel` — заменить функцию целиком:

```python
def test_blacklist_none_downgrades_via_funnel():
    """Ревью C2: строка `blacklisted is None -> errors.append("blacklist:unavailable")` прогнана
    полной воронкой на иначе-сильном домене (профиль test_clean_strong_domain_is_scored_and_bulk_ok).
    Авто-одобрения нет (Р2), поэтому «понижение» теперь значит: домен `scored`, с пометкой
    «вслепую» и ВНЕ пакета. Без строки-фикса errors остался бы пуст, и пакет взял бы домен с
    непроверенным блэклистом — тест бы упал."""
    from app.services import scoring_config as cfg
    did = _mk(domain="bl-none.ru", referring_domains=3000, lane="bid")
    wb = _Wayback()
    old = datetime.now(timezone.utc) - timedelta(days=365 * 9)
    out = scoring.score_domain(did, clients=_clients(old, wb, bl=None))
    assert "blacklist:unavailable" in out["errors"]
    assert out["score"] >= cfg.DECISION["approve_at"]      # сильный — исключает правило, а не балл
    assert out["status"] == "scored"                        # не rejected — не hard-reject
    assert wb.calls == 1                                    # blacklist:unavailable не блокирует T3
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
        assert scoring.blind_reason(d) == "блэклист НЕ проверен" and scoring.bulk_ok(d) is False
```

- `test_runtime_approve_at_downgrades_high_scorer_to_scored` — заменить функцию целиком (новое имя):

```python
def test_runtime_approve_at_never_makes_scoring_approve():
    """Р2: `approve_at` больше не участвует в решении скоринга — это «порог сильного кандидата»
    для превью и пакета. Даже порог на самом дне (клампится к manual_review_at) не даёт машине
    поставить `approved`: одобряет только человек."""
    from app.services import settings as st
    did = _mk(domain="runtime-approve.ru", referring_domains=100, lane="bid")
    wb = _Wayback()
    old = datetime.now(timezone.utc) - timedelta(days=365 * 9)
    st.update_settings(approve_at=0.0)
    out = scoring.score_domain(did, clients=_clients(old, wb))
    assert out["score"] > st.get_settings()["approve_at"]
    assert out["status"] == "scored" and out["reject_reason"] is None
```

- `test_ahrefs_skipped_when_feed_has_referring_domains`: `in ("approved", "scored")` -> `== "scored"`.
- `test_ahrefs_not_called_when_budget_exhausted`: `in ("approved", "scored", "rejected")` ->
  `in ("scored", "rejected")`.

**`backend/tests/test_m1_fixes.py`.**
- `test_rkn_or_blacklist_error_caps_at_scored` — заменить функцию целиком; заголовок-комментарий
  над ней `# ---------- I1: ошибка RKN/blacklist не даёт auto-approve ----------` заменить на
  `# ---------- I1: ошибка RKN/blacklist не пускает в пакетное одобрение ----------`:

```python
def test_rkn_or_blacklist_error_caps_at_scored():
    """Авто-одобрения нет (Р2): скоринг даёт максимум `scored` и при чистом прогоне. Упавшая
    проверка РКН/блэклиста держит домен вне ПАКЕТА — туда переехал гард из _decide."""
    from app.models.domain import Domain
    from app.services.scoring import bulk_ok, compute_score
    strong = {"wayback_checked": True, "prior_flags": {}, "age_years": 8,
              "referring_domains": 3000}
    clean = compute_score({**strong, "blacklisted": False, "errors": []})
    assert clean["status"] == "scored" and clean["score"] >= 0.70      # сильный, но одобряет человек

    def _dom(errors):
        return Domain(domain="i1.ru", wayback_checked=True, prior_flags={}, age_years=8,
                      score_breakdown={"errors": errors, "history_evidence": []})
    assert bulk_ok(_dom([])) is True                                   # базовая линия: пакет берёт
    for err in ("rkn:ConnectError", "blacklist:RuntimeError"):
        assert compute_score({**strong, "errors": [err]})["status"] == "scored"
        assert bulk_ok(_dom([err])) is False                           # проверка упала — вне пакета
```

- `test_blacklist_none_goes_to_errors_and_downgrades` — заменить функцию целиком:

```python
def test_blacklist_none_goes_to_errors_and_downgrades(monkeypatch):
    # is_blacklisted вернул None (транзиент) -> в sig.errors -> домен «вслепую», вне пакета.
    # Авто-одобрения нет (Р2): _decide даёт максимум scored даже на 0.9 с этой ошибкой.
    from app.models.domain import Domain
    from app.services import scoring
    sig_err = {"errors": ["blacklist:unavailable"]}
    assert scoring._decide(0.9, sig_err, 0.4) == "scored"
    d = Domain(domain="bl.ru", wayback_checked=True, prior_flags={}, age_years=8,
               score_breakdown={"errors": sig_err["errors"], "history_evidence": []})
    assert scoring.blind_reason(d) == "блэклист НЕ проверен" and scoring.bulk_ok(d) is False
```

**`backend/tests/test_wayback_classify.py`.**
- `test_trademark_risk_is_not_a_hard_reject_anymore` — заменить функцию целиком (развёрнута, новое имя):

```python
def test_trademark_risk_is_a_hard_reject_again():
    """В v1 ветка отказа была, а расчёта — не было (значение всегда NULL), и её сняли как
    призрак. В v2 у неё есть производитель: W0 ставит `trademark_risk=True` по бренд-токену в
    имени (domain_filters.brand_hit, Задача 9) — и отказ снова жёсткий."""
    out = compute_score({"wayback_checked": True, "prior_flags": {}, "trademark_risk": True,
                         "age_years": 10, "referring_domains": 300})
    assert out["status"] == "rejected" and "trademark" in out["breakdown"]["hard_reject"]
```

- `test_js_redirect_to_casino_is_not_approved_as_clean`: строку
  `assert out["status"] != "approved", "домен-редирект на казино авто-одобрен как чистый"` заменить на
  `assert out["status"] == "scored", "одобряет только человек (Р2)"`; ассерты `blind_reason`/`bulk_ok`
  ниже не трогать — это и есть гард.

**Фикстуры «чистого» домена без возраста** (новая ветка `blind_reason` делает их «вслепую»; добавить
возраст, смысл теста не меняется):
- `test_history_verdict.py::test_verdict_clean_only_when_wayback_really_checked` — в `Domain(domain="ok.ru", …)`
  добавить `age_years=10.0`;
- `test_history_verdict.py::test_unchecked_history_stays_out_of_bulk` — в `_add(domain="ok.com", …)`
  (домен переведён в `.com` Задачей 5) добавить `age_years=10.0` (у `ghost.ru` не добавлять);
- `test_history_verdict.py::test_stale_verdict_is_named_but_not_locked` — в `_add(domain="stale.ru", …)`
  добавить `age_years=10.0`; в докстринге две строки (фраза переносится, как в файле)

```
    Пакет его берёт — и это осознанно: вердикт держится на реальных прошлых уликах, авто-approve
    гардится по sig ТЕКУЩЕГО прогона, а запирать домен из-за ТРАНЗИЕНТНОГО сбоя архива значило бы
```

  заменить на

```
    Пакет его берёт — и это осознанно: вердикт держится на реальных прошлых уликах, машина сама
    не одобряет ничего (Р2), а запирать домен из-за ТРАНЗИЕНТНОГО сбоя архива значило бы
```

- `test_inbox.py::test_bulk_approve_skips_blind_domains` — в `_add(domain="clean.com", …)` (домен
  переведён в `.com` Задачей 5) добавить `age_years=10.0`;
- `test_inbox.py::test_bulk_preview_counts` — в `_add(domain="clean.ru", …)` добавить `age_years=10.0`;
- `test_job_stages.py::test_blind_reason_flags_unverified_history` — в `clean = Domain(domain="y.ru", …)`
  добавить `age_years=10.0`.

**Ожидание `scored` вместо «`approved` или `scored`»** (Р2 сужает ассерт):
- `test_pipeline.py::test_scoring_persists_and_jsonb_roundtrips`: `in ("approved", "scored")` -> `== "scored"`;
- `test_scoring_waves.py::test_commit_result_computes_score_for_survivor`: `in ("approved", "scored")` -> `== "scored"`;
- `test_transitions.py` — в тесте с ассертом `out["reject_reason"] is None and out["status"] in ("approved", "scored")`
  (реабилитация РКН перескором) часть `out["status"] in ("approved", "scored")` заменить на
  `out["status"] == "scored"` (проверка `reject_reason is None` остаётся).
- `test_whois_tci.py` не трогать: файл удаляет Задача 9.

**`backend/tests/test_scoring_weights.py`** — заменить файл целиком (ключи v2; переписаны `SIG`,
`test_weights_actually_move_the_score`, `test_saved_weights_reach_the_funnel`,
`test_form_without_weights_does_not_wipe_them`; остальные три теста — без изменений по смыслу):

```python
"""Веса критериев — рантайм, а не константа в коде.

Жалоба оператора (2026-07-13): «в настройках стоят все пункты, по которым идёт оценка донора —
нет возможности скорректировать». Веса жили в scoring_config.WEIGHTS и не менялись ничем.
"""
from app.services import scoring
from app.services import scoring_config as cfg
from app.services.settings import get_settings, update_settings

SIG = {"wayback_checked": True, "prior_flags": {}, "age_years": 8,
       "referring_domains": 3000, "dr": None}
ZERO = dict.fromkeys(cfg.WEIGHTS, 0.0)


def test_weights_default_to_config(sqlite_db):
    assert get_settings()["weights"] == cfg.WEIGHTS


def test_weights_actually_move_the_score(sqlite_db):
    """Ползунок обязан менять РЕЗУЛЬТАТ, а не только показания на экране: ровно этим болели
    пороги до фикса 2026-07 (двигали превью-счётчики, но не статус)."""
    base = scoring.compute_score(SIG)["score"]
    dr_only = scoring.compute_score(SIG, {**ZERO, "authority": 1.0})["score"]
    assert dr_only == 0.0                   # DR не дан, а он теперь ЕДИНСТВЕННЫЙ критерий
    assert base > 0.5                       # с дефолтными весами тот же домен — сильный
    rd_only = scoring.compute_score(SIG, {**ZERO, "rd": 1.0})["score"]
    assert rd_only > 0.9                    # RD=3000 = RD_FULL -> почти полный балл


def test_weights_are_normalised_so_thresholds_keep_meaning(sqlite_db):
    """Сумма весов не обязана быть 1.0. Без нормировки оператор, выкрутивший все ползунки в 1.0,
    получил бы score в разы больше — и пороги стали бы значить совсем не то, что показывают."""
    all_ones = dict.fromkeys(cfg.WEIGHTS, 1.0)
    doubled = {k: v * 2 for k, v in cfg.WEIGHTS.items()}
    assert scoring.compute_score(SIG, doubled)["score"] == scoring.compute_score(SIG)["score"]
    assert 0.0 <= scoring.compute_score(SIG, all_ones)["score"] <= 1.0


def test_degenerate_weights_fall_back_to_defaults(sqlite_db):
    """Все нули обнулили бы score ВСЕМ доменам и тихо превратили воронку в «всё отклонено»."""
    update_settings(weights=dict.fromkeys(cfg.WEIGHTS, 0.0))
    assert get_settings()["weights"] == cfg.WEIGHTS


def test_saved_weights_reach_the_funnel(sqlite_db, client):
    """Сквозной путь: форма -> БД -> get_settings -> compute_score."""
    r = client.post("/settings/save", data={
        "min_referring_domains": 10, "min_age_years": 3.0, "approve_at": 0.7,
        "manual_review_at": 0.35, "max_whois_per_run": 200, "max_ahrefs_per_run": 0,
        "nominet": "on", "w_history_cleanliness": 0.5, "w_topical_fit": 0.2, "w_age": 0.0,
        "w_rd": 0.5, "w_authority": 0.0, "w_anchor_quality": 0.3, "w_traffic_history": 0.1,
    }, follow_redirects=False)
    assert r.status_code == 303
    w = get_settings()["weights"]
    assert w["history_cleanliness"] == 0.5 and w["age"] == 0.0 and w["authority"] == 0.0
    assert w["topical_fit"] == 0.2 and w["anchor_quality"] == 0.3 and w["traffic_history"] == 0.1


def test_form_without_weights_does_not_wipe_them(sqlite_db, client):
    """Старая форма/скрипт без полей w_* не должны ОБНУЛИТЬ шкалу оценки."""
    update_settings(weights={**cfg.WEIGHTS, "history_cleanliness": 0.9})
    client.post("/settings/save", data={
        "min_referring_domains": 10, "min_age_years": 3.0, "approve_at": 0.7,
        "manual_review_at": 0.35, "max_whois_per_run": 200, "max_ahrefs_per_run": 0,
        "nominet": "on"}, follow_redirects=False)
    assert get_settings()["weights"]["history_cleanliness"] == 0.9
```

Тесты, которые чинит сама правка шаблона (KeyError `rd_proxy` при рендере `/settings`), — без правок:
`test_cf_panel.py::test_settings_page_links_to_cloudflare_tab`,
`test_web_fixes.py::test_settings_render_and_save`, `test_web_fixes.py::test_settings_save_accepts_max_whois`,
`test_web_fixes.py::test_settings_save_accepts_max_ahrefs`.

- [ ] **Шаг 6: Весь сьют, self-check, линт**

Запуск: `cd backend && ../.venv/bin/python -m pytest tests -q && ../.venv/bin/python -m app.services.scoring && cd .. && .venv/bin/python -m pyflakes backend/app backend/tests`
Ожидание: **832 passed** (821 + 11), self-check печатает `scoring compute_score ok: …`, pyflakes пуст.
Проверка, что авто-одобрения не осталось:
`grep -n '"approved" if\|status = "approved"\|approve_at"\]' backend/app/services/scoring.py` — пусто.

- [ ] **Шаг 7: Коммит**

```bash
git add -A backend/app backend/tests
git commit -m "feat(scoring): compute_score v2 — 7 компонентов, нейтральные 0.5, штраф PBN; авто-одобрения нет"
```

---

### Задача 9: W0 v2 (зоны, бренды) и W2 «доступность» через RDAP; возраст по старшей дате; удаление TCI

**Что меняется и почему.**
- W0: белый список зон и бренд-токены (`trademark` пишет `trademark_risk=True`).
- W2 `avail`: RDAP по бутстрапу IANA, зоны без RDAP — whois:43 через A-Parser. **W2 возраст только
  записывает** (`whois_created`, информационно `age_years`/`age_source="whois"`) и `too_young` не ставит
  (решение оператора Р5): у перехваченного и снова дропающегося домена RDAP — дата ПОСЛЕДНЕЙ регистрации.
  Чтобы не было окна без гейта молодости, в этой же задаче волна истории (W5, `_history_one`) берёт
  возраст как **старшую** из даты RDAP/whois и первого снимка Wayback и ставит `too_young` там.
- Гейт `too_young` в W5 судит, только если Wayback ОТВЕТИЛ (находка R2-1): при сбое архива вторая
  дата возраста неизвестна, а не «молода»; отказ по одной дате RDAP окончателен и терял бы
  перехваченный дроп (archive.org регулярно отвечает 429/503). Такой домен идёт дальше «вслепую»
  (`wayback:` в errors держит его вне пакета). EMD (`source="emd"`) гейт `too_young` не проходит вовсе
  (находка R2-14): это новорег, «молодость» — его суть.
- Домен без лейна (ручной список) в RDAP-статусе `pending delete`/`redemption period` получает лейн
  `bid` (находка 1.12) — иначе он висел бы `taken_undated` до самого дропа — и оценку дедлайна
  (находка R2-11): `pending delete` → сейчас + 5 суток, `redemption period` → сейчас + 35 суток (30
  выкупа + 5 удаления; при обоих статусах — 35). Без даты bid-домен никогда не закрылся бы обычным
  путём `acquirability_verdict` (у bid без даты судить нечем). Оценка идёт в `sig["acquire_deadline"]` и
  пишется `_commit_result` только в ПУСТУЮ колонку — реальную дату дропа она не перебивает.
- Обратная запись `d.acquire_deadline = state.acquire_deadline` из `_commit_result` удаляется (находка
  R2-12): её питала только проекция TCI (`_deadline_from_whois`), а без неё она могла лишь затереть
  свежую дату параллельного discovery снимком, взятым в начале прогона.
- Предохранители (находка 3.2): после 3 сбоев ПОДРЯД канал (RDAP `lookup`, A-Parser whois) до конца
  прогона не вызывается, домен получает `whois:circuit_open`. Счётчик — атрибут инстанса клиента, под
  локом из `_make_clients` (`_rdap_lock`, `_whois_lock`).
- Дата и возраст считаются внутри `try` (находка 3.6): наивная дата whois:43 считается UTC, дата,
  которую не посчитать, — сбой whois, а не «живой домен без вердикта».

**Files:**
- Modify:
  - `backend/app/services/scoring.py` — `FunnelState.source`, `FUNNEL_STAGES`, `_BLIND_RU["whois"]`,
    `_make_clients`, `_CONCURRENCY`, `_wave_t0`, `_whois_one` → `_avail_one`, `_wave_whois` → `_wave_avail`,
    `_history_one`, таблица волн, `_commit_result`, `score_domain`, `score_pending`; удалить `_deadline_from_whois`;
  - `backend/app/services/whois.py` — переписать целиком;
  - `backend/app/services/diagnostics.py` — убрать пинг `tci`.
- Delete: `backend/app/integrations/whois_tci.py`, `backend/tests/test_whois_tci.py` (его тесты
  предохранителя A-Parser и сквозные тесты маршрута переезжают в `test_whois.py`, см. шаг 5).
- Test: `backend/tests/test_waves_v2.py` (создать), `backend/tests/test_whois.py` (создать). Старые
  тесты — явный список в шаге 5.

**Interfaces:**
- Consumes: `RdapClient` (Задача 3: `has_rdap`, `lookup` -> `{"exists", "status": [нижний регистр],
  "registered_at": aware datetime | None}`, бутстрап не бросает), `domain_filters.tld_match/brand_hit`
  (Задача 1), `st["tld_allowlist"]`, `st["brand_tokens"]` (Задача 5).
- Produces:
  - `FunnelState(..., source: str | None = None)` — поле последнее, с дефолтом;
  - `_avail_one(s, clients, budget, st)`, `_wave_avail(states, clients, budget, st, run)`; `_whois_one`/
    `_wave_whois` больше нет;
  - `whois.probe(domain, clients) -> {"available", "created", "free_date": None, "whois_source":
    "rdap"|"aparser", "status": list[str]}` (`status` — статусы RDAP; у whois:43 — `[]`);
  - `whois.CircuitOpen(RuntimeError)` и `whois.guarded(client, attr, call, name, lock=None)` —
    предохранитель «3 сбоя подряд»; **Задача 10 защищает им Web Risk** (`webrisk:circuit_open`);
  - ключи клиентов: `clients["rdap"]`, `clients["_rdap_lock"]`; ключа `tci` нет;
  - коды ошибок: `whois:circuit_open` (предохранитель), `whois:<Исключение>` (сбой/битая дата);
  - ключи стадий `"t0"`, `"avail"`, подписи водопада `фильтры`, `доступность`; `_CONCURRENCY["avail"] = 12`
    (ключа `"whois"` нет);
  - сигнал `sig["trademark_risk"] = True` (пишется в колонку `Domain.trademark_risk` через `_commit_result`);
  - `sig["whois_created"]` (aware datetime | None), `sig["age_years"]`/`sig["age_source"]` — после W2
    `"whois"`, после W5 — старшая из двух (`"wayback"`, если архив старше);
  - `blind_reason`: `_BLIND_RU["whois"]` = «доступность и возраст НЕ проверены: …»;
  - `_history_one`: сбой `classify_history` → `wayback:<Исключение>` и выход из функции без гейта
    `too_young` (R2-1); гейт пропускает `s.source == "emd"` (R2-14);
  - `sig["acquire_deadline"]` — оценка дедлайна для домена, которого W2 перевела в `bid` по статусу RDAP
    (R2-11); `_commit_result` пишет её только в пустую колонку `acquire_deadline`; записи
    `d.acquire_deadline = state.acquire_deadline` в `_commit_result` больше нет (R2-12).
- **Для Задачи 12 (W5 + LLM):** возраст по старшей дате и гейт `too_young` УЖЕ в конце `_history_one`
  (блок «Гейт молодости — ЗДЕСЬ, а не в W2») — сохранить его и дописывать LLM-тему после него, как
  Задача 12 и планирует. Тест Р5 «RDAP 2023, первый снимок ~15 лет → возраст 15, не отказ» уже есть —
  `test_waves_v2.py::test_history_age_is_the_older_of_rdap_and_first_snapshot`; он зовёт
  `_history_one(s, {"wayback": AgedWB(...)}, …)` без клиента `llm` — Задаче 12 либо терпеть его
  отсутствие, либо добавить фейк LLM в этот тест. Так же без `llm` зовут `_history_one` тесты R2-1/R2-14
  (`test_history_wayback_down_does_not_judge_age_by_rdap_alone`, `test_history_emd_is_never_too_young`).
  Ветка `except` в `_history_one` кончается `return` (R2-1): при сбое Wayback ни гейт, ни тема (снимков
  нет) не выполняются — дописанное Задачей 12 в конец функции туда не доходит, и это верно.
- **Для Задач 10–13 (`test_waves_v2.py`):** заголовок файла держит только то, что использует Задача 9:
  `datetime/timedelta/timezone`, `scoring`, `get_settings`, `NOW`, `_st`, `_state`, `FakeRdap`
  (с параметром `status=("pending delete",)`), `FakeAp`, `AgedWB` (имя `FakeWB` оставлено Задаче 12 — она
  заводит свой Wayback-фейк с текстами снимков). Импорты `json`, `pathlib` (+ `FX`),
  `SimpleNamespace`, `app.db as db`, `Domain`, `threading` каждая следующая задача добавляет в шапку
  файла сама, когда её тест их использует (иначе pyflakes ругается на неиспользуемые).

- [ ] **Шаг 1: Написать падающие тесты** — создать два файла.

`backend/tests/test_waves_v2.py`:

```python
"""Волны v2 (t0 / avail / risk / links / history / deep) — юнит-тесты на фейках, без сети."""
from datetime import datetime, timedelta, timezone

from app.services import scoring
from app.services.settings import get_settings

NOW = datetime.now(timezone.utc)


def _st(**kw):
    return {**get_settings(), **kw}


def _state(domain, source="nominet", lane="bid", **kw):
    return scoring.FunnelState(domain_id=0, domain=domain, lane=lane, referring_domains=None,
                               acquire_deadline=kw.pop("acquire_deadline", None),
                               feed_flags=kw.pop("feed_flags", None), source=source)


class FakeRdap:
    def __init__(self, zones=("com", "uk", "net", "org"), exists=False, registered=None, boom=False,
                 status=("pending delete",)):
        self.zones, self.exists, self.registered, self.boom = zones, exists, registered, boom
        self.status, self.calls = list(status), 0

    def has_rdap(self, d):
        return d.rsplit(".", 1)[-1] in self.zones

    def lookup(self, d):
        self.calls += 1
        if self.boom:
            raise RuntimeError("rdap down")
        return {"exists": self.exists, "status": self.status if self.exists else [],
                "registered_at": self.registered if self.exists else None}


class FakeAp:
    def __init__(self, available=True, created=None):
        self.available, self.created, self.calls = available, created, 0

    def whois_probe(self, d):
        self.calls += 1
        return {"available": self.available, "created": self.created}


class AgedWB:
    """Wayback-фейк для волны истории: чистая проверенная история, первый снимок `age` лет назад."""
    def __init__(self, age=9.0):
        self.age = age

    def classify_history(self, d):
        return {"prior_flags": {}, "first_seen": NOW - timedelta(days=int(365.25 * self.age)),
                "age_years": self.age, "wayback_checked": True, "sampled": 5, "evidence": []}


def test_t0_feed_flag_zone_brand():
    s = [_state("flag.com", feed_flags={"block": True}), _state("x.ru", source="list"),
         _state("nordvpn-deals.com", source="list"), _state("ok.com", source="list")]
    scoring._wave_t0(s, _st())
    assert s[0].reject_reason == "feed_flag"
    assert (s[1].reject_reason, s[1].alive) == ("tld_closed", False)
    assert s[2].reject_reason == "trademark" and s[2].sig["trademark_risk"] is True
    assert s[3].alive and s[3].reject_reason is None


def test_avail_rdap_free_name_gets_free_lane_without_aparser():
    s, ap = _state("new-name.com", source="list", lane=None), FakeAp()
    scoring._avail_one(s, {"rdap": FakeRdap(exists=False), "aparser": ap}, None, _st())
    assert s.alive and s.sig["lane"] == "free" and s.sig["whois_source"] == "rdap" and ap.calls == 0


def test_avail_bid_pending_delete_keeps_original_age():
    reg = datetime(1998, 7, 29, 4, tzinfo=timezone.utc)
    s = _state("pharmaindustrie.com", lane="bid", acquire_deadline=NOW + timedelta(days=2))
    scoring._avail_one(s, {"rdap": FakeRdap(exists=True, registered=reg), "aparser": FakeAp()}, None, _st())
    assert s.alive and s.sig["whois_created"] == reg
    assert s.sig["age_years"] > 25 and s.sig["age_source"] == "whois"


def test_avail_records_young_registration_but_never_rejects_too_young():
    """Р5: W2 возраст только записывает. Дата RDAP у перехваченного и снова дропающегося домена —
    ПОСЛЕДНЯЯ регистрация; судит W5 по старшей из двух дат."""
    reg = NOW - timedelta(days=200)
    s = _state("recaught.com", lane="bid", acquire_deadline=NOW + timedelta(days=2))
    scoring._avail_one(s, {"rdap": FakeRdap(exists=True, registered=reg), "aparser": FakeAp()},
                       None, _st(min_age_years=3.0))
    assert s.alive and s.reject_reason is None and s.sig["whois_created"] == reg


def test_avail_free_lane_taken_is_not_acquirable():
    s = _state("taken.com", source="mx", lane="free")
    scoring._avail_one(s, {"rdap": FakeRdap(exists=True, registered=NOW - timedelta(days=30)),
                           "aparser": FakeAp()}, None, _st())
    assert s.reject_reason == "not_acquirable"


def test_avail_listed_domain_in_pending_delete_becomes_bid():
    """1.12: вставка из ExpiredDomains — лейна нет, RDAP говорит «pending delete». Это дроп, а не
    чужой занятый домен: лейн bid, домен идёт дальше, а не висит taken_undated до самого дропа.
    Без статуса удаления тот же «занят» остаётся нерешённым — это чужой живой домен."""
    reg = NOW - timedelta(days=4000)
    s = _state("dropping.com", source="list", lane=None)
    scoring._avail_one(s, {"rdap": FakeRdap(exists=True, registered=reg), "aparser": FakeAp()},
                       None, _st())
    assert s.alive and s.lane == "bid" and s.sig["lane"] == "bid"
    live = _state("someones.com", source="list", lane=None)
    scoring._avail_one(live, {"rdap": FakeRdap(exists=True, registered=reg, status=()),
                              "aparser": FakeAp()}, None, _st())
    assert not live.alive and live.unresolved_why == "taken_undated"


def test_avail_budget_spent_only_on_aparser_zones():
    ap = FakeAp(available=True)
    c = {"rdap": FakeRdap(), "aparser": ap}
    s_com, s_mx = _state("libre.com", source="list", lane=None), _state("libre.mx", source="mx", lane="free")
    zero = scoring.Budget(0)
    scoring._avail_one(s_com, c, zero, _st())
    scoring._avail_one(s_mx, c, zero, _st())
    assert s_com.alive                                     # RDAP бесплатный — бюджет не нужен
    assert s_mx.unresolved_why == "budget" and ap.calls == 0
    s_mx2 = _state("libre2.mx", source="mx", lane="free")
    scoring._avail_one(s_mx2, c, scoring.Budget(1), _st())
    assert s_mx2.alive and s_mx2.sig["whois_source"] == "aparser" and ap.calls == 1


def test_avail_rdap_error_unresolves_non_bid_but_bid_continues():
    c = {"rdap": FakeRdap(boom=True), "aparser": FakeAp()}
    s1, s2 = _state("a.com", source="list", lane=None), _state("b.com", lane="bid")
    scoring._avail_one(s1, c, None, _st())
    scoring._avail_one(s2, c, None, _st())
    assert s1.unresolved_why == "whois_failed" and any(e.startswith("whois:") for e in s1.sig["errors"])
    assert s2.alive


def test_avail_rdap_circuit_opens_after_three_failures():
    """3.2, урок v1 (TCI): после 3 сбоев lookup ПОДРЯД канал до конца прогона не вызывается —
    домены сразу получают whois:circuit_open, без сети и без ретрай-шторма. Последовательно, без
    таймингов: счётчик живёт на инстансе клиента."""
    rdap = FakeRdap(boom=True)
    c = {"rdap": rdap, "aparser": FakeAp()}
    states = [_state(f"d{i}.com", source="list", lane=None) for i in range(5)]
    for s in states:
        scoring._avail_one(s, c, None, _st())
    assert rdap.calls == 3                                  # 4-й и 5-й — без сети
    assert [s.sig["errors"][-1] for s in states] == ["whois:RuntimeError"] * 3 + ["whois:circuit_open"] * 2
    assert all(s.unresolved_why == "whois_failed" for s in states)


def test_avail_naive_date_is_utc_and_broken_date_is_a_whois_failure():
    """3.6: наивная дата whois:43 считается UTC (раньше `now - wc` падал TypeError'ом, волна глотала
    исключение, и домен шёл дальше «живым без вердикта»); дата, которую не посчитать, — сбой whois."""
    s = _state("naive.mx", source="mx", lane="free")
    scoring._avail_one(s, {"rdap": FakeRdap(), "aparser": FakeAp(available=True, created=datetime(2010, 1, 1))},
                       None, _st())
    assert s.alive and s.sig["lane"] == "free" and s.sig["whois_created"].tzinfo is not None
    assert s.sig["age_years"] > 15
    bad = _state("broken.mx", source="mx", lane="free")
    scoring._avail_one(bad, {"rdap": FakeRdap(), "aparser": FakeAp(available=True, created="2010-01-01")},
                       None, _st())
    assert not bad.alive and bad.unresolved_why == "whois_failed"
    assert bad.sig["errors"] == ["whois:AttributeError"]


def test_history_age_is_the_older_of_rdap_and_first_snapshot():
    """Р5: перехваченный и снова дропающийся домен — RDAP 2023 (последняя регистрация), первый
    снимок ~15 лет назад. Возраст ~15 по архиву, отказа too_young нет. Молоды обе даты — отказ
    здесь, в волне истории."""
    s = _state("recaught.com")
    s.sig.update({"whois_created": datetime(2023, 1, 1, tzinfo=timezone.utc), "age_years": 3.0,
                  "age_source": "whois"})
    scoring._history_one(s, {"wayback": AgedWB(age=15.0)}, _st(min_age_years=5.0))
    assert s.alive and s.sig["age_years"] == 15.0 and s.sig["age_source"] == "wayback"
    young = _state("young.com")
    young.sig.update({"whois_created": NOW - timedelta(days=365), "age_years": 1.0,
                      "age_source": "whois"})
    scoring._history_one(young, {"wayback": AgedWB(age=1.0)}, _st(min_age_years=5.0))
    assert young.reject_reason == "too_young" and not young.alive


def test_history_wayback_down_does_not_judge_age_by_rdap_alone():
    """R2-1: Wayback не ответил (archive.org регулярно отдаёт 429/503) — вторая дата возраста
    НЕИЗВЕСТНА, а не «молода». Отказ too_young по одной дате RDAP окончателен и потерял бы
    перехваченный дроп; домен идёт дальше «вслепую», `wayback:` держит его вне пакета."""
    class DownWB:
        def classify_history(self, d):
            raise RuntimeError("archive.org 503")
    s = _state("recaught.com")
    s.sig.update({"whois_created": NOW - timedelta(days=550), "age_years": 1.5,
                  "age_source": "whois"})
    scoring._history_one(s, {"wayback": DownWB()}, _st(min_age_years=3.0))
    assert s.alive and s.reject_reason is None
    assert s.sig["errors"] == ["wayback:RuntimeError"] and s.sig["age_source"] == "whois"


def test_history_emd_is_never_too_young():
    """R2-14: EMD — новорег, «молодость» — его суть: гейт too_young его не судит, даже если у
    имени был короткий прошлый сайт в архиве."""
    s = _state("mejorvpn.com", source="emd", lane="free")
    scoring._history_one(s, {"wayback": AgedWB(age=1.0)}, _st(min_age_years=5.0))
    assert s.alive and s.reject_reason is None


def test_avail_listed_domain_turned_bid_gets_estimated_deadline():
    """R2-11: домен из списка, которого W2 перевела в bid по статусу RDAP, без даты дропа никогда
    не закрылся бы обычным путём acquirability_verdict. Оценка: pending delete — 5 суток,
    redemption period (30 выкупа + 5 удаления) — 35. Известную дату дропа оценка не трогает."""
    reg = NOW - timedelta(days=4000)

    def turned(status, **kw):
        s = _state("dropping.com", source="list", lane=None, **kw)
        scoring._avail_one(s, {"rdap": FakeRdap(exists=True, registered=reg, status=status),
                               "aparser": FakeAp()}, None, _st())
        assert s.alive and s.lane == "bid"
        return s.sig.get("acquire_deadline")
    assert abs(turned(("pending delete",)) - (NOW + timedelta(days=5))) < timedelta(minutes=5)
    late = turned(("redemption period", "pending delete"))
    assert abs(late - (NOW + timedelta(days=35))) < timedelta(minutes=5)
    assert turned(("pending delete",), acquire_deadline=NOW + timedelta(days=2)) is None
    assert scoring.acquirability_verdict(False, NOW + timedelta(days=5), NOW + timedelta(days=8),
                                         lane="bid") == "taken"    # дедлайн прошёл — цикл закрыт
```

`backend/tests/test_whois.py`:

```python
"""whois-маршрутизатор v2 (services/whois.py): RDAP, иначе whois:43 через A-Parser; предохранители
каналов. Тесты предохранителя A-Parser перенесены из удалённого test_whois_tci.py (на зоне .mx — у
неё нет RDAP), сквозные тесты «score_domain -> маршрут» — на RDAP."""
import threading
from datetime import datetime, timedelta, timezone

import pytest

import app.db as db
from app.models.domain import Domain
from app.services import scoring, whois

NOW = datetime.now(timezone.utc)
_AP_OK = {"available": False, "created": None}
_FREE = {"exists": False, "status": [], "registered_at": None}


class _Rdap:
    """RDAP-фейк: зоны с RDAP; исход lookup — по списку, "boom" — исключение."""
    def __init__(self, outcomes=(), zones=("com",)):
        self._outcomes, self.zones, self.calls = list(outcomes), zones, []

    def has_rdap(self, d):
        return d.rsplit(".", 1)[-1] in self.zones

    def lookup(self, d):
        self.calls.append(d)
        out = self._outcomes.pop(0)
        if out == "boom":
            raise OSError("rdap timeout")
        return out


class _FlakyAparser:
    """whois_probe с исходом на каждый вызов по списку ("boom" — исключение)."""
    def __init__(self, outcomes=()):
        self._outcomes = list(outcomes)
        self.calls = []

    def whois_probe(self, domain):
        self.calls.append(domain)
        outcome = self._outcomes.pop(0)
        if outcome == "boom":
            raise OSError("connect error")
        return outcome


class _SpyLock:
    """Настоящий Lock, который считает входы: доказывает, что предохранитель берёт лок вокруг
    ОБЕИХ операций (гейт-чек и запись счётчика) — детерминированно, без таймингов."""
    def __init__(self):
        self._real = threading.Lock()
        self.enters = 0

    def __enter__(self):
        self._real.acquire()
        self.enters += 1

    def __exit__(self, *a):
        self._real.release()


def test_probe_routes_rdap_then_aparser():
    reg = NOW - timedelta(days=4000)
    rdap = _Rdap([{"exists": True, "status": ["pending delete"], "registered_at": reg}])
    ap = _FlakyAparser([_AP_OK])
    c = {"rdap": rdap, "aparser": ap}
    assert whois.probe("x.com", c) == {"available": False, "created": reg, "free_date": None,
                                       "whois_source": "rdap", "status": ["pending delete"]}
    assert whois.probe("x.mx", c) == {"available": False, "created": None, "free_date": None,
                                      "whois_source": "aparser", "status": []}
    assert rdap.calls == ["x.com"] and ap.calls == ["x.mx"]


def test_aparser_whois_circuit_breaker_skips_after_three_consecutive_failures():
    """A-Parser упал -> без предохранителя КАЖДЫЙ домен зоны без RDAP платил бы полный
    ретрай-шторм BaseClient. Первые 3 вызова — РЕАЛЬНЫЕ попытки (исходный OSError), 4-й —
    предохранитель открыт (CircuitOpen, whois_probe не звали: исходов ровно 3, иначе IndexError)."""
    ap = _FlakyAparser(["boom", "boom", "boom"])
    clients = {"rdap": None, "aparser": ap}
    for i in range(3):
        with pytest.raises(OSError):
            whois.probe(f"fail{i}.mx", clients)
    assert len(ap.calls) == 3
    with pytest.raises(whois.CircuitOpen):
        whois.probe("fourth.mx", clients)
    assert len(ap.calls) == 3


def test_aparser_whois_circuit_breaker_resets_on_success_between_failures():
    """Успех между сбоями сбрасывает счётчик: «2 сбоя / успех / 2 сбоя» — ни разу 3 подряд."""
    ap = _FlakyAparser(["boom", "boom", _AP_OK, "boom", "boom"])
    clients = {"rdap": None, "aparser": ap}
    with pytest.raises(OSError):
        whois.probe("a.mx", clients)
    with pytest.raises(OSError):
        whois.probe("b.mx", clients)
    assert whois.probe("c.mx", clients)["available"] is False
    with pytest.raises(OSError):
        whois.probe("d.mx", clients)
    with pytest.raises(OSError):
        whois.probe("e.mx", clients)
    assert len(ap.calls) == 5


def test_rdap_circuit_breaker_skips_after_three_consecutive_failures():
    """3.2: тот же предохранитель у RDAP lookup — лежащий RDAP-сервер не превращает волну из 12
    потоков в ретрай-шторм на каждый .com."""
    rdap = _Rdap(["boom", "boom", "boom"])
    clients = {"rdap": rdap, "aparser": _FlakyAparser()}
    for i in range(3):
        with pytest.raises(OSError):
            whois.probe(f"fail{i}.com", clients)
    with pytest.raises(whois.CircuitOpen):
        whois.probe("fourth.com", clients)
    assert len(rdap.calls) == 3


def test_rdap_circuit_breaker_resets_on_success_between_failures():
    rdap = _Rdap(["boom", "boom", _FREE, "boom", "boom"])
    clients = {"rdap": rdap, "aparser": _FlakyAparser()}
    for name in ("a.com", "b.com"):
        with pytest.raises(OSError):
            whois.probe(name, clients)
    assert whois.probe("c.com", clients)["available"] is True        # 404 — свободен, не сбой
    for name in ("d.com", "e.com"):
        with pytest.raises(OSError):
            whois.probe(name, clients)
    assert len(rdap.calls) == 5


def test_rdap_breaker_locks_both_the_gate_check_and_the_increment():
    """Каждая из 3 попыток до срабатывания берёт лок дважды: гейт-чек и инкремент. Пропуск любого
    из входов — непокрытая гонка на счётчике под 12 потоками волны."""
    lock = _SpyLock()
    rdap = _Rdap(["boom", "boom", "boom"])
    for _ in range(3):
        with pytest.raises(OSError):
            whois.probe("x.com", {"rdap": rdap, "aparser": _FlakyAparser(), "_rdap_lock": lock})
    assert lock.enters == 6 and rdap.lookup_failures == 3


def test_make_clients_wires_rdap_and_its_lock():
    """Опечатка в ключе `rdap` в _make_clients отключила бы RDAP целиком (все домены ушли бы в
    платный A-Parser), а сьют остался бы зелёным: фейки передают клиентов сами."""
    from app.integrations.rdap import RdapClient
    c = scoring._make_clients()
    assert isinstance(c["rdap"], RdapClient)
    assert "_rdap_lock" in c and "_whois_lock" in c and "tci" not in c


# --- воронка целиком: score_domain -> _wave_avail -> whois.probe -> RDAP ----------------------

class _FunnelWayback:
    def classify_history(self, domain):
        return {"prior_flags": {c: False for c in ("adult", "pharma", "casino", "gambling", "spam")},
                "first_seen": None, "age_years": 9.0, "wayback_checked": True, "sampled": 5}


def _mk(**kw):
    with db.SessionLocal() as s:
        d = Domain(source=kw.pop("source", "list"), status="discovered", **kw)
        s.add(d); s.commit(); s.refresh(d)
        return d.id


def _funnel_clients(rdap, ap):
    return {"rdap": rdap, "aparser": ap,
            "rkn": type("R", (), {"is_listed": lambda self, d: False})(),
            "blacklist": type("B", (), {"is_blacklisted": lambda self, d: False})(),
            "searxng": type("S", (), {"indexed_echo": lambda self, d: True})(),
            "wayback": _FunnelWayback()}


def test_funnel_routes_whois_through_rdap_without_touching_aparser():
    """Сквозной путь (перенос TCI-теста v1): RDAP-зона проходит W2 без единого обращения к
    A-Parser, источник виден оператору в score_breakdown."""
    reg = NOW - timedelta(days=365 * 9)
    did = _mk(domain="rdapgood.com", referring_domains=3000, lane="bid")
    rdap = _Rdap([{"exists": True, "status": ["pending delete"], "registered_at": reg}])
    ap = _FlakyAparser()
    out = scoring.score_domain(did, clients=_funnel_clients(rdap, ap))
    assert ap.calls == [] and rdap.calls == ["rdapgood.com"]
    assert out["status"] == "scored" and out["reject_reason"] is None
    with db.SessionLocal() as s:
        assert s.get(Domain, did).score_breakdown["whois_source"] == "rdap"


def test_funnel_rdap_decides_acquirability_for_non_bid_domain():
    """Денежный путь: домен без лейна (ручной список), RDAP 404 -> вердикт free -> лейн free —
    приобретаемость решил RDAP, а не источник."""
    did = _mk(domain="rdapfree.com", referring_domains=3000)
    rdap, ap = _Rdap([_FREE]), _FlakyAparser()
    out = scoring.score_domain(did, clients=_funnel_clients(rdap, ap))
    assert ap.calls == [] and out["reject_reason"] is None
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
    assert d.lane == "free" and d.score_breakdown["whois_source"] == "rdap"


class _DownWayback:
    def classify_history(self, domain):
        raise RuntimeError("archive.org 503")


def test_funnel_wayback_down_does_not_reject_too_young_by_rdap_alone():
    """R2-1 сквозь воронку: RDAP — 1,5 года (у перехваченного домена это ПОСЛЕДНЯЯ регистрация),
    Wayback лежит. Вторая дата неизвестна — отказа too_young нет; после коммита домен «вслепую»:
    blind_reason про Wayback, пакет его не берёт."""
    reg = NOW - timedelta(days=550)
    did = _mk(domain="recaught.com", referring_domains=3000, lane="bid")
    rdap = _Rdap([{"exists": True, "status": ["pending delete"], "registered_at": reg}])
    out = scoring.score_domain(did, clients={**_funnel_clients(rdap, _FlakyAparser()),
                                             "wayback": _DownWayback()})
    assert rdap.calls == ["recaught.com"]
    assert out["reject_reason"] != "too_young" and "wayback:RuntimeError" in out["errors"]
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
        assert "Wayback" in scoring.blind_reason(d) and scoring.bulk_ok(d) is False


def test_funnel_listed_pending_delete_domain_is_saved_as_bid_with_estimated_deadline():
    """R2-11 сквозь воронку: домен из списка (лейна нет), RDAP — pending delete. После коммита в
    строке домена лейн bid и оценка дедлайна (+5 суток): дальше его жизненный цикл закрывает
    обычный acquirability_verdict."""
    reg = NOW - timedelta(days=4000)
    did = _mk(domain="listed-drop.com", referring_domains=3000)
    rdap = _Rdap([{"exists": True, "status": ["pending delete"], "registered_at": reg}])
    scoring.score_domain(did, clients=_funnel_clients(rdap, _FlakyAparser()))
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
    assert d.lane == "bid" and d.acquire_deadline is not None
    dl = d.acquire_deadline.replace(tzinfo=timezone.utc)      # SQLite отдаёт дату без пояса
    assert abs(dl - (NOW + timedelta(days=5))) < timedelta(minutes=5)
```

- [ ] **Шаг 2: Запустить — убедиться, что падает**

Запуск: `cd backend && ../.venv/bin/python -m pytest tests/test_waves_v2.py tests/test_whois.py -v`
Ожидание: `24 failed, 1 passed`. В `test_waves_v2.py` все 14 — `TypeError: FunnelState.__init__() got an
unexpected keyword argument 'source'`; в `test_whois.py` (10 из 11) — нет `whois.CircuitOpen`, ключа `status` в
ответе `probe`, клиента `rdap` в `_make_clients` и маршрута через RDAP (`AttributeError`/`AssertionError`/`KeyError`;
два новых сквозных теста R2-1/R2-11 — `AssertionError`: RDAP не вызывался, лейн не `bid`).
Проходит только `test_aparser_whois_circuit_breaker_resets_on_success_between_failures`: предохранитель
A-Parser уже есть, тест переехал из `test_whois_tci.py`.

- [ ] **Шаг 3: Реализовать**

**`backend/app/services/whois.py`** — заменить содержимое целиком (TCI и его предохранитель уходят,
предохранитель A-Parser становится общим `guarded` и защищает ещё и RDAP):

```python
"""Доступность и дата регистрации домена — W2 `avail` воронки и перепроверка занятости.

v2: RDAP по бутстрапу IANA — для зон, где он есть (.com/.net/.org/.uk/.online/.xyz/.site/.si/.nl/
.in …): бесплатно и структурированно. Зоны без RDAP (.mx/.co/.nz/.de …) — whois:43 через A-Parser
Net::Whois, как в v1. TCI (.ru) удалён вместе с РФ. Логика выбора канала живёт здесь, а не в
транспорте (конвенция проекта: integrations/ = только транспорт).

ПРЕДОХРАНИТЕЛИ (урок v1: TCI и A-Parser в живом инциденте 2026-07-20). Лежащий канал без
предохранителя — ретрай-шторм BaseClient (3 попытки × backoff) на КАЖДЫЙ домен волны из 12
потоков. После `_FAILURE_LIMIT` сбоев ПОДРЯД канал считается мёртвым до конца прогона и не
вызывается вовсе: `probe` сразу поднимает `CircuitOpen`, воронка пишет `whois:circuit_open` и
обрабатывает домен как обычный сбой whois (unresolved для не-bid). Счётчик — атрибут инстанса
клиента (`rdap.lookup_failures`, `aparser.whois_failures`); клиенты пересоздаются раз в прогон
(`scoring._make_clients()`), поэтому сработавший предохранитель не переживает прогон. Под
конкурентностью волны счётчик меняется под общим локом из `_make_clients` (`_rdap_lock`,
`_whois_lock`): голый `+= 1` не атомарен.
"""
import logging
from contextlib import nullcontext

_log = logging.getLogger(__name__)

# После скольких сбоев ПОДРЯД канал считается мёртвым на этот прогон (RDAP и A-Parser whois).
_FAILURE_LIMIT = 3


class CircuitOpen(RuntimeError):
    """Предохранитель канала сработал — до конца прогона канал не вызывается."""


def guarded(client, attr: str, call, name: str, lock=None):
    """Вызвать `call()` под предохранителем со счётчиком `client.<attr>`. `lock` — общий лок
    волны; гейт-чек и запись счётчика — обе под ним (детерминированно проверяют спай-локом).
    Тем же помощником волна risk защищает Google Web Risk (scoring._risk_one)."""
    cm = lock if lock is not None else nullcontext()
    with cm:
        breaker_open = getattr(client, attr, 0) >= _FAILURE_LIMIT
    if breaker_open:
        raise CircuitOpen(f"{name}: предохранитель сработал, канал пропускается до конца прогона")
    try:
        out = call()
    except Exception:
        with cm:
            setattr(client, attr, getattr(client, attr, 0) + 1)
            tripped = getattr(client, attr) == _FAILURE_LIMIT
        if tripped:
            _log.warning("%s: %d сбоев подряд — предохранитель сработал, до конца прогона канал "
                         "пропускается", name, _FAILURE_LIMIT)
        raise
    with cm:
        setattr(client, attr, 0)            # канал жив — счётчик сбоев сброшен
    return out


def _aparser_whois(ap, domain: str, lock=None) -> dict:
    """A-Parser whois_probe под предохранителем (счётчик `ap.whois_failures`)."""
    return guarded(ap, "whois_failures", lambda: ap.whois_probe(domain), "A-Parser whois", lock)


def _rdap_lookup(rdap, domain: str, lock=None) -> dict:
    """RDAP lookup под предохранителем (счётчик `rdap.lookup_failures`). 404 — не сбой: lookup
    отвечает «домена нет» без исключения."""
    return guarded(rdap, "lookup_failures", lambda: rdap.lookup(domain), "RDAP", lock)


def probe(domain: str, clients: dict) -> dict:
    """{"available", "created", "free_date", "whois_source", "status"}.

    `free_date` всегда None: проекцию «освободится» давал только TCI (.ru). `status` — статусы
    RDAP в нижнем регистре (`["pending delete", …]`); у whois:43 — пустой список. Локи —
    `clients["_rdap_lock"]`/`clients["_whois_lock"]` (scoring._make_clients); вне волны (юнит-тесты)
    их нет — nullcontext."""
    rdap = clients.get("rdap")
    if rdap is not None and rdap.has_rdap(domain):
        r = _rdap_lookup(rdap, domain, clients.get("_rdap_lock"))
        return {"available": not r["exists"],
                "created": r["registered_at"] if r["exists"] else None,
                "free_date": None, "whois_source": "rdap", "status": list(r.get("status") or [])}
    pr = _aparser_whois(clients["aparser"], domain, clients.get("_whois_lock"))
    return {"available": pr.get("available"), "created": pr.get("created"),
            "free_date": None, "whois_source": "aparser", "status": []}
```

**`backend/app/services/scoring.py`:**

1) `FunnelState` — после строки `    alive: bool = True` добавить последнее поле (с дефолтом, чтобы старые
вызовы не сломались):

```python
    source: str | None = None       # v2: list/emd/… — W0 и W4/W6 ведут себя по-разному для EMD
```

2) В `FUNNEL_STAGES` первые два элемента (`{"key": "rd", …}`, `{"key": "whois", …}`) заменить на:

```python
    {"key": "t0", "label": "фильтры (зона/бренд)"},
    {"key": "avail", "label": "доступность (RDAP/whois)"},
```

3) В `_BLIND_RU` значение ключа `"whois"` (две строки, от `"whois": "возраст НЕ проверен: whois не ответил`
до `"занятость домена тоже не сверена",`) заменить на:

```python
    "whois": "доступность и возраст НЕ проверены: RDAP/whois не ответил — занятость не сверена, "
             "гейт «слишком молодой» не применялся",
```

4) Функцию `_make_clients` заменить целиком:

```python
def _make_clients() -> dict:
    """Собрать интеграционные клиенты один раз на прогон (переиспользуются между доменами).
    Локи — для предохранителей под конкурентностью волн (services/whois.py): счётчики сбоев
    живут на инстансах клиентов и меняются из 12 потоков волны."""
    from app.integrations.wayback import WaybackClient
    from app.integrations.rkn import RknClient
    from app.integrations.blacklist import BlacklistClient
    from app.integrations.searxng import SearxngClient
    from app.integrations.aparser import AParserClient
    from app.integrations.rdap import RdapClient
    return {
        "wayback": WaybackClient(), "rkn": RknClient(), "blacklist": BlacklistClient(),
        "searxng": SearxngClient(), "aparser": AParserClient(), "rdap": RdapClient(),
        "_whois_lock": threading.Lock(), "_rdap_lock": threading.Lock(),
        "_safebrowsing_lock": threading.Lock(),
    }
```

5) Функцию `_deadline_from_whois` удалить целиком (от `def _deadline_from_whois(` до комментария
`# После скольких сбоев ПОДРЯД safebrowsing_check …`, который остаётся). Проекцию «освободится» давал
только TCI.

6) Строку `_CONCURRENCY = {"whois": 12, "risk": 12, "history": 4, "ahrefs": 2}` заменить на
`_CONCURRENCY = {"avail": 12, "risk": 12, "history": 4, "ahrefs": 2}`.

7) Функцию `_wave_t0` заменить целиком:

```python
def _wave_t0(states: list, st: dict) -> None:
    """W0 — без сети: флаги фида, белый список зон, чужие VPN-бренды. Зоны режутся здесь ещё раз,
    хотя discovery режет их на входе: ручной список приходит без фильтра (оператор должен
    увидеть причину), а белый список мог сузиться после того, как домен попал в пул."""
    from app.services.domain_filters import brand_hit, tld_match
    for s in states:
        if not s.alive:
            continue
        if s.feed_flags and any(s.feed_flags.get(k) for k in ("rkn", "judicial", "block")):
            s.reject_reason, s.alive = "feed_flag", False
        elif not tld_match(s.domain, st["tld_allowlist"]):
            s.reject_reason, s.alive = "tld_closed", False
        elif brand_hit(s.domain, st["brand_tokens"]):
            s.sig["trademark_risk"] = True
            s.reject_reason, s.alive = "trademark", False
        elif (s.referring_domains is not None
              and s.referring_domains < st["min_referring_domains"]):
            s.reject_reason, s.alive = "low_rd", False     # уедет в W4 (Задача 11)
```

8) Функции `_whois_one` и `_wave_whois` удалить и на их месте вставить:

```python
def _avail_one(s: FunnelState, clients: dict, budget, st: dict) -> None:
    """W2 для ОДНОГО домена: доступность + дата регистрации. RDAP бесплатный и бюджета не тратит;
    кап max_whois_per_run — только на whois:43 через A-Parser (зоны без RDAP).

    Возраст здесь только ЗАПИСЫВАЕТСЯ (`whois_created`, информационно `age_years`), отказа
    `too_young` нет (решение оператора Р5): у перехваченного и снова дропающегося домена RDAP
    показывает дату ПОСЛЕДНЕЙ регистрации — 15 лет истории выглядели бы как 3 года. Возраст для
    решения — старшая из даты RDAP и первого снимка Wayback, отказ — в W5 (history)."""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)

    rdap = clients.get("rdap")
    via_rdap = rdap is not None and rdap.has_rdap(s.domain)   # бутстрап не бросает (rdap._FALLBACK)
    if not via_rdap and budget is not None and not budget.take():
        s.unresolved_why, s.alive = "budget", False
        return

    try:
        pr = whois_router.probe(s.domain, clients)
        wc = pr.get("created")
        if wc is not None and wc.tzinfo is None:
            wc = wc.replace(tzinfo=timezone.utc)   # наивная дата whois:43 — UTC, иначе TypeError ниже
        age = (now - wc).days / 365.25 if wc is not None else None
    except Exception as e:  # noqa: BLE001 — и сбой канала, и битая дата: домен НЕ идёт дальше «живым без вердикта»
        code = "circuit_open" if isinstance(e, whois_router.CircuitOpen) else type(e).__name__
        s.sig["errors"].append(f"whois:{code}")
        if s.lane != "bid":
            s.unresolved_why, s.alive = "whois_failed", False
            return
        pr, wc, age = {"available": None, "status": []}, None, None

    if pr.get("available") is not None:
        s.sig["acquirability_checked_at"] = now
    s.sig["whois_source"] = pr.get("whois_source")
    s.sig["whois_created"] = wc
    if age is not None:
        s.sig["age_years"], s.sig["age_source"] = round(age, 2), "whois"

    # Ручной список без лейна, а RDAP говорит «pending delete»/«redemption period»: это дроп, а не
    # чужой занятый домен. Без лейна он висел бы taken_undated до самого дропа (находка 1.12).
    # Даты дропа у него нет — оценка по статусу (находка R2-11): redemption period — 30 суток выкупа
    # + 5 удаления, pending delete — 5. Без даты bid-домен никогда не закрылся бы обычным путём
    # acquirability_verdict (у bid без даты судить нечем). В sig, а не в state: _commit_result
    # пишет оценку только в ПУСТУЮ колонку — реальную дату дропа она не перебивает.
    rdap_status = set(pr.get("status") or [])
    if s.lane is None and {"pending delete", "redemption period"} & rdap_status:
        s.lane = "bid"
        if s.acquire_deadline is None:
            days = 35 if "redemption period" in rdap_status else 5
            s.sig["acquire_deadline"] = now + timedelta(days=days)
    if s.lane == "bid":
        s.sig["lane"] = "bid"
        return

    v = acquirability_verdict(pr.get("available"), s.acquire_deadline, now, lane=s.lane)
    if v == "taken":
        s.reject_reason, s.alive = "not_acquirable", False
    elif v == "free":
        s.sig["lane"] = "free"
    else:
        s.unresolved_why = ("waiting" if v == "waiting"
                            else "whois_unclear" if pr.get("available") is None
                            else "taken_undated")
        s.alive = False


def _wave_avail(states: list, clients: dict, budget, st: dict, run) -> None:
    """W2 — доступность (RDAP, иначе whois:43 через A-Parser), конкурентно на выживших после W0."""
    _run_concurrent(states, _CONCURRENCY["avail"], run, "avail",
                    lambda s: _avail_one(s, clients, budget, st))
```

9) Функцию `_history_one` заменить целиком (возраст по старшей дате и гейт `too_young` — здесь, Р5):

```python
def _history_one(s: FunnelState, clients: dict, st: dict) -> None:
    """W5 для ОДНОГО домена: Wayback-история + категорийный hard-reject + возраст по старшей из
    двух дат (RDAP/whois из W2 и первый снимок) и гейт `too_young` (Р5)."""
    try:
        hist = clients["wayback"].classify_history(s.domain)
        pf = hist.get("prior_flags") or {}
        s.sig["prior_flags"] = pf
        s.sig["wayback_checked"] = hist.get("wayback_checked")
        s.sig["history_evidence"] = hist.get("evidence") or []
        s.sig["sampled"] = hist.get("sampled")
        s.sig["first_seen"] = hist.get("first_seen")
        # возраст для решения — СТАРШАЯ из даты RDAP/whois (W2) и первого снимка (Р5): у
        # перехваченного и снова дропающегося домена RDAP показывает ПОСЛЕДНЮЮ регистрацию
        wb_age = hist.get("age_years")
        if wb_age is not None and (s.sig.get("age_years") is None or wb_age > s.sig["age_years"]):
            s.sig["age_years"], s.sig["age_source"] = wb_age, "wayback"
        if any(pf.get(k) for k in cfg.HARD_REJECT_FLAGS):
            s.reject_reason = "history_dirty"
            s.alive = False
            return
    except Exception as e:  # noqa: BLE001
        s.sig["errors"].append(f"wayback:{type(e).__name__}")
        # Wayback не ответил (archive.org регулярно отдаёт 429/503) — вторая дата возраста
        # НЕИЗВЕСТНА, а не «молода». Отказ too_young по одной дате RDAP окончателен и потерял бы
        # перехваченный дроп (находка R2-1): гейт не судит, домен идёт дальше «вслепую» — `wayback:`
        # в errors держит его вне пакета (blind_reason).
        return

    # Гейт молодости — ЗДЕСЬ, а не в W2 (решение оператора Р5), по старшей из двух дат и ПОСЛЕ
    # history_dirty (грязь — более сильная причина). Первый снимок не раньше регистрации, так что
    # и старшая дата — нижняя оценка возраста: молодым домен объявляется только если молоды обе.
    # EMD — новорег: «молодость» — его суть, гейт его не судит (находка R2-14).
    if (s.source != "emd" and s.sig.get("age_years") is not None
            and s.sig["age_years"] < st["min_age_years"]):
        s.reject_reason = "too_young"
        s.alive = False
```

10) В таблице волн `_run_waves` две первые строки заменить на:

```python
        ("t0", "фильтры", lambda alive: _wave_t0(alive, st)),
        ("avail", "доступность", lambda alive: _wave_avail(alive, clients, whois_b, st, run)),
```

11) `_commit_result`:
- обе обратные записи дедлайна удалить (находка R2-12: их питала только проекция TCI, `_deadline_from_whois`
  удалён в п. 5, а `state.acquire_deadline` больше никто не меняет — запись могла лишь затереть дату
  параллельного discovery снимком, взятым в начале прогона). Это пары строк
  `            if state.acquire_deadline != d.acquire_deadline:` / `                d.acquire_deadline = state.acquire_deadline`
  в unresolved-ветке и `        if state.acquire_deadline != d.acquire_deadline:` / `            d.acquire_deadline = state.acquire_deadline`
  перед `if reject:` (вторую — вместе с пустой строкой после неё). Вместо них сразу после строк

```python
        if d is None or d.status not in ("discovered", "scored", "rejected"):
            return {"domain": state.domain, "status": d.status if d else "gone",
                    "skipped": "status"}
```

  (перед пустой строкой и `if state.unresolved_why is not None:`) вставить:

```python

        # Оценка дедлайна домена, которого W2 перевела в bid по статусу RDAP (находка R2-11), —
        # и для решённого, и для unresolved исхода. Только в ПУСТУЮ колонку: реальную дату дропа
        # (её мог записать и параллельный discovery посреди прогона) оценка не перебивает.
        if sig.get("acquire_deadline") is not None and d.acquire_deadline is None:
            d.acquire_deadline = sig["acquire_deadline"]
```

- в unresolved-ветке удалить три строки `if sig.get("deadline_source"): …` (до `"deadline_source": sig["deadline_source"]}`
  включительно);
- в кортеж колонок «пишем только проверенное» (`for col in ("lane", "whois_created", …, "referring_domains"):`)
  дописать последним `"trademark_risk"`;
- в `d.score_breakdown = {…}` удалить элемент `"deadline_source": _kept("deadline_source")` (вместе с
  запятой перед ним: последним остаётся `"whois_source": _kept("whois_source")}`).

12) `score_domain`: в создании `FunnelState(...)` после `feed_flags=d.feed_flags` добавить
`source=d.source`. `score_pending`: в `select(Domain.id, Domain.domain, Domain.lane, Domain.referring_domains,
Domain.acquire_deadline, Domain.feed_flags)` дописать `Domain.source`; в построении `states` —
`feed_flags=flags, source=src)` и распаковку `for (did, name, lane, rd, deadline, flags, src) in rows`.

**`diagnostics.py`:** в `_spec()` удалить запись `("tci", "TCI whois", …)` вместе с тремя строками
комментария над ней (`# Не critical: TCI — оптимизация …`).

Удалить файлы: `git rm backend/app/integrations/whois_tci.py backend/tests/test_whois_tci.py`.

- [ ] **Шаг 4: Запустить тесты задачи — проходят**

Запуск: `cd backend && ../.venv/bin/python -m pytest tests/test_waves_v2.py tests/test_whois.py -v`
Ожидание: 25 passed.

- [ ] **Шаг 5: Старые тесты — явный список (удаляется только TCI; тесты гейтов и инвариантов — переписываются)**

Правило перевода доменов (находка 5.2): `.ru` → `.com` — ТОЛЬКО в тестах, которые идут через воронку
скоринга (`score_domain`, `score_pending`, `_run_waves`, `_wave_*`), включая те, что проходили вхолостую
через `tld_closed`. Wayback-фикстуры (`test_wayback_window.py`, кроме двух тестов ниже) и тесты очереди
выкупа M2 (`buy-me.ru`, `q-panel.ru`, `test_transitions.py` вне четырёх тестов ниже) НЕ трогать: там
`.ru` — данные (сетка тарифов backorder — только .RU/.РФ). «.ru → .com в теле теста» значит: каждый
строковый литерал домена `"<имя>.ru"` (и f-строку `f"<имя>.ru"`) в КОДЕ функции заменить на `"<имя>.com"`.
Правило — только про строковые литералы: упоминания доменов в докстрингах и комментариях (`old-bid.ru`,
«витрины reg.ru/sweb») можно не трогать, а `source="reg_ru"` — не домен.

**A. Фейки TCI — убрать везде.** Во всех файлах `backend/tests/` удалить пару
`"tci": type("T", (), {"handles": lambda self, d: False})()` вместе с её запятой (она есть в
`test_aparser_envelope.py`, `test_funnel.py` — 4 места, `test_history_verdict.py`, `test_m1_fixes.py`,
`test_pipeline.py`, `test_recheck_acquirability.py`, `test_rescore.py` — 2, `test_scoring_waves.py`,
`test_transitions.py` — 2, `test_wayback_window.py`). Проверка: `grep -rn '"tci"' backend/tests` —
только `test_whois.py` (`"tci" not in c`).

**B. `test_whois_tci.py` удалён (26 тестов).** Переехали в `test_whois.py` (уже в шаге 1):
`test_aparser_whois_circuit_breaker_skips_after_three_consecutive_failures` и
`test_aparser_whois_circuit_breaker_resets_on_success_between_failures` (на зоне `.mx`),
сквозные `test_funnel_uses_tci_for_ru_without_touching_aparser` →
`test_funnel_routes_whois_through_rdap_without_touching_aparser` и
`test_funnel_whois_decides_acquirability_for_non_bid_domain` →
`test_funnel_rdap_decides_acquirability_for_non_bid_domain`; проверка ключа маршрута —
`test_make_clients_wires_rdap_and_its_lock`. Остальное (разбор TCI, его маршрутизатор и предохранитель,
`_deadline_from_whois`, `test_aparser_whois_circuit_breaker_covers_fallback_path_too` — фолбэк после TCI)
удалено вместе с TCI.

**C. `test_funnel.py` — весь файл воронки.** Во всём файле `.ru` → `.com` в строковых литералах доменов
(в т.ч. дефолт `_mk`: `"x.ru"` → `"x.com"`). Две функции заменить целиком (Р5 переносит гейт молодости
из W2 в волну истории):
- `test_too_young_rejects_before_wayback` →

```python
def test_too_young_rejects_in_history_wave_by_the_older_date():
    """Р5: W2 возраст только записывает — Wayback зовётся и для молодого по RDAP/whois домена:
    у перехваченного домена это дата ПОСЛЕДНЕЙ регистрации. Отказ too_young — в волне истории и
    только если молоды ОБЕ даты; молодая регистрация при старом архиве — не отказ."""
    young = datetime.now(timezone.utc) - timedelta(days=365)   # 1 год
    did = _mk(domain="young.com", referring_domains=5, lane="bid")
    wb = _Wayback(age_years=1.0)
    out = scoring.score_domain(did, clients=_clients(young, wb))
    assert out["status"] == "rejected" and out["reject_reason"] == "too_young"
    assert wb.calls == 1
    did = _mk(domain="recaught.com", referring_domains=5, lane="bid")
    out = scoring.score_domain(did, clients=_clients(young, _Wayback(age_years=9.0)))
    assert out["reject_reason"] is None and out["status"] == "scored"
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
    assert float(d.age_years) == 9.0 and d.score_breakdown["age_source"] == "wayback"
```

- `test_runtime_min_age_years_rejects_too_young` →

```python
def test_runtime_min_age_years_rejects_too_young():
    """Spec §G: рантайм min_age_years из /settings — 4-летний (и по whois, и по архиву) домен
    отклоняется too_young при поднятом пороге в 5 лет. Гейт — в волне истории (Р5)."""
    from app.services import settings as st
    did = _mk(domain="four-years.com", referring_domains=50, lane="bid")
    wb = _Wayback(age_years=4.0)
    st.update_settings(min_age_years=5.0)
    four_years = datetime.now(timezone.utc) - timedelta(days=365 * 4)
    out = scoring.score_domain(did, clients=_clients(four_years, wb))
    assert out["status"] == "rejected" and out["reject_reason"] == "too_young"
    assert wb.calls == 1                          # Р5: возраст судит волна истории по старшей дате
```

**D. `test_aparser_envelope.py`.** `.ru` → `.com` в телах `test_funnel_whois_down_domain_is_not_auto_approved`,
`test_funnel_whois_down_and_empty_archive_is_not_auto_approved`,
`test_funnel_whois_alive_domain_is_scored_and_bulk_ok`, `test_funnel_archive_age_still_gates_too_young`,
`test_funnel_whois_down_not_excluded_from_pool`. Функцию `test_funnel_too_young_still_rejected_when_whois_answers`
заменить целиком:

```python
def test_funnel_too_young_still_rejected_when_whois_answers():
    """Гейт молодости — на СТАРШЕЙ из двух дат (Р5), в волне истории: балл его не дублирует (юный
    домен с большим RD по баллу прошёл бы). whois отвечает «год назад», и архив помнит только
    год — домен-однолетка честно отбраковывается, хоть и уже после Wayback."""
    young = datetime.now(timezone.utc) - timedelta(days=365)
    did = _add(domain="young.com",
               acquire_deadline=datetime.now(timezone.utc) + timedelta(days=5))
    out = scoring.score_domain(did, _clients({"available": False, "created": young}, _WaybackAged(1.0)))
    assert out["status"] == "rejected" and out["reject_reason"] == "too_young", out
```

**E. `test_scoring_waves.py`.**
- `test_wave_t0_rejects_feed_flag_and_low_rd_without_touching_alive_ones` — заменить целиком:

```python
def test_wave_t0_rejects_feed_flag_and_low_rd_without_touching_alive_ones():
    st = {"min_referring_domains": 5, "tld_allowlist": ["com"], "brand_tokens": []}
    flagged = scoring.FunnelState(domain_id=1, domain="a.com", lane=None,
                                  referring_domains=10, acquire_deadline=None,
                                  feed_flags={"rkn": True})
    low_rd = scoring.FunnelState(domain_id=2, domain="b.com", lane=None,
                                 referring_domains=1, acquire_deadline=None,
                                 feed_flags=None)
    ok = scoring.FunnelState(domain_id=3, domain="c.com", lane=None,
                             referring_domains=50, acquire_deadline=None,
                             feed_flags=None)
    states = [flagged, low_rd, ok]
    scoring._wave_t0(states, st)
    assert flagged.alive is False and flagged.reject_reason == "feed_flag"
    assert low_rd.alive is False and low_rd.reject_reason == "low_rd"
    assert ok.alive is True and ok.reject_reason is None
```

- помощник `_clients_no_tci` — заменить целиком:

```python
def _clients_aparser_only(**kw):
    return {"aparser": _FakeAparserWhois(**kw), "_whois_lock": threading.Lock()}
```

- `test_wave_whois_rejects_too_young_bid_domain` → (Р5, новое имя)

```python
def test_wave_avail_records_young_age_but_does_not_reject_bid_domain():
    """Р5: W2 возраст только записывает. Молодая дата регистрации у bid-домена — не отказ: у
    перехваченного домена это дата ПОСЛЕДНЕЙ регистрации; судит волна истории по старшей дате."""
    st = {"min_age_years": 3.0}
    young = datetime.now(timezone.utc) - timedelta(days=200)
    s = scoring.FunnelState(domain_id=1, domain="young.com", lane="bid",
                            referring_domains=5, acquire_deadline=None, feed_flags=None)
    clients = _clients_aparser_only(available=False, created=young)
    scoring._wave_avail([s], clients, budget=None, st=st, run=None)
    assert s.alive is True and s.reject_reason is None
    assert s.sig["whois_created"] == young and s.sig["age_years"] < 1
```

- `test_wave_whois_marks_free_lane_and_survives` →

```python
def test_wave_avail_marks_free_lane_and_survives():
    st = {"min_age_years": 3.0}
    old = datetime.now(timezone.utc) - timedelta(days=365 * 10)
    s = scoring.FunnelState(domain_id=2, domain="free.com", lane=None,
                            referring_domains=5, acquire_deadline=None, feed_flags=None)
    clients = _clients_aparser_only(available=True, created=old)
    scoring._wave_avail([s], clients, budget=None, st=st, run=None)
    assert s.alive is True and s.sig["lane"] == "free"
```

- `test_wave_whois_budget_exhausted_marks_unresolved_without_network_call` →

```python
def test_wave_avail_budget_exhausted_marks_unresolved_without_network_call():
    st = {"min_age_years": 3.0}
    s = scoring.FunnelState(domain_id=3, domain="over.com", lane=None,
                            referring_domains=5, acquire_deadline=None, feed_flags=None)
    aparser = _FakeAparserWhois(available=True)
    clients = {"aparser": aparser, "_whois_lock": threading.Lock()}
    budget = scoring.Budget(0)
    scoring._wave_avail([s], clients, budget=budget, st=st, run=None)
    assert s.alive is False and s.unresolved_why == "budget"
    assert aparser.calls == 0          # бюджет исчерпан ДО сети — вызова не было
```

- `test_wave_whois_breaker_lock_has_no_lost_increments_under_real_overlap` — переименовать в
  `test_wave_avail_breaker_lock_has_no_lost_increments_under_real_overlap`, в теле `.ru` → `.com` и
  `scoring._wave_whois(` → `scoring._wave_avail(`; в докстринге класса `_SlowFakeRiskClients` ссылку
  `test_wave_whois_breaker_lock_has_no_lost_increments_under_real_overlap` заменить на новое имя;
- `test_wave_whois_actually_runs_concurrently_not_serially` → (находка 5.17: подсчёт одновременных
  входов вместо тайминга)

```python
def test_wave_avail_actually_runs_concurrently_not_serially():
    """Конкурентность волны — подсчётом одновременных входов, а не таймингом (находка 5.17:
    прежний порог 0.5 с флапал под нагрузкой). Барьер на _CONCURRENCY["avail"] участников
    пропускает, только если столько вызовов РЕАЛЬНО стоят в whois одновременно; последовательный
    обход упёрся бы в таймаут барьера, и домены упали бы. Пик выше пула — тоже провал."""
    n = scoring._CONCURRENCY["avail"]
    barrier = threading.Barrier(n, timeout=10)
    lock, active, peak = threading.Lock(), [0], [0]

    class _MeetingAparser:
        def whois_probe(self, d):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            try:
                barrier.wait()
            finally:
                with lock:
                    active[0] -= 1
            return {"available": True, "created": datetime.now(timezone.utc) - timedelta(days=3650)}

    states = [scoring.FunnelState(domain_id=i, domain=f"slow{i}.com", lane=None,
                                  referring_domains=5, acquire_deadline=None,
                                  feed_flags=None) for i in range(2 * n)]
    clients = {"aparser": _MeetingAparser(), "_whois_lock": threading.Lock()}
    scoring._wave_avail(states, clients, budget=None, st={"min_age_years": 3.0}, run=None)
    assert peak[0] == n                     # ровно пул: и не последовательно, и не шире
    assert all(s.alive for s in states)
```

- `test_wave_history_keeps_whois_age_over_wayback_fallback` → (Р5)

```python
def test_wave_history_takes_older_of_whois_and_wayback_age():
    """Р5: возраст для решения — старшая из даты RDAP/whois и первого снимка. Архивный возраст
    больше whois-ного — побеждает архив; меньше — остаётся whois-ный. Раньше whois всегда
    перебивал архив, и перехваченный домен с долгой историей выглядел молодым."""
    st = {"min_age_years": 3.0}
    recaught = scoring.FunnelState(domain_id=3, domain="c.com", lane=None, referring_domains=5,
                                   acquire_deadline=None, feed_flags=None)
    recaught.sig.update({"whois_created": datetime(2023, 1, 1, tzinfo=timezone.utc),
                         "age_years": 3.5, "age_source": "whois"})
    old = scoring.FunnelState(domain_id=4, domain="d.com", lane=None, referring_domains=5,
                              acquire_deadline=None, feed_flags=None)
    old.sig.update({"whois_created": datetime(2010, 1, 1, tzinfo=timezone.utc),
                    "age_years": 16.0, "age_source": "whois"})
    scoring._wave_history([recaught, old], {"wayback": _FakeWayback(age_years=9.0)}, st, run=None)
    assert recaught.alive and recaught.sig["age_years"] == 9.0
    assert recaught.sig["age_source"] == "wayback"
    assert old.alive and old.sig["age_years"] == 16.0 and old.sig["age_source"] == "whois"
```

- `test_run_waves_shrinks_pool_across_stages_and_writes_wave_history` — заменить целиком (сжатие пула
  теперь даёт W2 «занят без лейна и даты», а не `too_young`; ключи стадий `t0`/`avail`, подпись
  `доступность`):

```python
def test_run_waves_shrinks_pool_across_stages_and_writes_wave_history():
    """10 доменов -> половина выпадает на W2 (занят, лейна и даты нет -> не решить) -> итог:
    waterfall в job_run.message показывает уменьшение пула по волнам."""
    from app.services import jobs

    ids = [_mk_domain(domain=f"pool{i}.com", referring_domains=5) for i in range(10)]
    states = [scoring.FunnelState(domain_id=did, domain=f"pool{i}.com", lane=None,
                                  referring_domains=5, acquire_deadline=None, feed_flags=None)
             for i, did in enumerate(ids)]
    old = datetime.now(timezone.utc) - timedelta(days=365 * 10)

    class _Ap:
        def __init__(self):
            self.n = 0
        def whois_probe(self, d):
            self.n += 1
            # чётные — заняты без даты дропа и без лейна: W2 их не решает (taken_undated)
            return {"available": self.n % 2 != 0, "created": old}
        def safebrowsing_check(self, d): return False
        def ahrefs_probe(self, d): return {"dr": 1.0, "backlinks": 0, "referring_domains": None}
    clients = {"aparser": _Ap(),
              "rkn": type("R", (), {"is_listed": lambda self, d: False})(),
              "blacklist": type("B", (), {"is_blacklisted": lambda self, d: False})(),
              "searxng": type("S", (), {"indexed_echo": lambda self, d: True})(),
              "wayback": _FakeWayback(dirty=False, age_years=9.0),
              "_whois_lock": threading.Lock(), "_safebrowsing_lock": threading.Lock()}
    st = {"min_age_years": 3.0, "approve_at": 0.7, "manual_review_at": 0.4,
         "min_referring_domains": 1, "tld_allowlist": ["com"], "brand_tokens": []}

    with jobs.track("score", stages=[dict(x) for x in scoring.FUNNEL_STAGES]) as run:
        out = scoring._run_waves(states, clients, st, whois_budget=None,
                                 ahrefs_budget=None, run=run)
    assert len(out) == 10
    survived = [s for s in states if s.alive]
    assert 0 < len(survived) < 10          # реально сжалось, не всё выжило и не всё умерло
    last = jobs.last("score")
    assert "доступность" in last["message"] and ("->" in last["message"] or "→" in last["message"])
    # мини-полоски на чипах (2026-07-21): before/after написаны на КАЖДУЮ стадию, не только
    # в текстовый waterfall — jobCard() их и рисует.
    by_key = {s["key"]: s for s in last["stages"]}
    assert by_key["t0"]["before"] == 10 and by_key["t0"]["after"] == 10
    assert by_key["avail"]["before"] == 10 and by_key["avail"]["after"] == len(survived)
    assert by_key["ahrefs"]["before"] == by_key["ahrefs"]["after"] == len(survived)
```

- `test_run_waves_cancellation_between_waves_preserves_partial_progress` — заменить целиком:

```python
def test_run_waves_cancellation_between_waves_preserves_partial_progress():
    """НЕ ловим jobs.Cancelled сами вокруг вызова: jobs.track() ловит его ВНУТРИ своего
    generator'а (except Cancelled -> _close(..., "cancelled"), БЕЗ re-raise) — поймай
    исключение раньше, до границы `with`, и track() увидит нормальный выход из `with`,
    закрыв прогон как "done", а не "cancelled" (найдено ревью Task 1, 2026-07-21, тот же
    паттерн уже сломал сходный тест в test_scoring_waves.py при первом написании)."""
    from app.services import jobs

    ids = [_mk_domain(domain=f"cancel{i}.com", referring_domains=5) for i in range(5)]
    states = [scoring.FunnelState(domain_id=did, domain=f"cancel{i}.com", lane=None,
                                  referring_domains=5, acquire_deadline=None, feed_flags=None)
             for i, did in enumerate(ids)]
    clients = {"aparser": type("Ap", (), {
                  "whois_probe": lambda self, d: {"available": True, "created": datetime.now(timezone.utc) - timedelta(days=3650)}})(),
              "_whois_lock": threading.Lock()}
    st = {"min_age_years": 3.0, "approve_at": 0.7, "manual_review_at": 0.4,
         "min_referring_domains": 1, "tld_allowlist": ["com"], "brand_tokens": []}

    with jobs.track("score", stages=[dict(x) for x in scoring.FUNNEL_STAGES]) as run:
        jobs.request_cancel("score")
        scoring._run_waves(states, clients, st, whois_budget=None,
                           ahrefs_budget=None, run=run)
    last = jobs.last("score")
    assert last["status"] == "cancelled"
```

- `test_score_pending_builds_states_with_lane_and_rd_from_one_query` — `.ru` → `.com` в теле;
- `test_score_pending_reports_honest_count_when_cancelled_after_partial_commits` — заменить целиком
  (раньше проходил вхолостую через `tld_closed`; 3 домена теперь выходят на W2 как `not_acquirable`):

```python
def test_score_pending_reports_honest_count_when_cancelled_after_partial_commits(monkeypatch):
    """Task 9 self-review (c). `_run_waves()` на отмене делает `raise jobs.Cancelled()` ДО
    своего `return results` — локальный список результатов теряется вместе со стеком
    развёртывания, ХОТЯ `_checkpoint()` внутри уже мог реально закоммитить в БД домены
    волной(ами) РАНЬШЕ той, где прилетела отмена. Если считать `done=len(results)` голым — при
    отмене он ВСЕГДА 0, даже если реально отброшено N доменов: контракт docstring'а («частичное
    число, не len(rows)») соврёт. Здесь 2 домена low_rd (W0) + 3 not_acquirable (W2: лейн free,
    а домен занят) реально оседают в БД как rejected до отмены на волне risk."""
    ids = [_mk_domain(domain=f"lowrd{i}.com", referring_domains=0, lane="bid") for i in range(2)]
    ids += [_mk_domain(domain=f"taken{i}.com", referring_domains=5, lane="free") for i in range(3)]

    from app.services import jobs as jobs_mod
    real_wave_risk = scoring._wave_risk

    def spy_risk(states, clients, run):
        # к этому моменту W0 и W2 УЖЕ закоммитили все 5 (alive пуст) — отмена здесь
        # проверяет именно то, что происходит МЕЖДУ волнами, после реальных чекпоинтов.
        jobs_mod.request_cancel("score")
        return real_wave_risk(states, clients, run)
    monkeypatch.setattr(scoring, "_wave_risk", spy_risk)

    class _Ap:
        def whois_probe(self, d):
            return {"available": False, "created": datetime.now(timezone.utc) - timedelta(days=3650)}
    monkeypatch.setattr(scoring, "_make_clients", lambda: {
        "aparser": _Ap(), "_whois_lock": threading.Lock()})

    n = scoring.score_pending(limit=10)
    assert n == 5                        # все 5 реально осели в БД, не 0
    assert jobs_mod.last("score")["status"] == "cancelled"
    with db.SessionLocal() as s:
        statuses = {s.get(Domain, i).status for i in ids}
    assert statuses == {"rejected"}
```

**F. `.ru` → `.com` в телах сквозных тестов** (без других правок):
`test_history_verdict.py::test_funnel_marks_history_unknown_without_errors`,
`test_pipeline.py::test_scoring_hard_reject_on_rkn`,
`test_rescore.py::test_rescore_keeps_authority_without_a_new_dr_observation`,
`test_transitions.py::test_rescoring_is_the_honest_way_back`,
`test_transitions.py::test_rescore_early_exit_does_not_erase_rkn_evidence`,
`test_transitions.py::test_rescore_t0_exit_does_not_erase_history_evidence`,
`test_transitions.py::test_rescore_that_actually_ran_the_checks_still_rehabilitates`,
`test_wayback_window.py::test_score_breakdown_carries_history_evidence`,
`test_wayback_window.py::test_rejected_domain_also_keeps_its_evidence`,
`test_recheck_acquirability.py::test_scoring_stamps_acquirability_so_recheck_does_not_redo_it`.
Тесты реабилитации в `test_transitions.py` — инвариант «перескор не отмывает», их только переводим.

**G. `test_m1_fixes.py::test_score_pending_isolates_failure`:** `.ru` → `.com` в теле; `_whois_one` →
`_avail_one` (все четыре вхождения: два в докстринге, два в коде), «тело T1» → «тело W2».

**H. `test_funnel_stages_and_job_message.py::test_task10_funnel_stages_has_5_not_6`:** строку
`assert keys == ["rd", "whois", "risk", "history", "ahrefs"]` заменить на
`assert keys == ["t0", "avail", "risk", "history", "ahrefs"]`.

**I. `test_recheck_acquirability.py`:** autouse-фикстуру `_tci_disabled` удалить целиком (с декоратором
и докстрингом): RDAP в тестах закрывает autouse `_no_paid_keys` из Задачи 3 (бутстрап `{}` — whois
идёт в подменённый A-Parser).

**J. Докстринг `test_job_stages.py::test_score_pending_stops_on_cancel`:** «(workers=12 в _wave_whois)» →
«(workers=12 в _wave_avail)».

`deadline_source` в `panel.py`/шаблонах/`test_inbox.py` не трогать — их чистит Задача 15.

Проверка: `grep -rn "TciWhois\|whois_tci\|_deadline_from_whois\|_wave_whois\|_whois_one" backend/app backend/tests` —
только комментарий о `TciWhoisClient` над `_APARSER_SAFEBROWSING_LIMIT` в `scoring.py` (его удаляет
Задача 10 вместе с SafeBrowsing) и докстринг `test_whois.py`.

- [ ] **Шаг 6: Сьют + линт**

Запуск: `cd backend && ../.venv/bin/python -m pytest tests -q && cd .. && .venv/bin/python -m pyflakes backend/app backend/tests`
Ожидание: **831 passed** (832 − 26 из `test_whois_tci.py` + 14 `test_waves_v2.py` + 11 `test_whois.py`),
pyflakes пуст.

- [ ] **Шаг 7: Коммит**

```bash
git add -A backend/app backend/tests
git commit -m "feat(scoring): W0 зоны/бренды, W2 доступность через RDAP, возраст по старшей дате; TCI удалён"
```

---

### Задача 10: W3 «риск» — Google Web Risk + Spamhaus только с DQS; удаление РКН, Safe Browsing, эха

**Что меняется и почему.**
- Web Risk — коммерчески легальная замена Safe Browsing. Без ключа проверка не делается: домен
  «вслепую» (`webrisk:not_configured`), пакет его не возьмёт. Spamhaus DBL — только с DQS-ключом.
- **Угроза Web Risk не пишется в колонку `blacklisted`** (находка 1.5): это колонка Spamhaus, без DQS
  её никто не перепроверит и не снимет. Улика — `score_breakdown.webrisk_threats` (`_commit_result`
  хранит её через `_kept`), `transitions.dirty_reason` проверяет её — перескор, на котором Web Risk
  упал, угрозу не отмывает.
- Предохранитель Web Risk (находка 3.2): `whois.guarded` из Задачи 9, счётчик под локом
  `_webrisk_lock`, после 3 сбоев подряд — `webrisk:circuit_open` без сети до конца прогона.
- Авто-одобрения нет с Задачи 8, поэтому «risk-гарда» в `_decide` больше нет: «вслепую» по
  `webrisk:`/`blacklist:` держит `blind_reason` → `bulk_ok`.

**Files:**
- Modify:
  - `backend/app/services/scoring.py` — `_risk_one`, `_wave_risk`, `_make_clients`, `_BLIND_RU`,
    `FUNNEL_STAGES`, колонки и улики `_commit_result`; удалить `_APARSER_SAFEBROWSING_LIMIT` и импорт `nullcontext`;
  - `backend/app/services/transitions.py` — `dirty_reason` видит `webrisk_threats`; самопроверка `__main__`
    (находка R2-13: объектам `NS` нужен `score_breakdown`, иначе `python -m app.services.transitions` падает
    `AttributeError`);
  - `backend/app/integrations/aparser.py` — удалить `safebrowsing_check`, `archive_probe`,
    `_parse_safebrowsing`, `_parse_archive`, `_RE_SAFEBROWSING`, `_RE_ARCHIVE`;
  - `backend/app/integrations/searxng.py` — удалить `indexed_echo` (M5 зовёт `search`/`search_full`, не его);
  - `backend/app/services/diagnostics.py` — убрать `rkn`; `blacklist` пинговать только при `SPAMHAUS_DQS_KEY`.
- Delete: `backend/app/integrations/rkn.py`.
- Test: `backend/tests/test_waves_v2.py` (дописать). Старые тесты — явный список в шаге 5.

**Interfaces:**
- Consumes: `WebRiskClient` (Задача 3: `configured`, `threats(domain) -> list[str]`), `settings.SPAMHAUS_DQS_KEY`,
  `whois.guarded`/`whois.CircuitOpen` (Задача 9).
- Produces:
  - `_risk_one(s, clients)` — без параметра лока; `_wave_risk(states, clients, run)`;
  - ключи клиентов `clients["webrisk"]`, `clients["blacklist"]`, `clients["_webrisk_lock"]`; ключей `rkn`,
    `searxng`, `_safebrowsing_lock` в `_make_clients` нет;
  - сигналы: `sig["webrisk_threats"]` (list), `sig["blacklisted"]` — ТОЛЬКО от Spamhaus (при DQS);
  - коды ошибок: `webrisk:not_configured`, `webrisk:circuit_open`, `webrisk:<Исключение>`,
    `blacklist:unavailable`, `blacklist:<Исключение>`; `_BLIND_RU["webrisk"]` =
    «риск НЕ проверен: Web Risk не настроен или не ответил»;
  - `score_breakdown["webrisk_threats"]` (через `_kept`); `transitions.dirty_reason(d)` → `"blacklist"`,
    если он непуст;
  - `_commit_result` больше не пишет колонки `rkn_listed`, `indexed_echo` (легаси, остаются в БД);
  - `FUNNEL_STAGES[2]` = `{"key": "risk", "label": "риск (Web Risk)"}`;
  - `test_waves_v2.py`: в шапке добавлен `from app.models.domain import Domain`; фейки `FakeWR(configured,
    threats, boom)` (считает `calls`) и `FakeBL(listed)` — для Задач 11–13;
  - `test_waves_v2.py::test_make_clients_has_every_breaker_lock` (находка R2-15) перечисляет локи
    предохранителей `_make_clients`: `("_whois_lock", "_rdap_lock", "_webrisk_lock")`. **Задача 12, добавив
    `_llm_lock`, дописывает его в этот кортеж** (строка `    for lock in ("_whois_lock", "_rdap_lock", "_webrisk_lock"):`).
- **Фейки интеграционных тестов воронки** (задачи 11–13): домен, который тест ждёт в пакете
  (`bulk_ok` истинно), обязан пройти настроенный Web Risk — клиент
  `"webrisk": type("WR", (), {"configured": True, "threats": lambda self, d: []})()`; без него домен
  «вслепую». Тест, где Spamhaus обязан отработать, ставит `monkeypatch.setattr(settings, "SPAMHAUS_DQS_KEY", "k")`.
- **Для Задачи 11:** `test_aparser.py` после этой задачи держит только ahrefs-тесты
  (`test_parse_ahrefs_*`, `test_ahrefs_probe_sends_expected_options`) и импорт
  `from app.integrations.aparser import _parse_ahrefs, AParserClient` — Задача 11 удаляет их вместе с
  `ahrefs_probe` (тогда файл можно удалить целиком).

- [ ] **Шаг 1: Написать падающие тесты** — в шапке `test_waves_v2.py` перед строкой
  `from app.services import scoring` добавить `from app.models.domain import Domain`, в конец файла дописать:

```python
class FakeWR:
    def __init__(self, configured=True, threats=(), boom=False):
        self.configured, self._t, self.boom, self.calls = configured, list(threats), boom, 0

    def threats(self, d):
        self.calls += 1
        if self.boom:
            raise RuntimeError("webrisk down")
        return list(self._t)


class FakeBL:
    def __init__(self, listed=False):
        self.listed, self.calls = listed, 0

    def is_blacklisted(self, d):
        self.calls += 1
        return self.listed


def test_risk_without_webrisk_key_is_blind_not_rejected(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "SPAMHAUS_DQS_KEY", "")
    s, bl = _state("ok.com"), FakeBL()
    scoring._risk_one(s, {"webrisk": FakeWR(configured=False), "blacklist": bl})
    assert s.alive and "webrisk:not_configured" in s.sig["errors"]
    assert bl.calls == 0                                   # бесплатный Spamhaus — только некоммерческий


def test_risk_threat_rejects_as_blacklist_but_leaves_blacklisted_column_alone():
    """1.5: угроза Web Risk — улика в `webrisk_threats`; колонку `blacklisted` (сигнал Spamhaus)
    Web Risk не пишет: без DQS её никто бы не перепроверил, и чистый ответ её потом не снял бы."""
    s = _state("bad.com")
    scoring._risk_one(s, {"webrisk": FakeWR(threats=["MALWARE"]), "blacklist": FakeBL()})
    assert s.reject_reason == "blacklist" and s.sig["webrisk_threats"] == ["MALWARE"]
    assert "blacklisted" not in s.sig


def test_risk_webrisk_error_is_recorded_domain_stays():
    s = _state("ok.com")
    scoring._risk_one(s, {"webrisk": FakeWR(boom=True), "blacklist": FakeBL()})
    assert s.alive and "webrisk:RuntimeError" in s.sig["errors"]


def test_risk_webrisk_circuit_opens_after_three_failures():
    """3.2: лежащий Web Risk — после 3 сбоев ПОДРЯД без сети до конца прогона (счётчик на
    инстансе клиента, детерминированно, без таймингов). Домены едут дальше «вслепую»."""
    wr = FakeWR(boom=True)
    states = [_state(f"d{i}.com") for i in range(5)]
    for s in states:
        scoring._risk_one(s, {"webrisk": wr, "blacklist": FakeBL()})
    assert wr.calls == 3
    assert [s.sig["errors"][-1] for s in states] == ["webrisk:RuntimeError"] * 3 + ["webrisk:circuit_open"] * 2
    assert all(s.alive for s in states)


def test_risk_dqs_key_enables_spamhaus(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "SPAMHAUS_DQS_KEY", "k")
    s, bl = _state("spam.com"), FakeBL(listed=True)
    scoring._risk_one(s, {"webrisk": FakeWR(), "blacklist": bl})
    assert s.reject_reason == "blacklist" and bl.calls == 1 and s.sig["blacklisted"] is True


def test_decide_never_auto_approves_with_risk_error():
    sig = {"wayback_checked": True, "age_years": 9, "deep_checked": True,
           "errors": ["webrisk:not_configured"]}
    assert scoring._decide(0.95, sig, 0.4) == "scored"            # Р2: одобряет только человек


def test_blind_reason_names_webrisk_and_keeps_domain_out_of_bulk():
    d = Domain(domain="blind.com", wayback_checked=True, prior_flags={}, age_years=9.0,
               score_breakdown={"errors": ["webrisk:not_configured"], "history_evidence": []})
    assert "Web Risk" in scoring.blind_reason(d)
    assert scoring.bulk_ok(d) is False


def test_make_clients_has_every_breaker_lock():
    """Находка R2-15, урок v1 (2026-07-21: при переходе на волны лок получили не все клиенты риска):
    предохранитель без своего лока в _make_clients — тихая гонка на счётчике под 12 потоками волны,
    а сьют зелёный (фейки передают клиентов сами). Каждый предохранитель — свой лок; новый
    (Задача 12: _llm_lock) дописывается сюда."""
    c = scoring._make_clients()
    for lock in ("_whois_lock", "_rdap_lock", "_webrisk_lock"):
        assert hasattr(c.get(lock), "acquire"), lock


def test_webrisk_breaker_locks_both_the_gate_check_and_the_increment():
    """R2-15, урок v1: гонку на счётчике предохранителя ловит детерминированный спай-лок, а не
    тайминг. Каждая из 3 попыток Web Risk до срабатывания берёт `_webrisk_lock` дважды (гейт-чек и
    инкремент), 4-я — один раз (гейт-чек: канал уже закрыт). Пропуск любого входа — непокрытая
    гонка под 12 потоками волны risk."""
    import threading

    class SpyLock:
        def __init__(self):
            self._real, self.enters = threading.Lock(), 0

        def __enter__(self):
            self._real.acquire()
            self.enters += 1

        def __exit__(self, *a):
            self._real.release()
    lock, wr = SpyLock(), FakeWR(boom=True)
    for i in range(4):
        scoring._risk_one(_state(f"d{i}.com"), {"webrisk": wr, "blacklist": FakeBL(),
                                               "_webrisk_lock": lock})
    assert wr.calls == 3 and wr.threat_failures == 3
    assert lock.enters == 3 * 2 + 1
```

- [ ] **Шаг 2: Запустить — убедиться, что падает**

Запуск: `cd backend && ../.venv/bin/python -m pytest tests/test_waves_v2.py -k "risk or breaker_lock" -v`
Ожидание: `8 failed, 1 passed`. Шесть тестов `_risk_one` (пять + спай-лок) — `TypeError: _risk_one() missing
1 required positional argument: 'sb_lock'`; `test_blind_reason_names_webrisk_and_keeps_domain_out_of_bulk` — в
`blind_reason` нет строки про Web Risk; `test_make_clients_has_every_breaker_lock` — `AssertionError: _webrisk_lock`.
Проходит `test_decide_never_auto_approves_with_risk_error`: авто-одобрения нет с Задачи 8.

- [ ] **Шаг 3: Реализовать**

**`scoring.py`:**

1) В докстринге модуля строку `Order: pre-filter -> history (Wayback) -> risk (RKN, blacklist) -> indexed_echo (SearXNG)`
заменить на `Order: t0 (зоны/бренды) -> avail (RDAP/whois) -> risk (Web Risk, Spamhaus с DQS) -> history (Wayback)`.

2) Удалить строку импорта `from contextlib import nullcontext` (после удаления Safe Browsing не используется).

3) Комментарий над `FUNNEL_STAGES` (четыре строки от `# Чипы волн в панели: ключ -> подпись. Порядок = порядок волн в _run_waves. "эхо" сюда`
до `# у него никогда не было даже в старом _funnel — просто раньше это не было видно оператору.`) заменить
одной строкой:

```python
# Чипы волн в панели: ключ -> подпись. Порядок = порядок волн в _run_waves (таблица `waves`).
```

а элемент `{"key": "risk", "label": "РКН/блэклист/эхо"}` — на `{"key": "risk", "label": "риск (Web Risk)"}`.

4) В `_BLIND_RU` четыре строки (`"rkn": …`, `"blacklist": …`, `"safebrowsing": …`, `"searxng": …`) заменить на:

```python
    "webrisk": "риск НЕ проверен: Web Risk не настроен или не ответил",
    "blacklist": "блэклист НЕ проверен",
```

5) Функцию `_make_clients` заменить целиком:

```python
def _make_clients() -> dict:
    """Собрать интеграционные клиенты один раз на прогон (переиспользуются между доменами).
    Локи — для предохранителей под конкурентностью волн (services/whois.py): счётчики сбоев
    живут на инстансах клиентов и меняются из 12 потоков волны."""
    from app.integrations.wayback import WaybackClient
    from app.integrations.blacklist import BlacklistClient
    from app.integrations.aparser import AParserClient
    from app.integrations.rdap import RdapClient
    from app.integrations.webrisk import WebRiskClient
    return {
        "wayback": WaybackClient(), "blacklist": BlacklistClient(), "webrisk": WebRiskClient(),
        "aparser": AParserClient(), "rdap": RdapClient(),
        "_whois_lock": threading.Lock(), "_rdap_lock": threading.Lock(),
        "_webrisk_lock": threading.Lock(),
    }
```

6) Удалить блок предохранителя Safe Browsing: комментарий от `# После скольких сбоев ПОДРЯД safebrowsing_check (A-Parser) перестаём его звать до конца`
и строку `_APARSER_SAFEBROWSING_LIMIT = 3`.

7) Функции `_risk_one` и `_wave_risk` заменить целиком:

```python
def _risk_one(s: FunnelState, clients: dict) -> None:
    """W3 для ОДНОГО домена. Web Risk — коммерчески легальная замена Safe Browsing (тот «for
    non-commercial use only»). Без ключа проверку НЕ делаем и честно пишем «не настроено»: домен
    едет дальше, но «вслепую» — пакет его не возьмёт. Spamhaus DBL — только с платным DQS-ключом:
    бесплатное зеркало для коммерции запрещено (docs/v2/research/metrics-history.md).

    Угроза Web Risk НЕ пишется в колонку `blacklisted` (находка 1.5): та — сигнал Spamhaus, и без
    DQS её никто бы не перепроверил и не снял. Улика живёт в `score_breakdown.webrisk_threats`
    (_commit_result хранит её через _kept) — по ней transitions.dirty_reason держит домен грязным,
    и перескор, на котором Web Risk упал, угрозу не отмывает. Лежащий Web Risk — предохранитель
    «3 сбоя подряд» (whois.guarded): дальше `webrisk:circuit_open` без сети до конца прогона."""
    from app.config import settings
    wr = clients.get("webrisk")
    if wr is None or not wr.configured:
        s.sig["errors"].append("webrisk:not_configured")
    else:
        try:
            threats = whois_router.guarded(wr, "threat_failures", lambda: wr.threats(s.domain),
                                           "Google Web Risk", clients.get("_webrisk_lock"))
        except Exception as e:  # noqa: BLE001 — сбой проверки не приговор домену, но «вслепую»
            code = "circuit_open" if isinstance(e, whois_router.CircuitOpen) else type(e).__name__
            s.sig["errors"].append(f"webrisk:{code}")
        else:
            s.sig["webrisk_threats"] = threats
            if threats:
                s.reject_reason, s.alive = "blacklist", False
                return
    bl = clients.get("blacklist")
    if bl is not None and settings.SPAMHAUS_DQS_KEY:
        try:
            listed = bl.is_blacklisted(s.domain)
        except Exception as e:  # noqa: BLE001
            s.sig["errors"].append(f"blacklist:{type(e).__name__}")
        else:
            if listed is None:
                s.sig["errors"].append("blacklist:unavailable")
            else:
                s.sig["blacklisted"] = listed
                if listed:
                    s.reject_reason, s.alive = "blacklist", False


def _wave_risk(states: list, clients: dict, run) -> None:
    """W3 — Web Risk (+ Spamhaus при DQS), конкурентно на весь выживший после avail пул."""
    _run_concurrent(states, _CONCURRENCY["risk"], run, "risk", lambda s: _risk_one(s, clients))
```

8) `_commit_result`: в кортеже колонок «пишем только проверенное» убрать `"rkn_listed"` и `"indexed_echo"`
(колонки в БД остаются легаси, воронка их больше не пишет):

```python
        for col in ("lane", "whois_created", "acquirability_checked_at", "prior_flags",
                    "wayback_checked", "first_seen", "age_years", "blacklisted",
                    "dr", "referring_domains", "trademark_risk"):
```

а в `d.score_breakdown = {…}` последним элементом после `"whois_source": _kept("whois_source")` добавить
`"webrisk_threats": _kept("webrisk_threats")`:

```python
                             "whois_source": _kept("whois_source"),
                             "webrisk_threats": _kept("webrisk_threats")}
```

**`transitions.py`, `dirty_reason`:** после ветки `if d.blacklisted is True: … return "blacklist"` вставить:

```python
    # Угроза Web Risk — улика из score_breakdown, а не из колонки `blacklisted` (её Web Risk не
    # пишет, находка 1.5): перескор, на котором Web Risk упал, её не стирает (_kept).
    if (d.score_breakdown or {}).get("webrisk_threats"):
        return "blacklist"
```

**`transitions.py`, самопроверка** (находка R2-13: `dirty_reason` теперь читает `d.score_breakdown`, и
`SimpleNamespace` без него роняет `python -m app.services.transitions` в `AttributeError`). Блок
`if __name__ == "__main__":` в конце файла заменить целиком:

```python
if __name__ == "__main__":  # self-check без БД: политика чистая, ORM ей не нужен
    from types import SimpleNamespace as NS

    rkn = NS(domain="bad.ru", status="rejected", reject_reason="rkn", rkn_listed=True,
             blacklisted=None, prior_flags={}, wayback_checked=True, score_breakdown={})
    weak = NS(domain="weak.com", status="rejected", reject_reason="low_score", rkn_listed=False,
              blacklisted=None, prior_flags={}, wayback_checked=True, score_breakdown={})
    assert dirty_reason(rkn) == "rkn" and dirty_reason(weak) is None
    # угроза Web Risk — грязь по улике в score_breakdown (находка 1.5)
    assert dirty_reason(NS(**{**vars(weak), "score_breakdown": {"webrisk_threats": ["MALWARE"]}})) \
        == "blacklist"
    try:
        check(rkn, "approved", allowlist=["com"])
        raise AssertionError("грязь обязана быть отвергнута")
    except TransitionDenied:
        pass
    check(weak, "approved", allowlist=["com"])    # отсеянный ПОРОГОМ домен возвращается руками
    try:
        check(NS(**{**vars(weak), "domain": "weak.ru"}), "approved", allowlist=["com"])
        raise AssertionError("зона вне белого списка обязана быть отвергнута")
    except TransitionDenied:
        pass
    try:
        check(NS(domain="raw.ru", status="discovered", reject_reason=None, rkn_listed=None,
                 blacklisted=None, prior_flags={}, wayback_checked=True, score_breakdown={}),
              "purchased")
        raise AssertionError("покупка сырья мимо воронки обязана быть отвергнута")
    except TransitionDenied:
        pass
    print("transitions policy ok")
```

**`aparser.py`:** удалить целиком (вместе с их комментариями над ними): `_RE_SAFEBROWSING`, `_RE_ARCHIVE`,
функции `_parse_safebrowsing`, `_parse_archive`, методы `AParserClient.safebrowsing_check` и
`AParserClient.archive_probe`. Между `_RE_AHREFS` и `def _parse_ahrefs` оставить две пустые строки.

**`searxng.py`:** удалить метод `indexed_echo` целиком; в докстринге модуля «Used for M1 indexed_echo
(site:) and M4 competitor SERP.» заменить на «Used for M5 index checks (site:) and M4 competitor SERP.».

**`diagnostics.py`, `_spec()`:** удалить запись `("rkn", "РКН (antizapret)", …)` (две строки); в записи
`blacklist` первую строку заменить на
`        ("blacklist", "Spamhaus DBL (только с DQS)", "M1 · спам-лист", settings.SPAMHAUS_DQS_KEY, "M1", False,`
(без ключа — `skip`: бесплатное зеркало для коммерции запрещено).

Удалить файл: `git rm backend/app/integrations/rkn.py`.

`DIRTY_REASONS` и `REJECT_RU` не трогать: коды `rkn`/`safebrowsing` остаются для легаси-строк в БД.

- [ ] **Шаг 4: Запустить тесты задачи — проходят**

Запуск: `cd backend && ../.venv/bin/python -m pytest tests/test_waves_v2.py -v`
Ожидание: 23 passed (14 из Задачи 9 + 9).

- [ ] **Шаг 5: Старые тесты — явный список**

Удалить (логика удалена; тесты гейтов и инвариантов среди них нет):
- `test_aparser.py`: `test_parse_safebrowsing_flagged`, `test_parse_safebrowsing_clean`,
  `test_parse_safebrowsing_no_match_returns_none`, `test_parse_safebrowsing_empty_string`,
  `test_parse_archive_with_history`, `test_parse_archive_none_history`,
  `test_parse_archive_no_match_returns_all_none`, `test_parse_archive_empty_string`,
  `test_safebrowsing_check_sends_expected_parser`, `test_archive_probe_uses_no_proxy_preset` (10); импорт
  в шапке заменить на `from app.integrations.aparser import _parse_ahrefs, AParserClient`. Ahrefs-тесты
  файла остаются до Задачи 11 (она удаляет их вместе с `ahrefs_probe`);
- `test_funnel.py`: `test_rkn_rejects_before_wayback` и весь раздел от комментария
  `# --- SafeBrowsing hard-reject + Archive pre-gate (Тред D, Задача 2) ---` до комментария
  `# --- квота: воронка не платит whois'ом дважды за детерминированный ответ ---` (не включая его):
  классы `_AparserAlwaysFailsSB`, `_AparserFlakySB` и пять тестов `test_safebrowsing_*` (6 тестов);
- `test_m1_fixes.py`: раздел от `# ---------- I3: обрезанный дамп РКН не кэшируется молча ----------` до
  `# ---------- I4: гонка двух discovery не теряет батч ----------` (не включая его): `_FakeResp`, `_SpyLock`,
  `test_rkn_small_dump_raises_when_no_cache`, `test_rkn_small_dump_keeps_old_cache`,
  `test_rkn_ensure_loaded_serialized_by_class_lock` (3 теста); импорт `import time as _time` удалить;
- `test_scoring_waves.py`: классы `_FakeRiskClients`, `_SlowFakeRiskClients`, помощник `_risk_clients`,
  тесты `test_wave_risk_rejects_rkn`, `test_wave_risk_fills_echo_without_rejecting`,
  `test_wave_risk_safebrowsing_breaker_lock_has_no_lost_increments_under_real_overlap`,
  `test_wave_risk_safebrowsing_lock_covers_gate_check_and_increment` (4 теста).

Фейк «чистого настроенного Web Risk» — добавить последним ключом в словари клиентов помощников, чьи
домены тесты ждут чистыми/в пакете:
`"webrisk": type("WR", (), {"configured": True, "threats": lambda self, d: []})()` —
в `test_funnel.py::_clients` и `test_funnel.py::_clients_whois_raises` (после `"wayback": wayback` /
`"wayback": wb`), `test_aparser_envelope.py::_clients` (после `"wayback": wayback or _WaybackAged()`),
`test_history_verdict.py::_clients` (после `"wayback": wayback`).

Переписать:

`test_funnel.py::test_blacklist_rejects_before_wayback` →

```python
def test_blacklist_rejects_before_wayback(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "SPAMHAUS_DQS_KEY", "k")       # Spamhaus в воронке — только с DQS
    did = _mk(domain="blacklisted.com", referring_domains=50, lane="bid")
    wb = _Wayback()
    old = datetime.now(timezone.utc) - timedelta(days=365 * 8)
    out = scoring.score_domain(did, clients=_clients(old, wb, bl=True))
    assert out["status"] == "rejected" and out["reject_reason"] == "blacklist"
    assert wb.calls == 0            # blacklist — W3, Wayback до неё не доходит
```

`test_funnel.py::test_blacklist_none_downgrades_via_funnel` →

```python
def test_blacklist_none_downgrades_via_funnel(monkeypatch):
    """Ревью C2: строка `blacklisted is None -> errors.append("blacklist:unavailable")` прогнана
    полной воронкой на иначе-сильном домене (профиль test_clean_strong_domain_is_scored_and_bulk_ok).
    Авто-одобрения нет (Р2), поэтому «понижение» теперь значит: домен `scored`, с пометкой
    «вслепую» и ВНЕ пакета. Spamhaus в воронке — только с DQS-ключом (v2), поэтому ключ задан."""
    from app.config import settings
    from app.services import scoring_config as cfg
    monkeypatch.setattr(settings, "SPAMHAUS_DQS_KEY", "k")
    did = _mk(domain="bl-none.com", referring_domains=3000, lane="bid")
    wb = _Wayback()
    old = datetime.now(timezone.utc) - timedelta(days=365 * 9)
    out = scoring.score_domain(did, clients=_clients(old, wb, bl=None))
    assert "blacklist:unavailable" in out["errors"]
    assert out["score"] >= cfg.DECISION["approve_at"]      # сильный — исключает правило, а не балл
    assert out["status"] == "scored"                        # не rejected — не hard-reject
    assert wb.calls == 1                                    # blacklist:unavailable не блокирует историю
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
        assert scoring.blind_reason(d) == "блэклист НЕ проверен" and scoring.bulk_ok(d) is False
```

`test_funnel_stages_and_job_message.py::test_task10_funnel_stages_has_5_not_6` — три строки

```python
    # Verify risk label now includes echo
    risk_stage = next(s for s in scoring.FUNNEL_STAGES if s["key"] == "risk")
    assert "эхо" in risk_stage["label"]
```

заменить на

```python
    # v2: риск — это Google Web Risk (РКН, Safe Browsing и эхо удалены)
    risk_stage = next(s for s in scoring.FUNNEL_STAGES if s["key"] == "risk")
    assert "Web Risk" in risk_stage["label"]
```

`test_history_verdict.py::test_blind_reason_still_names_dead_checks` →

```python
def test_blind_reason_still_names_dead_checks():
    """Web Risk/блэклист остались «вслепую» по errors — история их не поглотила."""
    d = Domain(domain="r.com", wayback_checked=True, prior_flags=_CLEAN_FLAGS, age_years=10.0,
               score_breakdown={"errors": ["webrisk:ConnectError"]})
    assert "Web Risk" in scoring.blind_reason(d)
```

`test_m1_fixes.py::test_rkn_or_blacklist_error_caps_at_scored` → (новое имя; комментарий-заголовок над
ним `# ---------- I1: ошибка RKN/blacklist не пускает в пакетное одобрение ----------` → `… ошибка Web Risk/blacklist …`)

```python
def test_webrisk_or_blacklist_error_caps_at_scored():
    """Авто-одобрения нет (Р2): скоринг даёт максимум `scored` и при чистом прогоне. Упавшая
    проверка Web Risk/блэклиста держит домен вне ПАКЕТА — туда переехал гард из _decide."""
    from app.models.domain import Domain
    from app.services.scoring import bulk_ok, compute_score
    strong = {"wayback_checked": True, "prior_flags": {}, "age_years": 8,
              "referring_domains": 3000}
    clean = compute_score({**strong, "blacklisted": False, "errors": []})
    assert clean["status"] == "scored" and clean["score"] >= 0.70      # сильный, но одобряет человек

    def _dom(errors):
        return Domain(domain="i1.com", wayback_checked=True, prior_flags={}, age_years=8,
                      score_breakdown={"errors": errors, "history_evidence": []})
    assert bulk_ok(_dom([])) is True                                   # базовая линия: пакет берёт
    for err in ("webrisk:ConnectError", "webrisk:not_configured", "blacklist:RuntimeError"):
        assert compute_score({**strong, "errors": [err]})["status"] == "scored"
        assert bulk_ok(_dom([err])) is False                           # проверка упала — вне пакета
```

`test_pipeline.py` — помощник `_funnel_clients` и `test_scoring_hard_reject_on_rkn` заменить целиком:

```python
def _funnel_clients(whois_dt, threats=(), wb_flags=None):
    """Мок-клиенты воронки (см. test_funnel.py::_clients). whois_probe отдаёт «занят, но с датой» —
    домен-заглушка получает lane="bid" (см. вызовы ниже), чтобы приобретаемость не блокировала
    W2 до Web Risk/Wayback. threats — ответ Web Risk."""
    class _W:  # aparser
        def whois_probe(self, dom): return {"available": False, "created": whois_dt}
    class _WR:
        configured = True
        def threats(self, dom): return list(threats)
    class _Bl:
        def is_blacklisted(self, dom): return False
    class _Wb:
        def classify_history(self, dom):
            return wb_flags or {"prior_flags": {}, "wayback_checked": True,
                                "first_seen": None, "age_years": 10.0}
    return {"aparser": _W(), "webrisk": _WR(), "blacklist": _Bl(), "wayback": _Wb()}
```

```python
def test_scoring_hard_reject_on_webrisk_threat():
    from app.services import scoring
    from app.services.transitions import dirty_reason
    did = _add(Domain(domain="blocked.com", source="backorder", status="discovered", lane="bid"))
    # whois=None -> W2 без даты; угроза Web Risk рубит на W3, Wayback не вызывается
    out = scoring.score_domain(did, clients=_funnel_clients(None, threats=["MALWARE"]))
    assert out["status"] == "rejected" and out["score"] == 0.0
    assert out["reject_reason"] == "blacklist"
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
        assert d.clean is False and d.blacklisted is None          # 1.5: колонку Spamhaus не трогаем
        assert d.score_breakdown["webrisk_threats"] == ["MALWARE"]
        assert dirty_reason(d) == "blacklist"                      # грязь видна по улике Web Risk
```

`test_transitions.py` (тесты инварианта «грязь не доезжает до кассы» — только переписать):
- в помощниках `d(...)` тестов `test_dirty_reason_sees_verdict_and_raw_signals` и
  `test_manual_transition_checks_source_status_not_only_target` в словарь по умолчанию добавить
  `"score_breakdown": None` (после `"wayback_checked": True,`); в первом тесте после строки
  `assert dirty_reason(d(blacklisted=True)) == "blacklist"` добавить
  `assert dirty_reason(d(score_breakdown={"webrisk_threats": ["MALWARE"]})) == "blacklist"   # 1.5`;
- помощник `_clients` заменить целиком:

```python
def _clients(**over):
    """Клиенты воронки. По умолчанию всё чисто — тест портит ровно ту проверку, что изучает."""
    c = {
        "wayback": type("W", (), {"classify_history": lambda self, d: {
            "prior_flags": {}, "wayback_checked": True, "sampled": 3, "evidence": [],
            "first_seen": None, "age_years": None}})(),
        "blacklist": type("B", (), {"is_blacklisted": lambda self, d: False})(),
        "webrisk": type("WR", (), {"configured": True, "threats": lambda self, d: []})(),
        "aparser": type("A", (), {"whois_probe": lambda self, d: {
            "available": True, "created": datetime(2008, 1, 1, tzinfo=timezone.utc)}})(),
    }
    return {**c, **over}
```

- `test_rescoring_is_the_honest_way_back` (реабилитация по Spamhaus вместо РКН) →

```python
def test_rescoring_is_the_honest_way_back(monkeypatch):
    """Дверь для грязи не заперта наглухо — она просто не открывается КНОПКОЙ.

    Единственный путь обратно в оборот — перескор: воронка берёт домены из `rejected`, и если
    проверки сегодня говорят «чист», она сама чистит `reject_reason` и сигналы. Реабилитацию
    даёт машина по новым уликам, а не человек по настроению. (v2: РКН-проверки больше нет, путь
    назад показан на Spamhaus — он в воронке только с DQS-ключом.)
    """
    from app.config import settings
    from app.services import scoring
    from app.services.transitions import dirty_reason

    monkeypatch.setattr(settings, "SPAMHAUS_DQS_KEY", "k")
    did = _add(domain="unblocked.com", status="rejected", reject_reason="blacklist", blacklisted=True,
               lane="bid", referring_domains=300)

    class _WB:
        def classify_history(self, dom):
            return {"prior_flags": {}, "wayback_checked": True, "sampled": 3,
                    "evidence": [{"url": dom, "timestamp": "20150101", "cats": [], "chars": 900}],
                    "first_seen": None, "age_years": None}
    clients = {
        "wayback": _WB(),
        "blacklist": type("B", (), {"is_blacklisted": lambda self, d: False})(),   # из списка вышел
        "webrisk": type("WR", (), {"configured": True, "threats": lambda self, d: []})(),
        "aparser": type("A", (), {"whois_probe": lambda self, d: {
            "available": False, "created": datetime(2008, 1, 1, tzinfo=timezone.utc)}})(),
    }
    out = scoring.score_domain(did, clients=clients)
    assert out["reject_reason"] is None and out["status"] == "scored"
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
        assert dirty_reason(d) is None                    # улики переписаны — домен снова чист
        assert d.blacklisted is False
```

- `test_rescore_that_actually_ran_the_checks_still_rehabilitates` — заменить функцию целиком:

```python
def test_rescore_that_actually_ran_the_checks_still_rehabilitates(monkeypatch):
    """ЧТО ЛОМАЕТСЯ от запрета стирать улики: НИЧЕГО у настоящей реабилитации.

    Проверка, которая ОТРАБОТАЛА и сказала «чист», кладёт False — и домен выходит из грязи.
    Правило звучит «не стирай непроверенное», а не «не верь проверкам».
    """
    from app.config import settings
    from app.services import scoring, transitions

    monkeypatch.setattr(settings, "SPAMHAUS_DQS_KEY", "k")     # Spamhaus в воронке — только с DQS
    did = _add(domain="unblocked2.com", status="rejected", reject_reason="blacklist", blacklisted=True,
               lane="bid", referring_domains=300)
    out = scoring.score_domain(did, clients=_clients())
    assert out["reject_reason"] is None
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
        assert d.blacklisted is False and transitions.dirty_reason(d) is None
```

- новый тест (1.5) — вставить сразу после `test_rescore_that_actually_ran_the_checks_still_rehabilitates`
  (две пустые строки перед ним, следующий тест файла остаётся на месте):

```python
def test_rescore_with_webrisk_failure_does_not_launder_the_threat():
    """1.5: угроза Web Risk живёт в score_breakdown.webrisk_threats. Перескор, на котором Web Risk
    УПАЛ, её не проверял — и стереть не вправе: домен доезжает до `scored`, но остаётся грязным и
    кнопкой в оборот не возвращается."""
    from app.services import scoring, transitions

    class _WRDown:
        configured = True

        def threats(self, d):
            raise RuntimeError("webrisk down")

    did = _add(domain="malware-once.com", status="rejected", reject_reason="blacklist",
               lane="bid", referring_domains=300, score=0.0,
               score_breakdown={"webrisk_threats": ["MALWARE"], "errors": []})
    out = scoring.score_domain(did, clients=_clients(webrisk=_WRDown()))
    assert out["reject_reason"] is None and "webrisk:RuntimeError" in out["errors"]
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
        assert d.score_breakdown["webrisk_threats"] == ["MALWARE"]     # улику не стёрли
        assert transitions.dirty_reason(d) == "blacklist"
        with pytest.raises(transitions.TransitionDenied):
            transitions.check(d, "approved")
```

Проверка: `grep -rnE "integrations\.rkn|RknClient|safebrowsing_check|archive_probe|_parse_archive|_parse_safebrowsing|indexed_echo\(|_APARSER_SAFEBROWSING_LIMIT|nullcontext" backend/app backend/tests` —
только `services/whois.py` (`nullcontext`), докстринги `blacklist.py`/`test_blacklist_control.py`
(«как RknClient»), строка про `nullcontext` в докстринге `test_scoring_waves.py` и неиспользуемые методы
фейков (`safebrowsing_check`/`archive_probe`/`indexed_echo`) в помощниках тестов — их чистит Задача 16.

- [ ] **Шаг 6: Сьют, самопроверка `transitions`, линт**

Запуск: `cd backend && ../.venv/bin/python -m pytest tests -q && ../.venv/bin/python -m app.services.transitions && cd .. && .venv/bin/python -m pyflakes backend/app backend/tests`
Ожидание: **818 passed** (831 + 9 в `test_waves_v2.py` + 1 в `test_transitions.py` − 10 `test_aparser.py`
− 6 `test_funnel.py` − 3 `test_m1_fixes.py` − 4 `test_scoring_waves.py`), самопроверка печатает
`transitions policy ok`, pyflakes пуст.

- [ ] **Шаг 7: Коммит**

```bash
git add -A backend/app backend/tests
git commit -m "feat(scoring): W3 риск — Web Risk + Spamhaus только с DQS; РКН/Safe Browsing/эхо удалены"
```

---

### Задача 11: W4 «ссылки» — Ahrefs batch-analysis пачками; волна до истории

**Что меняется и почему.**
- RD и DR в v2 даёт Ahrefs batch-analysis (25 units за домен), а не фид. Волна идёт **до истории**:
  дешёвый по времени Ahrefs отсеивает пустышки (`low_rd`) раньше, чем 4 слота Wayback. Отказ `low_rd`
  уезжает из W0 в W4. Капча A-Parser `Rank::Ahrefs` (v1) удаляется вместе с капом `max_ahrefs_per_run`.
- **Сбой пачки — unresolved, а не «вслепую дальше»** (находка 1.8): без RD скор падает ниже порога, и
  домен навсегда уходил в `low_score` (`score_pending` берёт только `discovered`). Упавшая пачка и все
  следующие получают `unresolved_why="ahrefs_failed"`, следующие пачки прогона не шлются: протухший
  ключ дал бы 401 на каждой.
- **Пол остатка units и ключ — один раз в начале прогона** (решение оператора Р3, находка R2-10):
  автопилот гоняет свип раз в час, капы «на прогон» месяц не держат. Сразу после бесплатной W0
  `_paid_gate` ОДИН раз решает, пойдут ли платные волны: ключа Ahrefs нет (`api_key == ""`) →
  `unresolved_why="ahrefs_no_key"`; остаток units `None`, сбой запроса или меньше `units_floor` (если
  пол > 0) → `"units_floor"`. Так помечаются все живые не-EMD домены — ДО W2/W3: без W4 их всё равно
  не решить, а RDAP/whois:43 и Web Risk за них тратились бы впустую на каждом свипе. EMD идут дальше
  (W4 у них нет). В сообщение задачи — «Ahrefs: остаток N < пола M — платные волны пропущены» или
  «Ahrefs: ключ AHREFS_API_KEY не задан — платные волны пропущены». Пол 0 — пола нет, остаток не
  спрашиваем.
- **Выборка `score_pending` — не больше `max_links_per_run` не-EMD доменов** (R2-10): домен сверх капа
  W4 всё равно ушёл бы в `links_budget`, оплатив W2/W3. Остаток лимита выборки добирают EMD.
- **Причина сбоя пачки — в сообщении задачи** (R2-9): «Ahrefs W4: HTTPStatusError 401 — N доменов ждут
  следующего прогона» (401/403 — ключ не принят, 400 — кривой запрос). Без этого оператор видит
  «прогнано N», а домены молча висят в поиске; ошибка была только в логе скора.
- **Строки домена нет в ответе batch** (аномалия: на несуществующий домен Ahrefs отдаёт нули) — **тоже
  unresolved**, `unresolved_why="ahrefs_missing"`, а не «вслепую дальше» (решение координатора
  2026-10-02): без RD домен так же ушёл бы в `low_score` навсегда.
- **Домен, до которого платная волна не дошла, оценится СЛЕДУЮЩИМ прогоном** (находка 2.7): для
  `links_budget`, `units_floor`, `ahrefs_no_key`, `ahrefs_failed`, `ahrefs_missing` `_commit_result` не
  ставит `acquirability_checked_at`, иначе `scorable` вернул бы free/NULL-лейн только через сутки.
- **Пустое поле ответа ничего не затирает** (находка 4.12): DR из discovery лежит в строке домена;
  W4 пишет `dr`/`referring_domains` (и прочие поля) только непустыми, и `_commit_result` подставит
  сохранённый DR через `setdefault`.
- Авто-одобрения нет с Задачи 8. Ключ `"ahrefs"` в `_BLIND_RU` («ссылочный профиль НЕ проверен») остаётся
  для легаси-строк: в v2 любая ошибка W4 делает домен unresolved, и до инбокса он с ней не доезжает.

**Files:**
- Modify:
  - `backend/app/services/scoring.py` — `_wave_links` и помощники, порядок волн, `FUNNEL_STAGES`,
    `_CONCURRENCY`, `score_domain`, `score_pending`, `_make_clients`, `_commit_result`, `_run_waves`; из
    `_wave_t0` убрать `low_rd`; удалить `_ahrefs_one`/`_wave_ahrefs`;
  - `backend/app/integrations/aparser.py` — удалить `ahrefs_probe`, `_parse_ahrefs`, `_RE_AHREFS`;
  - `backend/app/services/settings.py`, `backend/app/services/scoring_config.py` — убрать `max_ahrefs_per_run`;
  - `backend/app/api/panel.py` — `settings_save` без `max_ahrefs_per_run`; флеш одиночного скора знает
    новые причины unresolved;
  - `backend/app/templates/settings.html` — удалить станцию «Кап Ahrefs-проверок за прогон».
- Delete: `backend/tests/test_aparser.py` (в нём остались только ahrefs-тесты).
- Test: `backend/tests/test_waves_v2.py` (дописать). Старые тесты — явный список в шаге 5.

**Interfaces:**
- Consumes: `AhrefsClient.batch(domains) -> {домен: {поля BATCH_FIELDS}}` и `units_left() -> int | None`
  (Задача 2), `st["max_links_per_run"]`, `st["min_referring_domains"]`, `st["units_floor"]` (Задача 5).
- Produces:
  - `_wave_links(states, clients, st, budget, run, notes=None)`; `_units_below_floor(clients, st) -> str | None`
    — **Задача 13 зовёт его же перед W6**; `_paid_gate(states, clients, st, notes)` — после W0, один раз
    за прогон (R2-10);
  - коды `unresolved_why`: `"links_budget"`, `"units_floor"`, `"ahrefs_no_key"`, `"ahrefs_failed"`,
    `"ahrefs_missing"` (кортеж `_PAID_UNRESOLVED` — без отметки сверки занятости; Задача 13 не добавляет
    кодов);
  - `score_pending` выбирает не-EMD доменов не больше `max_links_per_run` (R2-10);
  - сигналы `sig["dr"]`, `sig["referring_domains"]`, `sig["rd_dofollow"]`, `sig["ref_subnets"]`,
    `sig["backlinks"]`, `sig["organic_traffic"]` — только непустые; ошибки `ahrefs:missing` (строки нет в
    ответе) и `ahrefs:<Исключение>` — у unresolved-доменов, в логе скора;
  - `score_domain(domain_id, clients=None, whois_budget=None, links_budget=None, run=None)`;
  - `_run_waves(states, clients, st, whois_budget, links_budget, run, notes=None)` — `notes` (список
    вызывающего) дописывается в водопад и в итоговое сообщение `score_pending`; туда же пишут
    `_paid_gate` (пол/ключ) и `_wave_links` (причина сбоя пачки, R2-9);
  - `FUNNEL_STAGES`: `t0, avail, risk, links, history`; ключ `clients["ahrefs"]`; в `_CONCURRENCY`
    ключей `"ahrefs"`/`"whois"` нет;
  - `score_breakdown`: `ref_subnets`, `rd_dofollow` (через `_kept`) вместо `ahrefs_backlinks`; колонки
    `backlinks`, `organic_traffic`;
  - `test_waves_v2.py`: `import app.db as db` в шапке; `ROW`, `FakeAh(data, boom, anchors, history,
    history_boom, units)` (методы `anchors`/`metrics_history` — для W6 Задачи 13), `_mk(domain, source,
    lane, deadline, **kw)`, `_full_clients(rdap, ah, wb, **extra)`;
  - **фейк «Ahrefs без полей»** для старых интеграционных помощников (RD и DR остаются из строки
    домена, остаток выше пола) — ровно две строки: первая на отступе соседних ключей словаря, `"batch"`
    второй строки — точно под `"units_left"` (Задачи 13 и 16 ищут эти строки дословно; готовые пары
    «найти → заменить» по каждому помощнику — в шаге 5):

```python
"ahrefs": type("Ah", (), {"units_left": lambda self: 2_000_000,
                          "batch": lambda self, ds: {d: {} for d in ds}})(),
```

- [ ] **Шаг 1: Написать падающие тесты** — в шапке `test_waves_v2.py` перед строкой
  `from app.models.domain import Domain` добавить `import app.db as db`, в конец файла дописать:

```python
# --- W4 «ссылки» (Задача 11) -------------------------------------------------------------------

ROW = {"domain_rating": 0.0, "refdomains": 717, "refdomains_dofollow": 358, "refips_subnets": 198,
       "backlinks": 801, "org_traffic": 0}


class FakeAh:
    """Ahrefs API: batch (W4), остаток units (пол Р3), анкоры и история трафика (W6, Задача 13).
    `boom` роняет batch и anchors, `history_boom` — только metrics_history."""
    def __init__(self, data=None, boom=False, anchors=None, history=None, history_boom=False,
                 units=2_000_000):
        self.data, self.boom, self.history_boom, self.units = data or {}, boom, history_boom, units
        self._anchors, self._history = anchors or [], history or []
        self.batches, self.deep_calls, self.units_calls = [], 0, 0

    def units_left(self):
        self.units_calls += 1
        return self.units

    def batch(self, domains):
        self.batches.append(list(domains))
        if self.boom:
            raise RuntimeError("ahrefs down")
        return {d: self.data[d] for d in domains if d in self.data}

    def anchors(self, d, limit=50):
        self.deep_calls += 1
        if self.boom:
            raise RuntimeError("ahrefs down")
        return self._anchors

    def metrics_history(self, d, years=5, today=None):
        if self.history_boom:
            raise RuntimeError("history down")
        return self._history


def _mk(domain, source="nominet", lane="bid", deadline=None, **kw):
    with db.SessionLocal() as s:
        d = Domain(domain=domain, source=source, lane=lane, status="discovered",
                   acquire_deadline=deadline, **kw)
        s.add(d)
        s.commit()
        return d.id


def _full_clients(rdap, ah, wb, **extra):
    """Клиенты всей воронки на фейках: Web Risk настроен и чист, Spamhaus без DQS не зовётся."""
    return {"rdap": rdap, "aparser": FakeAp(), "webrisk": FakeWR(), "blacklist": FakeBL(),
            "ahrefs": ah, "wayback": wb, **extra}


def test_links_fills_signals_and_rejects_low_rd():
    a, b = _state("a.com"), _state("b.com")
    ah = FakeAh({"a.com": ROW, "b.com": {**ROW, "refdomains": 0}})
    scoring._wave_links([a, b], {"ahrefs": ah}, _st(min_referring_domains=1), None, None)
    assert a.alive and a.sig["referring_domains"] == 717 and a.sig["ref_subnets"] == 198
    assert a.sig["dr"] == 0.0 and a.sig["organic_traffic"] == 0
    assert b.reject_reason == "low_rd"


def test_links_batches_of_100_skip_emd_and_unresolve_missing():
    """Строки домена нет в ответе — как сбой: unresolved до следующего прогона, а не «вслепую» дальше
    (без RD скор ушёл бы в low_score навсегда)."""
    states = [_state(f"d{i}.com") for i in range(150)] + [_state("emd.com", source="emd", lane="free")]
    ah = FakeAh({})
    scoring._wave_links(states, {"ahrefs": ah}, _st(), None, None)
    assert [len(b) for b in ah.batches] == [100, 50]
    assert all("emd.com" not in b for b in ah.batches)
    assert states[0].unresolved_why == "ahrefs_missing" and not states[0].alive
    assert states[0].sig["errors"] == ["ahrefs:missing"] and states[150].alive


def test_links_budget_overflow_is_unresolved_not_judged_blind():
    states = [_state(f"d{i}.com") for i in range(3)]
    scoring._wave_links(states, {"ahrefs": FakeAh({})}, _st(), scoring.Budget(2), None)
    assert states[2].unresolved_why == "links_budget" and not states[2].alive


def test_links_batch_error_unresolves_rest_and_stops_sending():
    """1.8: упавшая пачка не «вслепую дальше» (без RD скор ниже порога -> low_score навсегда),
    а unresolved до следующего прогона. Следующие пачки не шлются: протухший ключ даёт 401 на каждой."""
    states = [_state(f"d{i}.com") for i in range(150)]
    ah = FakeAh(boom=True)
    scoring._wave_links(states, {"ahrefs": ah}, _st(), None, None)
    assert len(ah.batches) == 1
    assert all(s.unresolved_why == "ahrefs_failed" and not s.alive for s in states)
    assert states[149].sig["errors"] == ["ahrefs:RuntimeError"]


def test_links_empty_dr_does_not_erase_dr_from_discovery():
    """4.12: DR пришёл из discovery (строка домена), Ahrefs в W4 поля не отдал — пустое значение
    сохранённый DR не затирает."""
    did = _mk("dr-kept.com", deadline=NOW + timedelta(days=2), dr=12)
    ah = FakeAh({"dr-kept.com": {**ROW, "domain_rating": None}})
    scoring.score_domain(did, clients=_full_clients(
        FakeRdap(exists=True, registered=NOW - timedelta(days=4000)), ah, AgedWB()))
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
        assert float(d.dr) == 12.0 and d.referring_domains == 717


def test_links_batch_error_keeps_domain_discovered_and_unstamped():
    """1.8 + 2.7: сбой Ahrefs — домен остаётся discovered и БЕЗ отметки сверки занятости:
    `scorable` вернёт free-лейн только через сутки после отметки, а оценить его надо следующим
    прогоном."""
    did = _mk("libre.mx", source="mx", lane="free")
    out = scoring.score_domain(did, clients=_full_clients(FakeRdap(), FakeAh(boom=True), AgedWB()))
    assert out["unresolved"] is True and out["why"] == "ahrefs_failed"
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
        assert d.status == "discovered" and d.acquirability_checked_at is None


def test_links_missing_row_keeps_domain_discovered_and_unstamped():
    """Строки домена нет в ответе batch: домен остаётся discovered и без отметки сверки занятости —
    оценится следующим прогоном (решение координатора 2026-10-02)."""
    did = _mk("ghost-row.mx", source="mx", lane="free")
    out = scoring.score_domain(did, clients=_full_clients(FakeRdap(), FakeAh({}), AgedWB()))
    assert out["unresolved"] is True and out["why"] == "ahrefs_missing"
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
        assert d.status == "discovered" and d.acquirability_checked_at is None


def test_score_pending_takes_links_cap_from_settings(monkeypatch):
    """2.9: кап W4 — из /settings. Кап 1 и два домена -> один оценён, второй ждёт следующего прогона."""
    from app.services.settings import update_settings
    update_settings(max_links_per_run=1)
    for name in ("cap-a.com", "cap-b.com"):
        _mk(name, deadline=NOW + timedelta(days=2))
    ah = FakeAh({"cap-a.com": ROW, "cap-b.com": ROW})
    clients = _full_clients(FakeRdap(exists=True, registered=NOW - timedelta(days=4000)), ah, AgedWB())
    monkeypatch.setattr(scoring, "_make_clients", lambda: clients)
    scoring.score_pending(limit=10)
    assert [len(b) for b in ah.batches] == [1]
    with db.SessionLocal() as s:
        left = [d.domain for d in s.query(Domain).filter(Domain.status == "discovered")]
    assert len(left) == 1


def test_units_floor_skips_paid_wave_and_says_why(monkeypatch):
    """Р3: остаток units ниже пола -> W4 не тратит ничего, домены ждут следующего прогона (без
    отметки сверки), причина — в сообщении задачи. Остаток неизвестен (None) — то же самое.
    R2-10: решено ОДИН раз в начале прогона — W2 (RDAP) по таким доменам даже не ходила."""
    from app.services import jobs
    for units in (100_000, None):
        name = f"floor-{units}.com"
        did = _mk(name, deadline=NOW + timedelta(days=2))
        ah = FakeAh({name: ROW}, units=units)
        rdap = FakeRdap(exists=True, registered=NOW - timedelta(days=4000))
        clients = _full_clients(rdap, ah, AgedWB())
        monkeypatch.setattr(scoring, "_make_clients", lambda: clients)
        scoring.score_pending(limit=10)
        assert ah.batches == [] and ah.units_calls == 1 and rdap.calls == 0, units
        with db.SessionLocal() as s:
            d = s.get(Domain, did)
            assert d.status == "discovered" and d.acquirability_checked_at is None, units
    msg = jobs.last("score")["message"]
    assert "Ahrefs: остаток units неизвестен — платные волны пропущены" in msg, msg


def test_units_floor_message_and_zero_floor_means_no_floor(monkeypatch):
    from app.services import jobs
    from app.services.settings import update_settings
    did = _mk("low.com", deadline=NOW + timedelta(days=2))
    ah = FakeAh({"low.com": ROW}, units=100_000)
    clients = _full_clients(FakeRdap(exists=True, registered=NOW - timedelta(days=4000)), ah, AgedWB())
    monkeypatch.setattr(scoring, "_make_clients", lambda: clients)
    scoring.score_pending(limit=10)
    assert "Ahrefs: остаток 100 000 < пола 300 000 — платные волны пропущены" in jobs.last("score")["message"]
    update_settings(units_floor=0)                     # 0 — пола нет: остаток даже не спрашиваем
    ah.units_calls = 0
    scoring.score_pending(limit=10)
    assert ah.units_calls == 0 and ah.batches == [["low.com"]]
    with db.SessionLocal() as s:
        assert s.get(Domain, did).status == "scored"


def test_links_no_key_skips_avail_and_risk_for_non_emd(monkeypatch):
    """R2-10: платные волны не пойдут (ключа Ahrefs нет) — это известно ДО W2/W3. Не-EMD домен без
    W4 не решается, и RDAP с Web Risk за него тратились бы впустую на каждом свипе. Решено один раз
    после W0: домен ждёт следующего прогона без отметки сверки; EMD идёт как обычно (W4 у него нет)."""
    from app.integrations.ahrefs import AhrefsClient
    from app.services import jobs
    did = _mk("nokey.com", deadline=NOW + timedelta(days=2))
    _mk("nokey-emd.com", source="emd", lane="free")
    rdap, wr = FakeRdap(), FakeWR()
    clients = {**_full_clients(rdap, AhrefsClient(api_key=""), AgedWB()), "webrisk": wr}
    monkeypatch.setattr(scoring, "_make_clients", lambda: clients)
    scoring.score_pending(limit=10)
    assert rdap.calls == 1 and wr.calls == 1                # только EMD
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
        assert d.status == "discovered" and d.acquirability_checked_at is None
    msg = jobs.last("score")["message"]
    assert "Ahrefs: ключ AHREFS_API_KEY не задан — платные волны пропущены" in msg, msg


def test_score_pending_selects_non_emd_up_to_links_cap(monkeypatch):
    """R2-10: домен сверх капа W4 всё равно ушёл бы в links_budget, оплатив W2/W3 (RDAP/whois:43,
    Web Risk). Выборка берёт не-EMD доменов не больше капа; остаток лимита добирают EMD — W4 у них
    нет."""
    from app.services.settings import update_settings
    update_settings(max_links_per_run=1)
    for name in ("sel-a.com", "sel-b.com"):
        _mk(name, deadline=NOW + timedelta(days=2))
    _mk("sel-emd.com", source="emd", lane="free")
    seen = []
    monkeypatch.setattr(scoring, "_run_waves",
                        lambda states, *a, **kw: seen.extend(s.domain for s in states) or [])
    scoring.score_pending(limit=10)
    assert len(seen) == 2 and "sel-emd.com" in seen


def test_links_batch_error_reason_goes_to_job_message(monkeypatch):
    """R2-9: причина сбоя пачки W4 — в сообщении задачи, с HTTP-кодом (401/403 — ключ не принят,
    400 — кривой запрос), а не только в логе скора: иначе оператор видит «прогнано N», а домены
    молча висят в поиске."""
    import httpx
    from app.services import jobs

    class Ah401(FakeAh):
        def batch(self, domains):
            self.batches.append(list(domains))
            raise httpx.HTTPStatusError("401", request=httpx.Request("POST", "https://api.ahrefs.com/v3"),
                                        response=httpx.Response(401))
    _mk("key-gone.com", deadline=NOW + timedelta(days=2))
    clients = _full_clients(FakeRdap(exists=True, registered=NOW - timedelta(days=4000)), Ah401(), AgedWB())
    monkeypatch.setattr(scoring, "_make_clients", lambda: clients)
    scoring.score_pending(limit=10)
    msg = jobs.last("score")["message"]
    assert "Ahrefs W4: HTTPStatusError 401 — 1 доменов ждут следующего прогона" in msg, msg


def test_links_wave_cancel_between_batches():
    from app.services import jobs
    states = [_state(f"c{i}.com") for i in range(150)]
    ah = FakeAh({})
    with jobs.track("score", stages=[dict(x) for x in scoring.FUNNEL_STAGES]) as run:
        jobs.request_cancel("score")
        scoring._wave_links(states, {"ahrefs": ah}, _st(), None, run)
    assert len(ah.batches) == 1 and jobs.last("score")["status"] == "cancelled"


def test_single_score_flash_names_paid_wave_reasons(client, monkeypatch):
    """«▶ перепроверить» один домен: причина платной волны названа, а не «приобретаемость не
    определена» — занятость тут ни при чём."""
    from urllib.parse import unquote
    did = _mk("flash.com")
    for why, words in (("ahrefs_failed", "Ahrefs не ответил"), ("units_floor", "ниже пола"),
                       ("ahrefs_missing", "не вернул данных"), ("ahrefs_no_key", "ключ Ahrefs")):
        monkeypatch.setattr(scoring, "score_domain", lambda domain_id, why=why: {
            "domain": "flash.com", "status": "discovered", "unresolved": True, "why": why})
        loc = unquote(client.post(f"/domains/{did}/score", follow_redirects=False).headers["location"])
        assert words in loc, loc
```

- [ ] **Шаг 2: Запустить — убедиться, что падает**

Запуск: `cd backend && ../.venv/bin/python -m pytest tests/test_waves_v2.py -k "links or units_floor or flash" -v`
Ожидание: `15 failed, 23 deselected`. Пять юнит-тестов волны — `AttributeError: module
'app.services.scoring' has no attribute '_wave_links'`; сквозные — по ассертам: W4 ещё нет
(`referring_domains` пуст, нет ключа `unresolved`, Ahrefs не вызывался, в сообщении задачи нет причины),
без ключа RDAP спрошен по обоим доменам (`2 == 1`), выборка взяла все три домена (`3 == 2`), флеш
одиночного скора пишет «приобретаемость не определена».

- [ ] **Шаг 3: Реализовать**

**`scoring.py`:**

1) В докстринге модуля строку
`Order: t0 (зоны/бренды) -> avail (RDAP/whois) -> risk (Web Risk, Spamhaus с DQS) -> history (Wayback)`
и следующую `-> composite score + breakdown -> status scored | rejected (`approved` ставит только человек).`
заменить на:

```
Order: t0 (зоны/бренды) -> avail (RDAP/whois) -> risk (Web Risk, Spamhaus с DQS) -> links (Ahrefs
batch) -> history (Wayback) -> composite score + breakdown -> status scored | rejected (`approved` ставит только человек).
```

2) `FUNNEL_STAGES` — две последние строки
`{"key": "history", "label": "Wayback-история"},` / `{"key": "ahrefs", "label": "Ahrefs (платно)"},` заменить на:

```python
    {"key": "links", "label": "ссылки (Ahrefs)"},
    {"key": "history", "label": "Wayback-история"},
```

3) `_make_clients`: после `from app.integrations.webrisk import WebRiskClient` добавить
`from app.integrations.ahrefs import AhrefsClient`, в словарь — `"ahrefs": AhrefsClient(),` (после
`"rdap": RdapClient(),`).

4) `score_domain`: параметр `ahrefs_budget=None` → `links_budget=None`; вызов
`_run_waves([state], c, st, whois_budget, links_budget, run)`.

5) `score_pending`: блок от `stages = [dict(s) for s in FUNNEL_STAGES]` до `ahrefs_budget = [int(st["max_ahrefs_per_run"])]`
заменить на:

```python
    stages = [dict(s) for s in FUNNEL_STAGES]
    clients = _make_clients()
    # Budget, а не [int]: волна avail конкурентная (12 потоков), голый `box[0] -= 1` под ней — гонка
    whois_budget = Budget(int(st["max_whois_per_run"]))
    links_budget = Budget(int(st["max_links_per_run"]))
    notes: list[str] = []          # пояснения волн («остаток units ниже пола») — в итог задачи
```

вызов — `results = _run_waves(states, clients, st, whois_budget, links_budget, run=run, notes=notes)`
(перенос строки — по стилю файла), итоговый отчёт:

```python
            jobs.report(run, done=total, total=total, current="",
                        message=" · ".join([f"прогнано {total} доменов через воронку", *notes]))
```

(`idle_msg or` в этой ветке был мёртв: она исполняется только при непустом батче, где `idle_msg` — `None`.)

Выборка (находка R2-10) — в импорте `score_pending` `from sqlalchemy import select, func, case, and_` →
`from sqlalchemy import select, func, case, and_, or_`, и

НАЙТИ:
```
        rows = db.execute(
            select(Domain.id, Domain.domain, Domain.lane, Domain.referring_domains,
                   Domain.acquire_deadline, Domain.feed_flags, Domain.source)
            .where(Domain.status == "discovered", scorable(now))
            .order_by(tier,
                      Domain.acquire_deadline.asc(),          # внутри яруса — ближайший дроп первым
                      Domain.referring_domains.desc().nulls_last())   # равных по сроку разводит RD
            .limit(limit)
        ).all()
```
ЗАМЕНИТЬ НА:
```
        q = (select(Domain.id, Domain.domain, Domain.lane, Domain.referring_domains,
                    Domain.acquire_deadline, Domain.feed_flags, Domain.source)
             .where(Domain.status == "discovered", scorable(now))
             .order_by(tier,
                       Domain.acquire_deadline.asc(),         # внутри яруса — ближайший дроп первым
                       Domain.referring_domains.desc().nulls_last()))  # равных по сроку разводит RD
        # R2-10: не-EMD домен без W4 не решается (сверх капа — unresolved links_budget), а W2/W3 за
        # него уже заплачены. Берём таких не больше капа W4; остаток лимита добирают EMD (W4 у них нет).
        cap = min(limit, int(st["max_links_per_run"]))
        rows = db.execute(q.where(or_(Domain.source.is_(None), Domain.source != "emd"))
                          .limit(cap)).all()
        rows += db.execute(q.where(Domain.source == "emd").limit(limit - len(rows))).all()
```

6) `_CONCURRENCY` и две строки комментария над ним (`# history=4 — …`, `# ahrefs=2 — капча за штуку…`) заменить на:

```python
# history=4 — вежливость к archive.org (проектная ценность, не число для тюнинга).
# W4 «ссылки» здесь нет: она идёт пачками ПОСЛЕДОВАТЕЛЬНО (лимит Ahrefs 60 запросов/мин).
_CONCURRENCY = {"avail": 12, "risk": 12, "history": 4}
```

7) `_wave_t0`: удалить последнюю ветку `elif (s.referring_domains is not None and s.referring_domains < st["min_referring_domains"]): …`
(три строки, с комментарием `# уедет в W4 (Задача 11)`).

8) Функции `_ahrefs_one` и `_wave_ahrefs` удалить целиком; на их место (перед `def _commit_result`) вставить:

```python
_LINKS_BATCH = 100          # batch-analysis: до 100 целей за запрос

# W4 -> sig: поле ответа Ahrefs -> ключ сигнала (он же колонка Domain, кроме подсетей и dofollow,
# которые живут в score_breakdown).
_LINKS_FIELDS = (("domain_rating", "dr"), ("refdomains", "referring_domains"),
                 ("refdomains_dofollow", "rd_dofollow"), ("refips_subnets", "ref_subnets"),
                 ("backlinks", "backlinks"), ("org_traffic", "organic_traffic"))

# Платная волна не дошла до домена (ключа Ahrefs нет, пол остатка units, кап W4, сбой Ahrefs,
# строки домена нет в ответе). Такой домен оценится СЛЕДУЮЩИМ прогоном — поэтому _commit_result не
# ставит ему отметку сверки занятости: `scorable` вернул бы free/NULL-лейн только через
# RECHECK_EVERY после неё (находка 2.7).
_PAID_UNRESOLVED = ("links_budget", "units_floor", "ahrefs_no_key", "ahrefs_failed", "ahrefs_missing")


def _units_below_floor(clients: dict, st: dict) -> str | None:
    """Пол остатка units Ahrefs (решение оператора Р3): автопилот гоняет свип раз в час, капы «на
    прогон» месяц не держат. Перед платными волнами — один бесплатный запрос остатка (в начале
    прогона — `_paid_gate`, перед W6 — ещё раз: W4 уже потратила). Остаток неизвестен (None или
    сбой запроса) или ниже пола -> текст причины для сообщения задачи, волна units не тратит. Пол 0
    — пола нет, остаток не спрашиваем."""
    floor = int(st.get("units_floor") or 0)
    if floor <= 0:
        return None
    try:
        left = clients["ahrefs"].units_left()
    except Exception:  # noqa: BLE001 — остаток не узнать: тратить вслепую нельзя
        left = None
    if left is None:
        return "Ahrefs: остаток units неизвестен — платные волны пропущены"
    if left < floor:
        return (f"Ahrefs: остаток {left:,} < пола {floor:,} — платные волны пропущены"
                .replace(",", " "))
    return None


def _paid_gate(states: list, clients: dict, st: dict, notes: list) -> None:
    """Пойдут ли платные волны — решается ОДИН раз за прогон, сразу после бесплатной W0 (находка
    R2-10). Ключа Ahrefs нет или остаток units неизвестен/ниже пола — не-EMD домен без W4 всё равно
    не решится, а RDAP/whois:43 и Web Risk за него тратились бы впустую на каждом часовом свипе.
    Такие домены сразу unresolved (`ahrefs_no_key` / `units_floor`: без отметки сверки, оценятся
    следующим прогоном), причина — в `notes`. EMD идут дальше: W4 у них нет.

    Клиента Ahrefs в наборе нет (тестовые наборы без W4; `_make_clients` кладёт его всегда) —
    решать здесь нечем, тогда скажет сама W4 (`ahrefs_failed`)."""
    todo = [s for s in states if s.alive and s.source != "emd"]
    ah = clients.get("ahrefs")
    if not todo or ah is None:
        return
    if getattr(ah, "api_key", None) == "":             # у фейков тестов атрибута нет
        why, note = "ahrefs_no_key", "Ahrefs: ключ AHREFS_API_KEY не задан — платные волны пропущены"
    else:
        why, note = "units_floor", _units_below_floor(clients, st)
        if note is None:
            return
    for s in todo:
        s.unresolved_why, s.alive = why, False
    notes.append(note)


def _wave_links(states: list, clients: dict, st: dict, budget, run, notes: list | None = None) -> None:
    """W4 — ссылочный профиль из Ahrefs batch-analysis. Пачками по 100 и ПОСЛЕДОВАТЕЛЬНО: лимит API
    60 запросов/мин, одна пачка = один запрос, параллелить нечего. EMD пропускает — у новорега
    нечего мерить. Ключ и пол остатка units здесь не проверяются: это уже решил `_paid_gate` в
    начале прогона (R2-10).

    Домен, до которого волна не дошла (кап `max_links_per_run`, сбой Ahrefs, нет строки домена в
    ответе), НЕ судится без ссылок, а остаётся discovered (unresolved) до следующего прогона: без
    RD скор ниже порога, и домен навсегда ушёл бы в low_score (находка 1.8). Упала пачка — следующие
    не шлются: протухший ключ дал бы 401 на каждой. Причина с HTTP-кодом — в `notes`, то есть в
    сообщении задачи (R2-9): в логе скора её оператор не увидит.

    Пустое поле ответа ничего не затирает (находка 4.12): DR из discovery уже лежит в строке
    домена, и `_commit_result` подставит его, только если сигнала `dr` нет.

    Живой факт 2026-10-01: на дропах RD раздут автоматическим SEO-спамом (DR 0 при RD 700+), поэтому
    рядом с RD пишем подсети — compute_score режет `rd` вдвое при подозрении на спам-сетку."""
    from app.services import jobs
    todo = [s for s in states if s.alive and s.source != "emd"]
    if not todo:
        return
    jobs.report(run, stage="links", done=0, total=len(todo))
    eligible = []
    for s in todo:
        if budget is not None and not budget.take():
            s.unresolved_why, s.alive = "links_budget", False
        else:
            eligible.append(s)
    for i in range(0, len(eligible), _LINKS_BATCH):
        chunk = eligible[i:i + _LINKS_BATCH]
        try:
            data = clients["ahrefs"].batch([s.domain for s in chunk])
        except Exception as e:  # noqa: BLE001 — пачка упала: она и все следующие ждут прогона
            rest = eligible[i:]
            for s in rest:
                s.sig["errors"].append(f"ahrefs:{type(e).__name__}")
                s.unresolved_why, s.alive = "ahrefs_failed", False
            if notes is not None:
                # 401/403 — ключ не принят, 400 — кривой запрос: код нужен оператору, не только тип
                code = getattr(getattr(e, "response", None), "status_code", None)
                why = type(e).__name__ + (f" {code}" if code else "")
                notes.append(f"Ahrefs W4: {why} — {len(rest)} доменов ждут следующего прогона")
            return
        for s in chunk:
            row = data.get(s.domain)
            if row is None:
                # Строки домена нет в ответе (аномалия: на несуществующий домен Ahrefs отдаёт нули) —
                # как сбой: без RD скор ушёл бы в low_score навсегда. Домен ждёт следующего прогона.
                s.sig["errors"].append("ahrefs:missing")
                s.unresolved_why, s.alive = "ahrefs_missing", False
                continue
            for src, key in _LINKS_FIELDS:
                if row.get(src) is not None:
                    s.sig[key] = row[src]
            rd = row.get("refdomains")
            if rd is not None and rd < st["min_referring_domains"]:
                s.reject_reason, s.alive = "low_rd", False
        jobs.report(run, done=min(i + _LINKS_BATCH, len(eligible)), total=len(eligible))
        if jobs.cancelled(run):
            raise jobs.Cancelled()
```

9) `_commit_result`:
- в unresolved-ветке условие `if sig.get("acquirability_checked_at"):` →
  `if sig.get("acquirability_checked_at") and state.unresolved_why not in _PAID_UNRESOLVED:`;
- четыре строки комментария — от `# F25: Ahrefs зовётся ТОЛЬКО когда фид не дал RD (_wave_ahrefs) — …` по
  `# считал бы authority от 0.0, будто Ahrefs вообще не спрашивали. float(): `dr` —` включительно —
  заменить на три (следующие строки комментария, с `# Numeric, ORM отдаёт…`, не трогать):

```python
            # F25 / 4.12: W4 пишет `dr`/`referring_domains` только непустыми — DR из discovery
            # (или с прошлого прогона) лежит в строке домена, и без setdefault compute_score
            # считал бы authority от 0.0, будто Ahrefs вообще не спрашивали. float(): `dr` —
```

- в кортеж колонок цикла `for col in (...)` дописать `"backlinks", "organic_traffic"`
  (последняя строка кортежа: `"dr", "referring_domains", "trademark_risk", "backlinks", "organic_traffic"):`);
- в `score_breakdown` строку `"ahrefs_backlinks": _kept("ahrefs_backlinks"),` заменить на
  `"ref_subnets": _kept("ref_subnets"),` и `"rd_dofollow": _kept("rd_dofollow"),`.

10) `_run_waves`:
- сигнатура — `def _run_waves(states: list, clients: dict, st: dict, whois_budget, links_budget, run, notes: list | None = None) -> list:`
  (перенос строки — как сейчас);
- последнюю строку докстринга (`    элемент списка)."""` — конец фразы `… score_domain — единственный`)
  заменить на:

```
    элемент списка). `notes` — пояснения волн к водопаду («остаток units ниже пола»): сообщение
    задачи переписывается после каждой волны, и сказанное волной иначе стёрлось бы; список
    вызывающего (score_pending) — чтобы пояснение дожило и до итогового сообщения."""
```

- строки `ahrefs_b = ahrefs_budget if …` / `else _ListBudget(ahrefs_budget)` заменить на `links_b = links_budget if links_budget is None or hasattr(links_budget, "take") \`
  / `else _ListBudget(links_budget)` и сразу после них добавить `notes = [] if notes is None else notes`;
- в таблице `waves` строку `("t0", "фильтры", lambda alive: _wave_t0(alive, st)),` заменить на две
  (W0 и сразу решение «пойдут ли платные волны», R2-10):

```python
        # W0 и сразу решение «пойдут ли платные волны» (R2-10) — до того, как W2/W3 потратятся
        ("t0", "фильтры", lambda alive: (_wave_t0(alive, st), _paid_gate(alive, clients, st, notes))),
```

- там же строки `("history", …)` и `("ahrefs", …)` заменить на:

```python
        ("links", "ссылки", lambda alive: _wave_links(alive, clients, st, links_b, run, notes)),
        ("history", "history", lambda alive: _wave_history(alive, clients, st, run)),
```

- в отчёте после волны `message=" · ".join(waterfall)` → `message=" · ".join(waterfall + notes)`.

**`aparser.py`:** удалить блок от комментария `# Rank::Ahrefs resultString` до `def _parse_whois_created(`
(не включая её: `_RE_AHREFS` и `_parse_ahrefs`) и метод `ahrefs_probe` целиком; в первой строке
докстринга модуля `whois / SERP / keywords / coarse-DR` → `whois / SERP / keywords`.

**`settings.py`:** убрать `max_ahrefs_per_run` из `_KEYS_NUM`, `_BOUNDS`, `_defaults()` и `get_settings()`
(колонка в БД — легаси, см. 0025; `_row()` заполнит её дефолтом модели).

**`scoring_config.py`:** удалить строку `MAX_AHREFS_PER_RUN = 50 …`; второй и третий абзацы докстринга
модуля (от `v1 runs on the FREE stack` до `— it costs real money per captcha-solve.`) заменить на:

```
v2 (docs/v2/02-m1-discovery-scoring-spec.md): RDAP/whois, Google Web Risk, Ahrefs API v3
(batch-analysis в W4, анкоры и история трафика в W6 — под капами /settings и полом остатка
units), Wayback + LLM. Every component lands in Domain.score_breakdown for transparency.
```

**`panel.py`:**
- `settings_save`: убрать параметр `max_ahrefs_per_run: int = Form(50)` и аргумент
  `max_ahrefs_per_run=max_ahrefs_per_run` в `st.update_settings(...)`;
- `score_one_action`: в словарь причин после ключа `"budget": …` добавить:

```python
                "ahrefs_failed": "Ahrefs не ответил (ссылочный профиль) — домен остался в поиске, "
                                 "оценится следующим прогоном",
                "units_floor": "остаток units Ahrefs неизвестен или ниже пола (см. /settings) — "
                               "платные волны пропущены, домен остался в поиске",
                "ahrefs_missing": "Ahrefs не вернул данных по домену — домен остался в поиске, "
                                  "оценится следующим прогоном",
                "ahrefs_no_key": "ключ Ahrefs (AHREFS_API_KEY в .env) не задан — платные волны "
                                 "пропущены, домен остался в поиске",
```

**`settings.html`:** удалить станцию `<div class="station">` с плашкой «Кап Ahrefs-проверок за прогон»
целиком (до станции «Веса критериев»).

- [ ] **Шаг 4: Запустить новые тесты**

Запуск: `cd backend && ../.venv/bin/python -m pytest tests/test_waves_v2.py -v`
Ожидание: все тесты файла зелёные (23 + 15 = 38 passed).

- [ ] **Шаг 5: Старые тесты — явный список**

Удалить (логика удалена; тестов гейтов и инвариантов среди них нет):
- `backend/tests/test_aparser.py` — файл целиком (`test_parse_ahrefs_*` ×5, `test_ahrefs_probe_sends_expected_options`);
- `test_aparser_envelope.py::test_ahrefs_probe_raises_on_error_envelope`;
- `test_funnel.py`: класс `_AhrefsMock` и пять тестов от него до комментария
  `# --- квота: воронка не платит whois'ом дважды …` (не включая его): `test_ahrefs_skipped_when_feed_has_referring_domains`,
  `test_ahrefs_called_when_feed_has_no_referring_domains_and_budget_positive`, `test_ahrefs_not_called_when_budget_is_none`,
  `test_ahrefs_not_called_when_budget_exhausted`, `test_ahrefs_failure_does_not_crash_funnel` (их заменили тесты `links`);
- `test_scoring_waves.py`: класс `_FakeAhrefs` и шесть тестов `test_wave_ahrefs_*` (до блока `# _commit_result tests`);
- `test_web_fixes.py::test_settings_save_accepts_max_ahrefs` (поля в форме больше нет).

Фейк «Ahrefs без полей» (см. Interfaces) — добавить в словари клиентов ровно этими парами «найти →
заменить» (переносы и отступы — как есть: Задачи 13 и 16 ищут получившиеся строки дословно). Без него
`clients["ahrefs"]` → `KeyError` → вся пачка `ahrefs_failed`, и домен не доезжает до решения.

`backend/tests/test_funnel.py` (`_clients`):

НАЙТИ:
```
    return {"aparser": _W(), "rkn": _R(), "blacklist": _B(), "searxng": _S(),
            "wayback": wayback, "webrisk": type("WR", (), {"configured": True, "threats": lambda self, d: []})()}
```
ЗАМЕНИТЬ НА:
```
    return {"aparser": _W(), "rkn": _R(), "blacklist": _B(), "searxng": _S(),
            "wayback": wayback, "webrisk": type("WR", (), {"configured": True, "threats": lambda self, d: []})(),
            "ahrefs": type("Ah", (), {"units_left": lambda self: 2_000_000,
                                      "batch": lambda self, ds: {d: {} for d in ds}})()}
```

`backend/tests/test_funnel.py` (`_clients_whois_raises`):

НАЙТИ:
```
    return {"aparser": _W(), "rkn": _R(), "blacklist": _B(), "searxng": _S(),
            "wayback": wb, "webrisk": type("WR", (), {"configured": True, "threats": lambda self, d: []})()}
```
ЗАМЕНИТЬ НА:
```
    return {"aparser": _W(), "rkn": _R(), "blacklist": _B(), "searxng": _S(),
            "wayback": wb, "webrisk": type("WR", (), {"configured": True, "threats": lambda self, d: []})(),
            "ahrefs": type("Ah", (), {"units_left": lambda self: 2_000_000,
                                      "batch": lambda self, ds: {d: {} for d in ds}})()}
```

`backend/tests/test_aparser_envelope.py` (`_clients`):

НАЙТИ:
```
            "wayback": wayback or _WaybackAged(), "webrisk": type("WR", (), {"configured": True, "threats": lambda self, d: []})()}
```
ЗАМЕНИТЬ НА:
```
            "wayback": wayback or _WaybackAged(), "webrisk": type("WR", (), {"configured": True, "threats": lambda self, d: []})(),
            "ahrefs": type("Ah", (), {"units_left": lambda self: 2_000_000,
                                      "batch": lambda self, ds: {d: {} for d in ds}})()}
```

`backend/tests/test_history_verdict.py` (`_clients`):

НАЙТИ:
```
    return {"aparser": _W(), "rkn": _R(), "blacklist": _B(), "searxng": _S(), "wayback": wayback,
            "webrisk": type("WR", (), {"configured": True, "threats": lambda self, d: []})()}
```
ЗАМЕНИТЬ НА:
```
    return {"aparser": _W(), "rkn": _R(), "blacklist": _B(), "searxng": _S(), "wayback": wayback,
            "webrisk": type("WR", (), {"configured": True, "threats": lambda self, d: []})(),
            "ahrefs": type("Ah", (), {"units_left": lambda self: 2_000_000,
                                      "batch": lambda self, ds: {d: {} for d in ds}})()}
```

`backend/tests/test_pipeline.py` (`_funnel_clients`):

НАЙТИ:
```
    return {"aparser": _W(), "webrisk": _WR(), "blacklist": _Bl(), "wayback": _Wb()}
```
ЗАМЕНИТЬ НА:
```
    return {"aparser": _W(), "webrisk": _WR(), "blacklist": _Bl(), "wayback": _Wb(),
            "ahrefs": type("Ah", (), {"units_left": lambda self: 2_000_000,
                                      "batch": lambda self, ds: {d: {} for d in ds}})()}
```

`backend/tests/test_wayback_window.py` (`_clients`):

НАЙТИ:
```
    return {"aparser": _W(), "rkn": _R(), "blacklist": _B(), "searxng": _S(), "wayback": wayback}
```
ЗАМЕНИТЬ НА:
```
    return {"aparser": _W(), "rkn": _R(), "blacklist": _B(), "searxng": _S(), "wayback": wayback,
            "ahrefs": type("Ah", (), {"units_left": lambda self: 2_000_000,
                                      "batch": lambda self, ds: {d: {} for d in ds}})()}
```

`backend/tests/test_whois.py` (`_funnel_clients`):

НАЙТИ:
```
            "searxng": type("S", (), {"indexed_echo": lambda self, d: True})(),
            "wayback": _FunnelWayback()}
```
ЗАМЕНИТЬ НА:
```
            "searxng": type("S", (), {"indexed_echo": lambda self, d: True})(),
            "wayback": _FunnelWayback(),
            "ahrefs": type("Ah", (), {"units_left": lambda self: 2_000_000,
                                      "batch": lambda self, ds: {d: {} for d in ds}})()}
```

`backend/tests/test_rescore.py` (`_survives_to_score`; докстринг — ниже, в «Переписать»):

НАЙТИ:
```
        "searxng": type("S", (), {"indexed_echo": lambda self, d: True})(),
        "wayback": _CleanWayback(),
    }
```
ЗАМЕНИТЬ НА:
```
        "searxng": type("S", (), {"indexed_echo": lambda self, d: True})(),
        "wayback": _CleanWayback(),
        "ahrefs": type("Ah", (), {"units_left": lambda self: 2_000_000,
                                  "batch": lambda self, ds: {d: {} for d in ds}})(),
    }
```

`backend/tests/test_transitions.py` (`test_rescoring_is_the_honest_way_back`, инлайн-словарь `clients`):

НАЙТИ:
```
        "aparser": type("A", (), {"whois_probe": lambda self, d: {
            "available": False, "created": datetime(2008, 1, 1, tzinfo=timezone.utc)}})(),
    }
```
ЗАМЕНИТЬ НА:
```
        "aparser": type("A", (), {"whois_probe": lambda self, d: {
            "available": False, "created": datetime(2008, 1, 1, tzinfo=timezone.utc)}})(),
        "ahrefs": type("Ah", (), {"units_left": lambda self: 2_000_000,
                                  "batch": lambda self, ds: {d: {} for d in ds}})(),
    }
```

`backend/tests/test_transitions.py` (`_clients`):

НАЙТИ:
```
        "aparser": type("A", (), {"whois_probe": lambda self, d: {
            "available": True, "created": datetime(2008, 1, 1, tzinfo=timezone.utc)}})(),
    }
```
ЗАМЕНИТЬ НА:
```
        "aparser": type("A", (), {"whois_probe": lambda self, d: {
            "available": True, "created": datetime(2008, 1, 1, tzinfo=timezone.utc)}})(),
        "ahrefs": type("Ah", (), {"units_left": lambda self: 2_000_000,
                                  "batch": lambda self, ds: {d: {} for d in ds}})(),
    }
```

`backend/tests/test_recheck_acquirability.py` (`test_scoring_stamps_acquirability_so_recheck_does_not_redo_it`):

НАЙТИ:
```
        "searxng": type("S", (), {"indexed_echo": lambda self, d: False})(),
    }
```
ЗАМЕНИТЬ НА:
```
        "searxng": type("S", (), {"indexed_echo": lambda self, d: False})(),
        "ahrefs": type("Ah", (), {"units_left": lambda self: 2_000_000,
                                  "batch": lambda self, ds: {d: {} for d in ds}})(),
    }
```

`backend/tests/test_scoring_waves.py` (`test_run_waves_shrinks_pool_across_stages_and_writes_wave_history`;
метод `_Ap.ahrefs_probe` удаляется):

НАЙТИ:
```
        def safebrowsing_check(self, d): return False
        def ahrefs_probe(self, d): return {"dr": 1.0, "backlinks": 0, "referring_domains": None}
    clients = {"aparser": _Ap(),
```
ЗАМЕНИТЬ НА:
```
        def safebrowsing_check(self, d): return False
    clients = {"aparser": _Ap(),
              "ahrefs": type("Ah", (), {"units_left": lambda self: 2_000_000,
                                        "batch": lambda self, ds: {d: {} for d in ds}})(),
```

Переписать:

`test_funnel.py::test_low_rd_rejects` →

```python
def test_low_rd_rejects():
    """RD судит W4 по ответу Ahrefs (v2), до дорогой истории. Лейн bid: без лейна домен ушёл бы
    в unresolved ещё на W2 (whois «занят», даты дропа нет)."""
    did = _mk(domain="thin.com", referring_domains=0, lane="bid")
    wb = _Wayback()
    from app.services import settings as st
    st.update_settings(min_referring_domains=1)
    thin = type("Ah", (), {"units_left": lambda self: 2_000_000,
                           "batch": lambda self, ds: {d: {"refdomains": 0} for d in ds}})()
    out = scoring.score_domain(did, clients={**_clients(None, wb), "ahrefs": thin})
    assert out["reject_reason"] == "low_rd" and wb.calls == 0
```

`test_transitions.py::test_rescore_t0_exit_does_not_erase_history_evidence` (тест инварианта «перескор не
отмывает» — только переписать) → переименовать в `test_rescore_links_exit_does_not_erase_history_evidence`;
докстринг:

```python
    """Тот же корень с другого входа: поднял min_rd в /settings -> перескор -> low_rd на W4
    «ссылки» (v2: RD судит Ahrefs, и W4 идёт ДО истории).

    Волна истории не исполнялась. Грязная ИСТОРИЯ (prior_flags) и блэклист (Spamhaus без DQS не
    спрашивали) обязаны пережить это — иначе «ослабь порог обратно» возвращало бы домен уже
    отмытым.
    """
```

строки `update_settings(min_referring_domains=100) …` и `out = scoring.score_domain(did, clients=_clients())` →

```python
    update_settings(min_referring_domains=100)            # порог подняли — домен не проходит W4

    thin = type("Ah", (), {"units_left": lambda self: 2_000_000,
                           "batch": lambda self, ds: {d: {"refdomains": 5} for d in ds}})()
    out = scoring.score_domain(did, clients=_clients(ahrefs=thin))
```

(ассерты не меняются: `low_rd`, обе улики и снимки целы).

`test_rescore.py`:
- докстринг `_survives_to_score` →

```python
    """Клиенты, под которыми домен ЖИВЫМ доезжает до compute_score: whois старый (не too_young),
    блэклист чист, история чистая. Ahrefs (W4) отвечает строкой БЕЗ полей — ни DR, ни RD этот
    прогон не наблюдал, и сохранённые в строке домена значения обязаны дожить до скора."""
```

- `test_rescore_keeps_authority_without_a_new_dr_observation`: первый абзац докстринга →

```python
    """ПРОХОДИЛО на 45654e3 (до фикса): рескор домена, у которого DR уже сохранён (v2 — из
    discovery или прошлого прогона), а Ahrefs в этом прогоне поля DR не отдал, терял `authority` —
    `sig["dr"]` не подхватывал сохранённый `d.dr`, и `compute_score` считал его от нуля
    (находка 4.12: W4 пустым значением сигнал не пишет).
```

  три строки комментария `# `_survives_to_score`'s aparser mock has no `ahrefs_probe` …` удалить, вызов →
  `out = scoring.score_domain(did, clients=_survives_to_score(old))` (ассерты не меняются).

`test_scoring_waves.py`:
- `test_wave_t0_rejects_feed_flag_and_low_rd_without_touching_alive_ones` → переименовать в
  `test_wave_t0_rejects_feed_flag_and_leaves_rd_to_links_wave`, первой строкой тела — докстринг

```python
    """W0 не судит RD: в v2 его даёт Ahrefs в W4 «ссылки» (RD из строки домена — не наблюдение
    этого прогона). Домен с низким RD из строки проходит W0 живым."""
```

  и ассерт `low_rd.alive is False and low_rd.reject_reason == "low_rd"` → `low_rd.alive is True and low_rd.reject_reason is None`;
- `test_run_waves_shrinks_pool_across_stages_and_writes_wave_history`: `ahrefs_budget=None` → `links_budget=None`;
  ассерт `by_key["ahrefs"]["before"] == by_key["ahrefs"]["after"] == len(survived)` →
  `by_key["history"]["before"] == by_key["history"]["after"] == len(survived)`;
- `test_run_waves_cancellation_between_waves_preserves_partial_progress`: `ahrefs_budget=None` → `links_budget=None`;
- `test_score_pending_reports_honest_count_when_cancelled_after_partial_commits`: в докстринге
  «2 домена low_rd (W0)» → «2 домена feed_flag (W0)»; первую строку тела →
  `ids = [_mk_domain(domain=f"flag{i}.com", feed_flags={"block": True}, lane="bid") for i in range(2)]`.

`test_settings.py::test_max_ahrefs_per_run_default_and_zero_is_legal` → (тот же инвариант «0 у платного
капа легален» — на W6):

```python
def test_max_deep_per_run_zero_is_legal():
    """В отличие от max_whois_per_run (нижний кламп >=1), кап платной W6 может быть 0 — «анкоры не
    проверяем» (такие домены не попадут в пакет), а не опечатка, которую надо поднять до 1.
    Кап капчи A-Parser v1 (`max_ahrefs_per_run`) — легаси-колонка, настройки его не отдают."""
    from app.services.settings import get_settings, update_settings
    assert update_settings(max_deep_per_run=0)["max_deep_per_run"] == 0
    assert update_settings(max_deep_per_run=-5)["max_deep_per_run"] == 0      # клампится к 0, НЕ к 1
    assert "max_ahrefs_per_run" not in get_settings()
```

`test_funnel_stages_and_job_message.py::test_task10_funnel_stages_has_5_not_6`: ассерт порядка →
`assert keys == ["t0", "avail", "risk", "links", "history"]` (шестой чип добавит Задача 13).

`test_scoring_weights.py`: из обеих форм `/settings/save` убрать пару `"max_ahrefs_per_run": 0,` (поля в форме нет).

`test_job_stages.py`: обе `fake_run_waves` → `def fake_run_waves(states, clients, st, whois_budget, links_budget, run=None, **kw):`
(`score_pending` передаёт `notes=`, Задача 13 — ещё и `deep_budget=`).

- [ ] **Шаг 6: Сьют + линт**

Запуск: `cd backend && ../.venv/bin/python -m pytest tests -q && cd .. && .venv/bin/python -m pyflakes backend/app backend/tests`
Ожидание: **814 passed** (818 + 15 − 6 `test_aparser.py` − 1 `test_aparser_envelope.py` − 5 `test_funnel.py`
− 6 `test_scoring_waves.py` − 1 `test_web_fixes.py`), pyflakes пуст.
Проверка: `grep -rnE "ahrefs_probe|_parse_ahrefs|max_ahrefs_per_run|MAX_AHREFS|ahrefs_budget|_wave_ahrefs" backend/app backend/tests`
— только легаси-колонка в `models/settings.py` и две строки `test_settings.py::test_max_deep_per_run_zero_is_legal`.

- [ ] **Шаг 7: Коммит**

```bash
git add -A backend/app backend/tests
git commit -m "feat(scoring): W4 ссылки — Ahrefs batch-analysis пачками до истории; пол units; капча A-Parser удалена"
```

---

### Задача 12: W5 «история + тема» — LLM по видимому тексту снимков (мягкий сигнал)

**Что меняется и почему.**
- `classify_history` отдаёт тексты УЖЕ прочитанных снимков (`texts`) — лишних запросов к archive.org
  нет. LLM по ним определяет язык и тему прошлого сайта и близость к VPN (инвариант 4: тема видна
  человеку и входит в скор). Сигнал **мягкий**: ничего не отклоняет и не «ослепляет».
- **Возраст и гейт `too_young` уже на месте** (Задача 9, Р5): конец `_history_one` считает возраст по
  старшей из дат RDAP и первого снимка и отклоняет `too_young`. Задача его не трогает и не дублирует —
  тема дописывается ПОСЛЕ гейта и только выжившим. Тест Р5
  `test_waves_v2.py::test_history_age_is_the_older_of_rdap_and_first_snapshot` зовёт `_history_one` без
  клиента `llm` — так и остаётся: без `texts` в ответе Wayback и без клиента LLM тема просто не
  спрашивается.
- **Свой клиент классификации** (находка 3.3): `LlmClassifyClient` — таймаут 30 с и ОДНА попытка (без
  ретраев `BaseClient`), иначе зависший LiteLLM держал бы слот волны ~6 минут на домен. Предохранитель
  «3 сбоя подряд» — тот же `whois.guarded` (Задача 9) под локом `_llm_lock`: дальше «тема не определена»
  без вызова до конца прогона. Модель — `LLM_CLASSIFY_MODEL`, пусто → `LLM_MODEL` (Р6).
- **Лок предохранителя проверяется спай-локом** (находка R2-15, урок v1 2026-07-21: при переходе на
  волны лок получили не все клиенты): `_llm_lock` дописывается в
  `test_make_clients_has_every_breaker_lock`, а детерминированный тест считает входы в лок — как у
  Web Risk (Задача 10), не таймингом.
- **Строгий разбор** (находка 4.2): `vpn_adjacent` — только конечное число (не строка, не bool, не
  NaN/Infinity), кламп 0..1; без `topic_summary` ответ непригоден; `snapshots` — только список;
  парковка — только литерал `true`.
- **EMD** (находка 4.4): язык — рынка набора (его пишет discovery), W5 его не перезаписывает.
- **Перескор со сбоем LLM** (находка 4.5): `_commit_result` явно пишет `topic = None`, иначе старая
  тема соседствовала бы с «тема не определена». **Близость к VPN (`topical_relevance`) НЕ стирается**
  (решение координатора 2026-10-02): перескор при лежащем LLM не вправе снять исключение «прошлая тема
  далека от VPN» (Задача 13) — тот же принцип «перескор не отмывает». Язык не трогается.

**Files:**
- Create: `backend/app/services/history_llm.py`
- Modify:
  - `backend/app/integrations/llm.py` — параметр `timeout` у `LlmClient`, класс `LlmClassifyClient`;
  - `backend/app/integrations/wayback.py` — `classify_history` отдаёт `texts`;
  - `backend/app/services/scoring.py` — `_topic_one`, `_history_one`, `_make_clients`, `_commit_result`, `FUNNEL_STAGES`;
  - `backend/app/config.py` — `LLM_CLASSIFY_MODEL`; `.env.example` — строка `LLM_CLASSIFY_MODEL=`.
- Test: `backend/tests/test_history_llm.py` (создать); дописать в `test_waves_v2.py` и `test_wayback_classify.py`.
  Старые тесты задача не ломает (их Wayback-фейки не отдают `texts` — LLM не зовётся).

**Interfaces:**
- Consumes: `LlmClient.complete(system, prompt, **kwargs)` (существует), `whois.guarded` (Задача 9),
  помощники `test_waves_v2.py` из Задачи 11 (`_mk`, `_full_clients(rdap, ah, wb, **extra)`, `FakeAh`, `ROW`).
- Produces:
  - `history_llm.parse_answer(raw) -> {"market_lang", "topic", "topical_relevance", "parked_share"} | None`;
  - `history_llm.classify_topics(domain, texts, llm) -> dict | None` (модель выбирает клиент);
  - `LlmClassifyClient()` — `model`, `request` = одна попытка;
  - `wayback.classify_history(...)["texts"] -> [{"timestamp", "text"}]` (во всех трёх ветках);
  - `_topic_one(s, clients, texts)`; сигналы `sig["market_lang"]`, `sig["topic"]`, `sig["topical_relevance"]`,
    `sig["parked_share"]`, `sig["topic_unknown"]`;
  - ключи клиентов `clients["llm"]`, `clients["_llm_lock"]`; счётчик предохранителя `llm.classify_failures`;
  - `score_breakdown`: `topic_unknown`, `parked_share`; колонки `market_lang`, `topic`, `topical_relevance`;
  - `FUNNEL_STAGES[-1]` = `{"key": "history", "label": "история + тема"}`;
  - `test_waves_v2.py`: `FakeWB(dirty, checked, texts, age)` (тексты по умолчанию есть), `FakeLLM(answer, boom)`
    (считает `calls`) — для Задачи 13.

- [ ] **Шаг 1: Написать падающие тесты**

`backend/tests/test_history_llm.py`:

```python
"""W5: строгий разбор ответа LLM и клиент классификации. Мягкий сигнал — непригодный ответ = None,
не исключение."""
import httpx
import pytest

from app.integrations.llm import LlmClassifyClient
from app.services import history_llm

GOOD = ('Вот ответ: {"snapshots":[{"year":2015,"lang":"es","topic":"travel blog","parked":false},'
        '{"year":2019,"lang":"en","topic":"vpn reviews","parked":false},'
        '{"year":2022,"lang":"en","topic":"domain for sale","parked":true}],'
        '"topic_summary":"VPN and privacy reviews","vpn_adjacent":0.9} — готово')
TOPIC = '"topic_summary": "vpn blog"'


def test_parse_answer_extracts_json_from_prose_and_picks_latest_live_lang():
    r = history_llm.parse_answer(GOOD)
    assert r == {"market_lang": "en", "topic": "VPN and privacy reviews",
                 "topical_relevance": 0.9, "parked_share": 0.33}


def test_parse_answer_clamps_and_rejects_garbage():
    """4.2: `vpn_adjacent` — только конечное число (не строка, не bool, не NaN/Infinity), кламп 0..1."""
    assert history_llm.parse_answer('{"vpn_adjacent": 7, "snapshots": [], %s}' % TOPIC)["topical_relevance"] == 1.0
    assert history_llm.parse_answer('{"vpn_adjacent": -2, %s}' % TOPIC)["topical_relevance"] == 0.0
    assert history_llm.parse_answer("не JSON вовсе") is None
    assert history_llm.parse_answer('[1, 2]') is None
    for bad in ('"много"', '"0.5"', 'true', 'NaN', 'Infinity', 'null'):
        assert history_llm.parse_answer('{"vpn_adjacent": %s, %s}' % (bad, TOPIC)) is None, bad
    r = history_llm.parse_answer('{"vpn_adjacent": 0.2, "snapshots": [{"year": 2020, "lang": "Spanish"}], %s}' % TOPIC)
    assert r["market_lang"] is None


def test_parse_answer_is_strict_about_parked_snapshots_and_topic():
    """4.2: парковка — только литерал true; `snapshots` не списком — снимков нет; без
    `topic_summary` ответ непригоден («тема не определена»)."""
    r = history_llm.parse_answer('{"snapshots": [{"year": 2021, "lang": "de", "parked": "false"}], '
                                 '"topic_summary": "Reisen", "vpn_adjacent": 0.1}')
    assert r["market_lang"] == "de" and r["parked_share"] == 0.0
    r = history_llm.parse_answer('{"snapshots": 5, "topic_summary": "vpn", "vpn_adjacent": 0.5}')
    assert r["market_lang"] is None and r["parked_share"] is None and r["topic"] == "vpn"
    assert history_llm.parse_answer('{"snapshots": [], "vpn_adjacent": 0.5}') is None
    assert history_llm.parse_answer('{"snapshots": [], "topic_summary": "  ", "vpn_adjacent": 0.5}') is None


def test_classify_topics_truncates_and_asks_deterministically():
    seen = {}

    class LLM:
        def complete(self, system, prompt, **kw):
            seen.update(prompt=prompt, **kw)
            return GOOD
    texts = [{"timestamp": "20190101000000", "text": "x" * 5000}] * 7
    r = history_llm.classify_topics("a.com", texts, LLM())
    assert r["market_lang"] == "en" and seen["temperature"] == 0
    assert "model" not in seen                     # модель выбирает клиент (LLM_CLASSIFY_MODEL)
    assert seen["prompt"].count("--- снимок") == 5 and "x" * 2001 not in seen["prompt"]
    assert history_llm.classify_topics("a.com", [], LLM()) is None


def test_classify_client_one_attempt_short_timeout_and_model_fallback(monkeypatch):
    """3.3: зависший LiteLLM не держит слот волны истории ~6 минут (120 с × 3 попытки): у клиента
    классификации таймаут 30 с и ОДНА попытка. Р6: модель — LLM_CLASSIFY_MODEL, пусто -> LLM_MODEL."""
    from app.config import settings
    monkeypatch.setattr(settings, "LLM_CLASSIFY_MODEL", "")
    c = LlmClassifyClient()
    assert c.model == settings.LLM_MODEL and c._client.timeout.read == 30.0
    calls = []

    def down(method, url, **kw):
        calls.append(url)
        raise httpx.ConnectError("down")
    monkeypatch.setattr(c._client, "request", down)
    with pytest.raises(httpx.ConnectError):
        c.complete("s", "p")
    assert len(calls) == 1                          # без ретраев BaseClient
    monkeypatch.setattr(settings, "LLM_CLASSIFY_MODEL", "ollama/qwen2.5")
    assert LlmClassifyClient().model == "ollama/qwen2.5"
```

В `test_waves_v2.py::test_make_clients_has_every_breaker_lock` (находка R2-15):

НАЙТИ:
```
    for lock in ("_whois_lock", "_rdap_lock", "_webrisk_lock"):
```
ЗАМЕНИТЬ НА:
```
    for lock in ("_whois_lock", "_rdap_lock", "_webrisk_lock", "_llm_lock"):
```

Дописать в конец `test_waves_v2.py`:

```python
# --- W5 «история + тема» (Задача 12) -----------------------------------------------------------

class FakeWB:
    """Wayback с текстами прочитанных снимков (для темы W5)."""
    def __init__(self, dirty=False, checked=True, texts=None, age=9.0):
        self.dirty, self.checked, self.age = dirty, checked, age
        self.texts = texts if texts is not None else [{"timestamp": "20190101000000", "text": "vpn reviews"}]

    def classify_history(self, d):
        flags = {c: False for c in ("adult", "pharma", "casino", "gambling", "spam")}
        flags["casino"] = self.dirty
        return {"prior_flags": flags, "first_seen": None, "age_years": self.age,
                "wayback_checked": self.checked, "sampled": 5, "evidence": [], "texts": self.texts}


class FakeLLM:
    def __init__(self, answer=None, boom=False):
        self.answer, self.boom, self.calls = answer, boom, 0

    def complete(self, system, prompt, **kw):
        self.calls += 1
        if self.boom:
            raise RuntimeError("llm down")
        return self.answer or ('{"snapshots":[{"year":2019,"lang":"pl","topic":"vpn","parked":false}],'
                               '"topic_summary":"vpn blog","vpn_adjacent":0.8}')


def test_history_llm_fills_soft_signals():
    s = _state("a.com")
    scoring._history_one(s, {"wayback": FakeWB(), "llm": FakeLLM()}, _st())
    assert s.alive and s.sig["market_lang"] == "pl" and s.sig["topical_relevance"] == 0.8
    assert s.sig["topic"] == "vpn blog" and "topic_unknown" not in s.sig


def test_history_llm_failure_is_soft():
    s = _state("a.com")
    scoring._history_one(s, {"wayback": FakeWB(), "llm": FakeLLM(boom=True)}, _st())
    assert s.alive and s.sig.get("topic_unknown") is True and not s.sig["errors"]


def test_dirty_history_rejects_before_llm():
    s, llm = _state("a.com"), FakeLLM()
    scoring._history_one(s, {"wayback": FakeWB(dirty=True), "llm": llm}, _st())
    assert s.reject_reason == "history_dirty" and llm.calls == 0


def test_llm_circuit_opens_after_three_failures():
    """3.3: лежащий LiteLLM — после 3 сбоев ПОДРЯД без вызова до конца прогона (счётчик на
    инстансе клиента, детерминированно, без таймингов). Домены едут дальше с «тема не определена»."""
    llm = FakeLLM(boom=True)
    states = [_state(f"t{i}.com") for i in range(5)]
    for s in states:
        scoring._history_one(s, {"wayback": FakeWB(), "llm": llm}, _st())
    assert llm.calls == 3
    assert all(s.alive and s.sig["topic_unknown"] is True and not s.sig["errors"] for s in states)


def test_llm_breaker_locks_both_the_gate_check_and_the_increment():
    """R2-15, урок v1: гонку на счётчике предохранителя ловит детерминированный спай-лок, а не
    тайминг. Каждая из 3 попыток LLM до срабатывания берёт `_llm_lock` дважды (гейт-чек и
    инкремент), 4-я — один раз (гейт-чек: канал уже закрыт). Пропуск любого входа — непокрытая
    гонка под 4 потоками волны истории."""
    import threading

    class SpyLock:
        def __init__(self):
            self._real, self.enters = threading.Lock(), 0

        def __enter__(self):
            self._real.acquire()
            self.enters += 1

        def __exit__(self, *a):
            self._real.release()
    lock, llm = SpyLock(), FakeLLM(boom=True)
    for i in range(4):
        scoring._history_one(_state(f"t{i}.com"), {"wayback": FakeWB(), "llm": llm, "_llm_lock": lock},
                             _st())
    assert llm.calls == 3 and llm.classify_failures == 3
    assert lock.enters == 3 * 2 + 1


def test_emd_keeps_market_lang_of_its_set():
    """4.4: язык EMD — язык рынка набора (discovery); снимки прошлого сайта его не перезаписывают."""
    s = _state("mejorvpn.com", source="emd", lane="free")
    scoring._history_one(s, {"wayback": FakeWB(), "llm": FakeLLM()}, _st())
    assert "market_lang" not in s.sig and s.sig["topic"] == "vpn blog"


def test_llm_failure_on_rescore_clears_topic_but_keeps_relevance():
    """4.5: перескор со сбоем LLM не оставляет старую тему — «тема не определена» и в базе. А
    близость к VPN (0.1) остаётся: перескор при лежащем LLM не отмывает «тема далека от VPN»."""
    did = _mk("old-topic.com", deadline=NOW + timedelta(days=2), topic="casino reviews",
              topical_relevance=0.1, market_lang="en")
    scoring.score_domain(did, clients=_full_clients(
        FakeRdap(exists=True, registered=NOW - timedelta(days=4000)), FakeAh({"old-topic.com": ROW}),
        FakeWB(), llm=FakeLLM(boom=True)))
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
        assert d.topic is None and float(d.topical_relevance) == 0.1
        assert d.score_breakdown["topic_unknown"] is True and d.market_lang == "en"


def test_llm_topic_reaches_the_domain_row():
    did = _mk("pl-blog.com", deadline=NOW + timedelta(days=2))
    scoring.score_domain(did, clients=_full_clients(
        FakeRdap(exists=True, registered=NOW - timedelta(days=4000)), FakeAh({"pl-blog.com": ROW}),
        FakeWB(), llm=FakeLLM()))
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
        assert (d.market_lang, d.topic, float(d.topical_relevance)) == ("pl", "vpn blog", 0.8)
        assert d.score_breakdown["topic_unknown"] is None
```

Дописать в конец `test_wayback_classify.py`:

```python
def test_classify_history_returns_texts_of_read_snapshots(monkeypatch):
    """W5: тексты УЖЕ прочитанных снимков уходят в LLM-тему — лишних запросов к archive.org нет."""
    from app.integrations.wayback import WaybackClient
    wb = WaybackClient()
    snaps = [{"timestamp": f"20{10 + i}0101000000", "original": "http://x.com/"} for i in range(5)]
    monkeypatch.setattr(wb, "get_snapshots", lambda d, **kw: snaps)
    monkeypatch.setattr(wb, "_fetch_raw", lambda ts, orig: "<html><title>VPN blog</title><body>"
                        + "secure tunnel " * 300 + "</body></html>")
    out = wb.classify_history("x.com", sample=5, polite=0)
    assert out["wayback_checked"] is True and len(out["texts"]) == 5
    assert all(len(t["text"]) <= 2000 for t in out["texts"])
    monkeypatch.setattr(wb, "get_snapshots", lambda d, **kw: [])
    assert wb.classify_history("x.com", sample=5, polite=0)["texts"] == []
```

- [ ] **Шаг 2: Запустить — убедиться, что падает**

Запуск: `cd backend && ../.venv/bin/python -m pytest tests/test_history_llm.py -v`
Ожидание: ошибка сбора — `ImportError: cannot import name 'LlmClassifyClient' from 'app.integrations.llm'`.

Запуск: `cd backend && ../.venv/bin/python -m pytest tests/test_waves_v2.py tests/test_wayback_classify.py -k "llm or topic or market_lang or texts or every_breaker_lock" -v`
Ожидание: `9 failed, 3 passed`. Падают семь новых тестов волны (нет `market_lang`/`topic_unknown`, LLM не
зовётся — у спай-лок-теста `llm.calls == 0`, тема не доходит до строки домена),
`test_make_clients_has_every_breaker_lock` (`AssertionError: _llm_lock`) и
`test_classify_history_returns_texts_of_read_snapshots` (`KeyError: 'texts'`). Проходят
`test_dirty_history_rejects_before_llm` (LLM ещё не зовётся вовсе) и два старых `test_topic_switch_*`,
попавшие в фильтр.

- [ ] **Шаг 3: Реализовать**

`backend/app/services/history_llm.py`:

```python
"""W5: язык и тема прошлого сайта по видимому тексту снимков Wayback — через LLM (LiteLLM).

МЯГКИЙ сигнал (инвариант 3 v2): ничего не отклоняет и никого не «ослепляет». Жёсткие отказы по
истории — только детерминированный wayback._classify_text. Зачем тема вообще: политика Google
«expired domain abuse» — примеры в ней все про СМЕНУ темы (казино на бывшем сайте школы);
оператор обязан видеть, чем домен был, прежде чем делать из него VPN-сайт (инвариант 4).
"""
import json
import math
import re

_SYSTEM = (
    "Ты классификатор истории веб-сайтов. По тексту снимков сайта из веб-архива определи для "
    "КАЖДОГО снимка: язык (ISO 639-1, две латинские буквы), тему (до 6 слов по-английски) и "
    "признак припаркованного домена (заглушка продажи/парковки/ошибки). Затем общую тему сайта и "
    "насколько она близка к VPN, приватности, кибербезопасности, софту, интернет-сервисам или "
    "стримингу — число от 0 до 1. Ответь ТОЛЬКО JSON без пояснений: "
    '{"snapshots":[{"year":2019,"lang":"es","topic":"...","parked":false}],'
    '"topic_summary":"...","vpn_adjacent":0.0}')
_LANG = re.compile(r"^[a-z]{2}$")
_MAX_SNAPS, _MAX_CHARS = 5, 2000


def _year(x: dict) -> int:
    y = str(x.get("year") or "")[:4]
    return int(y) if y.isdigit() else 0


def parse_answer(raw: str) -> dict | None:
    """Строгий разбор (находка 4.2). JSON-объект, даже если LLM обернула его прозой;
    `vpn_adjacent` — только конечное число (не строка, не bool, не NaN/Infinity), клампится к 0..1;
    `topic_summary` обязателен; `snapshots` — только список, парковка — только литерал true.
    None — ответ непригоден: тема останется «не определена»."""
    s = raw or ""
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        data = json.loads(s[i:j + 1])
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    adj = data.get("vpn_adjacent")
    if isinstance(adj, bool) or not isinstance(adj, (int, float)) or not math.isfinite(adj):
        return None
    topic = str(data.get("topic_summary") or "").strip()[:120]
    if not topic:
        return None
    raw_snaps = data.get("snapshots")
    snaps = [x for x in raw_snaps if isinstance(x, dict)] if isinstance(raw_snaps, list) else []
    live = [x for x in snaps if x.get("parked") is not True]
    lang = next((str(x.get("lang") or "").strip().lower() for x in sorted(live, key=_year, reverse=True)
                 if _LANG.match(str(x.get("lang") or "").strip().lower())), None)
    return {"market_lang": lang, "topic": topic, "topical_relevance": max(0.0, min(1.0, float(adj))),
            "parked_share": round(1 - len(live) / len(snaps), 2) if snaps else None}


def build_prompt(domain: str, texts: list) -> str:
    parts = [f"Домен: {domain}"]
    for t in texts[:_MAX_SNAPS]:
        parts.append(f"--- снимок {str(t.get('timestamp') or '')[:4]} ---\n"
                     f"{str(t.get('text') or '')[:_MAX_CHARS]}")
    return "\n\n".join(parts)


def classify_topics(domain: str, texts: list, llm) -> dict | None:
    """Один вызов LLM на домен. Модель выбирает клиент (LlmClassifyClient: LLM_CLASSIFY_MODEL,
    пусто -> LLM_MODEL). Исключение транспорта пробрасывается — его считает предохранитель
    воронки; непригодный ответ — None, не исключение."""
    if not texts:
        return None
    return parse_answer(llm.complete(_SYSTEM, build_prompt(domain, texts), temperature=0))
```

**`integrations/llm.py`:**
- `LlmClient.__init__(self)` → `def __init__(self, timeout: float = 120.0):`, в `super().__init__(…)` —
  `timeout=timeout` (комментарий про 120 с оставить);
- в конец файла:

```python
class LlmClassifyClient(LlmClient):
    """Классификация темы снимков в W5 (services/history_llm.py): короткий ответ, а не страница.

    Таймаут 30 с и ОДНА попытка, без ретраев BaseClient: зависший LiteLLM иначе держал бы слот
    волны истории ~6 минут на КАЖДЫЙ домен (120 с × 3 попытки). Предохранитель «3 сбоя подряд»
    ставит воронка (scoring._topic_one, whois.guarded). Модель — LLM_CLASSIFY_MODEL (ollama-модель
    бокса), пусто -> LLM_MODEL."""
    def __init__(self):
        super().__init__(timeout=30.0)
        self.model = settings.LLM_CLASSIFY_MODEL or settings.LLM_MODEL

    def request(self, method: str, url: str, **kwargs):
        return self._request_once(method, url, **kwargs)
```

**`integrations/wayback.py`, метод `classify_history`:**
- в ветке «нет снимков» в возвращаемый словарь добавить `"texts": []`;
- после строки `evidence: list[dict] = []      # ЧТО именно смотрели …` добавить
  `texts: list[dict] = []         # тексты прочитанных снимков — для темы W5 (history_llm)`;
- внутри цикла под `if read:` после `ok += 1` добавить
  `texts.append({"timestamp": s["timestamp"], "text": text[:2000]})`;
- в двух оставшихся `return` (`wayback_checked: False` при малом покрытии и итоговый) добавить `"texts": texts`;
- две последние строки комментария про `topic_switch` (`# Тематическая ПРЕЕМСТВЕННОСТЬ донора инвариантом
  проекта не является …` / `… — только удалить.`) заменить на:

```python
        # Тематическую ПРЕЕМСТВЕННОСТЬ (инвариант 4 v2) судит не флаг категорий, а мягкий сигнал
        # темы W5 — LLM по `texts` ниже (services/history_llm.py).
```

**`config.py`:** после строки `LLM_MODEL: str = "mistral" …` добавить
`    LLM_CLASSIFY_MODEL: str = ""                      # W5: тема/язык снимков; пусто -> LLM_MODEL`.

**`.env.example`:** после строки `LLM_MODEL=mistral …` добавить две строки:

```
# W5 (тема/язык прошлого сайта): пусто -> LLM_MODEL. На боксе — ollama/<модель> из /v1/models
LLM_CLASSIFY_MODEL=
```

**`scoring.py`:**

1) `FUNNEL_STAGES`: `{"key": "history", "label": "Wayback-история"}` → `{"key": "history", "label": "история + тема"}`.

2) `_make_clients`: после `from app.integrations.ahrefs import AhrefsClient` добавить
`from app.integrations.llm import LlmClassifyClient`; строку `"_webrisk_lock": threading.Lock(),` заменить на
`"_webrisk_lock": threading.Lock(), "llm": LlmClassifyClient(), "_llm_lock": threading.Lock(),`.

3) Перед `def _history_one` вставить:

```python
def _topic_one(s: FunnelState, clients: dict, texts: list) -> None:
    """W5-мягкий: язык и тема прошлого сайта по УЖЕ прочитанным снимкам (LLM). Ничего не отклоняет
    и не ослепляет (инвариант 3: жёсткие отказы — только детерминированный классификатор). Сбой,
    непригодный ответ, нет клиента или сработал предохранитель «3 сбоя подряд» (зависший LiteLLM
    держал бы слот волны) -> «тема не определена». EMD: язык — рынка набора (discovery), снимки
    его не перезаписывают (находка 4.4)."""
    from app.services import history_llm
    llm, t = clients.get("llm"), None
    if llm is not None:
        try:
            t = whois_router.guarded(llm, "classify_failures",
                                     lambda: history_llm.classify_topics(s.domain, texts, llm),
                                     "LLM (тема W5)", clients.get("_llm_lock"))
        except Exception:  # noqa: BLE001 — мягкий сигнал: сбой LLM не отклоняет и не ослепляет
            t = None
    if not t:
        s.sig["topic_unknown"] = True
        return
    if s.source == "emd":
        t = {k: v for k, v in t.items() if k != "market_lang"}
    s.sig.update(t)
```

4) `_history_one`:
- последнюю строку докстринга `двух дат (RDAP/whois из W2 и первый снимок) и гейт `too_young` (Р5)."""` →
  `двух дат (RDAP/whois из W2 и первый снимок) и гейт `too_young` (Р5) + тема прошлого сайта."""`;
- перед `try:` добавить `texts: list = []`; внутри `try` сразу после
  `hist = clients["wayback"].classify_history(s.domain)` добавить `texts = hist.get("texts") or []`;
- в конец функции (после блока `too_young`, гейт не трогать):

```python

    # Тема — только выжившим и только по проверенной истории: LLM на уже отклонённый домен —
    # пустая трата времени, а по паре прочитанных снимков тему не судят.
    if s.alive and s.sig.get("wayback_checked") and texts:
        _topic_one(s, clients, texts)
```

5) `_commit_result`:
- в кортеж колонок цикла `for col in (...)` дописать `"market_lang", "topic", "topical_relevance"` — кортеж
  заканчивается строками:

```python
                    "dr", "referring_domains", "trademark_risk", "backlinks", "organic_traffic",
                    "market_lang", "topic", "topical_relevance"):
```

- сразу после цикла (перед `d.clean = …`):

```python
        if sig.get("topic_unknown"):
            # Исключение из правила «не затирать»: тему ЭТОТ прогон спрашивал, и ответа нет —
            # старая тема рядом с «тема не определена» врала бы (находка 4.5). Близость к VPN НЕ
            # стираем: перескор при лежащем LLM не вправе снять исключение «прошлая тема далека от
            # VPN» (тот же принцип «перескор не отмывает»). Язык не трогаем (у EMD он из набора).
            d.topic = None
```

- в `score_breakdown` последнюю строку `"webrisk_threats": _kept("webrisk_threats")}` заменить на:

```python
                             "webrisk_threats": _kept("webrisk_threats"),
                             "topic_unknown": sig.get("topic_unknown"),
                             "parked_share": _kept("parked_share")}
```

- [ ] **Шаг 4: Сьют + линт**

Запуск: `cd backend && ../.venv/bin/python -m pytest tests -q && cd .. && .venv/bin/python -m pyflakes backend/app backend/tests`
Ожидание: **828 passed** (814 + 5 `test_history_llm.py` + 8 `test_waves_v2.py` + 1 `test_wayback_classify.py`),
pyflakes пуст. Старые интеграционные тесты не меняются: их Wayback-фейки не отдают `texts`, LLM не зовётся.

- [ ] **Шаг 5: Коммит**

```bash
git add -A backend/app backend/tests .env.example
git commit -m "feat(scoring): W5 — язык и тема прошлого сайта через LLM (мягкий сигнал, предохранитель)"
```

---

### Задача 13: W6 «анкоры» + правила пакетного одобрения + ветка EMD + грязные причины

**Что меняется и почему.**
- **W6 — анкоры и пик трафика финалистов** (~1,1 тыс. units на домен): только для доменов с
  **проверенной историей** (`wayback_checked` — решение координатора 2026-10-02: домен «вслепую» в пакет
  всё равно не попадёт, units на него не тратим) и предварительным скором ≥ **рантайм**-порога
  `manual_review_at` (находка 2.8), лучшие первыми, под капом
  `max_deep_per_run` и полом остатка units (Р3: ниже пола W6 не идёт, анкоры «не проверены», причина —
  в сообщении задачи через `notes` из Задачи 11). Анкоры и история трафика — **отдельные вызовы**
  (находка 3.7): сбой истории — `deep_history:<Исключение>`, оплаченный вердикт по анкорам остаётся.
- **`deep_checked=True` только если анкоры реально получены** (находка 1.3): пустой список при живых
  донорах — `deep:empty`, «не проверено». Нет доноров (RD 0) — проверять нечего, это честное «проверено».
- **Спам-анкоры — грязь, и перескор её не отмывает** (находка 1.4): `sig["spam_anchors"]`, `_commit_result`
  хранит её через `_kept`, `transitions.dirty_reason` проверяет `score_breakdown.spam_anchors is True`.
  `DIRTY_REASONS` += `trademark`, `spam_anchors`.
- **Скрипт языка прошлого сайта — не спам** (Р1): `spam_anchor_ratio(anchors, domain, market_lang)`; карта
  `ja` → кана + CJK, `zh` → CJK, `ko` → хангыль, `th` → тайский, `ru/uk/bg/sr/kk/be/mk` → кириллица.
  Стоп-слова с множественным числом (находка 4.3): `casinos?`, `porn\w*`.
- **Язык неизвестен — правило скрипта одно не отклоняет** (находка R2-2). Язык для исключения Р1 — из
  W5 этого прогона, иначе с прошлого прогона: `FunnelState.market_lang` грузится из `Domain.market_lang`.
  Языка нет, а доля спама выше порога ТОЛЬКО за счёт правила скрипта — не отказ: `errors +=
  ["deep:lang_unknown"]`, `deep_checked=False` (вне пакета, но не грязь). Иначе сбой LLM давал бы вечную
  грязь `spam_anchors` (кнопкой не вернуть), а на перескоре пачкал бы ранее чистый домен — вопреки «сбой
  LLM не отклоняет». Флаг Ahrefs `is_spam` и стоп-слова отклоняют и без языка.
- **Авто-одобрения нет (Р2), гарда `deep_checked` в `_decide` нет.** Всё, что не даёт одобрить пакетом,
  живёт в `bulk_ok`/`blind_reason`: «анкоры НЕ проверены» (`deep_checked is not True`, кроме EMD);
  «прошлая тема далека от VPN» (`topical_relevance < 0.3`, `None` не исключает — новая `topic_far`);
  пустой балл (EMD). Такой домен человек одобряет кнопкой в строке, глядя на тему и анкоры.
- **EMD** — `scored` без балла (`score=None`, `score_breakdown.emd=True`): ни W4, ни W6 не тратятся, пакет
  его не берёт (находка 1.13).

**Files:**
- Create: `backend/app/services/link_signals.py`
- Modify:
  - `backend/app/services/scoring.py` — `_deep_one`, `_wave_deep`, `_CONCURRENCY`, `FUNNEL_STAGES`, таблица
    волн и `_run_waves` (`deep_budget`), `FunnelState.market_lang`, `score_domain`, `score_pending`,
    `_commit_result` (EMD, колонки, улики), `blind_reason`, новая `topic_far`, `bulk_ok`, докстринг модуля;
  - `backend/app/services/scoring_config.py` — `TOPIC_FAR_BELOW = 0.3`;
  - `backend/app/services/transitions.py` — `DIRTY_REASONS`, `dirty_reason` видит `spam_anchors`.
- Test: `backend/tests/test_link_signals.py` (создать); дописать в `test_waves_v2.py`. Старые тесты — явный
  список в шаге 5.

**Interfaces:**
- Consumes: `AhrefsClient.anchors/metrics_history` (Задача 2), `compute_score` (Задача 8),
  `_units_below_floor`, `notes`, `FakeAh`/`_mk`/`_full_clients`/`ROW` (Задача 11), `FakeWB`/`FakeLLM`
  (Задача 12), `st["max_deep_per_run"]`, `st["spam_anchor_max"]`, `st["manual_review_at"]`.
- Produces:
  - `link_signals.spam_anchor_ratio(anchors, domain, market_lang=None, scripts=True) -> float | None`
    (`scripts=False` — без правила скрипта, R2-2); `link_signals.peak_traffic(history) -> int | None`;
  - `FunnelState.market_lang` — язык прошлого сайта из БД (запасной для W6, R2-2);
  - `_deep_one(s, clients, st)`, `_wave_deep(states, clients, st, budget, run, notes=None)`;
  - `_run_waves(states, clients, st, whois_budget, links_budget, run, notes=None, deep_budget=None)`;
    `score_domain(domain_id, clients=None, whois_budget=None, links_budget=None, run=None, deep_budget=None)`;
  - сигналы `sig["deep_checked"]`, `sig["spam_anchor_ratio"]`, `sig["spam_anchors"]`, `sig["anchors"]`
    (топ-10), `sig["peak_traffic"]`; ошибки `deep:<Исключение>`, `deep:empty`, `deep:lang_unknown`,
    `deep_history:<Исключение>`;
  - `score_breakdown`: `deep_checked`, `spam_anchors` (`_kept`), `peak_traffic` (`_kept`); колонки `anchors`,
    `spam_anchor_ratio`; EMD-итог `{"status": "scored", "score": None, "breakdown": {"emd": True}}`;
  - `scoring.topic_far(d) -> bool` (порог `cfg.TOPIC_FAR_BELOW`); `bulk_ok(d)` дополнительно требует
    `not topic_far(d)` и `d.score is not None`; `blind_reason(d)` — «анкоры НЕ проверены: …»;
  - `FUNNEL_STAGES` — шесть чипов, последний `{"key": "deep", "label": "анкоры (Ahrefs)"}`;
    `_CONCURRENCY = {"avail": 12, "risk": 12, "history": 4, "deep": 2}`;
  - **для Задачи 15:** `bulk_ok` теперь ложен и у домена с ЧИСТОЙ историей (далёкая тема, EMD) — строка
    инбокса не вправе писать про такой «история не подтверждена».
  - **фикстуры «полностью проверенного» домена** в тестах пакета/инбокса несут `"deep_checked": True` в
    `score_breakdown` и непустой `score`; фейк «Ahrefs без полей» там, где домен должен попасть в пакет,
    дополнен методами W6:
    `"anchors": lambda self, d, limit=50: [{"anchor": d, "refdomains": 10, "is_spam": False}]`,
    `"metrics_history": lambda self, d, years=5, today=None: []`.

- [ ] **Шаг 1: Написать падающие тесты**

`backend/tests/test_link_signals.py`:

```python
"""W6: доля спам-анкоров (по refdomains) и пик трафика. Живой спам-профиль 2026-10-01."""
import json
import pathlib

from app.services.link_signals import peak_traffic, spam_anchor_ratio

FX = pathlib.Path(__file__).parent / "fixtures" / "v2"


def test_live_spam_profile_is_fully_spam():
    anchors = json.loads((FX / "ahrefs_anchors_spam.json").read_text())["anchors"]
    assert spam_anchor_ratio(anchors, "pharmaindustrie.com") == 1.0


def test_ratio_weighted_by_refdomains_word_boundaries_and_scripts():
    anchors = [{"anchor": "Seoul travel guide", "refdomains": 60, "is_spam": False},   # «seo» в «Seoul» — не спам
               {"anchor": "best casino bonus", "refdomains": 20, "is_spam": False},
               {"anchor": "официальный сайт", "refdomains": 20, "is_spam": False}]  # кириллица на латинском домене
    assert spam_anchor_ratio(anchors, "travel.com") == 0.4
    assert spam_anchor_ratio(anchors[2:], "xn--80ak6aa92e.com") == 0.0          # IDN-домен: кириллица — норма
    assert spam_anchor_ratio([], "a.com") is None
    assert spam_anchor_ratio([{"anchor": "x", "refdomains": 0}], "a.com") is None


def test_plural_stop_words():
    """4.3: множественное число и производные — тоже спам."""
    for text in ("online casinos list", "porno videos", "free slots", "payday loans"):
        assert spam_anchor_ratio([{"anchor": text, "refdomains": 5}], "a.com") == 1.0, text


def test_script_of_the_past_site_language_is_not_spam():
    """Р1: японский анкор на .com — норма для японского прошлого сайта и спам для испанского.
    Без языка прошлого сайта нелатинский скрипт на латинском домене — спам, как и было."""
    jp = [{"anchor": "東京のブログ", "refdomains": 30, "is_spam": False}]
    assert spam_anchor_ratio(jp, "tokyoblog.com", "ja") == 0.0
    assert spam_anchor_ratio(jp, "tokyoblog.com", "es") == 1.0
    assert spam_anchor_ratio(jp, "tokyoblog.com") == 1.0
    ru = [{"anchor": "официальный сайт", "refdomains": 10}]
    assert spam_anchor_ratio(ru, "site.com", "uk") == 0.0 and spam_anchor_ratio(ru, "site.com", "ja") == 1.0
    kr = [{"anchor": "서울 여행", "refdomains": 10}]
    assert spam_anchor_ratio(kr, "seoultrip.com", "ko") == 0.0
    assert spam_anchor_ratio(kr, "seoultrip.com", "zh") == 1.0
    assert spam_anchor_ratio([{"anchor": "casino 東京", "refdomains": 5}], "a.com", "ja") == 1.0   # стоп-слово — всегда спам
    # R2-2: scripts=False — доля без правила скрипта (язык прошлого сайта неизвестен)
    assert spam_anchor_ratio(jp, "tokyoblog.com", scripts=False) == 0.0
    assert spam_anchor_ratio([{"anchor": "casino 東京", "refdomains": 5}], "a.com", scripts=False) == 1.0


def test_peak_traffic():
    hist = json.loads((FX / "ahrefs_metrics_history.json").read_text())["metrics"]
    assert peak_traffic(hist) == 1850 and peak_traffic([]) is None
```

В шапке `test_waves_v2.py` перед строкой `from datetime import datetime, timedelta, timezone` добавить
`import json` и `import pathlib`; в конец файла дописать:

```python
# --- W6 «анкоры», правила пакета, EMD (Задача 13) ---------------------------------------------

FX = pathlib.Path(__file__).parent / "fixtures" / "v2"
SPAM = json.loads((FX / "ahrefs_anchors_spam.json").read_text())["anchors"]
CLEAN = [{"anchor": "goodvpnblog.com", "refdomains": 80, "is_spam": False},
         {"anchor": "VPN speed test results", "refdomains": 40, "is_spam": False}]
HIST = json.loads((FX / "ahrefs_metrics_history.json").read_text())["metrics"]
STRONG = {**ROW, "domain_rating": 35.0, "refdomains": 900, "refips_subnets": 700}


def _strong(s):
    s.sig.update({"wayback_checked": True, "age_years": 10.0, "dr": 35.0, "referring_domains": 900,
                  "ref_subnets": 700, "topical_relevance": 0.9})
    return s


def test_deep_spam_rejects_and_records():
    s = _strong(_state("a.com"))
    scoring._wave_deep([s], {"ahrefs": FakeAh(anchors=SPAM, history=HIST)}, _st(), None, None)
    assert s.reject_reason == "spam_anchors" and s.sig["deep_checked"] is True
    assert s.sig["spam_anchors"] is True and s.sig["spam_anchor_ratio"] == 1.0
    assert len(s.sig["anchors"]) <= 10


def test_deep_clean_keeps_and_sets_peak():
    s = _strong(_state("a.com"))
    scoring._wave_deep([s], {"ahrefs": FakeAh(anchors=CLEAN, history=HIST)}, _st(), None, None)
    assert s.alive and s.sig["deep_checked"] is True and s.sig["peak_traffic"] == 1850
    assert s.sig["spam_anchors"] is False


def test_deep_only_for_promising_best_first_within_cap():
    weak = _state("weak.com")                       # пустые сигналы -> предварительный скор < manual_review_at
    good, better = _strong(_state("good.com")), _strong(_state("better.com"))
    good.sig["dr"] = 10.0                           # DR зажат на 30 — разница должна быть НИЖЕ потолка
    emd = _strong(_state("emd.com", source="emd", lane="free"))
    ah = FakeAh(anchors=CLEAN, history=HIST)
    scoring._wave_deep([weak, good, better, emd], {"ahrefs": ah}, _st(), scoring.Budget(1), None)
    assert better.sig["deep_checked"] is True                 # лучший предварительный — первым
    assert good.sig["deep_checked"] is False and weak.sig["deep_checked"] is False
    assert "deep_checked" not in emd.sig and ah.deep_calls == 1


def test_deep_prescore_uses_runtime_threshold():
    """2.8: кандидатов W6 отбирает рантайм-порог manual_review_at из /settings, а не статичный."""
    s, ah = _strong(_state("a.com")), FakeAh(anchors=CLEAN, history=HIST)
    scoring._wave_deep([s], {"ahrefs": ah}, _st(manual_review_at=0.99), None, None)
    assert ah.deep_calls == 0 and s.sig["deep_checked"] is False
    weak, ah2 = _state("weak.com"), FakeAh(anchors=CLEAN)
    weak.sig["wayback_checked"] = True
    scoring._wave_deep([weak], {"ahrefs": ah2}, _st(manual_review_at=0.0), None, None)
    assert ah2.deep_calls == 1


def test_deep_skips_domain_without_checked_history():
    """W6 не тратит units на домен с непроверенной историей: в пакет он и так не попадёт
    («вслепую»), а человек сначала разберётся с историей (решение координатора 2026-10-02)."""
    s, ah = _strong(_state("a.com")), FakeAh(anchors=CLEAN, history=HIST)
    s.sig["wayback_checked"] = False
    scoring._wave_deep([s], {"ahrefs": ah}, _st(), None, None)
    assert ah.deep_calls == 0 and ah.units_calls == 0 and s.sig["deep_checked"] is False


def test_deep_error_leaves_unchecked():
    s = _strong(_state("a.com"))
    scoring._wave_deep([s], {"ahrefs": FakeAh(boom=True)}, _st(), None, None)
    assert s.alive and s.sig["deep_checked"] is False and "deep:RuntimeError" in s.sig["errors"]


def test_deep_empty_anchors_with_donors_is_not_checked():
    """1.3: доноры есть (RD 900), а анкоров нет — это не «чисто», а «не проверено». Нет доноров
    (RD 0) — проверять нечего, и это честное «проверено»."""
    s = _strong(_state("a.com"))
    scoring._wave_deep([s], {"ahrefs": FakeAh(anchors=[], history=HIST)}, _st(), None, None)
    assert s.alive and s.sig["deep_checked"] is False and "deep:empty" in s.sig["errors"]
    none = _strong(_state("nodonors.com"))
    none.sig["referring_domains"] = 0
    scoring._wave_deep([none], {"ahrefs": FakeAh(anchors=[], history=HIST)}, _st(), None, None)
    assert none.sig["deep_checked"] is True and none.sig["spam_anchor_ratio"] is None


def test_deep_history_failure_keeps_paid_anchor_verdict():
    """3.7: анкоры и история трафика — отдельные вызовы: сбой истории не выбрасывает оплаченный
    вердикт по анкорам и «вслепую» домен не делает."""
    s = _strong(_state("a.com"))
    scoring._wave_deep([s], {"ahrefs": FakeAh(anchors=CLEAN, history_boom=True)}, _st(), None, None)
    assert s.sig["deep_checked"] is True and s.sig["spam_anchor_ratio"] == 0.0
    assert "deep_history:RuntimeError" in s.sig["errors"] and "peak_traffic" not in s.sig


def test_deep_judges_anchor_script_by_past_site_language():
    """Р1: прошлый сайт японский — японские анкоры на .com не спам."""
    jp = [{"anchor": "東京のブログ", "refdomains": 50, "is_spam": False}]
    s = _strong(_state("tokyoblog.com"))
    s.sig["market_lang"] = "ja"
    scoring._wave_deep([s], {"ahrefs": FakeAh(anchors=jp)}, _st(), None, None)
    assert s.alive and s.sig["spam_anchor_ratio"] == 0.0


def test_deep_unknown_language_alone_is_not_spam_anchors():
    """R2-2: язык прошлого сайта неизвестен (LLM упал этим прогоном, в базе пусто) — отказ, который
    держится ТОЛЬКО на правиле скрипта, не выносится: `spam_anchors` — вечная грязь, кнопкой не
    вернуть, а сбой LLM не отклоняет. Домен — «анкоры не проверены» (вне пакета, но не грязь).
    Флаг Ahrefs `is_spam` и стоп-слова отклоняют и без языка."""
    jp = [{"anchor": "東京のブログ", "refdomains": 50, "is_spam": False}]
    s = _strong(_state("tokyoblog.com"))
    scoring._wave_deep([s], {"ahrefs": FakeAh(anchors=jp, history=HIST)}, _st(), None, None)
    assert s.alive and s.reject_reason is None and s.sig["deep_checked"] is False
    assert "deep:lang_unknown" in s.sig["errors"] and "spam_anchors" not in s.sig
    for extra in ({"anchor": "best casino bonus", "refdomains": 50, "is_spam": False},
                  {"anchor": "東京", "refdomains": 50, "is_spam": True}):
        bad = _strong(_state("tokyospam.com"))
        scoring._wave_deep([bad], {"ahrefs": FakeAh(anchors=jp + [extra], history=HIST)}, _st(), None, None)
        assert bad.reject_reason == "spam_anchors", extra


def test_llm_down_japanese_anchors_are_not_dirt():
    """R2-2 сквозной: LLM лежит, язык прошлого сайта неизвестен — японские анкоры не делают домен
    грязным (раньше: `spam_anchors` навсегда, а на перескоре — пятно на ранее чистом домене)."""
    from app.services import transitions
    did = _mk("tokyo-down.com", deadline=NOW + timedelta(days=2))
    jp = [{"anchor": "東京のブログ", "refdomains": 50, "is_spam": False}]
    out = scoring.score_domain(did, clients=_full_clients(
        FakeRdap(exists=True, registered=NOW - timedelta(days=4000)),
        FakeAh({"tokyo-down.com": STRONG}, anchors=jp, history=HIST), FakeWB(), llm=FakeLLM(boom=True)))
    assert out["status"] == "scored" and out["reject_reason"] is None
    assert "deep:lang_unknown" in out["errors"]
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
        assert transitions.dirty_reason(d) is None and d.score_breakdown["deep_checked"] is False
        assert scoring.bulk_ok(d) is False                  # «анкоры НЕ проверены» — только руками


def test_llm_down_uses_market_lang_from_previous_run(monkeypatch):
    """R2-2: LLM этого прогона лежит, но язык прошлого сайта известен с прошлого прогона (в БД) —
    японские анкоры японского прошлого сайта не спам, анкоры проверены. Оба входа воронки:
    перепроверка одного домена (score_domain) и пакетный прогон (score_pending)."""
    jp = [{"anchor": "東京のブログ", "refdomains": 50, "is_spam": False}]
    names = ("tokyo-one.com", "tokyo-batch.com")
    ids = [_mk(n, deadline=NOW + timedelta(days=2), market_lang="ja") for n in names]
    clients = _full_clients(FakeRdap(exists=True, registered=NOW - timedelta(days=4000)),
                            FakeAh({n: STRONG for n in names}, anchors=jp, history=HIST), FakeWB(),
                            llm=FakeLLM(boom=True))
    scoring.score_domain(ids[0], clients=clients)
    monkeypatch.setattr(scoring, "_make_clients", lambda: clients)
    scoring.score_pending(limit=10)
    with db.SessionLocal() as s:
        for did in ids:
            d = s.get(Domain, did)
            assert d.status == "scored" and d.score_breakdown["deep_checked"] is True, d.domain
            assert float(d.spam_anchor_ratio) == 0.0 and d.market_lang == "ja", d.domain


def test_units_floor_skips_deep_and_says_why():
    """Р3: остаток units ниже пола перед W6 — анкоры не проверяются (домен вне пакета), причина —
    в пояснениях водопада (а через них — в сообщении задачи, см. тест W4)."""
    s, ah, notes = _strong(_state("a.com")), FakeAh(anchors=CLEAN, units=100_000), []
    scoring._wave_deep([s], {"ahrefs": ah}, _st(), None, None, notes)
    assert ah.deep_calls == 0 and s.alive and s.sig["deep_checked"] is False
    assert notes == ["Ahrefs: остаток 100 000 < пола 300 000 — платные волны пропущены"]


def test_score_pending_takes_deep_cap_from_settings(monkeypatch):
    """2.9: кап W6 — из /settings. 0 = W6 выключен: ни одного платного вызова анкоров, домен —
    «анкоры не проверены»."""
    from app.services.settings import update_settings
    update_settings(max_deep_per_run=0)
    did = _mk("nodeep.com", deadline=NOW + timedelta(days=2))
    ah = FakeAh({"nodeep.com": STRONG}, anchors=CLEAN, history=HIST)
    clients = _full_clients(FakeRdap(exists=True, registered=NOW - timedelta(days=4000)), ah, FakeWB(),
                            llm=FakeLLM())
    monkeypatch.setattr(scoring, "_make_clients", lambda: clients)
    scoring.score_pending(limit=10)
    assert ah.deep_calls == 0
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
        assert d.status == "scored" and d.score_breakdown["deep_checked"] is False


def test_blind_reason_names_unchecked_anchors():
    """Р2: гард «без проверенных анкоров не одобрять» переехал из _decide в пакет. Домен,
    оценённый до W6 (ключа нет), — тоже «не проверены». У EMD ссылок нет — проверять нечего."""
    base = dict(wayback_checked=True, prior_flags={}, age_years=9.0)
    d = Domain(domain="anchors.com", score=0.8, score_breakdown={"errors": [], "deep_checked": False}, **base)
    assert "анкоры" in scoring.blind_reason(d) and scoring.bulk_ok(d) is False
    legacy = Domain(domain="v1.com", score=0.8, score_breakdown={"errors": []}, **base)
    assert "анкоры" in scoring.blind_reason(legacy)
    emd = Domain(domain="emd.com", score_breakdown={"errors": [], "emd": True}, **base)
    assert scoring.blind_reason(emd) is None


def test_far_past_topic_and_emd_stay_out_of_bulk():
    """Р2 + инвариант 4: прошлая тема далека от VPN (< 0.3) — такой домен человек одобряет только
    руками, глядя на тему; «тема не определена» (None) пакет не закрывает. EMD (балла нет) — тоже
    только руками."""
    base = dict(wayback_checked=True, prior_flags={}, age_years=9.0, score=0.8,
                score_breakdown={"errors": [], "deep_checked": True})
    assert scoring.bulk_ok(Domain(domain="near.com", topical_relevance=0.6, **base)) is True
    assert scoring.bulk_ok(Domain(domain="unknown.com", topical_relevance=None, **base)) is True
    far = Domain(domain="far.com", topical_relevance=0.1, **base)
    assert scoring.topic_far(far) is True and scoring.bulk_ok(far) is False
    emd = Domain(domain="emd.com", **{**base, "score": None, "score_breakdown": {"errors": [], "emd": True}})
    assert scoring.bulk_ok(emd) is False


def test_e2e_live_spam_drop_is_rejected_for_spam_anchors():
    did = _mk("pharmaindustrie.com", deadline=NOW + timedelta(days=2))
    out = scoring.score_domain(did, clients=_full_clients(
        FakeRdap(exists=True, registered=datetime(1998, 7, 29, tzinfo=timezone.utc)),
        FakeAh({"pharmaindustrie.com": ROW}, anchors=SPAM, history=HIST), FakeWB(), llm=FakeLLM()))
    assert out["status"] == "rejected" and out["reject_reason"] == "spam_anchors"


def test_e2e_clean_strong_domain_is_scored_and_lands_in_bulk():
    """Р2: машина ставит максимум `scored`; чистый сильный, полностью проверенный домен попадает в
    пакетное одобрение — одобряет его человек."""
    from app.api import panel
    did = _mk("goodvpnblog.com", source="list", lane=None)
    out = scoring.score_domain(did, clients=_full_clients(
        FakeRdap(exists=False),
        FakeAh({"goodvpnblog.com": STRONG}, anchors=CLEAN, history=[{"date": "2023-01-01", "org_traffic": 4000}]),
        FakeWB(age=10.0), llm=FakeLLM(answer='{"snapshots":[],"topic_summary":"vpn","vpn_adjacent":0.9}')))
    assert out["status"] == "scored" and out["reject_reason"] is None
    with db.SessionLocal() as s:
        ok, skipped = panel._bulk_candidates(s, 0.0)
        assert [d.domain for d in ok] == ["goodvpnblog.com"] and skipped == 0


def test_e2e_emd_is_scored_without_score_and_never_in_bulk():
    """EMD — `scored` без балла («решение за тобой»): ни W4, ни W6 не тратятся, пакет его не берёт
    даже при чистой истории (1.13)."""
    from app.api import panel
    did = _mk("mejorvpn.com", source="emd", lane="free", market_lang="es")
    ah = FakeAh({})
    out = scoring.score_domain(did, clients=_full_clients(FakeRdap(exists=False), ah, FakeWB(), llm=FakeLLM()))
    assert out["status"] == "scored" and out["score"] is None
    assert ah.batches == [] and ah.deep_calls == 0
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
        assert d.score is None and d.score_breakdown.get("emd") is True and d.market_lang == "es"
        assert scoring.bulk_ok(d) is False and panel._bulk_candidates(s, 0.0) == ([], 0)


def test_rescore_with_ahrefs_down_does_not_launder_spam_anchors():
    """1.4: отказ `spam_anchors` -> перескор, на котором Ahrefs упал (W6 не дошла) -> улика в
    score_breakdown цела, домен по-прежнему грязный и в оборот кнопкой не возвращается."""
    import pytest
    from app.services import transitions
    did = _mk("spammy.com", deadline=NOW + timedelta(days=2))
    clients = _full_clients(FakeRdap(exists=True, registered=NOW - timedelta(days=4000)),
                            FakeAh({"spammy.com": STRONG}, anchors=SPAM, history=HIST), FakeWB(), llm=FakeLLM())
    assert scoring.score_domain(did, clients=clients)["reject_reason"] == "spam_anchors"
    class _AnchorsDown(FakeAh):
        def anchors(self, d, limit=50):
            raise RuntimeError("ahrefs down")
    out = scoring.score_domain(did, clients={**clients, "ahrefs": _AnchorsDown({"spammy.com": STRONG})})
    assert out["reject_reason"] is None and "deep:RuntimeError" in out["errors"]
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
        assert d.score_breakdown["spam_anchors"] is True               # улику не стёрли
        assert transitions.dirty_reason(d) == "spam_anchors"
        with pytest.raises(transitions.TransitionDenied):
            transitions.check(d, "approved")


def test_rescore_with_llm_down_keeps_far_topic_out_of_bulk():
    """Сбой LLM на перескоре не снимает исключение «прошлая тема далека от VPN»: близость 0.1 из
    прошлого прогона сохранена, и полностью проверенный в остальном домен в пакет не попадает."""
    from app.api import panel
    did = _mk("far-topic.com", deadline=NOW + timedelta(days=2), topic="casino reviews",
              topical_relevance=0.1)
    out = scoring.score_domain(did, clients=_full_clients(
        FakeRdap(exists=True, registered=NOW - timedelta(days=4000)),
        FakeAh({"far-topic.com": STRONG}, anchors=CLEAN, history=HIST), FakeWB(),
        llm=FakeLLM(boom=True)))
    assert out["status"] == "scored"
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
        assert float(d.topical_relevance) == 0.1 and d.topic is None
        assert scoring.blind_reason(d) is None and scoring.topic_far(d) is True
        assert panel._bulk_candidates(s, 0.0) == ([], 1)


class CountingWB(FakeWB):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.calls = 0

    def classify_history(self, d):
        self.calls += 1
        return super().classify_history(d)


def test_e2e_early_exit_spends_nothing_expensive():
    """Спека §8: домен, отсеянный на W0–W3, не тратит ни units Ahrefs, ни запросов к archive.org.
    Бесплатный запрос остатка units — один раз в начале прогона, у переживших W0 (R2-10)."""
    for name, rdap, wr in (("nordvpn-deals.com", FakeRdap(exists=False), FakeWR()),            # W0 бренд
                           ("taken-free.com", FakeRdap(exists=True, registered=NOW), FakeWR()),  # W2 занят
                           ("malware.com", FakeRdap(exists=False), FakeWR(threats=["MALWARE"]))):  # W3 риск
        did = _mk(name, "mx" if name == "taken-free.com" else "list", "free" if name == "taken-free.com" else None)
        ah, wb = FakeAh({name: ROW}), CountingWB()
        out = scoring.score_domain(did, clients={**_full_clients(rdap, ah, wb), "webrisk": wr})
        assert out["status"] == "rejected", name
        assert ah.batches == [] and ah.deep_calls == 0 and wb.calls == 0, name
        assert ah.units_calls == (0 if name == "nordvpn-deals.com" else 1), name
```

- [ ] **Шаг 2: Запустить — убедиться, что падает**

Запуск: `cd backend && ../.venv/bin/python -m pytest tests/test_link_signals.py -v`
Ожидание: ошибка сбора — `ModuleNotFoundError: No module named 'app.services.link_signals'`.

Запуск: `cd backend && ../.venv/bin/python -m pytest tests/test_waves_v2.py -q`
Ожидание: `20 failed, 48 passed`. Падают 20 новых тестов (`AttributeError: … '_wave_deep'`, нет `topic_far`,
EMD скорится как обычный домен, нет грязи `spam_anchors`, нет `deep:lang_unknown` и `deep_checked`). Из новых
проходят два регрессионных стража, верные и до W6: `test_e2e_clean_strong_domain_is_scored_and_lands_in_bulk`
и `test_e2e_early_exit_spends_nothing_expensive`.

- [ ] **Шаг 3: Реализовать**

`backend/app/services/link_signals.py`:

```python
"""W6: сигналы ссылочного профиля финалистов — доля спам-анкоров и пик органического трафика.

Живой факт 2026-10-01: почти каждый дропающийся .com засыпан автоматическим SEO-спамом («Expert SEO
Links and Backlinks for <домен>…», is_spam=true у 10 из 10 анкоров). Доля считается ПО REFDOMAINS,
а не по числу строк: один анкор с 300 донорами весит больше десяти единичных.
Штрафа «обвал трафика» НЕТ намеренно: у expired-домена трафик падает ВСЕГДА (сайт умер) — без
сверки с датой последнего живого снимка это неотличимо от санкций и било бы по каждому ценному дропу.
"""
import re

_STOP = re.compile(r"\b(casinos?|slots?|judi|togel|viagra|cialis|replica|loans?|porn\w*|essay|payday|"
                   r"backlinks?|guest\s*posts?|seo)\b", re.I)
# Нелатинские скрипты: на ЛАТИНСКОМ домене это чужой язык спам-волны — кроме скрипта языка
# прошлого сайта (решение оператора Р1).
_SCRIPTS = {"cyr": re.compile(r"[Ѐ-ӿ]"), "thai": re.compile(r"[฀-๿]"),
            "kana": re.compile(r"[぀-ヿ]"), "cjk": re.compile(r"[一-鿿]"),
            "hangul": re.compile(r"[가-힯]")}
_LANG_SCRIPTS = {"ja": {"kana", "cjk"}, "zh": {"cjk"}, "ko": {"hangul"}, "th": {"thai"},
                 **{lg: {"cyr"} for lg in ("ru", "uk", "bg", "sr", "kk", "be", "mk")}}


def _foreign_script(text: str, allowed: set) -> bool:
    return any(rx.search(text) for name, rx in _SCRIPTS.items() if name not in allowed)


def spam_anchor_ratio(anchors, domain: str, market_lang: str | None = None,
                      scripts: bool = True) -> float | None:
    """Доля refdomains у анкоров со спам-признаком. None — данных нет (0 доноров).

    Нелатинский скрипт на латинском домене — признак спам-волны, КРОМЕ скрипта языка прошлого сайта
    (`market_lang` из W5, Р1): японский блог на .com иначе отклонялся бы навсегда. IDN-домен (xn--)
    скрипты не судит вовсе. `scripts=False` — доля без правила скрипта: когда язык прошлого сайта
    неизвестен, W6 смотрит, не держится ли отказ на одном этом правиле (находка R2-2). Стоп-слово и
    флаг Ahrefs `is_spam` — спам при любом языке."""
    latin = scripts and not any(lbl.startswith("xn--") for lbl in domain.split("."))
    allowed = _LANG_SCRIPTS.get((market_lang or "").lower(), set())
    total = spam = 0
    for a in anchors or ():
        rd = int(a.get("refdomains") or 0)
        text = str(a.get("anchor") or "")
        bad = (bool(a.get("is_spam")) or bool(_STOP.search(text))
               or (latin and _foreign_script(text, allowed)))
        total += rd
        spam += rd if bad else 0
    return None if total == 0 else round(spam / total, 4)


def peak_traffic(history) -> int | None:
    vals = [int(m.get("org_traffic") or 0) for m in history or ()]
    return max(vals) if vals else None
```

**`scoring_config.py`:** перед строкой `UNITS_FLOOR = 300_000 …` добавить

```python
TOPIC_FAR_BELOW = 0.3        # близость прошлой темы к VPN ниже — пакет не берёт (инвариант 4, Р2)
```

**`scoring.py`:**

1) Докстринг модуля: строки `Order: … -> links (Ahrefs` / `batch) -> history (Wayback) -> composite score + breakdown -> status scored | rejected (`approved` ставит только человек).`
заменить на:

```
Order: t0 (зоны/бренды) -> avail (RDAP/whois) -> risk (Web Risk, Spamhaus с DQS) -> links (Ahrefs
batch) -> history (Wayback + тема) -> deep (анкоры финалистов) -> composite score + breakdown ->
status scored | rejected (`approved` ставит только человек).
```

2) `FUNNEL_STAGES`: последним элементом добавить `{"key": "deep", "label": "анкоры (Ahrefs)"},`.

3) `blind_reason`: финальный `return None` (после проверки «возраст НЕ проверен») заменить блоком ниже — он
сам заканчивается `return None`, а после функции добавляет новую `topic_far`:

```python
    # Анкоры финалистов (W6) не проверены: кап W6, пол остатка units, Ahrefs не ответил, пустой
    # ответ при живых донорах — или домен оценён до W6 (ключа нет). На дропах RD раздут спамом:
    # без анкоров одобрять пакетом нельзя (Р2 — гард переехал сюда из _decide). У EMD ссылок нет.
    bd = d.score_breakdown or {}
    if not bd.get("emd") and bd.get("deep_checked") is not True:
        return ("анкоры НЕ проверены: кап W6, пол units, Ahrefs не ответил или язык прошлого сайта "
                "неизвестен — ссылочный профиль может быть спамом")
    return None


def topic_far(d) -> bool:
    """Прошлая тема далека от VPN (инвариант 4; политика Google «expired domain abuse» — это ровно
    смена темы). Такой домен человек одобряет только руками, глядя на тему: пакет его не берёт.
    None («тема не определена») не исключает: незнание — не улика (спека §3.1)."""
    tr = d.topical_relevance
    return tr is not None and float(tr) < cfg.TOPIC_FAR_BELOW
```

(между `return None` и `def topic_far` — две пустые строки, как между функциями файла.)

4) `bulk_ok`: последний абзац докстринга (от `` `dirty_reason` (аудит F9) добавлен СЮДА`` до закрывающих
кавычек) и тело заменить на:

```python
    `dirty_reason` (аудит F9) добавлен СЮДА, а не рядом с пакетом, по тому же правилу: новое
    основание «нельзя» обязано пройти через единый предикат, иначе строка инбокса подписала бы
    «история чистая» домен, который пакет молча пропускает. `history_verdict` ловит грязь ТОЛЬКО
    по `prior_flags`; РКН и блэклист — это отдельные колонки, и до сих пор их здесь не видел никто.

    v2 (Р2 — авто-одобрения нет): всё, что раньше держали гарды `_decide`, держит этот предикат.
    Плюс два основания «только руками»: прошлая тема далека от VPN (`topic_far`) и пустой балл
    (EMD — решение за человеком; SQL пакета `score >= x` его тоже не берёт).
    """
    from app.services.transitions import dirty_reason   # ленивый: transitions зовёт нас в ответ
    return (history_verdict(d) == "clean" and not blind_reason(d) and not dirty_reason(d)
            and not topic_far(d) and d.score is not None)
```

5) `score_domain`: в сигнатуру после `run: int | None = None` добавить `, deep_budget=None`; вызов →
`results = _run_waves([state], c, st, whois_budget, links_budget, run, deep_budget=deep_budget)`.
Язык прошлого сайта из БД (находка R2-2) — в `FunnelState` (в `score_domain`):

НАЙТИ:
```
                            acquire_deadline=d.acquire_deadline, feed_flags=d.feed_flags, source=d.source)
```
ЗАМЕНИТЬ НА:
```
                            acquire_deadline=d.acquire_deadline, feed_flags=d.feed_flags, source=d.source,
                            market_lang=d.market_lang)
```

и в класс `FunnelState` последним полем (после `source: str | None = None …`):

```python
    market_lang: str | None = None  # язык прошлого сайта из БД — запасной для W6, если LLM молчит (R2-2)
```

6) `score_pending`: после `links_budget = Budget(int(st["max_links_per_run"]))` добавить
`deep_budget = Budget(int(st["max_deep_per_run"]))       # 0 = W6 выключен: анкоры не проверены`; в вызов
`_run_waves(...)` дописать `deep_budget=deep_budget` (после `notes=notes`). Язык из БД (R2-2) — в выборке:

НАЙТИ:
```
        q = (select(Domain.id, Domain.domain, Domain.lane, Domain.referring_domains,
                    Domain.acquire_deadline, Domain.feed_flags, Domain.source)
```
ЗАМЕНИТЬ НА:
```
        q = (select(Domain.id, Domain.domain, Domain.lane, Domain.referring_domains,
                    Domain.acquire_deadline, Domain.feed_flags, Domain.source, Domain.market_lang)
```

НАЙТИ:
```
                          feed_flags=flags, source=src)
             for (did, name, lane, rd, deadline, flags, src) in rows]
```
ЗАМЕНИТЬ НА:
```
                          feed_flags=flags, source=src, market_lang=lang)
             for (did, name, lane, rd, deadline, flags, src, lang) in rows]
```

7) Две строки — `# W4 «ссылки» здесь нет: …` и `_CONCURRENCY = {"avail": 12, "risk": 12, "history": 4}` —
заменить на три (строку `# history=4 — …` над ними не трогать):

```python
# W4 «ссылки» здесь нет: она идёт пачками ПОСЛЕДОВАТЕЛЬНО (лимит Ahrefs 60 запросов/мин).
# deep=2 — W6 стоит ~1,1 тыс. units на домен: не спешим.
_CONCURRENCY = {"avail": 12, "risk": 12, "history": 4, "deep": 2}
```

8) Перед `def _commit_result` вставить:

```python
def _deep_one(s: FunnelState, clients: dict, st: dict) -> None:
    """W6 для ОДНОГО финалиста: анкоры, затем история трафика — ОТДЕЛЬНЫМИ вызовами (находка 3.7):
    сбой истории не выбрасывает уже оплаченный вердикт по анкорам.

    `deep_checked=True` — только если анкоры реально получены (находка 1.3): пустой список при
    живых донорах — это не «чисто», а «не проверено» (`deep:empty`). Сбой — `deep:<Исключение>`.
    Язык прошлого сайта неизвестен, а отказ держался бы только на правиле скрипта — `deep:lang_unknown`
    (находка R2-2): сбой LLM не отклоняет. Во всех трёх случаях домен идёт в решение, но вне пакета
    («анкоры НЕ проверены»).
    `spam_anchors` — улика для transitions.dirty_reason (находка 1.4): _commit_result хранит её
    через _kept, и перескор, на котором W6 не дошла, спам-домен не отмывает."""
    from app.services import link_signals
    try:
        anchors = clients["ahrefs"].anchors(s.domain)
    except Exception as e:  # noqa: BLE001
        s.sig["errors"].append(f"deep:{type(e).__name__}")
        return
    lang = s.sig.get("market_lang") or s.market_lang   # W5 этого прогона, иначе прошлый прогон (R2-2)
    ratio = link_signals.spam_anchor_ratio(anchors, s.domain, lang)
    if ratio is None and s.sig.get("referring_domains"):
        s.sig["errors"].append("deep:empty")        # доноры есть, а анкоров нет — не «чисто»
        return
    if (not lang and ratio is not None and ratio > st["spam_anchor_max"]
            and (link_signals.spam_anchor_ratio(anchors, s.domain, scripts=False) or 0.0)
            <= st["spam_anchor_max"]):
        # Язык прошлого сайта неизвестен, а отказ держится ТОЛЬКО на правиле скрипта: японский блог
        # на .com при упавшем LLM ушёл бы в вечную грязь. Не отказ, а «анкоры не проверены».
        s.sig["errors"].append("deep:lang_unknown")
        return
    s.sig["anchors"] = [{"anchor": str(a.get("anchor") or "")[:200], "refdomains": a.get("refdomains"),
                         "is_spam": bool(a.get("is_spam"))} for a in anchors[:10]]
    s.sig["spam_anchor_ratio"] = ratio
    s.sig["spam_anchors"] = ratio is not None and ratio > st["spam_anchor_max"]
    s.sig["deep_checked"] = True
    if s.sig["spam_anchors"]:
        s.reject_reason, s.alive = "spam_anchors", False
        return                                      # спам-дроп: историю трафика не покупаем
    try:
        s.sig["peak_traffic"] = link_signals.peak_traffic(clients["ahrefs"].metrics_history(s.domain))
    except Exception as e:  # noqa: BLE001 — вердикт по анкорам уже есть и остаётся
        s.sig["errors"].append(f"deep_history:{type(e).__name__}")


def _wave_deep(states: list, clients: dict, st: dict, budget, run, notes: list | None = None) -> None:
    """W6 — дорогая проверка (~1,1 тыс. units) ТОЛЬКО для тех, кто уже набрал предварительный скор
    ≥ manual_review_at — РАНТАЙМ-порог из /settings (находка 2.8) — и только с проверенной историей
    (`wayback_checked`): на заведомо слабый домен и на домен «вслепую», которого пакет всё равно не
    возьмёт, units не тратим. Лучшие первыми — кап `max_deep_per_run` уходит на тех, кого реально решать. Перед
    волной — пол остатка units (Р3): ниже пола W6 не идёт, анкоры «не проверены». EMD пропускает
    (у новорега нет ссылок)."""
    cands = []
    for s in states:
        if not s.alive or s.source == "emd":
            continue
        s.sig["deep_checked"] = False
        if not s.sig.get("wayback_checked"):
            continue        # история не проверена — домен и так вне пакета, units на него не тратим
        pre = compute_score(dict(s.sig), st.get("weights"))
        if "hard_reject" not in pre["breakdown"] and pre["score"] >= st["manual_review_at"]:
            cands.append((pre["score"], s))
    if not cands:
        return
    low = _units_below_floor(clients, st)
    if low:
        if notes is not None:
            notes.append(low)
        return
    picked = []
    for _, s in sorted(cands, key=lambda x: -x[0]):
        if budget is not None and not budget.take():
            break
        picked.append(s)
    _run_concurrent(picked, _CONCURRENCY["deep"], run, "deep", lambda s: _deep_one(s, clients, st))
```

9) `_commit_result`:
- ветка EMD — между `if reject: …` и `else:` (обычный скоринг):

```python
        elif state.source == "emd":
            # EMD — новорег: ни ссылок, ни трафика, скору не из чего складываться. Решение — за
            # человеком (спека §3.2): scored без балла, в инбоксе «EMD — решение за тобой», пакет
            # его не берёт (bulk_ok: балл пустой).
            result = {"score": None, "status": "scored", "breakdown": {"emd": True}}
```

- в кортеж колонок дописать `"anchors", "spam_anchor_ratio"` (последняя строка кортежа:
  `"market_lang", "topic", "topical_relevance", "anchors", "spam_anchor_ratio"):`);
- в `score_breakdown` последнюю строку `"parked_share": _kept("parked_share")}` заменить на:

```python
                             "parked_share": _kept("parked_share"),
                             "deep_checked": sig.get("deep_checked"),
                             "spam_anchors": _kept("spam_anchors"),
                             "peak_traffic": _kept("peak_traffic")}
```

  (`emd` попадает туда сам — через `**result["breakdown"]`; `d.clean = result["status"] != "rejected"` верно
  и для EMD; `DomainScoreLog.score` nullable — `None` принимает.)

10) `_run_waves`:
- сигнатура: после `notes: list | None = None` добавить `, deep_budget=None`;
- последнюю строку докстринга `    вызывающего (score_pending) — чтобы пояснение дожило и до итогового сообщения."""`
  заменить на:

```
    вызывающего (score_pending) — чтобы пояснение дожило и до итогового сообщения. `deep_budget` —
    кап W6 (None — без капа: ручная перепроверка одного домена)."""
```

- перед строкой `notes = [] if notes is None else notes` добавить:

```python
    deep_b = deep_budget if deep_budget is None or hasattr(deep_budget, "take") \
        else _ListBudget(deep_budget)
```

- в таблице `waves` последней строкой: `("deep", "анкоры", lambda alive: _wave_deep(alive, clients, st, deep_b, run, notes)),`.

`_decide` не трогать: он уже не одобряет (Задача 8), гарда `deep_checked` там нет и не будет.

**`transitions.py`:**
- комментарий и `DIRTY_REASONS` (от строки `# `not_acquirable` здесь НЕТ намеренно…` до конца `frozenset`) →

```python
# v2 (W0/W6): `trademark` — чужой VPN-бренд в имени (юридический риск), `spam_anchors` — ссылочный
# профиль засыпан спамом. Ни то ни другое не крутится порогом — до кассы никогда.
#
# `not_acquirable` здесь НЕТ намеренно: «домен занят» — это не грязь, а чужая покупка. Оператор,
# знающий, что домен всё-таки дропнулся, вправе вернуть его руками.
DIRTY_REASONS = frozenset({"rkn", "blacklist", "history_dirty", "feed_flag", "safebrowsing",
                           "legacy_ru", "tld_closed", "trademark", "spam_anchors"})
```

- в `dirty_reason` после проверки `webrisk_threats` (перед `if history_verdict(d) == "dirty":`):

```python
    # Спам-анкоры — улика W6 в score_breakdown (_kept): перескор, на котором Ahrefs упал или W6 не
    # дошла (кап, пол units), её не стирает (находка 1.4).
    if (d.score_breakdown or {}).get("spam_anchors") is True:
        return "spam_anchors"
```

- [ ] **Шаг 4: Запустить новые тесты**

Запуск: `cd backend && ../.venv/bin/python -m pytest tests/test_link_signals.py tests/test_waves_v2.py -q`
Ожидание: `73 passed` (5 + 68).

- [ ] **Шаг 5: Старые тесты — явный список**

Все правки — «переписать»: это тесты пакета, инбокса и гейта курации, удалять их нельзя. Новый смысл —
«полностью проверенный домен» теперь включает проверенные анкоры (Р2: гард переехал в пакет).

Фейк «Ahrefs без полей» (Задача 11) дополнить методами W6 (см. Interfaces) в двух помощниках — ровно этими
парами (Задача 16 ищет получившиеся строки дословно):

`backend/tests/test_funnel.py` (`_clients`; `_clients_whois_raises` не трогать — там `"wayback": wb`):

НАЙТИ:
```
            "wayback": wayback, "webrisk": type("WR", (), {"configured": True, "threats": lambda self, d: []})(),
            "ahrefs": type("Ah", (), {"units_left": lambda self: 2_000_000,
                                      "batch": lambda self, ds: {d: {} for d in ds}})()}
```
ЗАМЕНИТЬ НА:
```
            "wayback": wayback, "webrisk": type("WR", (), {"configured": True, "threats": lambda self, d: []})(),
            "ahrefs": type("Ah", (), {"units_left": lambda self: 2_000_000,
                                      "batch": lambda self, ds: {d: {} for d in ds},
                                      "anchors": lambda self, d, limit=50: [
                                          {"anchor": d, "refdomains": 10, "is_spam": False}],
                                      "metrics_history": lambda self, d, years=5, today=None: []})()}
```

`backend/tests/test_aparser_envelope.py` (`_clients`):

НАЙТИ:
```
            "wayback": wayback or _WaybackAged(), "webrisk": type("WR", (), {"configured": True, "threats": lambda self, d: []})(),
            "ahrefs": type("Ah", (), {"units_left": lambda self: 2_000_000,
                                      "batch": lambda self, ds: {d: {} for d in ds}})()}
```
ЗАМЕНИТЬ НА:
```
            "wayback": wayback or _WaybackAged(), "webrisk": type("WR", (), {"configured": True, "threats": lambda self, d: []})(),
            "ahrefs": type("Ah", (), {"units_left": lambda self: 2_000_000,
                                      "batch": lambda self, ds: {d: {} for d in ds},
                                      "anchors": lambda self, d, limit=50: [
                                          {"anchor": d, "refdomains": 10, "is_spam": False}],
                                      "metrics_history": lambda self, d, years=5, today=None: []})()}
```

Иначе W6 падает `deep:AttributeError`, и домены, которые эти тесты ждут в пакете
(`test_clean_strong_domain_is_scored_and_bulk_ok`, `test_funnel_whois_alive_domain_is_scored_and_bulk_ok`), ими
не являются. `test_history_verdict.py::_clients` W6-методы не нужны: его сквозной тест идёт с непроверенной
историей, а такой домен в W6 не попадает.

В `score_breakdown` фикстур «полностью проверенного» домена добавить `"deep_checked": True`:
- `test_aparser_envelope.py::_in_bulk` — в словарь `score_breakdown` перед `**breakdown`
  (тогда `test_known_age_is_scored_and_lands_in_bulk` снова в пакете, а два теста «whois упал» по-прежнему вне его);
- `test_history_verdict.py`: `test_verdict_clean_only_when_wayback_really_checked` (домен `ok.ru`),
  `test_unchecked_history_stays_out_of_bulk` (`ok.com`), `test_stale_verdict_is_named_but_not_locked`
  (`stale.ru`: `score_breakdown={"errors": ["wayback:RuntimeError"], "deep_checked": True}`);
- `test_inbox.py`: `test_bulk_approve_skips_blind_domains` (`clean.com`) и `test_bulk_preview_counts` (`clean.ru`);
- `test_job_stages.py::test_blind_reason_flags_unverified_history` (`clean` = `y.ru`).

Плюс непустой `score` там, где `Domain` строится без него и ждёт `bulk_ok is True`:
- `test_m1_fixes.py::test_webrisk_or_blacklist_error_caps_at_scored`, помощник `_dom` →
  `Domain(domain="i1.com", wayback_checked=True, prior_flags={}, age_years=8, score=0.8,`
  `score_breakdown={"errors": errors, "history_evidence": [], "deep_checked": True})`;
- `test_compute_score_v2.py::test_domain_without_any_age_is_blind_and_out_of_bulk`, словарь `kw` →
  `dict(domain="noage.com", wayback_checked=True, prior_flags={}, score=0.8,`
  `score_breakdown={"errors": [], "history_evidence": [], "deep_checked": True})`.

Шесть чипов (находка 5.13), `test_funnel_stages_and_job_message.py`:
- первая строка докстринга модуля → `"""FUNNEL_STAGES (6 волн v2: t0 → avail → risk → links → history → deep) и API-контракт, питающий волновую`;
- `test_task10_funnel_stages_has_5_not_6` → переименовать в `test_funnel_stages_are_the_six_v2_waves`, докстринг
  `"""v2: шесть чипов — по одному на волну, в порядке волн (эха больше нет)."""`, ассерты
  `len(scoring.FUNNEL_STAGES) == 6` и `keys == ["t0", "avail", "risk", "links", "history", "deep"]`
  (проверки `"echo" not in keys` и подписи риска — без изменений);
- `test_task10_job_card_shows_waterfall_for_running_score`: `assert len(score_job["stages"]) == 5  # …` →
  `assert len(score_job["stages"]) == 6  # шесть чипов волн v2`.

`test_job_stages.py::fake_run_waves` уже принимают `**kw` (Задача 11) — `deep_budget=` проходит.

- [ ] **Шаг 6: Сьют + линт**

Запуск: `cd backend && ../.venv/bin/python -m pytest tests -q && cd .. && .venv/bin/python -m pyflakes backend/app backend/tests`
Ожидание: **855 passed** (828 + 5 `test_link_signals.py` + 22 `test_waves_v2.py`), pyflakes пуст.

- [ ] **Шаг 7: Коммит**

```bash
git add -A backend/app backend/tests
git commit -m "feat(scoring): W6 анкоры финалистов, правила пакета (анкоры, тема, EMD), грязные trademark/spam_anchors"
```

---

### Задача 14: Панель — экран `/settings` v2

**Что меняется и почему.**
- Новые поля: `min_dr`, белый список зон, бренд-токены, наборы EMD (JSON), капы W4/W6, **пол остатка
  units** (`units_floor`, Р3), порог спам-анкоров. Все необязательные: поле, которого нет в форме, не
  затирает сохранённое — и `max_whois_per_run` тоже (`int | None = Form(None)`; раньше дефолт формы 200
  молча перезаписывал настройку, находка 6.1).
- **Остаток units — из кэша диагностики, без сети** (находка 3.4): синхронный `units_left()` на рендере
  (таймаут 60 с × 3 попытки) повесил бы страницу. Пинг Ahrefs в `/diag` (добавляется здесь) возвращает
  остаток числом, `diagnostics._run_one` кладёт число в результат проверки, `diag_cache.value("ahrefs")`
  отдаёт его экрану.
- **Пустая textarea — это «очистить»** (находка 6.1). FastAPI отдаёт пустое поле формы как «поля нет»
  (проверено: `str | None = Form(None)` получает `None` и для `""`). Поэтому форма v2 несёт скрытый маркер
  `v2_lists=1`: при нём пустые `tld_allowlist`/`brand_tokens`/`emd_sets` передаются как `""`. Наборы EMD
  очищаются в `[]`; пустые зоны и бренды `get_settings` читает как стартовый список (пустой белый список
  остановил бы всю машину) — это сказано в «зачем это» станций.
- **Ошибка JSON не теряет ввод** (находка 6.1): вместо редиректа — форма заново, статус 400, текст
  оператора в textarea и причина во флеше (класс `.flash.err` из `base.html`). Ничего не сохраняется
  (`update_settings` падает до commit).
- Наборы EMD показываются `json.dumps(..., ensure_ascii=False, indent=1)` — `|tojson` экранировал «grátis».
- **Р2:** `approve_at` подписан «Порог сильного кандидата» (значение по умолчанию для пакета в инбоксе и
  счётчик «сильных»), а не «авто-одобрение». Станции v1 «T0/T1» переписаны под волны v2 (W4 RD, W5
  возраст по старшей дате — счётчик превью возраста считает `age_years`, как W5); кап whois — «только
  зоны без RDAP».
- Цена W4 в тексте — 25 units за домен (живой замер).
- **Превью-счётчики не считают архив РФ-пула** (находка R2-16): после миграции 0025 тысячи старых .ru
  лежат как `rejected/legacy_ru` — машина их больше не судит, а `_pool_counts` раздувал ими каждый
  «сколько пройдёт». Заодно докстринг функции — на волны v2 (вместо `scoring._funnel`/T0, R2-20).

**Files:**
- Modify:
  - `backend/app/api/panel.py` — `_settings_page` (новая), `settings_view`, `settings_save`, `_pool_counts`
    (возраст по старшей дате, без архива `legacy_ru`);
  - `backend/app/services/diagnostics.py` — запись `ahrefs`, число в результате `_run_one`;
  - `backend/app/services/diag_cache.py` — `value(key)`;
  - `backend/app/templates/settings.html`.
- Test: `backend/tests/test_panel_settings_v2.py` (создать). Старые тесты задача не ломает: формы
  `test_web_fixes.py`/`test_scoring_weights.py` уже на ключах v2 (Задачи 6, 8, 11), а без маркера `v2_lists`
  списки не трогаются.
- Герметичность к ключам оператора уже обеспечена autouse `_no_paid_keys` (Задача 3) — заново не заводить.

**Interfaces:**
- Consumes: ключи настроек (Задачи 5, 6, 8; `units_floor` — Задача 5), `AhrefsClient.units_left` (Задача 2).
- Produces:
  - поля формы `min_dr`, `tld_allowlist`, `brand_tokens`, `emd_sets`, `max_links_per_run`, `max_deep_per_run`,
    `units_floor`, `spam_anchor_max`, маркер `v2_lists`;
  - `diag_cache.value(key) -> int | None`; результат проверки `/diag` может нести `"value"` (число);
  - запись диагностики `("ahrefs", "Ahrefs API", "M1 · DR / ссылки / анкоры", settings.AHREFS_API_KEY, "M1", True, …units_left())`
    — **Задача 16 её уже не добавляет**;
  - `_settings_page(request, db, emd_draft=None, form_err=None, status_code=200)`.

- [ ] **Шаг 1: Написать падающий тест** — `backend/tests/test_panel_settings_v2.py`

```python
"""Экран /settings v2: новые поля видны и сохраняются; пустая textarea очищает, отсутствующее поле
не трогает; плохой JSON EMD не затирает сохранённое и не теряет ввод; остаток units — из кэша /diag."""
from datetime import datetime, timedelta, timezone

import app.db as db
from app.models.domain import Domain
from app.services import diag_cache
from app.services import scoring_config as cfg
from app.services.settings import get_settings, update_settings

BASE = {"min_referring_domains": 1, "min_age_years": 3.0, "approve_at": 0.7, "manual_review_at": 0.4}


def test_settings_page_shows_v2_fields_and_dr_attribution(client):
    html = client.get("/settings").text
    for name in ("min_dr", "tld_allowlist", "brand_tokens", "emd_sets", "max_links_per_run",
                 "max_deep_per_run", "units_floor", "spam_anchor_max", "dropcatch", "nominet", "w_topical_fit"):
        assert f'name="{name}"' in html, name
    assert "Domain Rating by Ahrefs" in html and "max_ahrefs_per_run" not in html
    assert "Порог сильного кандидата" in html and "авто-одобрение" not in html     # Р2


def test_settings_save_v2_fields(client):
    r = client.post("/settings/save", data={**BASE, "v2_lists": "1", "min_dr": 8, "tld_allowlist": "com\nco.uk",
                                            "brand_tokens": "nordvpn", "max_links_per_run": 300,
                                            "max_deep_per_run": 5, "units_floor": 250000, "spam_anchor_max": 0.3,
                                            "emd_sets": '[{"keywords":["vpn gratis"],"tlds":["mx"]}]',
                                            "nominet": "on"}, follow_redirects=False)
    assert r.status_code == 303 and "err=" not in r.headers["location"]
    s = get_settings()
    assert s["min_dr"] == 8.0 and s["tld_allowlist"] == ["com", "co.uk"] and s["max_deep_per_run"] == 5
    assert s["units_floor"] == 250000 and s["max_links_per_run"] == 300 and s["spam_anchor_max"] == 0.3
    assert s["emd_sets"][0]["keywords"] == ["vpn gratis"] and s["sources_enabled"]["nominet"] is True


def test_settings_save_without_v2_fields_keeps_them(client):
    """Форма без полей v2 (старый шаблон, curl) не затирает сохранённое — и `max_whois_per_run`
    тоже: раньше дефолт формы 200 молча перезаписывал настройку (находка 6.1)."""
    update_settings(min_dr=12, tld_allowlist=["com"], max_whois_per_run=77, units_floor=150000)
    client.post("/settings/save", data=BASE, follow_redirects=False)
    s = get_settings()
    assert s["min_dr"] == 12.0 and s["tld_allowlist"] == ["com"]
    assert s["max_whois_per_run"] == 77 and s["units_floor"] == 150000


def test_empty_textarea_clears_emd_sets(client):
    """6.1: пустая textarea приходит в FastAPI как «поля нет»; форма v2 несёт маркер `v2_lists`, и
    пустое поле — это «очистить», а не «не трогать». Пустые зоны = стартовый список: пустой белый
    список остановил бы всю машину (settings.get_settings)."""
    update_settings(emd_sets=[{"keywords": ["vpn"], "tlds": ["com"]}], tld_allowlist=["com"])
    client.post("/settings/save", data={**BASE, "v2_lists": "1", "emd_sets": "", "tld_allowlist": "",
                                        "brand_tokens": "nordvpn"}, follow_redirects=False)
    s = get_settings()
    assert s["emd_sets"] == [] and s["tld_allowlist"] == cfg.TLD_ALLOWLIST


def test_bad_emd_json_keeps_old_sets_and_operator_input(client):
    """6.1: плохой JSON — ничего не сохранено, а ввод оператора не потерян: форма возвращается с его
    текстом и ошибкой (редирект унёс бы JSON в никуда)."""
    update_settings(emd_sets=[{"keywords": ["vpn"], "tlds": ["com"]}])
    r = client.post("/settings/save", data={**BASE, "v2_lists": "1",
                                            "emd_sets": '[{"keywords": ["mejor vpn"], oops'},
                    follow_redirects=False)
    assert r.status_code == 400
    assert "Не сохранено" in r.text and "mejor vpn" in r.text and "oops" in r.text
    assert get_settings()["emd_sets"][0]["keywords"] == ["vpn"]


def test_emd_sets_shown_without_unicode_escapes(client):
    """6.1: наборы показываются читаемо — «vpn grátis», а не «vpn gr\\u00e1tis» (так экранировал |tojson)."""
    update_settings(emd_sets=[{"market": "es-MX", "lang": "es", "keywords": ["vpn grátis"], "tlds": ["mx"]}])
    html = client.get("/settings").text
    assert "vpn grátis" in html and "\\u00e1" not in html


def test_units_left_comes_from_diag_cache_without_calling_ahrefs(client, monkeypatch):
    """3.4: /settings не ходит в Ahrefs на рендере (таймаут 60 с × 3 повесил бы страницу) — остаток
    из кэша диагностики, который фон обновляет раз в 5 минут."""
    from app.config import settings
    from app.integrations.ahrefs import AhrefsClient

    def _no_network(self):
        raise AssertionError("/settings не вправе ходить в Ahrefs на рендере")
    monkeypatch.setattr(settings, "AHREFS_API_KEY", "k")
    monkeypatch.setattr(AhrefsClient, "units_left", _no_network)
    monkeypatch.setattr(diag_cache, "_checks", None)
    assert "осталось units в месяце: <b>—</b>" in client.get("/settings").text
    monkeypatch.setattr(diag_cache, "_checks", [{"key": "ahrefs", "label": "Ahrefs API", "status": "ok",
                                                 "value": 1234567}])
    assert "осталось units в месяце: <b>1 234 567</b>" in client.get("/settings").text


def test_ahrefs_diag_records_units_left(monkeypatch):
    """Остаток units кладёт в кэш сам пинг Ahrefs в /diag (запрос бесплатный)."""
    from app.config import settings
    from app.integrations.ahrefs import AhrefsClient
    from app.services import diagnostics
    monkeypatch.setattr(settings, "AHREFS_API_KEY", "k")
    monkeypatch.setattr(AhrefsClient, "units_left", lambda self: 1500000)
    out = diagnostics.run_diagnostics(specs=[s for s in diagnostics._spec() if s[0] == "ahrefs"])
    assert out[0]["status"] == "ok" and out[0]["value"] == 1500000


def test_age_preview_counts_the_older_date(client):
    """Счётчик «проходит возраст» зеркалит W5 (Р5): возраст — старшая из даты RDAP/whois и первого
    снимка; перехваченный домен с молодой регистрацией, но 12 годами архива — проходит."""
    now = datetime.now(timezone.utc)
    with db.SessionLocal() as s:
        s.add_all([Domain(domain="recaught.com", whois_created=now - timedelta(days=365), age_years=12.0),
                   Domain(domain="old.com", whois_created=now - timedelta(days=3650)),
                   Domain(domain="young.com", whois_created=now - timedelta(days=365), age_years=1.0),
                   Domain(domain="unknown.com")])
        s.commit()
    assert client.get("/settings/preview?min_rd=0&min_age=3&approve=0.7&manual=0.4").json()["age"] == 2


def test_preview_counts_skip_legacy_ru_archive(client):
    """R2-16: архив РФ-пула v1 (`legacy_ru`, миграция 0025) машина больше не судит — превью
    «сколько пройдёт» его не считает: тысячи старых .ru раздували бы каждый счётчик."""
    old = datetime.now(timezone.utc) - timedelta(days=3650)
    with db.SessionLocal() as s:
        s.add_all([Domain(domain="old.ru", status="rejected", reject_reason="legacy_ru",
                          referring_domains=500, whois_created=old, score=0.9),
                   Domain(domain="live.com", referring_domains=500, whois_created=old, score=0.9)])
        s.commit()
    got = client.get("/settings/preview?min_rd=1&min_age=3&approve=0.7&manual=0.4").json()
    assert got == {"total": 1, "rd": 1, "age": 1, "approve": 1, "manual": 0}
```

- [ ] **Шаг 2: Запустить — убедиться, что падает**

Запуск: `cd backend && ../.venv/bin/python -m pytest tests/test_panel_settings_v2.py -v`
Ожидание: `10 failed` — полей v2 нет ни в шаблоне, ни в форме; `units_left`/`v2_lists`/записи `ahrefs` в
диагностике нет; превью считает возраст только по `whois_created` и считает архив `legacy_ru` (`total == 2`).

- [ ] **Шаг 3: Реализовать**

**`diagnostics.py`:**
- в `_spec()` после записи `aparser` добавить:

```python
        # остаток units — число: _run_one кладёт его в кэш, /settings показывает без похода в сеть
        ("ahrefs", "Ahrefs API", "M1 · DR / ссылки / анкоры", settings.AHREFS_API_KEY, "M1", True,
         lambda: __import__("app.integrations.ahrefs", fromlist=["x"]).AhrefsClient().units_left()),
```

- в `_run_one` строки `ok = bool(fn())` / `return {**base, "status": "ok" if ok else "fail", …}` заменить на:

```python
        v = fn()
        out = {**base, "status": "ok" if v else "fail",
               "ms": int((time.monotonic() - t0) * 1000), "error": None}
        if isinstance(v, int) and not isinstance(v, bool):
            out["value"] = v            # число (остаток units Ahrefs) — для экранов без сети
        return out
```

  (0 units — «fail»: без units платные волны стоят.)

**`diag_cache.py`:** перед `def alert()` добавить:

```python
def value(key: str):
    """Число из последней проверки `key` (остаток units Ahrefs) или None, пока кэша нет. Без сети:
    экран /settings не ждёт внешний сервис на рендере (находка 3.4)."""
    with _LOCK:
        for c in _checks or ():
            if c["key"] == key:
                return c.get("value")
    return None
```

**`panel.py`:**
- функцию `_pool_counts` целиком заменить на:

```python
def _pool_counts(db: Session, s: dict) -> dict:
    """Сколько доменов пула проходит каждый гейт при текущих порогах (превью эффекта).

    Правила счёта зеркалят волны (scoring._run_waves), иначе превью врёт: RD судит W4 и режет только
    ИЗВЕСТНЫЙ RD < порога — NULL (ещё не спрошен) проходит; возраст — W5, по старшей из даты
    RDAP/whois и первого снимка (Р5). Архив РФ-пула v1 (`legacy_ru`, миграция 0025) машина больше не
    судит — в превью его нет (находка R2-16): тысячи старых .ru раздували бы каждый счётчик.
    """
    from datetime import datetime, timezone, timedelta
    live = or_(Domain.reject_reason.is_(None), Domain.reject_reason != "legacy_ru")

    def n(*where) -> int:
        return db.scalar(select(func.count()).select_from(Domain).where(live, *where)) or 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=365.25 * s["min_age_years"])
    return {"total": n(),
            "rd": n(or_(Domain.referring_domains.is_(None),
                        Domain.referring_domains >= s["min_referring_domains"])),
            # возраст — старшая из даты RDAP/whois и первого снимка (Р5): age_years хранит решающий
            "age": n(or_(Domain.whois_created <= cutoff, Domain.age_years >= s["min_age_years"])),
            "approve": n(Domain.score >= s["approve_at"]),
            "manual": n(Domain.score >= s["manual_review_at"], Domain.score < s["approve_at"])}
```

- функцию `settings_view` целиком заменить на:

```python
def _settings_page(request: Request, db: Session, emd_draft: str | None = None,
                   form_err: str | None = None, status_code: int = 200):
    """Экран /settings. Остаток units Ahrefs — из кэша диагностики, без сети (находка 3.4).
    Наборы EMD — json.dumps без \\u-экранирования (|tojson прятал «grátis»); `emd_draft` —
    непринятый ввод оператора после ошибки JSON (находка 6.1)."""
    import json
    from app.services import settings as st
    s = st.get_settings()
    emd_text = emd_draft if emd_draft is not None else json.dumps(s["emd_sets"], ensure_ascii=False,
                                                                  indent=1)
    return templates.TemplateResponse(request, "settings.html", {
        "active": "settings", "s": s, "counts": _pool_counts(db, s),
        "units_left": diag_cache.value("ahrefs"), "emd_text": emd_text, "form_err": form_err},
        status_code=status_code)


@router.get("/settings", response_class=HTMLResponse)
def settings_view(request: Request, db: Session = Depends(get_session)):
    return _settings_page(request, db)
```

- функцию `settings_save` целиком заменить на:

```python
@router.post("/settings/save")
def settings_save(request: Request, db: Session = Depends(get_session),
                  min_referring_domains: int = Form(...), min_age_years: float = Form(...),
                  approve_at: float = Form(...), manual_review_at: float = Form(...),
                  max_whois_per_run: int | None = Form(None),
                  min_dr: float | None = Form(None), max_links_per_run: int | None = Form(None),
                  max_deep_per_run: int | None = Form(None), units_floor: int | None = Form(None),
                  spam_anchor_max: float | None = Form(None),
                  tld_allowlist: str | None = Form(None), brand_tokens: str | None = Form(None),
                  emd_sets: str | None = Form(None), v2_lists: str = Form(""),
                  dropcatch: str = Form(""), nominet: str = Form(""),
                  mx: str = Form(""), emd: str = Form(""),
                  w_history_cleanliness: float | None = Form(None),
                  w_topical_fit: float | None = Form(None), w_age: float | None = Form(None),
                  w_rd: float | None = Form(None), w_authority: float | None = Form(None),
                  w_anchor_quality: float | None = Form(None),
                  w_traffic_history: float | None = Form(None)):
    from app.services import settings as st
    # веса — опциональны: форма без них (старый шаблон, curl из скрипта) не должна ОБНУЛЯТЬ
    # шкалу оценки. None -> ключ не передаём, update_settings оставит прежние.
    weights = {k: v for k, v in (("history_cleanliness", w_history_cleanliness),
                                 ("topical_fit", w_topical_fit), ("age", w_age), ("rd", w_rd),
                                 ("authority", w_authority), ("anchor_quality", w_anchor_quality),
                                 ("traffic_history", w_traffic_history)) if v is not None}
    # Пустую textarea FastAPI отдаёт как «поля нет» (None). Форма v2 несёт маркер `v2_lists`: значит
    # эти поля в ней БЫЛИ, и пустое — это «очистить», а не «не трогать» (находка 6.1). Форма без
    # маркера (старый шаблон, curl) списки не трогает.
    if v2_lists:
        tld_allowlist, brand_tokens, emd_sets = tld_allowlist or "", brand_tokens or "", emd_sets or ""
    try:
        st.update_settings(min_referring_domains=min_referring_domains, min_age_years=min_age_years,
                           approve_at=approve_at, manual_review_at=manual_review_at,
                           max_whois_per_run=max_whois_per_run,
                           min_dr=min_dr, max_links_per_run=max_links_per_run,
                           max_deep_per_run=max_deep_per_run, units_floor=units_floor,
                           spam_anchor_max=spam_anchor_max, tld_allowlist=tld_allowlist,
                           brand_tokens=brand_tokens, emd_sets=emd_sets,
                           sources_enabled={"dropcatch": bool(dropcatch), "nominet": bool(nominet), "mx": bool(mx), "emd": bool(emd)},
                           weights=weights or None)
    except ValueError as e:
        # Ничего не сохранено (update_settings падает до commit). Ввод оператора не теряем: редирект
        # унёс бы его JSON в никуда — отдаём форму заново с его текстом и причиной.
        return _settings_page(request, db, emd_draft=emd_sets, status_code=400,
                              form_err=f"Не сохранено ничего: {e}. Наборы EMD — JSON-список, "
                                       "пример — в «зачем это» у станции EMD.")
    return _back("/settings", msg="Настройки сохранены")
```

**`settings.html`** — «найти → заменить» (фрагменты сверены: применённые по порядку к файлу после
Задачи 13, дают ровно шаблон, на котором сьют зелёный). Что делают правки: флеш ошибки формы и скрытый
маркер `v2_lists`; станции RD и возраста — под волны W4/W5; `approve_at` — «Порог сильного кандидата»
(Р2), `manual_review_at` — «на решение»; кап whois — «только зоны без RDAP»; после него — новые станции
(порог DR с атрибуцией Ahrefs, зоны, бренды, наборы EMD, капы Ahrefs и пол units с остатком, порог
спам-анкоров); в «Применить» — новые названия кнопок.

НАЙТИ:
```

<form method="post" action="/settings/save" style="max-width:760px; display:grid; gap:14px">
  <div class="station">
    <div class="plate">T0 · min доноров (RD) — <b>отсекает пустышки</b></div>
    <details class="what"><summary>зачем это</summary>
      <div class="what-body">Домены с RD ниже порога отбраковываются на входе (0 = не резать по RD).
      Сырые списки без RD проходят на T1.</div></details>
    <div class="go">
```
ЗАМЕНИТЬ НА:
```

{% if form_err %}<div class="flash err"><span class="tag">Ошибка</span><span>{{ form_err }}</span></div>{% endif %}
<form method="post" action="/settings/save" style="max-width:760px; display:grid; gap:14px">
  <input type="hidden" name="v2_lists" value="1">
  <div class="station">
    <div class="plate">W4 · min доноров (RD) — <b>отсекает пустышки</b></div>
    <details class="what"><summary>зачем это</summary>
      <div class="what-body">RD (refdomains) даёт Ahrefs в волне «ссылки» (W4), до дорогой истории.
      Ниже порога — отказ «мало доноров» (0 = не резать по RD). На дропах RD раздут спамом — честнее
      DR, его порог — ниже.</div></details>
    <div class="go">
```

НАЙТИ:
```
  <div class="station">
    <div class="plate">T1 · min возраст, лет — <b>отсекает молодые до дорогой истории</b></div>
    <details class="what"><summary>зачем это</summary>
      <div class="what-body">Возраст из whois (дата регистрации). Моложе порога → reject, не доходя до Wayback.</div></details>
    <div class="go">
```
ЗАМЕНИТЬ НА:
```
  <div class="station">
    <div class="plate">W5 · min возраст, лет — <b>по старшей из дат RDAP и архива</b></div>
    <details class="what"><summary>зачем это</summary>
      <div class="what-body">Возраст — старшая из даты регистрации (RDAP/whois) и первого снимка
      Wayback: у перехваченного и снова дропающегося домена RDAP показывает ПОСЛЕДНЮЮ регистрацию.
      Моложе порога — отказ «моложе порога» в волне истории.</div></details>
    <div class="go">
```

НАЙТИ:
```
  <div class="station">
    <div class="plate">Финальный скор · порог approve — <b>выше = авто-одобрение</b></div>
    <details class="what"><summary>зачем это</summary>
      <div class="what-body">Итоговый скор ≥ порога → домен одобряется автоматически.</div></details>
    <div class="go">
```
ЗАМЕНИТЬ НА:
```
  <div class="station">
    <div class="plate">Порог сильного кандидата — <b>с него начинается пакет в инбоксе</b></div>
    <details class="what"><summary>зачем это</summary>
      <div class="what-body">Машина сама не одобряет ничего: скоринг ставит максимум «на решении».
      Одобряешь ты — кнопкой в строке или пакетом. Этот порог — значение по умолчанию для «пакет от
      скора» в инбоксе и счётчик сильных кандидатов.</div></details>
    <div class="go">
```

НАЙТИ:
```
      <b id="v_approve">{{ s.approve_at }}</b>
      <span class="hint">уже approved: <b id="c_approve">{{ counts.approve }}</b></span>
    </div>
  </div>
  <div class="station">
    <div class="plate">Финальный скор · порог manual — <b>ниже = reject</b></div>
    <details class="what"><summary>зачем это</summary>
      <div class="what-body">Скор между manual и approve → ручной разбор (scored); ниже manual → reject.</div></details>
    <div class="go">
```
ЗАМЕНИТЬ НА:
```
      <b id="v_approve">{{ s.approve_at }}</b>
      <span class="hint">сильных (скор ≥ порога): <b id="c_approve">{{ counts.approve }}</b></span>
    </div>
  </div>
  <div class="station">
    <div class="plate">Порог «на решение» — <b>ниже = отказ «низкий скор»</b></div>
    <details class="what"><summary>зачем это</summary>
      <div class="what-body">Скор ≥ порога — домен приходит в инбокс «на решении», ниже — отказ.
      Этим же порогом отбираются кандидаты дорогой проверки анкоров (W6).</div></details>
    <div class="go">
```

НАЙТИ:
```
      <b id="v_manual">{{ s.manual_review_at }}</b>
      <span class="hint">в вилке ручного разбора: <b id="c_manual">{{ counts.manual }}</b></span>
    </div>
  </div>
  <div class="station">
    <div class="plate">Кап whois за прогон — <b>защита от сырого cctld</b></div>
    <details class="what"><summary>зачем это</summary>
      <div class="what-body">За один запуск проверки пробьём whois не более этого числа доменов
      (лучшие по RD — первыми). Остальные подождут следующего прогона.</div></details>
    <div class="go">
```
ЗАМЕНИТЬ НА:
```
      <b id="v_manual">{{ s.manual_review_at }}</b>
      <span class="hint">на решении, но ниже сильных: <b id="c_manual">{{ counts.manual }}</b></span>
    </div>
  </div>
  <div class="station">
    <div class="plate">Кап whois:43 за прогон — <b>только зоны без RDAP</b></div>
    <details class="what"><summary>зачем это</summary>
      <div class="what-body">RDAP (.com/.net/.org/.uk …) бесплатен и не капается. Кап — на whois:43
      через A-Parser для зон без RDAP (.mx/.co/.nz …): за один запуск не больше этого числа доменов,
      остальные подождут следующего прогона.</div></details>
    <div class="go">
```

НАЙТИ:
```
      <span class="hint">whois-вызовов за прогон максимум</span>
    </div>
```
ЗАМЕНИТЬ НА:
```
      <span class="hint">whois-вызовов за прогон максимум</span>
    </div>
  </div>
  <div class="station">
    <div class="plate">Порог DR на входе — <b>отсев спам-дропов ещё до базы</b></div>
    <details class="what"><summary>зачем это</summary>
      <div class="what-body">Почти каждый дропающийся домен засыпан автоматическим SEO-спамом: RD в
      сотни, а DR 0. Автоматические источники (DropCatch, Nominet, registry.mx) проходят бесплатный
      DR ещё на входе — ниже порога в базу не попадают. Ручной список и EMD этот фильтр не проходят.</div></details>
    <div class="go">
      <input type="range" name="min_dr" min="0" max="60" step="1" value="{{ s.min_dr|int }}"
             oninput="document.getElementById('v_min_dr').textContent=this.value" style="flex:1 1 200px">
      <b id="v_min_dr">{{ s.min_dr|int }}</b>
      <a class="hint" href="https://ahrefs.com/" target="_blank" rel="noopener">Domain Rating by Ahrefs</a>
    </div>
  </div>
  <div class="station">
    <div class="plate">Белый список зон — <b>какие TLD вообще берём</b></div>
    <details class="what"><summary>зачем это</summary>
      <div class="what-body">По одной зоне в строке (com, co.uk, mx …). Только зоны, где ты сам можешь
      быть владельцем и которые продаёт наш регистратор — см. docs/v2/research/cctld-markets.md.
      .com.mx и .com.co — отдельные зоны, NameSilo их не продаёт. Пустое поле — вернуть стартовый
      список (пустой белый список остановил бы всю машину).</div></details>
    <div class="go"><textarea name="tld_allowlist" rows="4" style="flex:1 1 320px">{{ s.tld_allowlist|join('\n') }}</textarea></div>
  </div>
  <div class="station">
    <div class="plate">Чужие VPN-бренды — <b>в имени домена = отказ</b></div>
    <details class="what"><summary>зачем это</summary>
      <div class="what-body">Домен с чужой торговой маркой (nordvpn-deals.com) — юридический риск, такой
      не покупаем. Токен с «vpn» или от 7 букв ищется подстрокой, короче — только отдельным словом
      между дефисами (иначе javastudio.com отклонялся бы по avast). Пустое поле — стартовый список.</div></details>
    <div class="go"><textarea name="brand_tokens" rows="4" style="flex:1 1 320px">{{ s.brand_tokens|join('\n') }}</textarea></div>
  </div>
  <div class="station">
    <div class="plate">Наборы EMD — <b>ключи × язык × зоны</b></div>
    <details class="what"><summary>зачем это</summary>
      <div class="what-body">JSON-список наборов. Пример:
      <code>[{"market":"es-MX","lang":"es","keywords":["mejor vpn","vpn gratis"],"tlds":["com","mx"]}]</code>.
      Машина соберёт имена склейкой и через дефис, проверит доступность; EMD всегда приходит к тебе
      на решение без балла. Пустое поле — наборов нет.</div></details>
    <div class="go"><textarea name="emd_sets" rows="5" style="flex:1 1 320px">{{ emd_text }}</textarea></div>
  </div>
  <div class="station">
    <div class="plate">Ahrefs: капы за прогон и пол остатка — <b>units в месяце ограничены</b></div>
    <details class="what"><summary>зачем это</summary>
      <div class="what-body">W4 «ссылки» — 25 units за домен; W6 «анкоры» ≈ 1,1 тыс. units за домен
      (только финалисты, лучшие первыми). Сверх капа W4 домен ждёт следующего прогона; без W6 домен не
      попадёт в пакетное одобрение («анкоры не проверены»). Пол остатка: если units в месяце меньше,
      платные волны не идут вовсе — автопилот гоняет оценку раз в час, и капы «на прогон» месяц не
      держат. 0 — пола нет.</div></details>
    <div class="go">
      <span class="f">W4 ссылки</span>
      <input type="range" name="max_links_per_run" min="1" max="2000" step="1" value="{{ s.max_links_per_run }}"
             oninput="document.getElementById('v_links').textContent=this.value" style="flex:1 1 160px">
      <b id="v_links">{{ s.max_links_per_run }}</b>
    </div>
    <div class="go">
      <span class="f">W6 анкоры</span>
      <input type="range" name="max_deep_per_run" min="0" max="200" step="1" value="{{ s.max_deep_per_run }}"
             oninput="document.getElementById('v_deep').textContent=this.value" style="flex:1 1 160px">
      <b id="v_deep">{{ s.max_deep_per_run }}</b>
    </div>
    <div class="go">
      <span class="f">пол остатка</span>
      <input type="range" name="units_floor" min="0" max="2000000" step="50000" value="{{ s.units_floor }}"
             oninput="document.getElementById('v_floor').textContent=this.value" style="flex:1 1 160px">
      <b id="v_floor">{{ s.units_floor }}</b>
      <span class="hint" title="по последней проверке /diag (обновляется раз в 5 минут)">осталось units в месяце: <b>{{ '{:,}'.format(units_left).replace(',', ' ') if units_left is not none else '—' }}</b></span>
    </div>
  </div>
  <div class="station">
    <div class="plate">Порог спам-анкоров — <b>доля по донорам</b></div>
    <details class="what"><summary>зачем это</summary>
      <div class="what-body">Доля доноров, чьи анкоры — спам (флаг Ahrefs, казино/фарма/SEO-реклама,
      чужая письменность — кроме языка прошлого сайта). Выше порога — отказ «спам-анкоры», это грязь:
      в оборот домен не вернётся.</div></details>
    <div class="go">
      <input type="range" name="spam_anchor_max" min="0" max="1" step="0.05" value="{{ s.spam_anchor_max }}"
             oninput="document.getElementById('v_spam').textContent=this.value" style="flex:1 1 200px">
      <b id="v_spam">{{ s.spam_anchor_max }}</b>
    </div>
```

НАЙТИ:
```
      <div class="what-body">«Сохранить» записывает пороги и источники в настройки — действовать начнут
      со следующего запуска ▶ Score / ↻ Discovery (уже отскоренные домены не пересчитываются).
      «Сбросить» возвращает стартовые значения из scoring_config.</div></details>
```
ЗАМЕНИТЬ НА:
```
      <div class="what-body">«Сохранить» записывает пороги и источники в настройки — действовать начнут
      со следующего запуска «Оценить домены» / «Найти дропы» (уже оценённые домены не пересчитываются).
      «Сбросить» возвращает стартовые значения из scoring_config.</div></details>
```

- [ ] **Шаг 4: Тесты, сьют, линт; рендер**

Запуск: `cd backend && ../.venv/bin/python -m pytest tests/test_panel_settings_v2.py -v && ../.venv/bin/python -m pytest tests -q && cd .. && .venv/bin/python -m pyflakes backend/app backend/tests`
Ожидание: `10 passed`; весь сьют **865 passed** (855 + 10), pyflakes пуст.

Рендер: классы новых станций — только существующие в `base.html` (`station`, `plate`, `details.what`,
`what-body`, `go`, `hint`, `f`, `flash err`, `tag`); проверить `grep -n "class=" backend/app/templates/settings.html`.
Глазами — поднять панель (`docker compose up` или throwaway `uvicorn`, как в `docs/DESIGN.md`), открыть
`/settings` на 1366 px и 1024 px: станции не переполняются, стиль совпадает с соседними. **Скриншот —
настоящий рендер, не мокап** (урок v1).

- [ ] **Шаг 5: Коммит**

```bash
git add -A backend/app backend/tests
git commit -m "feat(panel): /settings v2 — DR-порог, зоны, бренды, наборы EMD, капы и пол units Ahrefs"
```

---

### Задача 15: Панель — инбокс, реестр, подписи, легенда отказов

**Что меняется и почему.**
- **Инбокс v2:** язык прошлого сайта, тема с близостью к VPN, DR с подписью «Domain Rating by Ahrefs»
  и ссылкой (лицензия), «EMD — решение за тобой», «тема не определена», пометка **«прошлая тема далека
  от VPN»** (инвариант 4; `scoring.topic_far` из Задачи 13). Тема видна и в «Готовы к выкупу» — оттуда
  идут покупать (Р2).
- **Строка с чистой историей, которую пакет не берёт** (далёкая тема, EMD — Задача 13): раньше ветка
  `else` писала «история не подтверждена» — неправда. Теперь «история чистая · в пакет не идёт — решай
  сам», причина — строкой выше.
- **Фильтр `?lang=` не одобряет невидимое** (находка 1.6): при выбранном языке форма пакета скрыта,
  пустое состояние — «По языку X ничего нет», счётчик «на решении» и список языков — до фильтра.
  Разметка фильтра — `.chips/.chip`, как в `pool.html`, и стоит ДО ветки `{% if inbox %}` (находка 6.3).
- **«Пакет от скора» по умолчанию = `approve_at`** («порог сильного кандидата», Р2), а не зашитые 0.80.
- **Бейдж источника — явная карта** (находка 6.2): `labels.SOURCE_BADGE`
  `{"dropcatch":"dc","nominet":"uk","mx":"mx","emd":"emd","list":"руч"}` (+ легаси v1 `bo/cc/rg/sw`), не
  срез подписи `[:3]` («вру», «reg» путался с reg.ru).
- **Легенда отказов** (находка 6.4): «режет порог» — `low_rd`, `too_young`, `low_score`; «грязь» —
  `history_dirty`, `blacklist`, `spam_anchors`, `feed_flag`, легаси `rkn`, `safebrowsing`; «нельзя» —
  `not_acquirable`, `tld_closed`, `trademark`, `legacy_ru`. `low_dr` не производит никто — его нет.
- **Подписи v1** (находка 6.5): подсказка «▶ Оценить домены», «весь пул», блок «что делают эти три
  действия» — под шесть волн и Р2; кнопка «цены» (`/admin/refresh-prices` — тариф backorder для .RU/.РФ,
  в v2 РФ-доменов нет) снята с карточки M1, сам роут и его тест остаются до подпроекта 2 (выкуп).
  Легенда `pool.html` — под волны v2; колонка `echo` (SearXNG удалён в Задаче 10) заменена на `DR` с
  атрибуцией. Станции T0/T1 `settings.html` переписаны в Задаче 14.
- **Проекция whois удалена** вместе с её единственным производителем (TCI, Задача 9): ветки
  `ОСВОБОДИТСЯ*`/`projection_hint` в `domains.html`, `прогноз whois` в `pool.html`, `whois_projection` в
  `panel._expired`; дедлайн — только «СРОК ДРОПА» из источника (DropCatch/Nominet).
- **`_funnel_tally`** (находка 4.10): `spam_anchors` (W6) и `too_young` (W5, Р5) рождаются после Wayback —
  в «решено дёшево» их не записать.
- **Автопилот не обещает авто-одобрения** (находка R2-5): подпись стадии «Проверка» в `autopilot.html`
  «сильные и чистые уйдут в approved» противоречит Р2 → «проверенные придут к тебе на решение (scored) —
  одобряешь ты».
- **EMD-новорег с пустым архивом — не тревога** (находка R2-14): `sampled == 0` у EMD — норма (прошлого
  сайта нет). Вместо «⚠ история НЕ проверена … ▶ перепроверить» — нейтральное «архив пуст — новорег»
  (`scoring.emd_newreg`; пакет EMD и так не берёт); «возраст НЕ проверен» у EMD тоже не пишется — возраст
  новорега не критерий. Упал Wayback (`wayback:` в ошибках) — это по-прежнему «история НЕ проверена».
- **Зона вне белого списка — ни кнопки, ни пакета** (следствие R2-19, Задача 5: политика запрещает
  `→ approved` такой зоне). Один предикат `transitions.zone_closed` для политики, реестра и пакета:
  `pool.html` не предлагает «↩ вернуть в approved» отклонённому порогом домену чужой зоны (кнопка вела в
  гарантированный отказ), `_bulk_candidates` такие домены не берёт (одно чтение настроек на пакет) и
  считает в «пропущено». Флеш пакета называет причины пропуска v2, а не только «историю».

**Files:**
- Modify:
  - `backend/app/services/labels.py` — коды v2 в `REJECT_RU`, `SOURCE_RU`/`SOURCE_BADGE`, `source_ru`/`source_badge`;
  - `backend/app/services/scoring.py` — `emd_newreg` (новая), `blind_reason` (EMD-новорег);
  - `backend/app/services/transitions.py` — `zone_closed` (новая), её зовёт `refuse_closed_zone`;
  - `backend/app/api/panel.py` — фильтры Jinja, `domains_view` (`?lang=`, `inbox_total`, `langs`, `far_ids`,
    `newreg_ids`, `bulk_default`), `domains_pool_view` (`closed_ids`), `_bulk_candidates`, флеш пакета,
    `_expired`, `_funnel_tally`;
  - `backend/app/templates/domains.html`, `backend/app/templates/pool.html`, `backend/app/templates/autopilot.html`.
- Test: `backend/tests/test_panel_inbox_v2.py` (создать); `backend/tests/test_labels.py` — дописать. Старые
  тесты — явный список в шаге 5.

**Interfaces:**
- Consumes: колонки `market_lang`, `topic`, `topical_relevance`, `dr`; `score_breakdown.emd`, `.topic_unknown`,
  `.deep_checked`; `scoring.topic_far`, `bulk_ok` (Задача 13); `get_settings()["approve_at"]`.
- Produces: фильтры Jinja `source_ru`, `source_badge`; query-параметр `/domains?lang=xx`; контекст
  `domains.html`: `inbox_total`, `langs`, `f_lang`, `far_ids`, `newreg_ids`, `bulk_default`; контекст
  `pool.html`: `closed_ids`; `scoring.emd_newreg(d) -> bool`; `transitions.zone_closed(d, allowlist=None) -> bool`.

- [ ] **Шаг 1: Написать падающие тесты**

`backend/tests/test_panel_inbox_v2.py`:

```python
"""Инбокс v2: язык, тема, DR с атрибуцией Ahrefs, EMD, фильтр по языку без «невидимого» пакета,
пакет от «порога сильного кандидата», легенда отказов v2, раскладка прогона."""
import re

import app.db as db
from app.models.domain import Domain
from app.models.domain_score_log import DomainScoreLog
from app.services import jobs

CHECKED = {"errors": [], "deep_checked": True}


def _add(**kw):
    with db.SessionLocal() as s:
        d = Domain(**kw)
        s.add(d)
        s.commit()
        return d.id


def test_inbox_shows_lang_topic_dr_attribution_and_emd(client):
    _add(domain="polski-blog.com", source="nominet", status="scored", score=0.6, dr=12,
         market_lang="pl", topic="VPN and privacy blog", topical_relevance=0.8, score_breakdown=CHECKED)
    _add(domain="mejorvpn.com", source="emd", status="scored", score=None, market_lang="es",
         score_breakdown={"emd": True, "errors": []})
    html = client.get("/domains").text
    assert "polski-blog.com" in html and "VPN and privacy blog" in html and "близость к VPN <b>80%</b>" in html
    assert "Domain Rating by Ahrefs" in html and "EMD — решение за тобой" in html
    assert 'title="источник: Nominet">uk</span>' in html and 'title="источник: EMD">emd</span>' in html


def test_inbox_lang_filter_hides_bulk_and_counts_before_filter(client):
    """1.6: на /domains?lang=pl оператор видит только польские домены, а пакет взял бы все языки —
    поэтому при выбранном языке форма пакета скрыта. Счётчик «на решении» — до фильтра."""
    _add(domain="pl-site.com", source="nominet", status="scored", score=0.9, market_lang="pl",
         score_breakdown=CHECKED)
    _add(domain="es-site.com", source="nominet", status="scored", score=0.9, market_lang="es",
         score_breakdown=CHECKED)
    html = client.get("/domains?lang=pl").text
    assert "pl-site.com" in html and "es-site.com" not in html
    assert 'action="/domains/bulk-approve"' not in html and "пакетное одобрение скрыто" in html
    assert '<div class="v">2</div><div class="k">на решении</div>' in html
    assert 'class="chip on" href="/domains?lang=pl"' in html
    assert 'action="/domains/bulk-approve"' in client.get("/domains").text


def test_inbox_lang_filter_empty_state_keeps_the_filter(client):
    """6.3: пусто по языку — не «Решать нечего», а «по языку X ничего нет», и переключатель
    языков на месте (он стоит ДО ветки пустого инбокса — иначе вернуться некуда)."""
    _add(domain="pl-site.com", source="nominet", status="scored", score=0.9, market_lang="pl",
         score_breakdown=CHECKED)
    html = client.get("/domains?lang=de").text
    assert "По языку de ничего нет" in html and "Решать нечего" not in html
    assert 'href="/domains?lang=pl"' in html


def test_bulk_default_is_the_strong_candidate_threshold(client):
    """Р2: «пакет от скора» по умолчанию = approve_at («порог сильного кандидата»), а не зашитые 0.80."""
    from app.services.settings import update_settings
    update_settings(approve_at=0.65)
    _add(domain="a.com", source="nominet", status="scored", score=0.9, score_breakdown=CHECKED)
    assert 'name="min_score" value="0.65"' in client.get("/domains").text


def test_far_topic_is_marked_and_clean_history_still_named_clean(client):
    """Инвариант 4 + Р2: прошлая тема далека от VPN — пометка в строке; пакет такой домен не берёт,
    но история у него ЧИСТАЯ — строка не вправе писать «история не подтверждена». Тема видна и
    в «Готовы к выкупу», откуда идут покупать."""
    _add(domain="far.com", source="nominet", status="scored", score=0.8, wayback_checked=True,
         prior_flags={}, age_years=9.0, topic="casino reviews", topical_relevance=0.1,
         score_breakdown=CHECKED)
    _add(domain="ready.com", source="nominet", status="approved", score=0.8, topic="VPN deals",
         topical_relevance=0.9, score_breakdown=CHECKED)
    html = client.get("/domains").text
    assert "прошлая тема далека от VPN" in html
    assert "история чистая" in html and "история не подтверждена" not in html
    assert client.get("/domains/bulk-preview?min_score=0.5").json() == {"n": 0, "skipped": 1}
    ready = html[html.index("Готовы к выкупу"):]
    assert "тема: VPN deals" in ready


def test_reject_legend_groups_v2_codes(client):
    """6.4: спам-анкоры и легаси Safe Browsing/РКН — грязь; чужая зона, бренд, архив РФ — «нельзя»;
    `low_dr` не производит никто — в легенде его нет."""
    for i, code in enumerate(("spam_anchors", "safebrowsing", "rkn", "tld_closed", "trademark",
                              "legacy_ru", "low_rd")):
        _add(domain=f"r{i}.com", status="rejected", reject_reason=code)
    html = client.get("/domains").text
    for code, kind in (("spam_anchors", "dirt"), ("safebrowsing", "dirt"), ("rkn", "dirt"),
                       ("tld_closed", "taken"), ("trademark", "taken"), ("legacy_ru", "taken"),
                       ("low_rd", "thr")):
        assert re.search(rf'<code>{code}</code></div>\s*<div class="why-bar"><i class="k-{kind}"', html), code
    assert "Низкий DR" not in html


def test_funnel_tally_counts_spam_anchors_and_too_young_as_reached_wayback(client):
    """4.10 + Р5: `spam_anchors` (W6) и `too_young` (W5) рождаются ПОСЛЕ Wayback — в «решено дёшево»
    их не записать."""
    did = _add(domain="tally.com", status="discovered")
    with jobs.track("score") as run:
        with db.SessionLocal() as s:
            for reason in ("spam_anchors", "too_young", "tld_closed"):
                s.add(DomainScoreLog(domain_id=did, run_id=run, outcome="rejected",
                                     reject_reason=reason, score=None, sig={}))
            s.commit()
        t = client.get("/api/jobs/live").json()["jobs"][0]["tally"]
        assert t["reached_wayback"] == 2 and t["before_wayback"] == 1


def test_autopilot_score_stage_says_the_human_approves(client):
    """R2-5 + Р2: стадия «Проверка» автопилота не обещает «сильные и чистые уйдут в approved» —
    машина ставит максимум scored, одобряет человек."""
    html = client.get("/autopilot").text
    assert "уйдут в approved" not in html and "придут к тебе на решение" in html


def test_emd_with_empty_archive_is_newreg_not_blind(client):
    """R2-14: у EMD-новорега пустой архив — норма. Вместо «⚠ история НЕ проверена … ▶ перепроверить»
    строка говорит нейтрально «архив пуст — новорег» (пакет EMD и так не берёт); возраст у новорега
    не критерий. Упал Wayback — это по-прежнему «история НЕ проверена»."""
    from app.services import scoring
    _add(domain="mejorvpn.com", source="emd", status="scored", score=None, market_lang="es",
         score_breakdown={"emd": True, "errors": [], "sampled": 0, "history_evidence": []})
    html = client.get("/domains").text
    assert "архив пуст — новорег" in html
    assert "история НЕ проверена" not in html and "возраст НЕ проверен" not in html
    down = Domain(domain="vpngratis.com", score_breakdown={"emd": True, "errors": ["wayback:ReadTimeout"],
                                                          "sampled": 0})
    assert scoring.blind_reason(down) == "история НЕ проверена: Wayback был недоступен"


def test_bulk_skips_domains_outside_the_zone_allowlist(client):
    """R2-19: зона вне белого списка — в approved домен не вернуть даже руками (transitions), и пакет
    его не берёт (иначе падал бы отказом политики), а считает в «пропущено». Зону добавили — берёт."""
    from app.services.settings import update_settings
    for name in ("v1-left.ru", "fresh.com"):
        _add(domain=name, source="nominet", status="scored", score=0.9, wayback_checked=True,
             prior_flags={}, age_years=9.0, score_breakdown=CHECKED)
    assert client.get("/domains/bulk-preview?min_score=0.5").json() == {"n": 1, "skipped": 1}
    update_settings(tld_allowlist=["com", "ru"])
    assert client.get("/domains/bulk-preview?min_score=0.5").json() == {"n": 2, "skipped": 0}


def test_pool_does_not_offer_return_for_closed_zone(client):
    """R2-19: отклонённый ПОРОГОМ домен вне белого списка зон (.ru v1) — «↩ вернуть в approved» не
    предлагается: политика (transitions.zone_closed) его не пустит, а кнопка, которая не может
    сработать, — ложное предложение. Причина названа, перескор остаётся."""
    _add(domain="weak.ru", status="rejected", reject_reason="low_score", score=0.3)
    html = client.get("/domains/pool?status=rejected").text
    assert "↩ вернуть в approved" not in html and "зона не в белом списке" in html
    assert "▶ перепроверить" in html
```

Дописать в конец `backend/tests/test_labels.py`:

```python


def test_v2_labels():
    from app.services.labels import reject_ru, source_badge, source_ru
    assert reject_ru("spam_anchors") == "спам-анкоры" and reject_ru("legacy_ru") == "РФ (архив v1)"
    assert reject_ru("tld_closed") == "зона не наша" and reject_ru("trademark") == "чужой бренд"
    assert source_ru("nominet") == "Nominet" and source_ru("emd") == "EMD" and source_ru(None) == ""
    # 6.2: бейдж — явная карта, не срез [:3] («вру», «reg» путался с reg.ru)
    assert [source_badge(s) for s in ("dropcatch", "nominet", "mx", "emd", "list")] == ["dc", "uk", "mx", "emd", "руч"]
    assert source_badge("backorder") == "bo" and source_badge("zzz") == "?" and source_badge(None) == "?"
```

- [ ] **Шаг 2: Запустить — убедиться, что падает**

Запуск: `cd backend && ../.venv/bin/python -m pytest tests/test_panel_inbox_v2.py tests/test_labels.py -v`
Ожидание: `12 failed, 5 passed` — одиннадцать тестов инбокса (нет темы, DR, фильтра языка, порога пакета,
легенды v2, раскладки `spam_anchors`/`too_young`; автопилот обещает approved; EMD-новорег — «⚠ история НЕ
проверена»; пакет берёт `.ru` (`n == 2`); реестр предлагает «↩ вернуть в approved» домену `.ru`) и
`test_v2_labels` (`ImportError: cannot import name 'source_badge'`); старые пять тестов `test_labels.py`
зелёные.

- [ ] **Шаг 3: Реализовать**

Все правки ниже — «найти фрагмент → заменить». Фрагменты сверены: применённые по порядку к файлам после
Задачи 14, они дают ровно файлы, на которых сьют зелёный.

**`backend/app/services/labels.py`:**

НАЙТИ:
```
    "safebrowsing": "Google Safe Browsing",
}
```
ЗАМЕНИТЬ НА:
```
    "safebrowsing": "Google Safe Browsing",
    # v2 (коды v1 выше остаются: в базе есть легаси-строки)
    "tld_closed": "зона не наша", "trademark": "чужой бренд", "spam_anchors": "спам-анкоры",
    "legacy_ru": "РФ (архив v1)",
}

# Источник домена (Domain.source). Легаси v1 — для старых строк реестра.
SOURCE_RU = {"dropcatch": "DropCatch", "nominet": "Nominet", "mx": "registry.mx", "emd": "EMD",
             "list": "вручную", "backorder": "backorder (v1)", "cctld": "cctld (v1)",
             "reg_ru": "reg.ru (v1)", "sweb": "sweb (v1)"}
# Бейдж в строке — явная карта, не срез подписи: [:3] давал «вру» и «reg» (путался с reg.ru).
SOURCE_BADGE = {"dropcatch": "dc", "nominet": "uk", "mx": "mx", "emd": "emd", "list": "руч",
                "backorder": "bo", "cctld": "cc", "reg_ru": "rg", "sweb": "sw"}
```

НАЙТИ:
```

def lane_ru(v):
```
ЗАМЕНИТЬ НА:
```

def source_ru(v):
    return SOURCE_RU.get(v, v) if v else ""


def source_badge(v):
    return SOURCE_BADGE.get(v, "?") if v else "?"


def lane_ru(v):
```

**`backend/app/services/scoring.py`** (R2-14 — EMD-новорег):

НАЙТИ:
```
def blind_reason(d) -> str | None:
```
ЗАМЕНИТЬ НА:
```
def emd_newreg(d) -> bool:
    """EMD без единого прочитанного снимка (находка R2-14): у новорега пустой архив — норма, а не
    «история НЕ проверена». Пометка «⚠ … ▶ перепроверить» звала бы перепроверять то, чего нет; пакет
    EMD всё равно не берёт (балла нет). Снимки есть, но прочитано мало (`history_evidence`), или
    Wayback упал (`wayback:` в errors) — не сюда: тогда история и правда не проверена."""
    bd = d.score_breakdown or {}
    errors = [str(e) for e in (bd.get("errors") or [])]
    return (bool(bd.get("emd")) and bd.get("sampled") == 0 and not bd.get("history_evidence")
            and not any(e.startswith("wayback:") for e in errors))


def blind_reason(d) -> str | None:
```

НАЙТИ:
```
    errors = [str(e) for e in ((d.score_breakdown or {}).get("errors") or [])]
    if history_verdict(d) == "unknown":
        if any(e.startswith("wayback:") for e in errors):
```
ЗАМЕНИТЬ НА:
```
    errors = [str(e) for e in ((d.score_breakdown or {}).get("errors") or [])]
    # EMD-новорег с пустым архивом (R2-14): «история НЕ проверена» тут не тревога, а норма — строка
    # инбокса скажет «архив пуст — новорег» (emd_newreg); остальные проверки ниже идут как обычно.
    if history_verdict(d) == "unknown" and not emd_newreg(d):
        if any(e.startswith("wayback:") for e in errors):
```

НАЙТИ:
```
    if d.whois_created is None and d.first_seen is None and d.age_years is None:
        return "возраст НЕ проверен: возраста нет ни из RDAP/whois, ни из архива"
```
ЗАМЕНИТЬ НА:
```
    # У EMD возраст не критерий: новорег, гейт «слишком молодой» W5 его не судит (R2-14).
    if (not (d.score_breakdown or {}).get("emd") and d.whois_created is None
            and d.first_seen is None and d.age_years is None):
        return "возраст НЕ проверен: возраста нет ни из RDAP/whois, ни из архива"
```

**`backend/app/services/transitions.py`** (R2-19 — один предикат зоны для политики, реестра и пакета):

НАЙТИ:
```
def refuse_closed_zone(d, allowlist=None) -> None:
```
ЗАМЕНИТЬ НА:
```
def zone_closed(d, allowlist=None) -> bool:
    """Зона домена вне белого списка (/settings) — ручной путь в `approved` закрыт (находка R2-19).
    ОДИН предикат для политики (refuse_closed_zone), реестра (кнопка «↩ вернуть в approved») и пакета
    (panel._bulk_candidates) — иначе кнопка предлагала бы то, что политика отвергнет.
    `allowlist=None` — список из /settings; кто судит пачку доменов, передаёт его сам (одно чтение)."""
    from app.services.domain_filters import tld_match
    if allowlist is None:
        from app.services.settings import get_settings
        allowlist = get_settings()["tld_allowlist"]
    return not tld_match(d.domain, allowlist)


def refuse_closed_zone(d, allowlist=None) -> None:
```

НАЙТИ:
```
    from app.services.domain_filters import tld_match
    if allowlist is None:
        from app.services.settings import get_settings
        allowlist = get_settings()["tld_allowlist"]
    if not tld_match(d.domain, allowlist):
        raise TransitionDenied(
```
ЗАМЕНИТЬ НА:
```
    if zone_closed(d, allowlist):
        raise TransitionDenied(
```

**`backend/app/api/panel.py`:**

НАЙТИ:
```
from app.services.labels import (status_ru as _status_ru, reject_ru as _reject_ru,
                                 lane_ru as _lane_ru, index_ru as _index_ru)
templates.env.filters["status_ru"] = _status_ru
templates.env.filters["reject_ru"] = _reject_ru
```
ЗАМЕНИТЬ НА:
```
from app.services.labels import (status_ru as _status_ru, reject_ru as _reject_ru,
                                 lane_ru as _lane_ru, index_ru as _index_ru,
                                 source_ru as _source_ru, source_badge as _source_badge)
templates.env.filters["status_ru"] = _status_ru
templates.env.filters["source_ru"] = _source_ru
templates.env.filters["source_badge"] = _source_badge
templates.env.filters["reject_ru"] = _reject_ru
```

НАЙТИ:
```

    Такой домен доезжает до инбокса и живёт там до перепроверки: для lane='bid' воронка T1
    короткозамыкает лейном и приобретаемость на скоринге не судит вовсе. Держать его наверху
    как «срочный» — значит звать оператора решать судьбу покойника (ревью 2026-07-13).

    Проекция whois (free-date, `deadline_source == 'whois_projection'`) сюда НЕ годится:
    она значит «освободится, если не продлят», а не «окно ловли». Домен, дождавшийся своей
    проекции и реально дропнувшийся, приходит в инбокс с ПРОШЕДШЕЙ датой (при available=True
    свежего free-date нет, обновлять нечем) — и если прогон Score отстал больше чем на
    DROP_GRACE, свободный домен носил бы красное «окно закрыто, домен занят» на экране, с
    которого ИДУТ ПОКУПАТЬ. Та же ложь, против которой сделана подпись «ОСВОБОДИТСЯ*»."""
    from app.services.scoring import DROP_GRACE
    if (d.score_breakdown or {}).get("deadline_source") == "whois_projection":
        return False
    dl = _deadline_utc(d)
```
ЗАМЕНИТЬ НА:
```

    Такой домен доезжает до инбокса и живёт там до перепроверки: для lane='bid' воронка W2
    короткозамыкает лейном и приобретаемость на скоринге не судит вовсе. Держать его наверху
    как «срочный» — значит звать оператора решать судьбу покойника (ревью 2026-07-13).
    Дедлайн в v2 — только дата дропа из источника (DropCatch/Nominet): проекции whois больше нет
    (её давал TCI, удалён вместе с РФ)."""
    from app.services.scoring import DROP_GRACE
    dl = _deadline_utc(d)
```

НАЙТИ:
```
@router.get("/domains", response_class=HTMLResponse)
def domains_view(request: Request, db: Session = Depends(get_session)):
    """Инбокс решений: только то, где ждут ТЕБЯ. Полный реестр — /domains/pool."""
    from datetime import datetime, timedelta, timezone
```
ЗАМЕНИТЬ НА:
```
@router.get("/domains", response_class=HTMLResponse)
def domains_view(request: Request, lang: str | None = None, db: Session = Depends(get_session)):
    """Инбокс решений: только то, где ждут ТЕБЯ. Полный реестр — /domains/pool.

    `?lang=xx` — фильтр по языку прошлого сайта. Счётчик «на решении» и список языков — ДО
    фильтра; при выбранном языке форма пакета скрыта: пакет взял бы и невидимые домены других
    языков (находка 1.6)."""
    from datetime import datetime, timedelta, timezone
```

НАЙТИ:
```
    from app.services.scoring import (blind_reason, bulk_ok, history_evidence, history_note,
                                      history_verdict, stale_donors, DROP_GRACE)
    from app.services.transitions import dirty_reason
```
ЗАМЕНИТЬ НА:
```
    from app.services.scoring import (blind_reason, bulk_ok, emd_newreg, history_evidence,
                                      history_note, history_verdict, stale_donors, topic_far,
                                      DROP_GRACE)
    from app.services.settings import get_settings
    from app.services.transitions import dirty_reason
```

НАЙТИ:
```
    ready = db.execute(select(Domain).where(Domain.status == "approved").order_by(*order)).scalars().all()
    counts = _domain_counts(db)
```
ЗАМЕНИТЬ НА:
```
    ready = db.execute(select(Domain).where(Domain.status == "approved").order_by(*order)).scalars().all()
    inbox_total = len(inbox)
    langs = sorted({d.market_lang for d in inbox + ready if d.market_lang})
    if lang:
        inbox = [d for d in inbox if d.market_lang == lang]
        ready = [d for d in ready if d.market_lang == lang]
    counts = _domain_counts(db)
```

НАЙТИ:
```
                   history_evidence(d), bulk_ok(d), history_note(d)) for d in inbox],
        # окно дропа закрыто — купить уже нельзя. Домен уехал вниз и не «срочный», но выглядит
```
ЗАМЕНИТЬ НА:
```
                   history_evidence(d), bulk_ok(d), history_note(d)) for d in inbox],
        "inbox_total": inbox_total, "langs": langs, "f_lang": lang or "",
        # прошлая тема далека от VPN (инвариант 4) — пометка в инбоксе и в «Готовы к выкупу»
        "far_ids": {d.id for d in inbox + ready if topic_far(d)},
        # EMD-новорег с пустым архивом (R2-14) — нейтральное «архив пуст», а не «⚠ НЕ проверена»
        "newreg_ids": {d.id for d in inbox if emd_newreg(d)},
        # Р2: «пакет от скора» по умолчанию = «порог сильного кандидата» из /settings
        "bulk_default": get_settings()["approve_at"],
        # окно дропа закрыто — купить уже нельзя. Домен уехал вниз и не «срочный», но выглядит
```

НАЙТИ:
```
    """Живая раскладка исходов ЭТОГО прогона по domain_score_log — сколько уже отсеяно
    ДО дорогого Wayback (T0-T2: RD/whois/РКН-блэклист/safebrowsing) и сколько реально дошло
    до него (scored — Wayback пройден по определению; rejected/history_dirty — дошёл и там
```
ЗАМЕНИТЬ НА:
```
    """Живая раскладка исходов ЭТОГО прогона по domain_score_log — сколько уже отсеяно
    ДО дорогого Wayback (W0–W4: зоны/бренды, доступность, риск, ссылки) и сколько реально дошло
    до него (scored — Wayback пройден по определению; rejected/history_dirty — дошёл и там
```

НАЙТИ:
```
            by_reason[label] = by_reason.get(label, 0) + n
            # history_dirty и low_score рождаются ТОЛЬКО когда _funnel() прошёл ДО КОНЦА
```
ЗАМЕНИТЬ НА:
```
            by_reason[label] = by_reason.get(label, 0) + n
            # v2: too_young решает W5 (история, Р5), spam_anchors — W6 (после истории): оба уже
            # сожгли Wayback, как и history_dirty/low_score.
            # history_dirty и low_score рождаются ТОЛЬКО когда _funnel() прошёл ДО КОНЦА
```

НАЙТИ:
```
            # оператору успокаивающую (и неверную) картину (находка ревью 2026-07-20).
            if reason in ("history_dirty", "low_score"):
                reached_wayback += n
```
ЗАМЕНИТЬ НА:
```
            # оператору успокаивающую (и неверную) картину (находка ревью 2026-07-20).
            if reason in ("history_dirty", "too_young", "low_score", "spam_anchors"):
                reached_wayback += n
```

НАЙТИ:
```
    from app.services.scoring import bulk_ok
    rows = db.execute(select(Domain).where(Domain.status == "scored",
                                           Domain.score >= min_score)).scalars().all()
    ok = [d for d in rows if bulk_ok(d)]
    return ok, len(rows) - len(ok)
```
ЗАМЕНИТЬ НА:
```
    from app.services.scoring import bulk_ok
    from app.services.settings import get_settings
    from app.services.transitions import zone_closed
    allow = get_settings()["tld_allowlist"]          # одно чтение настроек на пакет
    rows = db.execute(select(Domain).where(Domain.status == "scored",
                                           Domain.score >= min_score)).scalars().all()
    # Зона вне белого списка (R2-19): политика не пустит такой домен в approved — пакет его не
    # берёт (иначе падал бы отказом политики) и считает в «пропущено».
    ok = [d for d in rows if not zone_closed(d, allow) and bulk_ok(d)]
    return ok, len(rows) - len(ok)
```

НАЙТИ:
```
        msg += f" · пропущено (историю не подтвердить или она грязная): {skipped} — их реши руками"
```
ЗАМЕНИТЬ НА:
```
        msg += (f" · пропущено (не все проверки пройдены, тема далека от VPN, EMD или зона вне "
                f"белого списка): {skipped} — их реши руками в строке")
```

НАЙТИ:
```
    from app.services.transitions import dirty_reason
    rows = db.execute(stmt.order_by(Domain.score.desc().nulls_last(),
```
ЗАМЕНИТЬ НА:
```
    from app.services.settings import get_settings
    from app.services.transitions import dirty_reason, zone_closed
    allow = get_settings()["tld_allowlist"]          # одно чтение настроек на страницу
    rows = db.execute(stmt.order_by(Domain.score.desc().nulls_last(),
```

НАЙТИ:
```
        "dirty_by_id": {d.id: _reject_ru(r) for d in rows if (r := dirty_reason(d)) is not None},
        "f_status": status or "", "f_min_score": "" if min_score is None else min_score,
```
ЗАМЕНИТЬ НА:
```
        "dirty_by_id": {d.id: _reject_ru(r) for d in rows if (r := dirty_reason(d)) is not None},
        # зона вне белого списка (R2-19): тот же предикат, что у политики, — кнопку «↩ вернуть в
        # approved» шаблон не рисует (она вела в гарантированный отказ)
        "closed_ids": {d.id for d in rows if d.status == "rejected" and zone_closed(d, allow)},
        "f_status": status or "", "f_min_score": "" if min_score is None else min_score,
```

**`backend/app/templates/domains.html`:**

НАЙТИ:
```
{% block content %}
{# Одна формулировка на обе таблицы (инбокс и пул) — иначе при правке разъедутся. #}
{% set projection_hint = "проекция whois: домен освободится в этот день, ЕСЛИ владелец его не продлит. Это не подтверждённый дроп — у продлеваемых доменов такая дата тоже есть." %}
```
ЗАМЕНИТЬ НА:
```
{% block content %}
```

НАЙТИ:
```
      <a class="fcell" href="/domains/pool?status=discovered" title="сырьё из фида, ещё не оценено"><div class="v">{{ counts.get('discovered', 0) }}</div><div class="k">найдено</div></a>
      <span class="fcell {{ 'gate' if inbox }}" title="ждут твоего решения — список ниже"><div class="v">{{ inbox|length }}</div><div class="k">на решении</div></span>
      <span class="fcell" title="одобрены — можно ставить в очередь выкупа"><div class="v">{{ ready|length }}</div><div class="k">одобрено</div></span>
```
ЗАМЕНИТЬ НА:
```
      <a class="fcell" href="/domains/pool?status=discovered" title="сырьё из фида, ещё не оценено"><div class="v">{{ counts.get('discovered', 0) }}</div><div class="k">найдено</div></a>
      <span class="fcell {{ 'gate' if inbox_total }}" title="ждут твоего решения — список ниже (все языки)"><div class="v">{{ inbox_total }}</div><div class="k">на решении</div></span>
      <span class="fcell" title="одобрены — можно ставить в очередь выкупа"><div class="v">{{ ready|length }}</div><div class="k">одобрено</div></span>
```

НАЙТИ:
```
      <button class="btn btn-acc" title="забрать свежие дропы из включённых источников">↻ Найти дропы</button></form>
    <form class="inline" method="post" action="/admin/refresh-prices">
      <button class="btn btn-sm" title="перечитать базовую цену бэкордера">цены</button></form>
    <span class="sep"></span>
    <form class="inline" method="post" action="/run/score" style="display:flex; gap:8px; align-items:center">
      <label class="f">оценить <input type="number" name="n" value="5" min="1" max="50" style="width:64px"></label>
      <button class="btn btn-acc" title="прогнать N найденных доменов через воронку: RD → whois-возраст → РКН/блэклист → эхо → история. ТОЛЬКО отсюда домены попадают «на решение» (~15-20 с на домен)">▶ Оценить домены</button>
    </form>
    <form class="inline" method="post" action="/run/score">
      <input type="hidden" name="n" value="100000">
      <button class="btn" title="оценить весь пул найденных; бюджет whois — max_whois_per_run на /settings. При тысячах доменов это часы">весь пул</button></form>
    <span class="sep"></span>
```
ЗАМЕНИТЬ НА:
```
      <button class="btn btn-acc" title="забрать свежие дропы из включённых источников">↻ Найти дропы</button></form>
    <span class="sep"></span>
    <form class="inline" method="post" action="/run/score" style="display:flex; gap:8px; align-items:center">
      <label class="f">оценить <input type="number" name="n" value="5" min="1" max="50" style="width:64px"></label>
      <button class="btn btn-acc" title="прогнать N найденных доменов через 6 волн: зоны/бренды → доступность (RDAP/whois) → риск (Web Risk) → ссылки (Ahrefs) → история и тема (Wayback + LLM) → анкоры финалистов. ТОЛЬКО отсюда домены попадают «на решение»">▶ Оценить домены</button>
    </form>
    <form class="inline" method="post" action="/run/score">
      <input type="hidden" name="n" value="100000">
      <button class="btn" title="оценить весь пул найденных; капы whois:43, Ahrefs W4/W6 и пол units — на /settings. При тысячах доменов это часы">весь пул</button></form>
    <span class="sep"></span>
```

НАЙТИ:
```
      <b>Поиск дропов</b> забирает освобождающиеся домены из включённых источников
      (см. <a href="/settings">/settings</a>), дедуплицирует и складывает как <b>найдено</b>.
      Ничего не оценивает и не покупает.<br>
      <b>Проверка</b> гоняет их по воронке дёшево→дорого: RD из фида → whois-возраст →
      РКН/блэклист → эхо в индексе → история Wayback → Ahrefs. Чистые и сильные — сразу
      <b>одобрено</b>, спорные попадают <b>к тебе на решение</b>, грязные — <b>отклонено</b>.<br>
      <b>Перепроверка занятости</b> сверяет whois'ом уже отобранных доноров: список протухает —
```
ЗАМЕНИТЬ НА:
```
      <b>Поиск дропов</b> забирает освобождающиеся домены из включённых источников
      (см. <a href="/settings">/settings</a>), режет чужие зоны и спам-дропы по бесплатному DR
      Ahrefs ещё на входе и складывает остальное как <b>найдено</b>. Ничего не оценивает и не покупает.<br>
      <b>Оценка</b> гоняет их по шести волнам дёшево→дорого: зоны/бренды → доступность (RDAP/whois) →
      риск (Web Risk) → ссылки (Ahrefs) → история и тема прошлого сайта (Wayback + LLM) → анкоры
      финалистов. Машина сама не одобряет ничего: проверенные приходят <b>к тебе на решение</b>,
      грязные и слабые — <b>отклонено</b>.<br>
      <b>Перепроверка занятости</b> сверяет whois'ом уже отобранных доноров: список протухает —
```

НАЙТИ:
```

{% if inbox %}
<div class="card" style="margin-bottom:12px">
  <form method="post" action="/domains/bulk-approve" style="display:flex; gap:12px; align-items:center; flex-wrap:wrap">
    <label class="f">Одобрить все со score ≥
      <input type="number" id="bulk-score" name="min_score" value="0.80" step="0.05" min="0" max="1" style="width:80px"></label>
    <button class="btn btn-acc" title="перевести подходящие домены в approved — это твоё решение, деньги не тратятся">✓ Одобрить пакет</button>
    <span class="hint">попадёт <b id="bulk-n">…</b> доменов</span>
    <span class="hint" style="color:var(--acc2); margin-left:auto">⚠ помеченные «вслепую» в пакет не попадают</span>
  </form>
```
ЗАМЕНИТЬ НА:
```

{# фильтр по языку прошлого сайта — ДО ветки пустого инбокса: иначе при пустом результате
   переключатель исчезает и вернуться некуда #}
{% if langs %}<div class="chips">
  <a class="chip {{ 'on' if not f_lang }}" href="/domains" title="все языки прошлого сайта">все языки</a>
  {% for l in langs %}<a class="chip {{ 'on' if f_lang == l }}" href="/domains?lang={{ l }}"
     title="только домены, чей прошлый сайт был на языке {{ l }}">{{ l }}</a>{% endfor %}
</div>{% endif %}

{% if inbox %}
{% if f_lang %}
<div class="card" style="margin-bottom:12px"><span class="hint" style="color:var(--acc2)">
  пакетное одобрение скрыто: выбран язык {{ f_lang }}, а пакет взял бы и домены других языков —
  <a href="/domains">все языки</a></span></div>
{% else %}
<div class="card" style="margin-bottom:12px">
  <form method="post" action="/domains/bulk-approve" style="display:flex; gap:12px; align-items:center; flex-wrap:wrap">
    <label class="f">Одобрить все со score ≥
      <input type="number" id="bulk-score" name="min_score" value="{{ '%.2f'|format(bulk_default) }}" step="0.05" min="0" max="1" style="width:80px"></label>
    <button class="btn btn-acc" title="перевести подходящие домены в approved — это твоё решение, деньги не тратятся">✓ Одобрить пакет</button>
    <span class="hint">попадёт <b id="bulk-n">…</b> доменов</span>
    <span class="hint" style="color:var(--acc2); margin-left:auto"
          title="«вслепую», анкоры не проверены, прошлая тема далека от VPN, EMD — только кнопкой в строке">⚠ непроверенные, далёкие по теме и EMD в пакет не попадают</span>
  </form>
```

НАЙТИ:
```
</script>
```
ЗАМЕНИТЬ НА:
```
</script>
{% endif %}
```

НАЙТИ:
```
        {% if d.acquire_deadline %}
          {# ДВА РАЗНЫХ ФАКТА под одной датой. Из фида (backorder delete_date / архив cctld)
             это дата дропа. Из whois TCI — ПРОЕКЦИЯ free-date «освободится, если не продлят»:
             она есть у КАЖДОГО занятого домена, даже у yandex.ru (живая проба 2026-07-20),
             и дропом не является. Подписывать их одинаково значит врать оператору на всём
             бездедлайновом пуле — тот же принцип, что у метки «оценён вслепую». #}
          {% set projected = (d.score_breakdown or {}).get('deadline_source') == 'whois_projection' %}
          {% if projected %}
            <div class="hint" style="font-size:10px; letter-spacing:.08em"
                 title="{{ projection_hint }}">ОСВОБОДИТСЯ*</div>
          {% else %}
            <div class="hint" style="font-size:10px; letter-spacing:.08em"
                 title="подтверждённая дата дропа из источника (backorder/cctld)">СРОК ДРОПА</div>
          {% endif %}
          <div style="font-weight:700; {{ 'color:var(--acc2)' if urgent else 'color:var(--mut)' }}">
```
ЗАМЕНИТЬ НА:
```
        {% if d.acquire_deadline %}
          <div class="hint" style="font-size:10px; letter-spacing:.08em"
               title="дата дропа из источника (DropCatch/Nominet)">СРОК ДРОПА</div>
          <div style="font-weight:700; {{ 'color:var(--acc2)' if urgent else 'color:var(--mut)' }}">
```

НАЙТИ:
```
        <span class="src-badge {{ 'src-bid' if d.lane=='bid' else 'src-free' }}"
              title="источник: {{ d.source or '—' }}">{{ {'backorder':'bo','cctld':'cc','reg_ru':'rg','sweb':'sw'}.get(d.source, '?') }}</span>{{ d.domain }}
        <a href="https://web.archive.org/web/*/{{ d.domain }}" target="_blank" rel="noopener"
```
ЗАМЕНИТЬ НА:
```
        <span class="src-badge {{ 'src-bid' if d.lane=='bid' else 'src-free' }}"
              title="источник: {{ d.source|source_ru or '—' }}">{{ d.source|source_badge }}</span>{{ d.domain }}
        <a href="https://web.archive.org/web/*/{{ d.domain }}" target="_blank" rel="noopener"
```

НАЙТИ:
```
          score <b>{{ '%.2f'|format(d.score|float) if d.score is not none else '—' }}</b> ·
          RD <b>{{ d.referring_domains if d.referring_domains is not none else '—' }}</b> ·
          {{ '%.0f'|format(d.age_years|float) if d.age_years is not none else '—' }} лет
        </div>
        {# «история чистая» — заявление о ФАКТЕ, а не отсутствие ошибок: его вправе нести только
```
ЗАМЕНИТЬ НА:
```
          score <b>{{ '%.2f'|format(d.score|float) if d.score is not none else '—' }}</b> ·
          DR <b>{{ '%.0f'|format(d.dr|float) if d.dr is not none else '—' }}</b>
          <a class="hint" href="https://ahrefs.com/" target="_blank" rel="noopener"
             title="лицензия Ahrefs: DR показывается только с подписью">Domain Rating by Ahrefs</a> ·
          RD <b>{{ d.referring_domains if d.referring_domains is not none else '—' }}</b> ·
          {{ '%.0f'|format(d.age_years|float) if d.age_years is not none else '—' }} лет
          {% if d.market_lang %}· язык <b>{{ d.market_lang }}</b>{% endif %}
        </div>
        {# тема прошлого сайта (W5, LLM) — инвариант 4: человек видит, чем домен был #}
        {% set bd = d.score_breakdown or {} %}
        {% if bd.get('emd') %}
          <div class="hint" style="color:var(--acc2)" title="новорег под ключ: ни ссылок, ни истории — скору не из чего складываться">EMD — решение за тобой</div>
        {% elif d.topic %}
          <div class="hint" title="тема прошлого сайта по снимкам Wayback (LLM). Политика Google «expired domain abuse»: далёкая от VPN прошлая тема — риск">тема: {{ d.topic }} · близость к VPN <b>{{ '%.0f'|format((d.topical_relevance or 0)|float * 100) }}%</b></div>
        {% elif bd.get('topic_unknown') %}
          <div class="hint">тема не определена</div>
        {% endif %}
        {% if d.id in far_ids %}
          <div class="hint" style="color:var(--acc2)" title="близость прошлой темы к VPN ниже 30%: такой домен одобряют только руками, глядя на тему — пакет его не берёт">⚠ прошлая тема далека от VPN</div>
        {% endif %}
        {# «история чистая» — заявление о ФАКТЕ, а не отсутствие ошибок: его вправе нести только
```

НАЙТИ:
```
        {% elif ok %}<div class="hint" style="color:var(--ok)">история чистая</div>
        {% else %}<div class="hint" style="color:var(--acc2)">история не подтверждена</div>{% endif %}
```
ЗАМЕНИТЬ НА:
```
        {% elif ok %}<div class="hint" style="color:var(--ok)">история чистая</div>
        {# история проверена и чиста, но пакет домен не берёт: далёкая тема или EMD (без балла) —
           причина названа строкой выше. «не подтверждена» здесь было бы неправдой. #}
        {% elif hist == 'clean' %}<div class="hint" style="color:var(--ok)">история чистая
          <span class="hint" style="color:var(--acc2)">· в пакет не идёт — решай сам</span></div>
        {# EMD-новорег (R2-14): пустой архив — норма, а не «история НЕ проверена» #}
        {% elif d.id in newreg_ids %}<div class="hint"
             title="Wayback не дал ни одного снимка (или ни один не открылся). Для EMD-новорега это норма: прошлого сайта нет. Пакет EMD не берёт — решаешь сам">архив пуст — новорег</div>
        {% else %}<div class="hint" style="color:var(--acc2)">история не подтверждена</div>{% endif %}
```

НАЙТИ:
```
</table>
</div>
{% else %}
<div class="card empty" style="text-align:center">
```
ЗАМЕНИТЬ НА:
```
</table>
</div>
{% elif f_lang %}
<div class="card empty">По языку {{ f_lang }} ничего нет — <a href="/domains">все языки</a>.</div>
{% else %}
<div class="card empty" style="text-align:center">
```

НАЙТИ:
```
    <input type="hidden" name="n" value="5">
    <button class="btn btn-acc">▶ Запустить проверку</button></form>
  <form class="inline" method="post" action="/run/discovery">
```
ЗАМЕНИТЬ НА:
```
    <input type="hidden" name="n" value="5">
    <button class="btn btn-acc">▶ Оценить домены</button></form>
  <form class="inline" method="post" action="/run/discovery">
```

НАЙТИ:
```
      <td class="dom">{{ d.domain }} <span class="hint">score {{ '%.2f'|format(d.score|float) if d.score is not none else '—' }}</span>
        {# ГРЯЗЬ — прямо в строке, с которой идут покупать. Отмытый домен (approved с живым
```
ЗАМЕНИТЬ НА:
```
      <td class="dom">{{ d.domain }} <span class="hint">score {{ '%.2f'|format(d.score|float) if d.score is not none else '—' }}</span>
        {# тема прошлого сайта — и здесь, откуда идут покупать (инвариант 4) #}
        {% if d.topic %}<div class="hint">тема: {{ d.topic }} · близость к VPN <b>{{ '%.0f'|format((d.topical_relevance or 0)|float * 100) }}%</b></div>
        {% elif (d.score_breakdown or {}).get('emd') %}<div class="hint">EMD — новорег под ключ</div>{% endif %}
        {% if d.id in far_ids %}<div class="hint" style="color:var(--acc2)">⚠ прошлая тема далека от VPN</div>{% endif %}
        {# ГРЯЗЬ — прямо в строке, с которой идут покупать. Отмытый домен (approved с живым
```

НАЙТИ:
```
      <td class="num">{{ '%.0f'|format(d.acquire_price|float) if d.acquire_price is not none else '—' }}</td>
      <td class="num">{{ d.acquire_deadline.strftime('%d.%m') if d.acquire_deadline else '—' }}{%
        if d.acquire_deadline and (d.score_breakdown or {}).get('deadline_source') == 'whois_projection'
        %}<span class="hint"
          title="{{ projection_hint }}">*</span>{% endif %}
        {# отсюда ИДУТ ПОКУПАТЬ — метка «окно закрыто» тут дороже всего #}
```
ЗАМЕНИТЬ НА:
```
      <td class="num">{{ '%.0f'|format(d.acquire_price|float) if d.acquire_price is not none else '—' }}</td>
      <td class="num">{{ d.acquire_deadline.strftime('%d.%m') if d.acquire_deadline else '—' }}
        {# отсюда ИДУТ ПОКУПАТЬ — метка «окно закрыто» тут дороже всего #}
```

НАЙТИ:
```

{% set RU = {'low_rd': ('Мало доноров', 'thr'), 'too_young': ('Молодой домен', 'thr'),
             'low_score': ('Низкий скор', 'thr'), 'history_dirty': ('Грязная история', 'dirt'),
             'rkn': ('РКН', 'dirt'), 'blacklist': ('Блэклист', 'dirt'),
             'feed_flag': ('Флаг источника', 'dirt'),
             'not_acquirable': ('Занят', 'taken')} %}
<dialog id="why" class="modal">
```
ЗАМЕНИТЬ НА:
```

{# thr — режет порог (крутится на /settings); dirt — грязь (не трогать, в оборот не вернуть);
   taken — нельзя: занят, чужая зона, чужой бренд, архив РФ. Легаси v1 (rkn, safebrowsing) — грязь. #}
{% set RU = {'low_rd': ('Мало доноров', 'thr'), 'too_young': ('Молодой домен', 'thr'),
             'low_score': ('Низкий скор', 'thr'), 'history_dirty': ('Грязная история', 'dirt'),
             'blacklist': ('Блэклист / Web Risk', 'dirt'), 'spam_anchors': ('Спам-анкоры', 'dirt'),
             'feed_flag': ('Флаг источника', 'dirt'), 'rkn': ('РКН (v1)', 'dirt'),
             'safebrowsing': ('Safe Browsing (v1)', 'dirt'),
             'not_acquirable': ('Занят', 'taken'), 'tld_closed': ('Зона не наша', 'taken'),
             'trademark': ('Чужой бренд', 'taken'), 'legacy_ru': ('РФ (архив v1)', 'taken')} %}
<dialog id="why" class="modal">
```

НАЙТИ:
```
      ({{ (reasons_thr / reasons_total * 100)|round|int if reasons_total else 0 }}%): можно ослабить</span>
    <span><i class="sw k-dirt"></i> грязь/РКН — не трогать</span>
    <a class="btn btn-acc btn-sm" href="/settings" style="margin-left:auto">⚙ настроить пороги →</a>
```
ЗАМЕНИТЬ НА:
```
      ({{ (reasons_thr / reasons_total * 100)|round|int if reasons_total else 0 }}%): можно ослабить</span>
    <span><i class="sw k-dirt"></i> грязь — не трогать</span>
    <span><i class="sw k-taken"></i> нельзя — занят, чужая зона, бренд или архив РФ</span>
    <a class="btn btn-acc btn-sm" href="/settings" style="margin-left:auto">⚙ настроить пороги →</a>
```

**`backend/app/templates/pool.html`:**

НАЙТИ:
```
    <span class="badge b-discovered">{{ 'discovered'|status_ru }}</span><span class="who">машина</span>
      <span class="desc">найден в фиде, ещё не оценён — ждёт ▶ Запуск проверки</span>
    <span class="badge b-scored">{{ 'scored'|status_ru }}</span><span class="who">машина</span>
      <span class="desc">оценён, но не дотянул до авто-одобрения — реши сам: ✓ или ✗</span>
    <span class="badge b-approved">{{ 'approved'|status_ru }}</span><span class="who">машина / ты</span>
      {# «чистый» здесь было ЛОЖЬЮ (аудит F13): approve ставит и человек — в т.ч. домену, чью
         историю подтвердить не удалось. Статус говорит «решение принято», а не «проверено». #}
      <span class="desc">одобрен к выкупу — купи руками у провайдера, потом отметь 🛒.
        <b>«Одобрен» ≠ «история чистая»</b>: одобрить может и человек, в том числе домен, чью
        историю машина не подтвердила. Вердикт истории и снимки — в <a href="/domains">инбоксе M1</a></span>
    <span class="badge b-rejected">{{ 'rejected'|status_ru }}</span><span class="who">машина / ты</span>
      <span class="desc">отклонён — причина в колонке «статус» (фраза + <code>код</code>):
      <code>low_rd</code> мало доноров, <code>feed_flag</code> флаг источника, <code>too_young</code>
      моложе порога, <code>rkn</code> реестр РКН, <code>blacklist</code> блэклист,
      <code>history_dirty</code> грязная история, <code>low_score</code> низкий скор,
      <code>not_acquirable</code> нельзя купить (занят, не на бэкордере)</span>
    <span class="badge b-purchased">{{ 'purchased'|status_ru }}</span><span class="who">только ты</span>
```
ЗАМЕНИТЬ НА:
```
    <span class="badge b-discovered">{{ 'discovered'|status_ru }}</span><span class="who">машина</span>
      <span class="desc">найден источником или добавлен списком, ещё не оценён — ждёт «▶ Оценить домены»</span>
    <span class="badge b-scored">{{ 'scored'|status_ru }}</span><span class="who">машина</span>
      <span class="desc">прошёл шесть волн — машина сама не одобряет ничего, решаешь ты: ✓ или ✗</span>
    <span class="badge b-approved">{{ 'approved'|status_ru }}</span><span class="who">только ты</span>
      {# «чистый» здесь было ЛОЖЬЮ (аудит F13): approve ставит человек — в т.ч. домену, чью
         историю подтвердить не удалось. Статус говорит «решение принято», а не «проверено». #}
      <span class="desc">одобрен к выкупу — купи руками у провайдера, потом отметь 🛒.
        <b>«Одобрен» ≠ «история чистая»</b>: одобрить можно и домен, чью историю машина не
        подтвердила. Вердикт истории и снимки — в <a href="/domains">инбоксе M1</a></span>
    <span class="badge b-rejected">{{ 'rejected'|status_ru }}</span><span class="who">машина / ты</span>
      <span class="desc">отклонён — причина в колонке «статус» (фраза + <code>код</code>) по волнам:
      W0 <code>tld_closed</code> зона не наша, <code>trademark</code> чужой бренд, <code>feed_flag</code>
      флаг источника · W2 <code>not_acquirable</code> занят · W3 <code>blacklist</code> Web Risk / Spamhaus ·
      W4 <code>low_rd</code> мало доноров · W5 <code>history_dirty</code> грязная история,
      <code>too_young</code> моложе порога · W6 <code>spam_anchors</code> спам-анкоры · итог
      <code>low_score</code> низкий скор. Легаси v1: <code>legacy_ru</code> архив РФ-пула,
      <code>rkn</code> реестр РКН, <code>safebrowsing</code> Google Safe Browsing</span>
    <span class="badge b-purchased">{{ 'purchased'|status_ru }}</span><span class="who">только ты</span>
```

НАЙТИ:
```
    <span class="hint">score</span><span class="who">0–1</span>
      <span class="desc">сводная оценка качества: история + возраст + ссылки + индекс</span>
    <span class="hint">RD</span><span class="who">из фида</span>
      <span class="desc">referring domains — сколько доменов ссылается (авторитетность)</span>
    <span class="hint">echo</span><span class="who">SearXNG</span>
      <span class="desc">✓ = домен ещё встречается в поисковом индексе (был жив)</span>
    <span class="hint">флаги</span><span class="who">Wayback/РКН</span>
      <span class="desc">красные метки грязной истории — любой флаг = авто-reject</span>
    <span class="hint">⌛</span><span class="who">ссылка</span>
```
ЗАМЕНИТЬ НА:
```
    <span class="hint">score</span><span class="who">0–1</span>
      <span class="desc">сводная оценка: история + тема прошлого сайта + возраст + доноры + DR + анкоры + трафик</span>
    <span class="hint">RD</span><span class="who">Ahrefs (W4)</span>
      <span class="desc">referring domains — сколько доменов ссылается; на дропах раздут спамом</span>
    <span class="hint">DR</span><span class="who">Ahrefs</span>
      <span class="desc"><a href="https://ahrefs.com/" target="_blank" rel="noopener">Domain Rating by Ahrefs</a> —
      главный честный сигнал на спам-дропах</span>
    <span class="hint">флаги</span><span class="who">Wayback</span>
      <span class="desc">красные метки грязной истории — любой флаг = отказ (метка РКН — у легаси v1)</span>
    <span class="hint">⌛</span><span class="who">ссылка</span>
```

НАЙТИ:
```
{% if rows %}
<table>
  <thead><tr>
    <th>домен</th><th>статус</th><th>лейн</th><th class="num">цена</th><th>score</th><th class="num">RD</th>
    <th class="num">возраст</th><th>echo</th><th>флаги истории</th><th class="num">найден</th>
    <th>следующий шаг</th>
```
ЗАМЕНИТЬ НА:
```
{% if rows %}
<div class="hint" style="margin-bottom:6px">DR — <a href="https://ahrefs.com/" target="_blank" rel="noopener">Domain Rating by Ahrefs</a></div>
<table>
  <thead><tr>
    <th>домен</th><th>статус</th><th>лейн</th><th class="num">цена</th><th>score</th><th class="num">RD</th>
    <th class="num">возраст</th><th class="num" title="Domain Rating by Ahrefs">DR</th><th>флаги истории</th><th class="num">найден</th>
    <th>следующий шаг</th>
```

НАЙТИ:
```
      <td class="dom">
        {% set projected = (d.score_breakdown or {}).get('deadline_source') == 'whois_projection' %}
        <span class="src-badge {{ 'src-bid' if d.lane=='bid' else 'src-free' }}"
          title="источник: {{ d.source or '—' }} · лейн: {{ d.lane|lane_ru or '—' }}{% if d.acquire_deadline %} · {{ 'прогноз whois' if projected else 'дедлайн' }} {{ d.acquire_deadline.strftime('%d.%m') }}{% if projected %} (не подтверждённый дроп — освободится, ЕСЛИ владелец не продлит){% endif %}{% endif %}{% if d.acquire_price %} · цена {{ '%.0f'|format(d.acquire_price|float) }}{% endif %}{% if d.price_checked_at %} · обновлено {{ d.price_checked_at.strftime('%d.%m') }}{% endif %}"
          >{{ {'backorder':'bo','cctld':'cc','reg_ru':'rg','sweb':'sw'}.get(d.source, (d.source or '?')[:2]) }}</span>{{ d.domain }}
        <a href="https://web.archive.org/web/*/{{ d.domain }}" target="_blank" rel="noopener"
```
ЗАМЕНИТЬ НА:
```
      <td class="dom">
        <span class="src-badge {{ 'src-bid' if d.lane=='bid' else 'src-free' }}"
          title="источник: {{ d.source|source_ru or '—' }} · лейн: {{ d.lane|lane_ru or '—' }}{% if d.acquire_deadline %} · дедлайн {{ d.acquire_deadline.strftime('%d.%m') }}{% endif %}{% if d.acquire_price %} · цена {{ '%.0f'|format(d.acquire_price|float) }}{% endif %}{% if d.price_checked_at %} · обновлено {{ d.price_checked_at.strftime('%d.%m') }}{% endif %}"
          >{{ d.source|source_badge }}</span>{{ d.domain }}
        <a href="https://web.archive.org/web/*/{{ d.domain }}" target="_blank" rel="noopener"
```

НАЙТИ:
```
      <td class="num">{{ '%.1f'|format(d.age_years|float) if d.age_years is not none else '—' }}</td>
      <td>{% if d.indexed_echo %}<span style="color:var(--ok)" title="домен встречается в поисковом индексе">✓</span>{% else %}<span class="hint">—</span>{% endif %}</td>
      <td>
```
ЗАМЕНИТЬ НА:
```
      <td class="num">{{ '%.1f'|format(d.age_years|float) if d.age_years is not none else '—' }}</td>
      <td class="num">{{ '%.0f'|format(d.dr|float) if d.dr is not none else '—' }}</td>
      <td>
```

НАЙТИ:
```
{% else %}
<div class="empty">Пусто{{ ' по фильтру «' + f_status + '»' if f_status }}. Начни со станции
  «↻ Поиск дропов» — фид принесёт свежие дропы, затем «▶ Запуск проверки» их оценит.</div>
{% endif %}
```
ЗАМЕНИТЬ НА:
```
{% else %}
<div class="empty">Пусто{{ ' по фильтру «' + f_status + '»' if f_status }}. Начни с
  «↻ Найти дропы» в <a href="/domains">инбоксе M1</a> (или добавь домены списком выше), затем
  «▶ Оценить домены».</div>
{% endif %}
```

НАЙТИ:
```
          {% else %}
            <form class="inline" method="post" action="/domains/{{ d.id }}/set-status">
              <input type="hidden" name="status" value="approved">
              <button class="btn-sm" title="отсеян порогом, а не грязью — вернуть в approved (годен к покупке)">↩ вернуть в approved</button></form>
```
ЗАМЕНИТЬ НА:
```
          {% elif d.id in closed_ids %}
            {# R2-19: зона вне белого списка — политика (transitions.zone_closed) в approved не пустит #}
            <span class="hint" title="зоны нет в белом списке зон (/settings): v2 не судит и не выкупает такие домены. Добавишь зону в список — кнопка вернётся">зона не в белом списке — не возвращается</span>
          {% else %}
            <form class="inline" method="post" action="/domains/{{ d.id }}/set-status">
              <input type="hidden" name="status" value="approved">
              <button class="btn-sm" title="отсеян порогом, а не грязью — вернуть в approved (годен к покупке)">↩ вернуть в approved</button></form>
```

**`backend/app/templates/autopilot.html`** (R2-5):

НАЙТИ:
```
    ('score','Проверка','прогнать воронку скоринга; сильные и чистые уйдут в approved','до гейта курации', a.cap_score),
```
ЗАМЕНИТЬ НА:
```
    ('score','Проверка','прогнать шесть волн скоринга; проверенные придут к тебе на решение (scored) — одобряешь ты','до гейта курации', a.cap_score),
```

- [ ] **Шаг 4: Старые тесты — явный список**

Удалить (фича удалена вместе с производителем — whois TCI, Задача 9; тестов гейтов среди них нет):
- `test_inbox.py::test_projection_deadline_is_labelled_honestly`;
- `test_inbox.py::test_projection_never_shows_window_closed`;
- `test_inbox.py::test_pool_labels_projection_deadline_too`.

Не трогать (проходят и стерегут, что подпись проекции не вернётся): `test_inbox.py::test_feed_deadline_keeps_drop_label`,
`::test_expired_feed_deadline_still_shows_window_closed`, `::test_pool_keeps_plain_deadline_label_for_feed_date`;
`test_web_fixes.py::test_refresh_prices_route` (роут остаётся, снята только кнопка на карточке M1).

Переписать (R2-19: домен, который тест ждёт в пакете или с кнопкой «↩ вернуть в approved», — из зоны белого
списка; это тесты пакета и гейта курации — только переписать, смысл не меняется):

`backend/tests/test_inbox.py` (`test_bulk_preview_counts`):

НАЙТИ:
```
    _add(domain="clean.ru", age_years=10.0, status="scored", score=0.9, wayback_checked=True,
```
ЗАМЕНИТЬ НА:
```
    _add(domain="clean.com", age_years=10.0, status="scored", score=0.9, wayback_checked=True,
```

`backend/tests/test_history_verdict.py` (`test_stale_verdict_is_named_but_not_locked`):

НАЙТИ:
```
    _add(domain="stale.ru", age_years=10.0, status="scored", score=0.825, wayback_checked=True,
```
ЗАМЕНИТЬ НА:
```
    _add(domain="stale.com", age_years=10.0, status="scored", score=0.825, wayback_checked=True,
```

`backend/tests/test_aparser_envelope.py` (`_in_bulk` — `test_known_age_is_scored_and_lands_in_bulk` снова в пакете):

НАЙТИ:
```
        d = Domain(domain="bulk-probe.ru", source="backorder", status="scored", score=out["score"],
```
ЗАМЕНИТЬ НА:
```
        d = Domain(domain="bulk-probe.com", source="backorder", status="scored", score=out["score"],
```

`backend/tests/test_transitions.py` (`test_pool_offers_rescore_instead_of_return_for_dirt` — тест инварианта
«грязь кнопкой не возвращается»; отклонённый порогом домен `.ru` кнопку больше не получает, его случай —
`test_panel_inbox_v2.py::test_pool_does_not_offer_return_for_closed_zone`):

НАЙТИ:
```
    _add(domain="weak.ru", status="rejected", reject_reason="low_score", score=0.3)
```
ЗАМЕНИТЬ НА:
```
    _add(domain="weak.com", status="rejected", reject_reason="low_score", score=0.3)
```

- [ ] **Шаг 5: Тесты, сьют, линт; рендер**

Запуск: `cd backend && ../.venv/bin/python -m pytest tests/test_panel_inbox_v2.py tests/test_labels.py -v && ../.venv/bin/python -m pytest tests -q && cd .. && .venv/bin/python -m pyflakes backend/app backend/tests`
Ожидание: `17 passed`; весь сьют **874 passed** (865 + 11 + 1 − 3 удалённых в шаге 4), pyflakes пуст.

Рендер: новые элементы — только классы `base.html` (`chips`, `chip`, `on`, `card`, `empty`, `hint`,
`src-badge`, `k-thr`/`k-dirt`/`k-taken`, `sw`, `why-*`). Глазами — `/domains` и `/domains/pool` на 1366 px и
1024 px с несколькими тестовыми строками (EMD, тема, «далека от VPN», «анкоры не проверены», отказ
`spam_anchors` в легенде, фильтр языка). Настоящий рендер, не мокап.

- [ ] **Шаг 6: Коммит**

```bash
git add -A backend/app backend/tests
git commit -m "feat(panel): инбокс v2 — язык и тема, DR с атрибуцией Ahrefs, EMD, фильтр языка без невидимого пакета, легенда v2"
```

---

### Задача 16: Диагностика v2, финальная чистка, документация

**Что меняется и почему.**
- **`/diag` v2:** RDAP (бутстрап IANA), Google Web Risk, DropCatch, Nominet, registry.mx. Запись `ahrefs`
  (остаток units) уже есть — её добавила Задача 14. Фон обновляет диагностику каждые 5 минут
  (`main._diag_loop`), поэтому:
  - **DropCatch пингуется только при включённом источнике** (находка 1.7, инвариант 6: ToS не прочитан —
    никаких автоматических обращений; иначе 288 вызовов `GetFileUrl` в сутки и красный баннер от
    выключенного источника). Выключен — skip с причиной «источник выключен в /settings»;
  - **Web Risk — только наличие ключа** (находка 3.9): настоящий lookup каждые 5 минут съедал бы ~8,6 тыс.
    из 100 тыс. бесплатных вызовов в месяц; сбой самого Web Risk видно по `webrisk:` в воронке;
  - `RdapClient.ping()` бросает при недоступном IANA — `/diag` показывает «fail» с причиной (`_run_one`
    ловит `Exception`); тесты `/diag` подменяют `ping`/`run_diagnostics`, сеть не трогают.
- **Финальная чистка по точному критерию** (находка 5.15): нет импортов удалённых модулей; комментарии и
  докстринги с v1-логикой (backorder-фид, cctld, витрины reg.ru/sweb, `T0–T3`, `_funnel`, РКН-проверка) в
  `scoring.py`, `scoring_config.py`, моделях, `blacklist.py`, `diagnostics.py` переписаны под v2;
  `config.RKN_SOURCE_URL` и его строки в `.env.example` удалены, остальные тексты `.env.example` — под v2
  (находка 6.5); мёртвые методы и ключи фейков (`safebrowsing_check`, `archive_probe`, `indexed_echo`,
  `is_listed`, ключи `rkn`/`searxng`, параметры `rkn=`/`indexed_echo=`/`safebrowsing=`, `_safebrowsing_lock`)
  удалены из помощников тестов.
- **Законные остатки v1 не удаляются** (находка 1.2) — список в шаге 4. **Тесты гейтов и инвариантов
  (`test_transitions.py`, `test_acquisition*.py`, `test_bulk*`/тесты пакета в `test_inbox.py`,
  `test_history_verdict.py`, guard-тесты «перескор не отмывает» в `test_rescore.py`) не удалять:
  легаси-коды v1 (`rkn`, `cctld`, `feed_flags={"rkn": True}`) в них — законные фикстуры.**
- JSON API `/api/domains` — внутренний (находка 6.6): DR без подписи допустим там, где его не показывают
  людям; это записано в докстринге модуля.
- Проекция whois (`whois_projection`) уже удалена в Задаче 15 вместе с её тестами — здесь только проверка.
- **Красный баннер — только от критичных проверок** (находка R2-18): лежащий некритичный источник
  (Nominet, registry.mx, DropCatch, Spamhaus, Cloudflare/aaPanel до подпроекта 2) — строка на `/diag`, а
  не баннер на всех экранах: иначе он горел бы неделями, и его перестали бы читать.
- **Остатки v1, найденные вторым ревью** (находка R2-20): комментарии `panel.py` (`_FUNNEL_JOBS`,
  `_funnel_tally` — T0–T3b, `_funnel`), `models/domain_score_log.py`, `models/domain.py` (T1, «backorder
  delete_date»), `aparser.py` («запрет авто-approve»), подсказка `domains.html` «сырьё из фида»; подсказки о
  грязи в `domains.html`/`pool.html`/`queue.html` называют коды v2 (спам-анкоры, Web Risk, чужой бренд, чужая
  зона, архив РФ). Докстринг `panel._pool_counts` переписан уже в Задаче 14.
- **`docs/DEPLOY.md` — строка отката** (находка R2-4): откат миграции идёт раньше отката кода; откат v2 → v1
  — только восстановлением дампа (Задача 17).

**Files:**
- Modify:
  - `backend/app/services/diagnostics.py` — пинги v2, `_dropcatch_on`, `_SKIP_WHY`;
  - `backend/app/services/scoring.py`, `scoring_config.py`, `backend/app/models/settings.py`,
    `backend/app/models/domain.py`, `backend/app/integrations/blacklist.py` — комментарии/докстринги v1;
  - `backend/app/config.py`, `.env.example` — без `RKN_SOURCE_URL`, тексты v2;
  - `backend/app/api/domains.py` — докстринг «API внутренний»;
  - `backend/app/services/diag_cache.py` — баннер только от критичных проверок (R2-18);
  - `backend/app/api/panel.py`, `backend/app/models/domain_score_log.py`, `backend/app/integrations/aparser.py`,
    `backend/app/templates/domains.html`, `pool.html`, `queue.html` — остатки v1 (R2-20);
  - помощники тестов: `test_funnel.py`, `test_history_verdict.py`, `test_aparser_envelope.py`,
    `test_wayback_window.py`, `test_rescore.py`, `test_scoring_waves.py`, `test_m1_fixes.py`,
    `test_wayback_classify.py`, `test_whois.py`, `test_recheck_acquirability.py`;
  - `README.md` («Что где», «Ресурсы»); `docs/DONORS.md` (строка-указатель); `docs/DEPLOY.md` (строка отката);
    `docs/v2/CLAUDE.md` («Текущее состояние»).
- Test: `backend/tests/test_diag_v2.py` (создать).

**Interfaces:**
- Consumes: `.ping()` клиентов Задач 3–4, `WebRiskClient.configured` (Задача 3), `get_settings()["sources_enabled"]`.
- Produces: записи `_spec()` `rdap`, `webrisk`, `dropcatch`, `nominet`, `registry_mx`; `diagnostics._dropcatch_on() -> "1" | ""`;
  `diagnostics._SKIP_WHY`; `diag_cache.alert()["down"]` — только критичные проверки.

- [ ] **Шаг 1: Написать падающий тест** — `backend/tests/test_diag_v2.py`

```python
"""/diag v2: пингуются международные интеграции, РФ-проверок нет; выключенный DropCatch не
пингуется (ToS не прочитан, инвариант 6); Web Risk в фоне — только наличие ключа."""
from app.services import diagnostics


def _row(key):
    return next(s for s in diagnostics._spec() if s[0] == key)


def test_spec_keys_v2():
    keys = [s[0] for s in diagnostics._spec()]
    for k in ("ahrefs", "rdap", "webrisk", "dropcatch", "nominet", "registry_mx", "wayback", "aparser", "llm"):
        assert k in keys, k
    for k in ("rkn", "tci", "backorder"):
        assert k not in keys, k


def test_dropcatch_is_not_pinged_while_source_is_off(monkeypatch):
    """1.7: фон обновляет /diag каждые 5 минут — это 288 вызовов GetFileUrl в сутки при
    непрочитанном ToS. Выключенный источник — skip без сети, с честной причиной."""
    from app.integrations.dropcatch import DropCatchClient
    from app.services.settings import update_settings
    calls = []
    monkeypatch.setattr(DropCatchClient, "ping", lambda self: calls.append(1) or True)
    update_settings(sources_enabled={"dropcatch": False, "nominet": True, "mx": True, "emd": True})
    out = diagnostics.run_diagnostics(specs=[_row("dropcatch")])[0]
    assert out["status"] == "skip" and "выключен" in out["error"] and calls == []
    update_settings(sources_enabled={"dropcatch": True, "nominet": True, "mx": True, "emd": True})
    assert diagnostics.run_diagnostics(specs=[_row("dropcatch")])[0]["status"] == "ok" and calls == [1]


def test_webrisk_diag_checks_key_without_lookup(monkeypatch):
    """3.9: настоящий lookup каждые 5 минут съедал бы ~8,6 тыс. из 100 тыс. бесплатных вызовов
    в месяц. Фон проверяет только наличие ключа."""
    from app.config import settings
    from app.integrations.webrisk import WebRiskClient

    def _no_lookup(self, d):
        raise AssertionError("диагностика не вправе тратить lookup Web Risk")
    monkeypatch.setattr(WebRiskClient, "threats", _no_lookup)
    monkeypatch.setattr(settings, "WEBRISK_API_KEY", "k")
    assert diagnostics.run_diagnostics(specs=[_row("webrisk")])[0]["status"] == "ok"
    monkeypatch.setattr(settings, "WEBRISK_API_KEY", "")
    assert diagnostics.run_diagnostics(specs=[_row("webrisk")])[0]["status"] == "skip"


def test_rdap_ping_failure_is_red_not_a_crash(monkeypatch):
    """RdapClient.ping бросает при недоступном IANA — /diag показывает «fail» с причиной, а не 500."""
    from app.integrations.rdap import RdapClient

    def _down(self):
        raise RuntimeError("IANA down")
    monkeypatch.setattr(RdapClient, "ping", _down)
    out = diagnostics.run_diagnostics(specs=[_row("rdap")])[0]
    assert out["status"] == "fail" and "IANA down" in out["error"]


def test_red_banner_only_for_critical_checks(monkeypatch):
    """R2-18: глобальный красный баннер — только от критичных проверок. Лежащий Nominet или
    registry.mx (некритичные источники) виден на /diag, но баннером на всех экранах не горит."""
    from app.services import diag_cache
    monkeypatch.setattr(diag_cache, "_checks", None)          # кэш — модульный глобал: вернётся
    monkeypatch.setattr(diag_cache, "_checked_at", None)      # после теста, в чужой рендер не утечёт
    monkeypatch.setattr(diag_cache, "run_diagnostics", lambda: [
        {"key": "nominet", "label": "Nominet", "status": "fail", "critical": False},
        {"key": "registry_mx", "label": "registry.mx", "status": "fail", "critical": False},
        {"key": "wayback", "label": "Wayback", "status": "fail", "critical": True}])
    diag_cache.refresh()
    assert diag_cache.alert()["down"] == ["Wayback"]
```

- [ ] **Шаг 2: Запустить — убедиться, что падает**

Запуск: `cd backend && ../.venv/bin/python -m pytest tests/test_diag_v2.py -v`
Ожидание: `5 failed` — записей `rdap`/`webrisk`/`dropcatch` в `_spec()` нет (`AssertionError: rdap`,
`StopIteration` у остальных); баннер собирает и некритичные (`['Nominet', 'registry.mx', 'Wayback']`).

- [ ] **Шаг 3: Реализовать диагностику** — `backend/app/services/diagnostics.py` («найти → заменить»,
  фрагменты сверены: применённые по порядку к файлам после Задачи 15, дают ровно файлы с зелёным сьютом)

НАЙТИ:
```
возвращает статус (ok/fail/skip + latency + ошибка). Параллельно, с таймаутом,
чтобы страница не висела на медленном пинге (Wayback/RKN).
```
ЗАМЕНИТЬ НА:
```
возвращает статус (ok/fail/skip + latency + ошибка). Параллельно, с таймаутом,
чтобы страница не висела на медленном пинге (Wayback).
```

НАЙТИ:
```

def _spec():
```
ЗАМЕНИТЬ НА:
```

def _dropcatch_on() -> str:
    """DropCatch пингуется, только пока источник включён: ToS не прочитан (инвариант 6 — никаких
    автоматических обращений), а фон обновляет /diag каждые 5 минут — 288 вызовов в сутки."""
    try:
        from app.services.settings import get_settings
        return "1" if get_settings()["sources_enabled"].get("dropcatch") else ""
    except Exception:  # noqa: BLE001 — БД недоступна: не пингуем, причину покажет строка «db»
        return ""


# Причина skip, если она не «нет ключа».
_SKIP_WHY = {"dropcatch": "источник выключен в /settings (ToS не прочитан) — не пингуем"}


def _spec():
```

НАЙТИ:
```
         lambda: __import__("app.integrations.ahrefs", fromlist=["x"]).AhrefsClient().units_left()),
        ("blacklist", "Spamhaus DBL (только с DQS)", "M1 · спам-лист", settings.SPAMHAUS_DQS_KEY, "M1", False,
```
ЗАМЕНИТЬ НА:
```
         lambda: __import__("app.integrations.ahrefs", fromlist=["x"]).AhrefsClient().units_left()),
        ("rdap", "RDAP (IANA)", "M1 · доступность и возраст", "1", "M1", True,
         lambda: __import__("app.integrations.rdap", fromlist=["x"]).RdapClient().ping()),
        # только наличие ключа: настоящий lookup каждые 5 минут съедал бы ~8,6 тыс. из 100 тыс.
        # бесплатных вызовов в месяц. Сбой самого Web Risk видно по `webrisk:` в воронке.
        ("webrisk", "Google Web Risk", "M1 · риск (замена Safe Browsing)", settings.WEBRISK_API_KEY, "M1", False,
         lambda: __import__("app.integrations.webrisk", fromlist=["x"]).WebRiskClient().configured),
        ("dropcatch", "DropCatch", "M1 · дропы .com/.net/.org", _dropcatch_on(), "M1", False,
         lambda: __import__("app.integrations.dropcatch", fromlist=["x"]).DropCatchClient().ping()),
        ("nominet", "Nominet", "M1 · дропы .uk", "1", "M1", False,
         lambda: __import__("app.integrations.nominet", fromlist=["x"]).NominetClient().ping()),
        ("registry_mx", "registry.mx", "M1 · удалённые .mx", "1", "M1", False,
         lambda: __import__("app.integrations.registry_mx", fromlist=["x"]).RegistryMxClient().ping()),
        ("blacklist", "Spamhaus DBL (только с DQS)", "M1 · спам-лист", settings.SPAMHAUS_DQS_KEY, "M1", False,
```

НАЙТИ:
```
    if not need_cred:
        return {**base, "status": "skip", "ms": None, "error": "нет кредов в .env"}
    t0 = time.monotonic()
```
ЗАМЕНИТЬ НА:
```
    if not need_cred:
        return {**base, "status": "skip", "ms": None, "error": _SKIP_WHY.get(key, "нет кредов в .env")}
    t0 = time.monotonic()
```

`backend/app/services/diag_cache.py` (находка R2-18):

НАЙТИ:
```
        down = [c for c in _checks
                if c["key"] not in _NON_EXTERNAL and c["status"] == "fail"]
```
ЗАМЕНИТЬ НА:
```
        # Только КРИТИЧНЫЕ (R2-18): лежащий некритичный источник (Nominet, registry.mx, DropCatch,
        # Spamhaus, Cloudflare/aaPanel до подпроекта 2) — строка на /diag, а не баннер на всех
        # экранах: иначе он горел бы неделями, и его перестали бы читать.
        down = [c for c in _checks
                if c["key"] not in _NON_EXTERNAL and c.get("critical") and c["status"] == "fail"]
```

- [ ] **Шаг 4: Финальная чистка**

**4.1. Конфиг и `.env.example`.**

`backend/app/config.py`:

НАЙТИ:
```

    # rkn — источник реестра (antizapret primary; z-i заморожен 2025-10)
    RKN_SOURCE_URL: str = "https://antizapret.prostovpn.org/domains-export.txt"

    # spamhaus/surbl — нужен свой резолвер (публичные 8.8.8.8/1.1.1.1 блокируются)
```
ЗАМЕНИТЬ НА:
```

    # spamhaus/surbl — нужен свой резолвер (публичные 8.8.8.8/1.1.1.1 блокируются)
```

`.env.example`:

НАЙТИ:
```

# --- Метрики: FREE-путь (см. docs/api/README.md) ---
# Скоринг на бесплатных сигналах: Wayback + РКН + Spamhaus + Ahrefs DR/backlinks/
# referring-domains (через A-Parser Rank::Ahrefs + RuCapcha, см. docs/superpowers/
# specs/2026-07-08-ahrefs-dr-design.md). OpenPageRank — DEPRECATED, удалён.
AHREFS_API_KEY=
```
ЗАМЕНИТЬ НА:
```

# --- Метрики (v2, docs/v2/02-m1-discovery-scoring-spec.md) ---
# Ahrefs API v3: бесплатный DR на входе discovery, batch-analysis (W4) и анкоры (W6) — units в
# месяце ограничены, капы и пол остатка — на /settings. Ключ — свой, не MCP-коннектор чата.
AHREFS_API_KEY=
```

НАЙТИ:
```

# --- backorder.ru (M1 discovery — публичный/без auth; login нужен для ВЫКУПА в M2) ---
BACKORDER_LOGIN=
```
ЗАМЕНИТЬ НА:
```

# --- backorder.ru (выкуп M2 до подпроекта 2; в discovery v2 не используется) ---
BACKORDER_LOGIN=
```

НАЙТИ:
```

# --- РКН источник (M1 hard-reject) ---
RKN_SOURCE_URL=https://antizapret.prostovpn.org/domains-export.txt   # primary (z-i заморожен 2025-10)

# --- Spamhaus / SURBL (M1 hard-flag) — нужен СВОЙ резолвер (публичные 8.8.8.8/1.1.1.1 блокируются) ---
DNS_RESOLVER=172.28.0.53          # unbound из docker-compose.yml (сеть combine); пусто = системный
SPAMHAUS_DQS_KEY=                 # опц. free DQS-ключ (≤100k/день) как альтернатива своему резолверу
WEBRISK_API_KEY=
```
ЗАМЕНИТЬ НА:
```

# --- Риск W3: Google Web Risk (без ключа — «риск не проверен») + Spamhaus DBL только с DQS ---
DNS_RESOLVER=172.28.0.53          # unbound из docker-compose.yml (сеть combine); пусто = системный
SPAMHAUS_DQS_KEY=                 # DQS-ключ: без него Spamhaus в воронке не зовётся (зеркало для коммерции запрещено)
WEBRISK_API_KEY=
```

**4.2. Комментарии и докстринги с v1-логикой** (поведение не меняется).

`backend/app/services/scoring.py`:

НАЙТИ:
```
#
# `delete_date` в фиде backorder — ДАТА без времени ("2026-07-08", см. docs/api/backorder.md),
# и discovery._parse_deadline превращает её в 00:00 UTC дня дропа. Значит уже в 00:01 того же
# дня условие «дедлайн в будущем» ложно — а домен ещё зарегистрирован: реестр освобождает его
# в течение дня. Без этого запаса перепроверка отбраковывала бы дроп РОВНО В ТОТ ДЕНЬ, когда
# его можно ловить, то есть выбрасывала бы самые ценные домены. Запас покрывает и полуночное
```
ЗАМЕНИТЬ НА:
```
#
# Дата дропа у DropCatch — ДАТА без времени (`Drop Date`), discovery кладёт её как 00:00 UTC дня
# дропа. Значит уже в 00:01 того же дня условие «дедлайн в будущем» ложно — а домен ещё
# зарегистрирован: реестр освобождает его в течение дня. Без этого запаса перепроверка отбраковывала бы дроп РОВНО В ТОТ ДЕНЬ, когда
# его можно ловить, то есть выбрасывала бы самые ценные домены. Запас покрывает и полуночное
```

НАЙТИ:
```

# Как часто перепробовать домен, у которого дедлайна НЕТ (витрины reg.ru/sweb дропов дату не
# отдают; у cctld она может не разобраться из имени архива).
#
```
ЗАМЕНИТЬ НА:
```

# Как часто перепробовать домен, у которого дедлайна НЕТ (registry.mx, ручной список и EMD даты
# дропа не несут).
#
```

НАЙТИ:
```

# whois упал, но возраст всё-таки известен — из Wayback (`age_source='wayback'`, фолбэк в
# _funnel). Прежний текст здесь ЛГАЛ ровно в том состоянии, где показывался: он утверждал, что
# гейт «слишком молодой» не применялся, — а он применялся (_funnel сравнивает Wayback-возраст с
# min_age_years). Правда в другом: возраст по архиву — это НИЖНЯЯ оценка (первый снимок не
```
ЗАМЕНИТЬ НА:
```

# whois упал, но возраст всё-таки известен — из Wayback (`age_source='wayback'`, волна истории).
# Прежний текст здесь ЛГАЛ ровно в том состоянии, где показывался: он утверждал, что гейт
# «слишком молодой» не применялся, — а он применялся (W5 сравнивает возраст по архиву с
# min_age_years). Правда в другом: возраст по архиву — это НИЖНЯЯ оценка (первый снимок не
```

НАЙТИ:
```

    ЕДИНСТВЕННОЕ место, где решается «можно ли ещё купить». Его зовут и воронка (T1, при
    первом скоринге), и перепроверка (recheck_acquirability, потом) — двух версий правды
```
ЗАМЕНИТЬ НА:
```

    ЕДИНСТВЕННОЕ место, где решается «можно ли ещё купить». Его зовут и воронка (W2, при
    первом скоринге), и перепроверка (recheck_acquirability, потом) — двух версий правды
```

НАЙТИ:
```
    'waiting' — домен занят СЕЙЧАС, и это нормально: дроп ещё не наступил. Так выглядит
    любой backorder-кандидат до своей delete_date.
    'taken' — занят, и ждать больше нечего: дедлайн с запасом прошёл (домен продлили или
```
ЗАМЕНИТЬ НА:
```
    'waiting' — домен занят СЕЙЧАС, и это нормально: дроп ещё не наступил. Так выглядит
    любой bid-кандидат (DropCatch/Nominet) до даты дропа.
    'taken' — занят, и ждать больше нечего: дедлайн с запасом прошёл (домен продлили или
```

НАЙТИ:
```
        #   bid  — «занят» это НОРМА, домен ждёт своего дропа;
        #   NULL — лейн НЕИЗВЕСТЕН (записи старше коммита 69ef659, сырые витрины), и принимать
        #          незнание за «домен свободного лейна» нельзя. Ровно так на живом боксе утекли
```
ЗАМЕНИТЬ НА:
```
        #   bid  — «занят» это НОРМА, домен ждёт своего дропа;
        #   NULL — лейн НЕИЗВЕСТЕН (ручной список: лейн определит RDAP), и принимать
        #          незнание за «домен свободного лейна» нельзя. Ровно так на живом боксе утекли
```

НАЙТИ:
```
def scorable(now):
    """SQL-условие «этот домен МОЖЕТ пройти T1 прямо сейчас» — фильтр выборки score_pending.

    Без него воронка платит whois'ом за ответ, который уже знает. Не-bid домен до своего дропа
    ГАРАНТИРОВАННО занят (реестр освобождающихся на то и реестр), вердикт вернёт `waiting`, домен
    останется discovered — и следующий прогон купит тот же ответ заново. Пока такие домены
    терминально уезжали в rejected (баг с lane=NULL), пул не копился; теперь cctld везёт дедлайн,
    и весь реестр (~9.5 тыс.) законно ждёт дропа неделями. Один `весь пул` выжигал бы
    max_whois_per_run на одних и тех же строках с нулевым продвижением.

    Берём, значит, только тех, у кого есть шанс:
      · lane='bid' — backorder: T1 короткозамкнут лейном, whois нужен ради возраста;
      · дроп НАСТУПИЛ (`deadline <= now`) — сегодня whois впервые может сказать «свободен».
```
ЗАМЕНИТЬ НА:
```
def scorable(now):
    """SQL-условие «этот домен МОЖЕТ пройти W2 прямо сейчас» — фильтр выборки score_pending.

    Без него воронка платит whois'ом за ответ, который уже знает. Не-bid домен до своего дропа
    ГАРАНТИРОВАННО занят, вердикт вернёт `waiting`, домен останется discovered — и следующий
    прогон купит тот же ответ заново: один `весь пул` выжигал бы max_whois_per_run на одних и
    тех же строках с нулевым продвижением.

    Берём, значит, только тех, у кого есть шанс:
      · lane='bid' — DropCatch/Nominet: W2 короткозамкнут лейном, RDAP/whois нужен ради возраста;
      · дроп НАСТУПИЛ (`deadline <= now`) — сегодня whois впервые может сказать «свободен».
```

НАЙТИ:
```
      · дедлайна НЕТ — раз в RECHECK_EVERY. Здесь одним шансом обойтись нельзя: «занят сегодня»
        без даты дропа не говорит ничего про день освобождения, и домен (вся популяция
        reg.ru/sweb) никогда не увидел бы собственного дропа.
    """
```
ЗАМЕНИТЬ НА:
```
      · дедлайна НЕТ — раз в RECHECK_EVERY. Здесь одним шансом обойтись нельзя: «занят сегодня»
        без даты дропа не говорит ничего про день освобождения, и домен (ручной список без
        лейна) никогда не увидел бы собственного дропа.
    """
```

НАЙТИ:
```
                 links_budget=None, run: int | None = None, deep_budget=None) -> dict:
    """Полная воронка для ОДНОГО домена — внешний контракт идентичен дореформенному:
    та же сигнатура, та же форма ответа. Внутри строит батч из ОДНОГО FunnelState и
    прогоняет его через тот же волновой конвейер, что и score_pending (Task 9) —
    волны на батче размера 1 линеаризуются в тот же порядок стадий, что был у _funnel."""
    from app.db import SessionLocal
```
ЗАМЕНИТЬ НА:
```
                 links_budget=None, run: int | None = None, deep_budget=None) -> dict:
    """Полная воронка для ОДНОГО домена (кнопка «▶ перепроверить»): батч из ОДНОГО FunnelState
    через тот же волновой конвейер, что и score_pending. Капы по умолчанию не действуют (None),
    пол остатка units — действует."""
    from app.db import SessionLocal
```

НАЙТИ:
```
        #
        # RD есть только у backorder; у cctld/витрин он NULL — значит по RD домен, дропающийся
        # СЕГОДНЯ, лёг бы вперемешку с кулдаун-пулом, и при n=5 пул вытеснял бы его НИКОГДА не
        # доскоренным. Но и голая дата ASC неверна: «самая ранняя» — это ПРОТУХШИЙ дедлайн
        # месячной давности, то есть дроп, который мы уже упустили. Он встал бы впереди
        # сегодняшнего и жёг бы полный дорогой путь (whois+РКН+Wayback ≈ 60 с) на покойника —
        # для lane='bid' воронка его даже не отбракует (T1 короткозамкнут лейном).
        expired = and_(Domain.acquire_deadline.is_not(None),
```
ЗАМЕНИТЬ НА:
```
        #
        # RD до W4 неизвестен (его даёт Ahrefs) — значит по RD домен, дропающийся СЕГОДНЯ, лёг
        # бы вперемешку с бездатным пулом, и при n=5 пул вытеснял бы его НИКОГДА не доскоренным.
        # Но и голая дата ASC неверна: «самая ранняя» — это ПРОТУХШИЙ дедлайн месячной давности,
        # то есть дроп, который мы уже упустили. Он встал бы впереди сегодняшнего и жёг бы
        # платный путь (Ahrefs + Wayback) на покойника — для lane='bid' воронка его даже не
        # отбракует (W2 короткозамкнут лейном).
        expired = and_(Domain.acquire_deadline.is_not(None),
```

НАЙТИ:
```
                # внутри уже мог реально закоммитить в БД часть states волной(ами) РАНЬШЕ той,
                # на которой прилетела отмена (T0/whois/risk/history each пишут в БД сразу по
                # завершении своей волны, до общего возврата). Если считать done=0 в этом
```
ЗАМЕНИТЬ НА:
```
                # внутри уже мог реально закоммитить в БД часть states волной(ами) РАНЬШЕ той,
                # на которой прилетела отмена (t0/avail/risk/links/history пишут в БД сразу по
                # завершении своей волны, до общего возврата). Если считать done=0 в этом
```

НАЙТИ:
```

    ЗАЧЕМ. Скоринг решает приобретаемость ОДИН раз (T1) и больше к ней не возвращается.
    Но список доноров протухает: домен, одобренный неделю назад, сегодня может быть уже
```
ЗАМЕНИТЬ НА:
```

    ЗАЧЕМ. Скоринг решает приобретаемость ОДИН раз (W2) и больше к ней не возвращается.
    Но список доноров протухает: домен, одобренный неделю назад, сегодня может быть уже
```

НАЙТИ:
```
            # получить отметку, иначе он вечно висит в голове nulls_first-очереди и выедает весь
            # бюджет: если таких доменов больше бюджета (а это ровно авария «фид сменил формат
            # delete_date»), перепроверка никогда не дойдёт до остального списка и молча выродится
            # в no-op. Статус не трогаем — домен остаётся кандидатом; счётчик unknown в сводке
```
ЗАМЕНИТЬ НА:
```
            # получить отметку, иначе он вечно висит в голове nulls_first-очереди и выедает весь
            # бюджет: если таких доменов больше бюджета (а это ровно авария «источник сменил формат
            # даты дропа»), перепроверка никогда не дойдёт до остального списка и молча выродится
            # в no-op. Статус не трогаем — домен остаётся кандидатом; счётчик unknown в сводке
```

НАЙТИ:
```
def _wave_history(states: list, clients: dict, st: dict, run) -> None:
    """T3 — Wayback-история, конкурентно на весь выживший после risk пул. Конкурентность
    жёстко 4 — вежливость к archive.org, некрутящаяся константа (не /settings)."""
```
ЗАМЕНИТЬ НА:
```
def _wave_history(states: list, clients: dict, st: dict, run) -> None:
    """W5 — Wayback-история и тема, конкурентно на весь выживший после links пул. Конкурентность
    жёстко 4 — вежливость к archive.org, некрутящаяся константа (не /settings)."""
```

НАЙТИ:
```
def _commit_result(state: FunnelState, run, st: dict) -> dict:
    """Записать итог ОДНОГО FunnelState в БД — прямой перенос хвоста сегодняшнего
    score_domain() (после вызова _funnel, было строки 684-811), но принимает state
    вместо только что вычисленного sig/reject внутри той же функции: волны финализируют
    домен в момент его выхода из конвейера (см. _run_waves), не в конце одной функции.
```
ЗАМЕНИТЬ НА:
```
def _commit_result(state: FunnelState, run, st: dict) -> dict:
    """Записать итог ОДНОГО FunnelState в БД: волны финализируют домен в момент его выхода из
    конвейера (см. _run_waves), не в конце одной функции.
```

НАЙТИ:
```
        # СИГНАЛЫ ПИШЕМ ТОЛЬКО ИЗ ПРОВЕРОК, КОТОРЫЕ В ЭТОМ ПРОГОНЕ РЕАЛЬНО ОТРАБОТАЛИ —
        # НЕ blind overwrite. Воронка выходит рано на разных волнах (T0 не зовёт вообще
        # ничего, whois-волна — только whois); РКН/блэклист/Wayback при таком выходе не
        # исполнялись, sig о них молчит. Безусловный `setattr` отсюда отмывал бы грязь:
        # домен, отклонённый за РКН, после рескора терял бы ВСЕ улики (rkn_listed=None) и
        # снова становился чистым для политики — кнопка реабилитации сработала бы не «по
```
ЗАМЕНИТЬ НА:
```
        # СИГНАЛЫ ПИШЕМ ТОЛЬКО ИЗ ПРОВЕРОК, КОТОРЫЕ В ЭТОМ ПРОГОНЕ РЕАЛЬНО ОТРАБОТАЛИ —
        # НЕ blind overwrite. Воронка выходит рано на разных волнах (W0 не зовёт вообще
        # ничего, W2 — только RDAP/whois); риск/ссылки/Wayback/анкоры при таком выходе не
        # исполнялись, sig о них молчит. Безусловный `setattr` отсюда отмывал бы грязь:
        # домен, отклонённый за блэклист, после рескора терял бы ВСЕ улики (blacklisted=None) и
        # снова становился чистым для политики — кнопка реабилитации сработала бы не «по
```

`backend/app/services/scoring_config.py` (`PREFILTER["min_dr_proxy"]` не читает никто — удаляется):

НАЙТИ:
```

# Stage B — light pre-filter (drop obvious garbage before the heavy Wayback pass).
# Lenient on RD: the backorder feed already gives >=1 donor, and the project takes
# domains for clean history, NOT for link juice.
PREFILTER = {
    "min_referring_domains": 1,   # from feed `links`
    "min_dr_proxy": 0.0,          # Ahrefs DR 0..100; 0 = don't gate on it
}
```
ЗАМЕНИТЬ НА:
```

# W4 — порог доноров (refdomains из Ahrefs batch). Мягкий: проект берёт домены за чистую
# историю, а не за «сок»; DR режет спам-дропы ещё на входе discovery (MIN_DR ниже).
PREFILTER = {
    "min_referring_domains": 1,
}
```

НАЙТИ:
```
# Дефолты для рантайм-настроек (services/settings.py сидит из них при первом обращении).
MIN_AGE_YEARS = 3.0                                          # T1 whois-гейт: моложе — reject too_young
SOURCES_ENABLED = {"dropcatch": False, "nominet": True, "mx": True, "emd": True}  # dropcatch — после проверки ToS оператором
MAX_WHOIS_PER_RUN = 200        # кап whois-пробоев за один прогон проверки (защита от сырого cctld)
```
ЗАМЕНИТЬ НА:
```
# Дефолты для рантайм-настроек (services/settings.py сидит из них при первом обращении).
MIN_AGE_YEARS = 3.0                                          # W5: возраст по старшей дате; моложе — too_young
SOURCES_ENABLED = {"dropcatch": False, "nominet": True, "mx": True, "emd": True}  # dropcatch — после проверки ToS оператором
MAX_WHOIS_PER_RUN = 200        # кап whois:43 через A-Parser за прогон (зоны без RDAP; RDAP не капается)
```

`backend/app/models/settings.py`:

НАЙТИ:
```
    sources_enabled: Mapped[dict] = mapped_column(JSONB, default=dict)
    # веса критериев оценки донора (history_cleanliness/age/rd_proxy/indexed_echo/authority).
    # Были зашиты в scoring_config.WEIGHTS — оператор видел, ПО ЧЕМУ его судят, но не мог
```
ЗАМЕНИТЬ НА:
```
    sources_enabled: Mapped[dict] = mapped_column(JSONB, default=dict)
    # веса критериев оценки донора (семь компонентов v2 — scoring_config.WEIGHTS).
    # Были зашиты в scoring_config.WEIGHTS — оператор видел, ПО ЧЕМУ его судят, но не мог
```

`backend/app/models/domain.py`:

НАЙТИ:
```
    # funnel bookkeeping
    reject_reason: Mapped[str | None] = mapped_column(String(32))    # low_rd|feed_flag|too_young|rkn|blacklist|history_dirty|low_score|not_acquirable
    whois_created: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # дата регистрации (первичный возраст)
```
ЗАМЕНИТЬ НА:
```
    # funnel bookkeeping
    reject_reason: Mapped[str | None] = mapped_column(String(32))    # v2: tld_closed|trademark|feed_flag|not_acquirable|blacklist|low_rd|history_dirty|too_young|spam_anchors|low_score; легаси v1: rkn|safebrowsing|legacy_ru
    whois_created: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # дата регистрации (первичный возраст)
```

`backend/app/integrations/blacklist.py`:

НАЙТИ:
```
        (до рестарта контейнера) загонял бы КАЖДЫЙ последующий домен в путь «история не
        проверена», хотя Spamhaus восстановился через секунду. RknClient уже делает так же —
        при неудачной загрузке `_loaded_at` не выставляется, и следующий вызов ретраит."""
        with BlacklistClient._control_lock:
```
ЗАМЕНИТЬ НА:
```
        (до рестарта контейнера) загонял бы КАЖДЫЙ последующий домен в путь «история не
        проверена», хотя Spamhaus восстановился через секунду."""
        with BlacklistClient._control_lock:
```

`backend/app/api/domains.py` (находка 6.6):

НАЙТИ:
```
    GET /domains?status=scored&min_score=0.7  -> candidate domains, best score first.
"""
```
ЗАМЕНИТЬ НА:
```
    GET /domains?status=scored&min_score=0.7  -> candidate domains, best score first.

API ВНУТРЕННИЙ (скрипты оператора, LAN за Basic-auth): `dr` отдаётся без подписи. Лицензия
Ahrefs требует «Domain Rating by Ahrefs» со ссылкой там, где DR показывают людям, — это делает
панель; наружу этот JSON не публикуется (находка 6.6).
"""
```

**4.2а. Остатки v1, найденные вторым ревью** (находка R2-20; поведение не меняется, подсказки о грязи —
под коды v2):

`backend/app/api/panel.py` (находка R2-20 — комментарии `_funnel_tally` и `_FUNNEL_JOBS`):

НАЙТИ:
```
# Джобы, что прогоняют scoring.score_domain() по одному домену за раз (T0-T3b) — им и
# нужна живая раскладка исхода, у discovery/sweep/cf_sync domain_score_log вообще не пишется.
```
ЗАМЕНИТЬ НА:
```
# Джобы, что гонят домены через волны скоринга (W0–W6) — им и
# нужна живая раскладка исхода, у discovery/sweep/cf_sync domain_score_log вообще не пишется.
```

НАЙТИ:
```
    балл, см. scoring.py:799). Чипы стадий в jobCard() показывают только ТЕКУЩИЙ домен (и
    правильно — каждый начинает с RD, см. jobs._advance) — без этого счётчика оператор не
    видел ничего, что подтверждает: дешёвые стадии реально отсеивают быстро, а не «все домены
    идут по кругу». None, если для этого прогона ещё нет ни одной строки (свежий старт) —
```
ЗАМЕНИТЬ НА:
```
    балл, см. scoring._commit_result). Чипы волн в jobCard() показывают только ТЕКУЩУЮ волну —
    без этого счётчика оператор не видел ничего, что подтверждает: дешёвые волны реально
    отсеивают быстро, а не «все домены идут по кругу». None, если для этого прогона ещё нет ни
    одной строки (свежий старт) —
```

НАЙТИ:
```
            reached_wayback += n           # scored всегда прошёл T3 — таков порядок _funnel
```
ЗАМЕНИТЬ НА:
```
            reached_wayback += n           # scored всегда прошёл W5 (Wayback) — таков порядок волн
```

НАЙТИ:
```
            # history_dirty и low_score рождаются ТОЛЬКО когда _funnel() прошёл ДО КОНЦА
            # (вернул None) — history_dirty на самом T3, low_score позже, на самом _decide()
            # по уже посчитанному score (scoring.py:799: `reject_reason = reject or
```
ЗАМЕНИТЬ НА:
```
            # history_dirty и low_score рождаются ТОЛЬКО когда домен дошёл до истории (W5) —
            # history_dirty на самой W5, low_score позже, на самом _decide()
            # по уже посчитанному score (scoring._commit_result: `reject_reason = reject or
```

`backend/app/models/domain_score_log.py`:

НАЙТИ:
```
"""Append-only история решений scoring.score_domain() по домену. Каждый прогон
воронки (T0-T3) добавляет НОВУЮ строку — Domain.score_breakdown остаётся "последний
```
ЗАМЕНИТЬ НА:
```
"""Append-only история решений скоринга по домену. Каждый прогон
воронки (волны W0–W6) добавляет НОВУЮ строку — Domain.score_breakdown остаётся "последний
```

НАЙТИ:
```
    sig: Mapped[dict] = mapped_column(JSONB)                  # полный снимок sig из _funnel()
```
ЗАМЕНИТЬ НА:
```
    sig: Mapped[dict] = mapped_column(JSONB)                  # полный снимок sig после волн (_commit_result)
```

`backend/app/models/domain.py`:

НАЙТИ:
```
    acquire_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # дедлайн ловли (backorder delete_date)
```
ЗАМЕНИТЬ НА:
```
    acquire_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # дедлайн ловли (дата дропа источника или оценка по статусу RDAP)
```

НАЙТИ:
```
    # приобретаемость один раз (T1) и больше не возвращается — а список доноров протухает:
```
ЗАМЕНИТЬ НА:
```
    # приобретаемость один раз (W2) и больше не возвращается — а список доноров протухает:
```

`backend/app/integrations/aparser.py`:

НАЙТИ:
```
        вызывающий код (_funnel -> sig["errors"] -> метка «вслепую» + запрет авто-approve).
```
ЗАМЕНИТЬ НА:
```
        вызывающий код (W2: whois.probe -> sig["errors"] -> метка «вслепую», домен вне пакета).
```


`backend/app/templates/domains.html`:

НАЙТИ:
```
      <a class="fcell" href="/domains/pool?status=discovered" title="сырьё из фида, ещё не оценено"><div class="v">{{ counts.get('discovered', 0) }}</div><div class="k">найдено</div></a>
```
ЗАМЕНИТЬ НА:
```
      <a class="fcell" href="/domains/pool?status=discovered" title="найдены источниками или добавлены списком, ещё не оценены"><div class="v">{{ counts.get('discovered', 0) }}</div><div class="k">найдено</div></a>
```

НАЙТИ:
```
             (реестр РКН / блэклист / история / флаг фида), а «✓ одобрить» ниже снята. #}
```
ЗАМЕНИТЬ НА:
```
             (история / спам-анкоры / Web Risk / бренд / зона; легаси — РКН), а «✓ одобрить» ниже снята. #}
```

НАЙТИ:
```
                  title="объективный признак (реестр РКН, блэклист, грязная история или флаг источника) — не наш порог, ослабить его на /settings нечем. Портфель держится на чистой истории: такой домен не одобряем и не покупаем.">выкуп запрещён — грязь</span></div>
```
ЗАМЕНИТЬ НА:
```
                  title="объективный признак (грязная история, спам-анкоры, угроза Web Risk/Spamhaus, чужой VPN-бренд, флаг источника, зона вне белого списка или архив РФ; легаси v1 — реестр РКН, Safe Browsing) — не наш порог, ослабить его на /settings нечем. Портфель держится на чистой истории: такой домен не одобряем и не покупаем.">выкуп запрещён — грязь</span></div>
```

НАЙТИ:
```
                  title="домен был отклонён воронкой по объективному признаку (реестр РКН, блэклист, грязная история или флаг источника). Портфель держится на чистой истории — такой домен не покупаем. Вернуть его в оборот может только перепроверка, если проверки скажут, что он чист.">выкуп запрещён — грязь</span></div>
```
ЗАМЕНИТЬ НА:
```
                  title="домен был отклонён воронкой по объективному признаку (грязная история, спам-анкоры, угроза Web Risk/Spamhaus, чужой VPN-бренд, флаг источника, зона вне белого списка или архив РФ; легаси v1 — реестр РКН, Safe Browsing). Портфель держится на чистой истории — такой домен не покупаем. Вернуть его в оборот может только перепроверка, если проверки скажут, что он чист.">выкуп запрещён — грязь</span></div>
```

`backend/app/templates/pool.html`:

НАЙТИ:
```
                  title="РКН, блэклист, грязная история или флаг источника — это факт о домене, а не наш порог. Ослабить его нечем: портфель держится на чистой истории. Единственный путь назад — перепроверка: если проверки сегодня скажут «чист», воронка сама снимет отметку.">⛔ грязь — не возвращается</span>
```
ЗАМЕНИТЬ НА:
```
                  title="Грязная история, спам-анкоры, угроза Web Risk/Spamhaus, чужой VPN-бренд, флаг источника, зона вне белого списка или архив РФ (легаси v1 — реестр РКН, Safe Browsing): это факт о домене, а не наш порог. Ослабить его нечем: портфель держится на чистой истории. Единственный путь назад — перепроверка: если проверки сегодня скажут «чист», воронка сама снимет отметку.">⛔ грязь — не возвращается</span>
```

`backend/app/templates/queue.html`:

НАЙТИ:
```
               title="домен отклонён воронкой по объективному признаку (РКН, блэклист, грязная история, флаг источника). Портфель держится на чистой истории — этот домен не покупаем ни по какой ставке.">
```
ЗАМЕНИТЬ НА:
```
               title="домен отклонён воронкой по объективному признаку (грязная история, спам-анкоры, угроза Web Risk/Spamhaus, чужой VPN-бренд, флаг источника, зона вне белого списка или архив РФ; легаси v1 — реестр РКН, Safe Browsing). Портфель держится на чистой истории — этот домен не покупаем ни по какой ставке.">
```

**4.3. Мёртвые методы и ключи фейков в тестах** (их никто не зовёт с Задач 9–11; правки — только в
помощниках, ассерты не меняются).

`backend/tests/test_funnel.py`:

НАЙТИ:
```

def _clients(whois_dt=None, wayback=None, rkn=False, bl=False, indexed_echo=True,
             whois=None, whois_raises=False, safebrowsing=False):
    """whois: dict {"available":..., "created":...} (новый формат, приобретаемость известна
    явно). whois_dt: старый позиционный аргумент (только дата) — оборачивается в
    {"available": False, "created": whois_dt} (занят, но с датой регистрации — для тестов,
    доходящих до T2/T3 через lane="bid" на тестовом Domain). whois_raises=True — whois_probe
    бросает (недоступен). safebrowsing: True = зафлагован, False = чист, None = падает
    (исключение)."""
    pr = whois if whois is not None else {"available": False, "created": whois_dt}
```
ЗАМЕНИТЬ НА:
```

def _clients(whois_dt=None, wayback=None, bl=False, whois=None, whois_raises=False):
    """whois: dict {"available":..., "created":...} (новый формат, приобретаемость известна
    явно). whois_dt: старый позиционный аргумент (только дата) — оборачивается в
    {"available": False, "created": whois_dt} (занят, но с датой регистрации — для тестов,
    доходящих до W3+ через lane="bid" на тестовом Domain). whois_raises=True — whois_probe
    бросает (недоступен). bl — ответ Spamhaus (в воронке зовётся только с DQS-ключом)."""
    pr = whois if whois is not None else {"available": False, "created": whois_dt}
```

НАЙТИ:
```
            return pr
        def safebrowsing_check(self, dom):
            if safebrowsing is None:
                raise RuntimeError("safebrowsing timeout")
            return safebrowsing
    class _R:
        def is_listed(self, dom): return rkn
    class _B:
        def is_blacklisted(self, dom): return bl
    class _S:
        def indexed_echo(self, dom): return indexed_echo
    return {"aparser": _W(), "rkn": _R(), "blacklist": _B(), "searxng": _S(),
            "wayback": wayback,
```
ЗАМЕНИТЬ НА:
```
            return pr
    class _B:
        def is_blacklisted(self, dom): return bl
    return {"aparser": _W(), "blacklist": _B(),
            "wayback": wayback,
```

НАЙТИ:
```

def _clients_whois_raises(wb, rkn=False, bl=False, indexed_echo=True,
                          safebrowsing=False):
    """Как _clients, но whois_probe падает (недоступен) — для Finding-1 фолбэка."""
    class _W:  # aparser
        def whois_probe(self, dom): raise RuntimeError("whois timeout")
        def safebrowsing_check(self, dom):
            if safebrowsing is None:
                raise RuntimeError("safebrowsing timeout")
            return safebrowsing
    class _R:
        def is_listed(self, dom): return rkn
    class _B:
        def is_blacklisted(self, dom): return bl
    class _S:
        def indexed_echo(self, dom): return indexed_echo
    return {"aparser": _W(), "rkn": _R(), "blacklist": _B(), "searxng": _S(),
            "wayback": wb,
```
ЗАМЕНИТЬ НА:
```

def _clients_whois_raises(wb, bl=False):
    """Как _clients, но whois_probe падает (недоступен) — для Finding-1 фолбэка."""
    class _W:  # aparser
        def whois_probe(self, dom): raise RuntimeError("whois timeout")
    class _B:
        def is_blacklisted(self, dom): return bl
    return {"aparser": _W(), "blacklist": _B(),
            "wayback": wb,
```

НАЙТИ:
```
    old_enough = datetime.now(timezone.utc) - timedelta(days=1150)   # ~3.15 года, чуть старше порога
    out = scoring.score_domain(did, clients=_clients(old_enough, wb, indexed_echo=False))
    assert out["status"] == "rejected" and out["reject_reason"] == "low_score"
```
ЗАМЕНИТЬ НА:
```
    old_enough = datetime.now(timezone.utc) - timedelta(days=1150)   # ~3.15 года, чуть старше порога
    out = scoring.score_domain(did, clients=_clients(old_enough, wb))
    assert out["status"] == "rejected" and out["reject_reason"] == "low_score"
```

`backend/tests/test_history_verdict.py`:

НАЙТИ:
```
    """Воронка целиком на фейках: whois «занят + дата регистрации» (домен создаётся с lane='bid',
    поэтому T1 короткозамкнут лейном и «занят» — норма), РКН/блэклист чисты, эхо есть."""
    class _W:
        def whois_probe(self, dom): return {"available": False, "created": created}
        def safebrowsing_check(self, dom): return False
        def archive_probe(self, dom): return {"times": None, "first": None, "last": None}
    class _R:
        def is_listed(self, dom): return False
    class _B:
        def is_blacklisted(self, dom): return False
    class _S:
        def indexed_echo(self, dom): return True
    return {"aparser": _W(), "rkn": _R(), "blacklist": _B(), "searxng": _S(), "wayback": wayback,
            "webrisk": type("WR", (), {"configured": True, "threats": lambda self, d: []})(),
```
ЗАМЕНИТЬ НА:
```
    """Воронка целиком на фейках: whois «занят + дата регистрации» (домен создаётся с lane='bid',
    поэтому W2 короткозамкнут лейном и «занят» — норма), Web Risk и блэклист чисты."""
    class _W:
        def whois_probe(self, dom): return {"available": False, "created": created}
    class _B:
        def is_blacklisted(self, dom): return False
    return {"aparser": _W(), "blacklist": _B(), "wayback": wayback,
            "webrisk": type("WR", (), {"configured": True, "threats": lambda self, d: []})(),
```

`backend/tests/test_aparser_envelope.py`:

НАЙТИ:
```
def _sig(**kw) -> dict:
    """Домен-мечта: история проверена и чиста, ссылочная масса за потолком, эхо в индексе есть.
    Ровно так выглядит bid-домен, чей whois не ответил (lane известен из фида — воронка едет
    дальше и добирает возраст из архива)."""
    return {"wayback_checked": True, "prior_flags": dict(_CLEAN_FLAGS),
            "referring_domains": 5000, "indexed_echo": True, "errors": [], **kw}
```
ЗАМЕНИТЬ НА:
```
def _sig(**kw) -> dict:
    """Домен-мечта: история проверена и чиста, ссылочная масса за потолком.
    Ровно так выглядит bid-домен, чей whois не ответил (lane известен из фида — воронка едет
    дальше и добирает возраст из архива)."""
    return {"wayback_checked": True, "prior_flags": dict(_CLEAN_FLAGS),
            "referring_domains": 5000, "errors": [], **kw}
```

НАЙТИ:
```
            return whois
        def safebrowsing_check(self, dom): return False
        def archive_probe(self, dom): return {"times": None, "first": None, "last": None}
    class _R:
        def is_listed(self, dom): return False
    class _B:
        def is_blacklisted(self, dom): return False
    class _S:
        def indexed_echo(self, dom): return True
    return {"aparser": _W(), "rkn": _R(), "blacklist": _B(), "searxng": _S(),
            "wayback": wayback or _WaybackAged(),
```
ЗАМЕНИТЬ НА:
```
            return whois
    class _B:
        def is_blacklisted(self, dom): return False
    return {"aparser": _W(), "blacklist": _B(),
            "wayback": wayback or _WaybackAged(),
```

`backend/tests/test_wayback_window.py`:

НАЙТИ:
```
            return {"available": False, "created": datetime(2012, 1, 1, tzinfo=timezone.utc)}
    class _R:
        def is_listed(self, dom): return False
    class _B:
        def is_blacklisted(self, dom): return False
    class _S:
        def indexed_echo(self, dom): return True
    return {"aparser": _W(), "rkn": _R(), "blacklist": _B(), "searxng": _S(), "wayback": wayback,
            "ahrefs": type("Ah", (), {"units_left": lambda self: 2_000_000,
```
ЗАМЕНИТЬ НА:
```
            return {"available": False, "created": datetime(2012, 1, 1, tzinfo=timezone.utc)}
    class _B:
        def is_blacklisted(self, dom): return False
    return {"aparser": _W(), "blacklist": _B(), "wayback": wayback,
            "ahrefs": type("Ah", (), {"units_left": lambda self: 2_000_000,
```

`backend/tests/test_rescore.py` (guard-тест «W0 ничего не зовёт» остаётся guard-тестом — вместо РКН/эха он теперь сторожит Web Risk и Ahrefs):

НАЙТИ:
```
        "aparser": _Whois(),
        "rkn": type("R", (), {"is_listed": lambda self, d: False})(),
        "blacklist": type("B", (), {"is_blacklisted": lambda self, d: False})(),
        "searxng": type("S", (), {"indexed_echo": lambda self, d: True})(),
        "wayback": _CleanWayback(),
```
ЗАМЕНИТЬ НА:
```
        "aparser": _Whois(),
        "blacklist": type("B", (), {"is_blacklisted": lambda self, d: False})(),
        "wayback": _CleanWayback(),
```

НАЙТИ:
```
    Регрессией к моменту старта Задачи 17 эта часть уже НЕ является — она была зелёной и до
    правок этой задачи. Оставляем как страховку: T0 (feed_flag) выходит ДО whois вообще, а
    значит `sig` в этом прогоне не содержит ни `prior_flags`, ни `age_years`, ни `rkn_listed` —
```
ЗАМЕНИТЬ НА:
```
    Регрессией к моменту старта Задачи 17 эта часть уже НЕ является — она была зелёной и до
    правок этой задачи. Оставляем как страховку: W0 (feed_flag) выходит ДО whois вообще, а
    значит `sig` в этом прогоне не содержит ни `prior_flags`, ни `age_years`, ни `rkn_listed` —
```

НАЙТИ:
```
    class _MustNotBeCalled:
        """Ни один из T1-T3 клиентов не имеет права позваться — T0 отклоняет раньше всех."""
        def whois_probe(self, dom):
            raise AssertionError("T0 обязан отклонить ДО whois (feed_flags.rkn)")
        def is_listed(self, dom):
            raise AssertionError("T0 обязан отклонить ДО РКН")
        def is_blacklisted(self, dom):
            raise AssertionError("T0 обязан отклонить ДО блэклиста")
        def indexed_echo(self, dom):
            raise AssertionError("T0 обязан отклонить ДО эха")
        def classify_history(self, dom):
            raise AssertionError("T0 обязан отклонить ДО Wayback")

    guard = _MustNotBeCalled()
    clients = {"aparser": guard, "rkn": guard, "blacklist": guard, "searxng": guard,
               "wayback": guard}
```
ЗАМЕНИТЬ НА:
```
    class _MustNotBeCalled:
        """Ни один клиент W2–W6 не имеет права позваться — W0 отклоняет раньше всех."""
        def whois_probe(self, dom):
            raise AssertionError("W0 обязан отклонить ДО whois (feed_flags.rkn)")
        def threats(self, dom):
            raise AssertionError("W0 обязан отклонить ДО Web Risk")
        def is_blacklisted(self, dom):
            raise AssertionError("W0 обязан отклонить ДО блэклиста")
        def batch(self, doms):
            raise AssertionError("W0 обязан отклонить ДО Ahrefs")
        def classify_history(self, dom):
            raise AssertionError("W0 обязан отклонить ДО Wayback")

    guard = _MustNotBeCalled()
    clients = {"aparser": guard, "webrisk": guard, "blacklist": guard, "ahrefs": guard,
               "wayback": guard}
```

`backend/tests/test_scoring_waves.py`:

НАЙТИ:
```
        return {"available": self.available, "created": self.created}

    def safebrowsing_check(self, domain):
        return False
```
ЗАМЕНИТЬ НА:
```
        return {"available": self.available, "created": self.created}
```

НАЙТИ:
```
    s.sig.update({"wayback_checked": True, "prior_flags": {}, "age_years": 10,
                 "indexed_echo": True, "dr": None})
    out = scoring._commit_result(s, run=None, st={"approve_at": 0.7, "manual_review_at": 0.4})
```
ЗАМЕНИТЬ НА:
```
    s.sig.update({"wayback_checked": True, "prior_flags": {}, "age_years": 10,
                 "dr": None})
    out = scoring._commit_result(s, run=None, st={"approve_at": 0.7, "manual_review_at": 0.4})
```

НАЙТИ:
```
            return {"available": self.n % 2 != 0, "created": old}
        def safebrowsing_check(self, d): return False
    clients = {"aparser": _Ap(),
              "ahrefs": type("Ah", (), {"units_left": lambda self: 2_000_000,
                                        "batch": lambda self, ds: {d: {} for d in ds}})(),
              "rkn": type("R", (), {"is_listed": lambda self, d: False})(),
              "blacklist": type("B", (), {"is_blacklisted": lambda self, d: False})(),
              "searxng": type("S", (), {"indexed_echo": lambda self, d: True})(),
              "wayback": _FakeWayback(dirty=False, age_years=9.0),
              "_whois_lock": threading.Lock(), "_safebrowsing_lock": threading.Lock()}
    st = {"min_age_years": 3.0, "approve_at": 0.7, "manual_review_at": 0.4,
```
ЗАМЕНИТЬ НА:
```
            return {"available": self.n % 2 != 0, "created": old}
    clients = {"aparser": _Ap(),
              "ahrefs": type("Ah", (), {"units_left": lambda self: 2_000_000,
                                        "batch": lambda self, ds: {d: {} for d in ds}})(),
              "blacklist": type("B", (), {"is_blacklisted": lambda self, d: False})(),
              "wayback": _FakeWayback(dirty=False, age_years=9.0),
              "_whois_lock": threading.Lock()}
    st = {"min_age_years": 3.0, "approve_at": 0.7, "manual_review_at": 0.4,
```

НАЙТИ:
```
            return {"available": True, "created": datetime.now(timezone.utc) - timedelta(days=3650)}
        def safebrowsing_check(self, d): return False
    monkeypatch.setattr(scoring, "_make_clients", lambda: {
        "aparser": _Ap(),
        "rkn": type("R", (), {"is_listed": lambda self, d: False})(),
        "blacklist": type("B", (), {"is_blacklisted": lambda self, d: False})(),
        "searxng": type("S", (), {"indexed_echo": lambda self, d: True})(),
        "wayback": _FakeWayback(), "_whois_lock": threading.Lock(),
        "_safebrowsing_lock": threading.Lock()})
```
ЗАМЕНИТЬ НА:
```
            return {"available": True, "created": datetime.now(timezone.utc) - timedelta(days=3650)}
    monkeypatch.setattr(scoring, "_make_clients", lambda: {
        "aparser": _Ap(),
        "blacklist": type("B", (), {"is_blacklisted": lambda self, d: False})(),
        "wayback": _FakeWayback(), "_whois_lock": threading.Lock()})
```

`backend/tests/test_m1_fixes.py`:

НАЙТИ:
```
    out = compute_score({"wayback_checked": True, "prior_flags": {"spam": True},
                         "age_years": 12, "referring_domains": 500, "indexed_echo": True})
    assert out["status"] == "rejected" and out["score"] == 0.0
```
ЗАМЕНИТЬ НА:
```
    out = compute_score({"wayback_checked": True, "prior_flags": {"spam": True},
                         "age_years": 12, "referring_domains": 500})
    assert out["status"] == "rejected" and out["score"] == 0.0
```

НАЙТИ:
```
    out = compute_score({"wayback_checked": True, "prior_flags": {}, "age_years": 8,
                         "referring_domains": 3000, "indexed_echo": True, "dr": None})
    assert out["breakdown"]["components"]["authority"] == 0.0
```
ЗАМЕНИТЬ НА:
```
    out = compute_score({"wayback_checked": True, "prior_flags": {}, "age_years": 8,
                         "referring_domains": 3000, "dr": None})
    assert out["breakdown"]["components"]["authority"] == 0.0
```

НАЙТИ:
```
    without_dr = compute_score({"wayback_checked": True, "prior_flags": {}, "age_years": 8,
                                "referring_domains": 3000, "indexed_echo": True, "dr": None})
    with_dr = compute_score({"wayback_checked": True, "prior_flags": {}, "age_years": 8,
                             "referring_domains": 3000, "indexed_echo": True, "dr": 30})
    assert with_dr["breakdown"]["components"]["authority"] > 0.0
```
ЗАМЕНИТЬ НА:
```
    without_dr = compute_score({"wayback_checked": True, "prior_flags": {}, "age_years": 8,
                                "referring_domains": 3000, "dr": None})
    with_dr = compute_score({"wayback_checked": True, "prior_flags": {}, "age_years": 8,
                             "referring_domains": 3000, "dr": 30})
    assert with_dr["breakdown"]["components"]["authority"] > 0.0
```

НАЙТИ:
```
    строит клиентов сама через _make_clients(), поэтому патчим саму фабрику, иначе реальный
    боевой прогон (whois на 192.168.1.77, РКН antizapret, DNS к dbl.spamhaus.org, archive.org)
    дёргается для доменов #2,#3 (Finding-1, ревью Task 7)."""
    class _W:  # aparser
        def whois_probe(self, dom):
            return {"available": False, "created": None}
    class _R:
        def is_listed(self, dom): return False
    class _B:
        def is_blacklisted(self, dom): return False
    class _S:
        def indexed_echo(self, dom): return False
    class _WB:
```
ЗАМЕНИТЬ НА:
```
    строит клиентов сама через _make_clients(), поэтому патчим саму фабрику, иначе реальный
    боевой прогон (whois на 192.168.1.77, DNS к dbl.spamhaus.org, archive.org) дёргается для
    доменов #2,#3 (Finding-1, ревью Task 7)."""
    class _W:  # aparser
        def whois_probe(self, dom):
            return {"available": False, "created": None}
    class _B:
        def is_blacklisted(self, dom): return False
    class _WB:
```

НАЙТИ:
```
                    "first_seen": None, "age_years": 9.0, "wayback_checked": True, "sampled": 5}
    return {"aparser": _W(), "rkn": _R(), "blacklist": _B(), "searxng": _S(),
            "wayback": _WB()}
```
ЗАМЕНИТЬ НА:
```
                    "first_seen": None, "age_years": 9.0, "wayback_checked": True, "sampled": 5}
    return {"aparser": _W(), "blacklist": _B(), "wayback": _WB()}
```

`backend/tests/test_wayback_classify.py`:

НАЙТИ:
```
    out = compute_score({"wayback_checked": h["wayback_checked"], "prior_flags": h["prior_flags"],
                         "age_years": 16, "referring_domains": 2219, "indexed_echo": True})
    assert out["status"] == "scored", "одобряет только человек (Р2)"
```
ЗАМЕНИТЬ НА:
```
    out = compute_score({"wayback_checked": h["wayback_checked"], "prior_flags": h["prior_flags"],
                         "age_years": 16, "referring_domains": 2219})
    assert out["status"] == "scored", "одобряет только человек (Р2)"
```

НАЙТИ:
```
    out = compute_score({"prior_flags": pf, "wayback_checked": True,
                         "age_years": 15, "referring_domains": 2000, "indexed_echo": True})
    assert out["status"] == "rejected" and out["score"] == 0.0
```
ЗАМЕНИТЬ НА:
```
    out = compute_score({"prior_flags": pf, "wayback_checked": True,
                         "age_years": 15, "referring_domains": 2000})
    assert out["status"] == "rejected" and out["score"] == 0.0
```

`backend/tests/test_whois.py`:

НАЙТИ:
```
    return {"rdap": rdap, "aparser": ap,
            "rkn": type("R", (), {"is_listed": lambda self, d: False})(),
            "blacklist": type("B", (), {"is_blacklisted": lambda self, d: False})(),
            "searxng": type("S", (), {"indexed_echo": lambda self, d: True})(),
            "wayback": _FunnelWayback(),
```
ЗАМЕНИТЬ НА:
```
    return {"rdap": rdap, "aparser": ap,
            "blacklist": type("B", (), {"is_blacklisted": lambda self, d: False})(),
            "wayback": _FunnelWayback(),
```

`backend/tests/test_recheck_acquirability.py`:

НАЙТИ:
```
        "wayback": _Wayback(),
        "rkn": type("R", (), {"is_listed": lambda self, d: False})(),
        "blacklist": type("B", (), {"is_listed": lambda self, d: False})(),
        "searxng": type("S", (), {"indexed_echo": lambda self, d: False})(),
        "ahrefs": type("Ah", (), {"units_left": lambda self: 2_000_000,
```
ЗАМЕНИТЬ НА:
```
        "wayback": _Wayback(),
        "blacklist": type("B", (), {"is_blacklisted": lambda self, d: False})(),
        "ahrefs": type("Ah", (), {"units_left": lambda self: 2_000_000,
```

**4.4. Проверка — точный критерий.**

```bash
grep -rnE "import .*(rkn|whois_tci|cctld|regru_drops|sweb_drops|checktrust|metrics)\b" backend/app backend/tests | grep -v __pycache__
```

Ожидание: пусто.

```bash
grep -rniE "РКН|эхо|RKN_SOURCE|rkn|safebrowsing|indexed_echo|cctld|regru|sweb|whois_tci|TciWhois|archive_probe|ahrefs_probe|normalize_row|max_ahrefs_per_run|deadline_source|whois_projection|rd_proxy" backend/app backend/tests .env.example docker-compose.yml | grep -v __pycache__ | grep -v alembic/versions
```

Каждая строка вывода обязана попасть в один из **законных остатков** — всё остальное удалить или переписать:
- легаси-колонки моделей: `Domain.rkn_listed`, `Domain.indexed_echo`, `ScoringSettings.max_ahrefs_per_run`
  (строка колонки в `models/settings.py`); легаси-коды в комментариях колонок `Domain.reject_reason` и
  `Domain.feed_flags`;
- коды v1 в базе: `rkn`/`safebrowsing` в `DIRTY_REASONS`, `REJECT_RU`, легенде `domains.html`/`pool.html`,
  `SOURCE_RU`/`SOURCE_BADGE` (`cctld`, `reg_ru`, `sweb`); ветка `rkn_listed` в `transitions.dirty_reason` и
  самопроверка `__main__` там же; флаг «РКН» в `pool.html`;
- флаг фида `rkn` в `scoring._wave_t0` (`feed_flags`) и `feed_flags={"rkn": True}` в тестах;
- `integrations/registrar.py` и `REGRU_*` в `config.py`/`.env.example`/`diagnostics._SECRET_FIELDS` — M3
  (смена NS у .ru-регистратора), не M1;
- комментарии и подсказки об истории денежного гейта (аудит F9/F13: «отмытый РКН-домен») в `transitions.py`,
  `panel.py`, `acquisition.py`, `pipeline.py`, `base.html`, `domains.html`, `pool.html`, `queue.html`, и
  докстринги `scoring.bulk_ok`/`_decide`, говорящие о прошлом;
- имя справочника `docs/v2/research/cctld-markets.md` в подсказке `settings.html`;
- тесты: `test_transitions.py` (стр. с `rkn` — пример грязи), `test_migration_0025.py`, литерал v1 в
  `test_fresh_install.py`, `source="cctld"`/`"sweb"` как метка источника (`test_funnel.py`, `test_web_fixes.py`,
  `test_pricing.py`, `test_optimizator_acquisition.py`, `test_jobs_api.py`, `test_scoring_waves.py`),
  `reject_reason="rkn"` в `test_scoring_waves.py::test_commit_result_writes_rejected_and_log_row`, легаси-колонка
  `rkn_listed` и флаг фида в guard-тесте `test_rescore.py::test_early_t0_reject_does_not_erase_saved_evidence`,
  докстринги истории (`test_funnel.py`, `test_m1_fixes.py`, `test_whois.py`, `test_index_truth.py`,
  `test_inbox.py`), строка «РКН, Safe Browsing и эхо удалены» в `test_funnel_stages_and_job_message.py`,
  «эхо промпта» в `test_content_contract.py`, ключи v1 в `test_discovery_v2.py`, отрицательные проверки
  `max_ahrefs_per_run` в `test_settings.py::test_max_deep_per_run_zero_is_legal` и
  `test_panel_settings_v2.py`, коды v1 в тестах легенды и подписей (`test_panel_inbox_v2.py`, `test_labels.py`),
  `"rkn"` в `test_diag_v2.py`, упоминание SafeBrowsing в `test_blacklist_control.py`;
- старые миграции (`alembic/versions`).

`backorder.py`/`optimizator.py` и их тесты (выкуп M2) остаются до подпроекта 2 — в `discovery.py` и
`scoring.py` их нет: `grep -n "BackorderClient\|backorder\." backend/app/services/discovery.py backend/app/services/scoring.py` → пусто.

- [ ] **Шаг 5: Документация**

`README.md`:

НАЙТИ:
```
- `backend/app/services/` — бизнес-логика по модулям M1–M6 (+ `diagnostics.py` для /diag).
- `backend/tests/` — пайплайн-тесты на SQLite (гейты, скоринг, публикация, экраны).
```
ЗАМЕНИТЬ НА:
```
- `backend/app/services/` — бизнес-логика по модулям M1–M6 (+ `diagnostics.py` для /diag).
  - `discovery.py` — v2-поиск: DropCatch / Nominet / registry.mx / EMD / ручной список → белый
    список зон → бесплатный DR Ahrefs на входе (память `dr_seen`);
  - `scoring.py` — воронка из 6 волн (`t0 → avail → risk → links → history → deep`), скор и
    правила пакетного одобрения (`approved` ставит только человек);
  - `domain_filters.py`, `link_signals.py`, `history_llm.py` — чистые функции: зоны/бренды/EMD,
    спам-анкоры и пик трафика, разбор ответа LLM о языке и теме прошлого сайта.
- `backend/app/integrations/{ahrefs,rdap,webrisk,dropcatch,nominet,registry_mx}.py` — клиенты v2
  (Ahrefs API v3, RDAP с бутстрапом IANA, Google Web Risk, списки дропов).
- `backend/tests/` — пайплайн-тесты на SQLite (гейты, скоринг, публикация, экраны).
```

НАЙТИ:
```
## Ресурсы (сводка — детали в `docs/api/README.md`)
- **Метрики доменов** — бесплатный стек (Wayback / РКН / Spamhaus / RD из фида); DR-прокси OpenPageRank отпал (free-регистрация закрыта), платные API — опция позже.
- **Контент** — LiteLLM `192.168.1.77:4000` (mistral-large + ollama, без ключа).
- **SERP** — SearXNG `192.168.1.77:8080` (free); **whois/keywords** — A-Parser `:9091`.
- **Discovery** — backorder.ru (публичный фид, без auth). **GSC** исключён из v1 (индексация — ручной `site:`).
- **Прод-VPS** (origin для сайтов) — aaPanel, проверен вживую; Cloudflare — DNS + маскировка origin.
```
ЗАМЕНИТЬ НА:
```
## Ресурсы (сводка — детали в `docs/api/README.md`)
- **Метрики доменов (v2)** — Ahrefs API v3 (DR на входе, batch W4, анкоры W6 — под капами и полом units), RDAP/whois, Google Web Risk, Wayback + LLM-тема; Spamhaus — только с DQS.
- **Контент** — LiteLLM `192.168.1.77:4000` (mistral-large + ollama, без ключа).
- **SERP** — SearXNG `192.168.1.77:8080` (free); **whois/keywords** — A-Parser `:9091`.
- **Discovery (v2)** — DropCatch (после проверки ToS), Nominet (.uk), registry.mx, EMD, ручной список. **GSC** исключён (индексация — ручной `site:`).
- **Прод-VPS** (origin для сайтов) — aaPanel, проверен вживую; Cloudflare — DNS + маскировка origin.
```

`docs/DONORS.md` — первой строкой (и пустая строка после неё):

```
> v2 (2026-10-01): методика международной воронки — docs/v2/02-m1-discovery-scoring-spec.md. Ниже — v1 (.ru), история.
```

`docs/DEPLOY.md` (находка R2-4):

НАЙТИ:
```
- Откат: `git revert` + `up -d --build`. Данные БД в volume, миграции — только вперёд
  (для отката данных — `alembic downgrade`).
```
ЗАМЕНИТЬ НА:
```
- Откат: `git revert` + `up -d --build`. Данные БД в volume, миграции — только вперёд. Если откат
  задевает миграцию, схему откатывают РАНЬШЕ кода (`alembic downgrade <ревизия>` в контейнере
  нового кода): старый код не стартует на схеме, о ревизии которой не знает (`Can't locate revision`
  → backend в цикле перезапусков, кнопка git-pull недоступна). Откат v2 → v1 — только восстановлением
  дампа, снятого до обновления (`docs/v2/03-m1-plan.md`, Задача 17): `downgrade` миграции 0025 теряет
  решения курации v1.
```

`docs/v2/CLAUDE.md`, раздел «Текущее состояние» — первым абзацем:

```
**Подпроект 1 (M1) реализован** в ветке `feat/v2-m1-international`: 879 passed, pyflakes чист. Живой прогон
и калибровка — Задача 17 плана. JSON API `/api/domains` внутренний: DR там без подписи, атрибуция
«Domain Rating by Ahrefs» — в панели.
```

- [ ] **Шаг 6: Сьют, линт, коммит**

```bash
cd backend && ../.venv/bin/python -m pytest tests -q && cd .. && .venv/bin/python -m pyflakes backend/app backend/tests
git add backend/app backend/tests .env.example README.md docs/DONORS.md docs/DEPLOY.md docs/v2/
git commit -m "chore(v2): диагностика международных интеграций, чистка РФ-кода M1, документация"
```

Ожидание: **879 passed** (874 + 5 `test_diag_v2.py`), pyflakes пуст. `git add` — явным списком, не `-A`:
голый `-A` захватил бы посторонние `docs/*review*.md` и корневой `CLAUDE.md` (находка 5.6).

После этой задачи — **whole-branch ревью** (`combine-reviewer`, затем `superpowers:finishing-a-development-branch`).
Мерж в `main` и пуш — только по слову оператора.

---

### Задача 17: Живой прогон на боксе и калибровка (вместе с оператором)

Без кода, кроме шага 10б: это проверка «Готово, когда» из спеки (§9). Исполнитель-агент шаги на боксе не
выполняет — только ведёт оператора. Команды для оператора — PowerShell, из `D:\combine_machine`: без `~`,
без `#`-комментариев в строке с командой, без unix-путей (пути внутри контейнера в командах `docker compose`
— не про PowerShell). Сервис БД — `db`, пользователь и база — `portfolio`. Панель — Basic-auth из `.env`.

- [ ] **Шаг 0: Автопилот выключен на время деплоя и калибровки** (находка R2-5). До всего остального: на
  `/autopilot` снять мастер-тумблер «автопилот включён» и сохранить. Открыть в браузере
  `http://192.168.1.77:8000/api/jobs/live`: в `"jobs"` не должно быть задач — git-pull обрывает идущие, а
  повторный запуск оборванной задачи заблокирован, пока она не протухнет (`STALE_MIN` = 5 минут). Идёт
  задача — дождаться конца или нажать «✕ Отменить» на её карточке. Почему так: свип раз в час сам делает
  discovery и платный скоринг — до ключей и калибровки он тратил бы units вслепую; а при включённой
  автоочереди одобренный тобой .com уходил бы в заказ backorder, у которого нет тарифов для .com
  (подтвердить нельзя, отмена возвращает в `approved`, следующий свип снова ставит в очередь — цикл; деньги
  не уходят, но очередь засорена). Включать автопилот — только после шага 11.

- [ ] **Шаг 1: Бэкап базы ДО обновления кода** (находка R2-4). Миграция 0025 необратима по смыслу:
  `downgrade` теряет решения курации v1, а код v1 на базе с 0025 не стартует (`Can't locate revision
  '0025_v2_m1'` → backend в цикле перезапусков, кнопка git-pull недоступна). Дамп — через `-f` внутри
  контейнера, не через `>`: в PowerShell 5.1 перенаправление пишет UTF-16 и портит дамп.

```powershell
cd D:\combine_machine
docker compose exec -T db pg_dump -U portfolio -d portfolio -Fc -f /tmp/pre_v2.dump
docker compose cp db:/tmp/pre_v2.dump D:\pre_v2.dump
```

  Убедиться, что `D:\pre_v2.dump` есть и не пустой (`Get-Item D:\pre_v2.dump`). Заодно — что из базы v1
  зайдёт в воронку v2 (не-РФ домены; РФ миграция 0025 отправит в архив `legacy_ru`). Записать вывод:

```powershell
docker compose exec db psql -U portfolio -d portfolio -P pager=off -c "SELECT status, count(*) FROM domains WHERE NOT (domain LIKE '%.ru' OR domain LIKE '%.su' OR domain LIKE '%.xn--p1ai') GROUP BY status"
```

  **Откат v2 → v1 — только восстановлением дампа** (не `alembic downgrade`). Код возвращается тем же
  путём, каким его доставляет шаг 2 (git внутри контейнера, репозиторий смонтирован в `/repo`), но при
  остановленных backend и worker — поэтому одноразовым контейнером `run`:

```powershell
cd D:\combine_machine
docker compose stop backend worker
docker compose cp D:\pre_v2.dump db:/tmp/pre_v2.dump
docker compose exec -T db pg_restore -U portfolio -d portfolio --clean --if-exists /tmp/pre_v2.dump
docker compose run --rm --no-deps backend git -C /repo -c safe.directory=/repo checkout -f -B main c758b3e
docker compose up -d
```

  После отката кнопку «Обновить из git» в `/diag` не нажимать: `origin/main` уже v2 — pull вернёт его.

- [ ] **Шаг 2: Доставить код на бокс.** После мержа и пуша (по слову оператора) — кнопка git-pull в
  `/diag`. Backend сам выполнит `alembic upgrade head` (0025). Если `/diag` пишет, что нужен ребилд
  (зависимости не менялись — не должен), в PowerShell:

```powershell
cd D:\combine_machine
docker compose up -d --build
```

- [ ] **Шаг 3: Ключи и модель темы.** `WEBRISK_API_KEY` — если готов проект GCP (без ключа домены
  приходят «риск не проверен» и в пакет не попадают — это честно, не ошибка).

  **Модель для темы W5 (Р6).** Посмотреть модели LiteLLM бокса:

```powershell
(Invoke-RestMethod -Uri http://192.168.1.77:4000/v1/models).data.id
```

  Выбрать ollama-модель из списка (строка вида `ollama/<модель>`) и прописать её в `.env`:

```powershell
notepad D:\combine_machine\.env
```

  Строка `LLM_CLASSIFY_MODEL=ollama/<модель>` (пусто — классификатор берёт `LLM_MODEL`, mistral: разрешено
  оператором, но платно). Затем:

```powershell
cd D:\combine_machine
docker compose up -d
```

  **Живая проверка модели** (находка R2-17) — ДВАЖДЫ подряд: первый запуск холодный (ollama грузит модель),
  второй тёплый. Ожидание: оба меньше 30 с (у классификатора таймаут 30 с и одна попытка — дольше значит
  «тема не определена» на каждом домене) и непустой ответ. Холодный дольше 30 с, тёплый быстрее — терпимо
  (первый домен прогона уйдёт с «тема не определена»); тёплый дольше 30 с — модель слишком тяжёлая,
  выбрать полегче:

```powershell
docker compose exec backend python -c "import time;from app.integrations.llm import LlmClassifyClient as C;c=C();t=time.time();a=c.complete('Reply with one word','lorem ipsum dolor sit amet '*400,temperature=0);print(c.model,round(time.time()-t,1),a[:60])"
```

  **Живая проверка ключа Web Risk** (если ключ прописан). Ожидание: `True` (настоящий lookup `example.com`, 1
  из 100 тыс. бесплатных вызовов в месяц):

```powershell
docker compose exec backend python -c "from app.integrations.webrisk import WebRiskClient as W;print(W().ping())"
```

- [ ] **Шаг 3а: DR пачкой своим ключом** (перенесено из шага 0 Задачи 2, решение оператора 2026-10-02).
  Строго до первого discovery (шаг 6): проверить, что СВОЙ ключ приложения принимает бесплатный DR пачкой
  `targets[]` (через MCP-коннектор это снято 2026-10-01). В PowerShell:

```powershell
cd D:\combine_machine
docker compose exec backend python -c "import httpx;from app.config import settings as s;r=httpx.post('https://api.ahrefs.com/v3/public/domain-rating-free',headers={'Authorization':'Bearer '+s.AHREFS_API_KEY,'Accept':'application/json'},json={'targets':['example.com','nordvpn.com']},timeout=30);print(r.status_code);print(r.text[:600])"
```

  Ожидание: `200` и обе цели в ответе, вида
  `{"domain_rating":{"targets":[{"target":"example.com/","domain_rating":94.0},{"target":"nordvpn.com/","domain_rating":88.0}],"license":"…"}}`.
  Фикстуру `ahrefs_dr_free.json` этим ответом НЕ перезаписывать: на живом ответе с IDN держится тест
  IDNA-ключей.
  - **`401`** — ключ не вписан (до v2 он приложению был не нужен). Вписать в `.env` строку
    `AHREFS_API_KEY=<ключ API v3 из кабинета Ahrefs>` (`notepad D:\combine_machine\.env`), затем
    `docker compose up -d` и повторить команду.
  - **`200`, но в ответе одна цель из двух, или `4xx` с ключом, где ошибка про `targets`** — пачку API не
    принимает. Выключить в `/settings` источники Nominet и registry.mx и с ними discovery не запускать:
    при частичном ответе домены без DR запоминаются в `dr_seen` на 4 суток и выпадают. Остановиться и решить
    с оператором фолбэк из мастер-спеки §8: поштучный бесплатный DR с капом для Nominet и .mx (лимит
    60 запросов в минуту), DropCatch выключен навсегда. Это новый код — отдельная задача со своим ревью.
    EMD и ручной список DR не проходят: шаги 6–8 на них идут и без фолбэка.
  - Любой другой ответ (`403` с вписанным ключом, `5xx`, таймаут) — показать оператору текст ответа и
    разобраться вместе, не гадать.

- [ ] **Шаг 4: `/diag`.** Зелёные: ahrefs, rdap, nominet, registry_mx, wayback, aparser, llm. Web Risk —
  зелёный при ключе (проверяется только наличие ключа, без lookup) или «skip» без него. **DropCatch —
  «skip: источник выключен в /settings»**: до проверки ToS он не пингуется вовсе (инвариант 6). Spamhaus —
  «skip» без `SPAMHAUS_DQS_KEY`. Красный баннер вверху панели горит только от критичных проверок; лежащий
  Nominet или registry.mx виден здесь, на `/diag`.
- [ ] **Шаг 5: `/settings`.** Проверить дефолты v2 (DR 5, зоны, бренды, капы W4 500 / W6 20, пол units
  300 000, порог спам-анкоров 0.2). **Пороги v1 переехали из базы без проверки** (находка R2-17) — сверить
  каждый под смысл v2 и записать, что выставлено:
  - `min_referring_domains` — теперь RD из Ahrefs batch (W4), а не из фида backorder; на дропах RD раздут
    спамом;
  - `min_age_years` — теперь возраст по старшей из дат RDAP и первого снимка (W5), отказ — там же;
  - `approve_at` — теперь «порог сильного кандидата» (значение пакета по умолчанию), машина не одобряет;
  - `manual_review_at` — граница «на решение / отказ низкий скор» и отбор кандидатов W6;
  - `max_whois_per_run` — теперь кап только whois:43 через A-Parser (зоны без RDAP: .mx/.co …).

  **Остаток units виден** в станции Ahrefs («осталось units в месяце: N» — из кэша `/diag`, обновляется
  раз в 5 минут; «—» — кэша ещё нет, обнови `/diag`). Добавить один набор EMD, например
  `[{"market":"es-MX","lang":"es","keywords":["mejor vpn","vpn gratis"],"tlds":["com","mx"]}]`. Записать
  остаток units до прогона.
- [ ] **Шаг 6: Discovery** (кнопка «Найти дропы»; Nominet и registry.mx — только если шаг 3а прошёл).
  Записать водопад из карточки задачи: строк → наши зоны → новых → DR≥5 → сохранено, по каждому источнику;
  упавший источник виден там же.
- [ ] **Шаг 7: Оценить домены.** Записать водопад волн (`t0 → avail → risk → links → history → deep`) и
  расход units (до/после на `/settings`). Если в сообщении задачи «Ahrefs: остаток … < пола …» или «ключ
  AHREFS_API_KEY не задан» — платные волны пропущены, не-EMD домены даже не ходили в RDAP/Web Risk и
  дождутся следующего прогона. «Ahrefs W4: HTTPStatusError 401 …» — ключ не принят.
- [ ] **Шаг 8: Проверка глазами.** Открыть 3 домена «на решении» и 3 отклонённых. Сверить с Wayback (кнопка
  ⌛) и, при желании, с Ahrefs в браузере. Язык и тема правдоподобны? Спам-анкоры действительно спам?
  Счётчик «одобрено» сам не вырос (Р2: `approved` ставит только человек).
- [ ] **Шаг 9: whois:43 для .mx/.co** (находка R2-17). Источник ответа и ошибки — в журнале скоринга
  `domain_score_log` (у нерешённых доменов `score_breakdown` нет, журнал пишется на каждом исходе):

```powershell
cd D:\combine_machine
docker compose exec db psql -U portfolio -d portfolio -P pager=off -c "SELECT d.domain, d.status, l.outcome, l.sig->>'whois_source' AS src, l.sig->'errors' AS err FROM domain_score_log l JOIN domains d ON d.id = l.domain_id WHERE d.domain LIKE '%.mx' OR d.domain LIKE '%.co' ORDER BY l.id DESC LIMIT 30"
```

  Ожидание: `src = aparser` и осмысленная доступность (не сплошь `whois_unclear`/`whois:` в `err`). Если
  A-Parser не разбирает ответ .mx/.co — завести задачу «python-whois как фолбэк» (`research`: подходит, 1
  зависимость).
- [ ] **Шаг 10: Живые образцы Web Risk и metrics-history** (находки R2-3, R2-21; инвариант 7 — фикстуры
  этих двух ответов сняты по документации).

  **10а (оператор, на боксе).** После появления ключа Web Risk — два запроса, вывод каждого прислать в чат
  целиком. Тестовая malware-страница Google (ждём `MALWARE`) и `example.com` (ждём `{}`):

```powershell
cd D:\combine_machine
docker compose exec backend python -c "import json;from app.integrations.webrisk import WebRiskClient as W,URL,THREATS;w=W();r=w.request('GET',URL,params=[('threatTypes',t) for t in THREATS]+[('uri','http://testsafebrowsing.appspot.com/s/malware.html')],headers={'X-Goog-Api-Key':w.api_key});print(json.dumps(r.json()))"
docker compose exec backend python -c "import json;from app.integrations.webrisk import WebRiskClient as W,URL,THREATS;w=W();r=w.request('GET',URL,params=[('threatTypes',t) for t in THREATS]+[('uri','http://example.com/')],headers={'X-Goog-Api-Key':w.api_key});print(json.dumps(r.json()))"
```

  `metrics-history` своим ключом на один домен (одна платная выборка истории, ~60 строк) — вывод прислать
  целиком:

```powershell
docker compose exec backend python -c "import json;from app.integrations.ahrefs import AhrefsClient as A;c=A();r=c._request_once('GET',c.base_url+'/site-explorer/metrics-history',headers=c._headers(),params={'target':'nordvpn.com','mode':'subdomains','date_from':'2021-10-01','history_grouping':'monthly','select':'date,org_traffic'});print(json.dumps(r.json(),ensure_ascii=False))"
```

  **10б (разработчик, на Mac, в рабочей ветке; маленький кодовый шаг с тестом и коммитом).**
  - Ответы Web Risk — дословно в `backend/tests/fixtures/v2/webrisk_malware.json` и
    `backend/tests/fixtures/v2/webrisk_clean.json`; ответ `metrics-history` — дословно вместо
    `backend/tests/fixtures/v2/ahrefs_metrics_history.json`.
  - В конец `backend/tests/test_rdap_webrisk.py`:

```python


def test_webrisk_parses_live_samples(monkeypatch):
    """R2-3: живые ответы Web Risk (Задача 17) — тестовая malware-страница Google и example.com —
    разбираются парсером: угроза названа, чистый URL — пустой список, а не исключение."""
    c = WebRiskClient(api_key="k")
    for name, want in (("webrisk_malware.json", ["MALWARE"]), ("webrisk_clean.json", [])):
        body = json.loads((FX / name).read_text())
        monkeypatch.setattr(c, "request", lambda m, u, _b=body, **kw: httpx.Response(
            200, json=_b, request=httpx.Request(m, u)))
        assert c.threats("x.com") == want, name
```

  - Числа, завязанные на старую фикстуру `metrics-history`: `<S>` — `org_traffic` второй строки живого
    образца, `<P>` — максимум `org_traffic` по нему. `test_ahrefs_api.py::test_anchors_and_metrics_history_request_shape`:
    `assert hist[1]["org_traffic"] == 1850` → `== <S>`; `test_link_signals.py::test_peak_traffic`:
    `peak_traffic(hist) == 1850` → `== <P>`; `test_waves_v2.py::test_deep_clean_keeps_and_sets_peak`:
    `s.sig["peak_traffic"] == 1850` → `== <P>`.
  - Пометки «по документации, живьём не снят» убрать: в докстринге `integrations/webrisk.py` абзац
    «Формат ответа — ПО ДОКУМЕНТАЦИИ, живьём не снят …» → «Формат ответа снят живьём (Задача 17, фикстуры
    `webrisk_*.json`); разбор строгий: незнакомая форма — ValueError (W3 пишет `webrisk:ValueError`, домен
    «вслепую»), а не [] — тихое «чисто» отправило бы непроверенный домен в пакет.»; в докстринге
    `AhrefsClient.metrics_history` «Формат снят по документации, НЕ живьём (живой образец — Задача 17).
    Поэтому ответ …» → «Формат снят живьём (Задача 17, `ahrefs_metrics_history.json`). Ответ …»;
    двухстрочный комментарий `# Web Risk: JSON ниже — ПО ДОКУМЕНТАЦИИ …` в `test_rdap_webrisk.py` — удалить;
    в `docs/v2/CLAUDE.md` «Не сняты живьём … ответ Web Risk и `metrics-history` Ahrefs» → «сняты живьём в
    Задаче 17».
  - **Тест упал** — живой формат не тот, что в документации: правится парсер (`WebRiskClient.threats` /
    `AhrefsClient.metrics_history`) под образец, а не образец под парсер; строгость (незнакомая форма —
    исключение, не «чисто») сохраняется.
  - Сьют и линт: `cd backend && ../.venv/bin/python -m pytest tests -q && cd .. && .venv/bin/python -m pyflakes backend/app backend/tests`
    — **880 passed** (879 + 1), pyflakes пуст. Коммит:

```bash
git add backend/app/integrations/webrisk.py backend/app/integrations/ahrefs.py backend/tests docs/v2/CLAUDE.md
git commit -m "test(v2): живые образцы Web Risk и metrics-history Ahrefs — фикстуры и парсер"
```

- [ ] **Шаг 11: Калибровка и включение автопилота.** По водопадам подобрать `min_dr`, капы W4/W6, пол units,
  при необходимости `PBN_SUBNET_RATIO`, `TOPIC_FAR_BELOW` и `NORM["RD_FULL"]` в `scoring_config.py`. Записать
  итоговые значения и цифры прогона в «Текущее состояние» `docs/v2/CLAUDE.md`. Изменение кода
  (`scoring_config`) — отдельный маленький коммит с тестами. Затем `/autopilot`:
  - **`auto_queue` (автоочередь выкупа) — выключена до подпроекта 2**: backorder не умеет .com (см. шаг 0);
  - `cap_score` выбрать осознанно: units в месяц ≈ свипов в месяц × (`cap_score` × 25 на W4 + до
    `max_deep_per_run` × ~1,1 тыс. на W6); свип раз в час — это ~720 прогонов в месяц, пол остатка units
    остановит платные волны, но не раньше, чем месяц выбран до пола;
  - только после этого — мастер-тумблер «автопилот включён».

---

## Покрытие спеки (для ревьюера)

Спека — ревизия 3 (`02-m1-discovery-scoring-spec.md`), решения оператора и находки — `04-plan-review.md`.

| Спека (`02-…-spec.md`) | Задачи |
|---|---|
| §0а Авто-одобрения нет; гарды → правила пакета (`bulk_ok`), `approve_at` = «порог сильного кандидата» (Р2) | 8 (`_decide`), 13 (`bulk_ok`, e2e «в пакете»), 14 (подпись), 15 (дефолт пакета) |
| §0а Возраст — старшая из дат RDAP и первого снимка, `too_young` в W5 (Р5) | 9 (гейт в `_history_one`), 12 (не дублирует), 14 (счётчик превью) |
| §0а Память DR `dr_seen` на 4 суток (Р4) | 5 (таблица), 6 (`_dr_filter`) |
| §0а Пол остатка `units_floor` перед W4/W6 (Р3) | 5 (колонка), 11 (один раз в начале прогона, до W2/W3; сообщение задачи), 13 (W6), 14 (поле и остаток units) |
| §0а Скрипт языка прошлого сайта — не спам (Р1) | 13 |
| §0а Batch Ahrefs по `index` (IDN в Юникоде) | 2 |
| §0а Сбой Ahrefs в W4 → пачка unresolved (и строка домена без ответа — `ahrefs_missing`) | 11 |
| §0а Предохранители: статичная карта RDAP; RDAP, Web Risk, LLM — 3 сбоя подряд | 3, 9, 10, 12 |
| §2 Источники, фильтр зон и DR на входе, ручной список, EMD | 1, 2, 4, 6, 14 |
| §2 Упавший источник виден в сообщении задачи (и при пустом дне) | 6 |
| §3 `_run_waves` — цикл по таблице волн, 6 чипов | 7 (цикл), 9–12 (волны), 13 (шестой чип `deep`) |
| §3/§3.1 W0 зоны, бренды, флаги | 1, 9 |
| §3/§3.1 W2 RDAP + whois:43, `pending delete` → bid | 3, 9 |
| §3/§3.1 W3 Web Risk / Spamhaus только с DQS | 3, 10 |
| §3/§3.1 W4 batch-analysis, PBN, кап и пол, пустое поле не затирает DR | 2, 8, 11 |
| §3/§3.1 W5 Wayback + LLM-тема (строгий разбор, EMD, сбой → тема NULL) | 12 |
| §3/§3.1 W6 анкоры, пик трафика, `deep_checked`, `spam_anchors` (только при проверенной истории) | 2, 13 |
| §3.2 Компоненты, веса, штраф PBN, EMD без балла | 8, 13 |
| §3.2 Решение без авто-одобрения, правила пакета | 8, 10, 11, 13, 15 |
| §3.2 `DIRTY_REASONS`, перескор не отмывает (`webrisk_threats`, `spam_anchors`) | 5, 10, 13 |
| §4 Миграция 0025, настройки, архив РФ-пула | 5, 6 |
| §4 Бюджет units (капы, пол, DR бесплатно) | 5, 6, 11, 13, 14 |
| §5 Интеграции и удаления | 1, 2, 3, 4, 6, 9, 10, 11, 12, 16 |
| §6 `/settings` | 6 (источники, ключи v2), 8 (веса), 11 (кап W4 вместо капа капчи), 14 |
| §6 `/domains`, `/domains/pool`, модалка причин | 6 (`add-list`), 15 |
| §6 `/diag` (DropCatch только при включённом источнике, Web Risk без lookup) | 14 (Ahrefs), 16 |
| §7 Ошибки и деградация | 3 (IANA), 6 (источник, DR), 9 (RDAP/whois; Wayback упал — не `too_young`), 10 (Web Risk), 11 (Ahrefs W4, кап, пол, нет ключа), 12 (LLM), 13 (W6; язык неизвестен — не `spam_anchors`), 14 (`/settings` без сети), 16 (баннер только от критичных) |
| §8 Ранний выход не тратит Ahrefs и Wayback | 13 (`test_e2e_early_exit_spends_nothing_expensive`) |
| §8 Спам-дроп → `spam_anchors`; штраф PBN на DR 5 | 8, 13 |
| §8 Без авто-одобрения: гард-тесты v1 → «не попадает в пакет» | 8, 13 |
| §8 Перескор не отмывает (`spam_anchors`, Web Risk) | 10, 11 (`low_rd` на W4), 13 |
| §8 Тесты гейтов не удаляются — только переписываются | все задачи (явные списки старых тестов), 16 (законные остатки v1) |
| §8 Сеть в тестах: платные ключи обнулены, RDAP-бутстрап подменён | 3 |
| §8 Миграция на SQLite | 5 |
| §9 Готово, когда | 16, 17 |
| §11 Открытые вопросы: DR пачкой (живая проверка), ToS DropCatch, модель ollama | 17 (шаг 3а), 16/17 (DropCatch skip), 12/17 (`LLM_CLASSIFY_MODEL`) |

Второй раунд ревью (`04-plan-review.md`, «Раунд 2»):

| Находка | Задачи |
|---|---|
| R2-1 Wayback упал → не `too_young` по одной дате RDAP | 9 |
| R2-2 Сбой LLM → не `spam_anchors`: язык из БД, иначе `deep:lang_unknown` | 13 |
| R2-3 Web Risk: строгий разбор; живой образец → фикстура | 3, 17 |
| R2-4 Бэкап до миграции, откат v2 → v1 только дампом | 16 (`DEPLOY.md`), 17 |
| R2-5 Автопилот выключен на деплой и калибровку; подпись стадии «Проверка» | 15, 17 |
| R2-6 Платные методы Ahrefs — одна попытка (`_request_once`) | 2 |
| R2-7 DR-free: пустое или чужое тело — сбой пачки, не в `dr_seen` | 6 |
| R2-8 DR-фильтр: 401/403 или пустой ключ — опрос прекращается | 6 |
| R2-9 Причина сбоя W4 (с HTTP-кодом) — в сообщении задачи | 11 |
| R2-10 Платные волны не пойдут — W2/W3 по не-EMD не тратятся; выборка ≤ капа W4 | 11 |
| R2-11 Домен из списка, ставший `bid`, получает оценку дедлайна | 9 |
| R2-12 Нет обратной записи дедлайна из состояния волны | 9 |
| R2-13 Самопроверка `python -m app.services.transitions` | 10 |
| R2-14 EMD: (а) без `too_young`; (б) пустой архив — «архив пуст — новорег» | 9, 15 |
| R2-15 Спай-лок-тесты Web Risk и LLM, все локи в `_make_clients` | 10, 12 |
| R2-16 Превью-счётчики `/settings` без архива `legacy_ru` | 14 |
| R2-17 Задача 17: журнал скоринга, живые проверки LLM и Web Risk, пороги v1; 401 в шаге 0 Задачи 2 | 2, 17 |
| R2-18 Красный баннер — только от критичных проверок | 16 |
| R2-19 `→ approved` запрещён зоне вне белого списка; реестр без кнопки, пакет без таких доменов | 5, 15 |
| R2-20 Остатки v1 в комментариях и подсказках о грязи | 14 (`_pool_counts`), 16 |
| R2-21 `metrics_history`: ответ без `metrics` — ошибка; живой образец → фикстура | 2, 17 |
| R2-22 Шапка плана: фокус ревью, карта файлов, таблица покрытия | шапка |
| R2.8 Уточнения текста D1–D12 (исполнитель на Sonnet) | 3 (D4), 4 (D5), 6 (D6, D8), 8 (D7), 9 (D12), 10 (D3), 11 и 16 (D1), 16 (D2, список остатков) |
