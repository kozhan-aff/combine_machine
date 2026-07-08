# Ahrefs DR/backlinks/referring-domains через A-Parser (design)

## Контекст

Брейнсторм-пакет 2026-07-08, пункт B: "узнавать DR - backlinks - outlinks через ahrefs
(a-parcer)" — заменить мёртвый OpenPageRank (free-регистрация закрыта, `DR_FULL`/
`authority`-компонент сейчас не входит в `WEIGHTS` вообще, эффективно всегда 0) на
реальный DR + добавить backlinks/referring-domains из Ahrefs, скрейпленного через
A-Parser (не платный Ahrefs API).

Живьём проверено на боксе (не с Mac — A-Parser внутренний, живого доступа к формату
ответа со стороны не было):
- Пресет `Rank::Ahrefs` с опциями `Use proxy=true`, `Proxy Checker=<любой рабочий>`,
  `Result format=$query: $rating, $bl, $domains\n`, `Util::Turnstile preset=RuCapcha`
  реально работает — Turnstile решается (`Util::Turnstile: Status(2): OK`), запрос
  уходит на `https://ahrefs.com/v4/stGetFreeBacklinksOverview` (200 OK).
- `lev777.casino: none, 476, 268` — `rating`(DR)=`"none"` (буквально строка, не число),
  `bl`(backlinks)=476, `domains`(referring domains)=268.
- `wikipedia.org: 97, 3421649284, 4000871` — `rating`=97 (реально верный DR Wikipedia,
  валидирует формат), огромные backlinks/referring domains.

Вывод: `rating` независимо nullable от `bl`/`domains` — некоторым доменам Ahrefs не
присваивает DR, хотя backlinks-данные есть. Парсер обязан обрабатывать `"none"` как
`None`, не как ошибку парсинга числа.

Опции в `oneRequest` передаются как **массив** `{name, value}`-пар (не объект) —
выяснено методом проб на живом API: `queries` → `"Query not set"` (нужно `query`
строкой) → `options` как объект → `"Error: options must by array"` → массив принят.

## 1. `AParserClient.ahrefs_probe()` — новый метод (`integrations/aparser.py`)

```python
_RE_AHREFS = re.compile(
    r"^(?P<domain>\S+):\s*(?P<rating>none|\d+),\s*(?P<bl>\d+),\s*(?P<domains>\d+)\s*$",
    re.I | re.M,
)


def _parse_ahrefs(text: str) -> dict:
    """resultString 'domain: rating, bl, domains' -> dr/backlinks/referring_domains.
    rating может быть буквально 'none' (Ahrefs не присвоил DR этому домену) — None,
    не 0 (0 было бы неверным сигналом «домен без авторитета», а не «нет данных»)."""
    m = _RE_AHREFS.search(text or "")
    if not m:
        return {"dr": None, "backlinks": None, "referring_domains": None}
    rating = m.group("rating")
    return {
        "dr": None if rating.lower() == "none" else int(rating),
        "backlinks": int(m.group("bl")),
        "referring_domains": int(m.group("domains")),
    }
```

```python
    def ahrefs_probe(self, domain: str) -> dict:
        """Rank::Ahrefs через капча-решатель (пресет RuCapcha, живьём проверено 2026-07-08).
        Дорогой вызов (капча за деньги) — вызывающий код решает, когда его делать
        (см. scoring.py _funnel: только для доменов без RD из фида, T3-выжившие)."""
        res = self._call("oneRequest", {
            "query": domain,
            "parser": "Rank::Ahrefs",
            "preset": "default",
            "configPreset": "default",
            "options": [
                {"name": "Use proxy", "value": True},
                {"name": "Proxy Checker", "value": settings.APARSER_PROXY_CHECKER},
                {"name": "Result format", "value": "$query: $rating, $bl, $domains\n"},
                {"name": "Util::Turnstile preset", "value": "RuCapcha"},
            ],
        })
        return _parse_ahrefs(self._result_string(res))
```

Новая настройка `APARSER_PROXY_CHECKER` (в `.env`, дефолт `"Free Proxy 2"` — то самое
рабочее значение из живого Задания) — вынесена в конфиг, а не захардкожена, потому что
это имя прокси-чекера специфично для конкретного A-Parser-инстанса пользователя
(в его UI было и `Free Proxy 2`, и `ipv6_free` между скриншотами — значит это выбираемая
опция, а не константа кода).

## 2. Где вызывается — `scoring.py` `_funnel()`

Заменяет текущий мёртвый блок OpenPageRank (строки 187-191 сейчас: `if c["opr"] is not
None: ...`). Новое место — там же, сразу после Wayback (T3), **только если фид не дал
RD**:

```python
    # T3b — Ahrefs (дорого, капча за деньги): ТОЛЬКО если фид не дал RD (cctld/reg_ru/
    # sweb — у backorder RD уже есть и доверяем ему, повторно не проверяем) и бюджет жив.
    if d.referring_domains is None and ahrefs_budget is not None and ahrefs_budget[0] > 0:
        ahrefs_budget[0] -= 1
        try:
            ah = c["aparser"].ahrefs_probe(d.domain)
            sig["dr"] = ah["dr"]
            sig["ahrefs_backlinks"] = ah["backlinks"]
            if ah["referring_domains"] is not None:
                sig["referring_domains"] = ah["referring_domains"]
        except Exception as e:  # noqa: BLE001
            sig["errors"].append(f"ahrefs:{type(e).__name__}")
    return None
```

`_make_clients()` теряет `opr`/`OpenPageRankClient` (см. §4). `score_domain()`/
`score_pending()` заводят `ahrefs_budget = [int(st.get("max_ahrefs_per_run", 50))]`
рядом с `whois_budget`, тем же паттерном (мутабельный `[int]`, общий на прогон).

## 3. Композитный скор — `scoring.py`/`scoring_config.py`

`compute_score()` уже считает `comp["authority"]` из `sig.get("dr")` — просто добавляем
его в `cfg.WEIGHTS`, чего сейчас нет (поэтому эффективно всегда 0). Новые веса
(сумма=1.0, существующий тест `assert abs(sum(cfg.WEIGHTS.values()) - 1.0) < 1e-9`
это проверяет):

| Компонент | Было | Стало |
|---|---|---|
| history_cleanliness | 0.40 | 0.35 |
| age | 0.20 | 0.18 |
| rd_proxy | 0.30 | 0.27 |
| indexed_echo | 0.10 | 0.08 |
| authority (DR) | (нет в WEIGHTS) | **0.12** |

`NORM["DR_FULL"]`: `6.0` (было калибровано под OpenPageRank 0-10, комментарий
"~6 already strong") → **`30.0`** (Ahrefs 0-100; для доменов-кандидатов на выкуп DR
30+ — уже сильный сигнал, DR=97 у Wikipedia — потолок шкалы, не ориентир для дропов).

`backlinks` ($bl) — НЕ отдельный компонент скора (сильно коррелирует с referring
domains, не даёт независимого сигнала) — только `sig["ahrefs_backlinks"]` →
`d.score_breakdown` для человека в карточке домена.

## 4. Удаление мёртвого OpenPageRank

`integrations/openpagerank.py`, `OPENPAGERANK_API_KEY` (`.env`/`.env.example`/
`config.py`), импорт/использование в `_make_clients()` — удаляются целиком. Ключ не
регистрируется с 2026 (free-регистрация закрыта), функционально заменяется этим
изменением. `docs/api/openpagerank.md` — пометить как `DEPRECATED`, не удалять
(историческая запись, зачем отказались).

## 5. Кап на прогон — runtime-настройка, как `max_whois_per_run`

Самопроверка спеки нашла неточность в черновике: `max_whois_per_run` — НЕ просто
константа в коде, а полноценный runtime-параметр (колонка в БД, ползунок на
`/settings`, `services/settings.py get_settings()/update_settings()`). Ahrefs дороже
whois (капча стоит денег за штуку) — заслуживает как минимум того же уровня контроля,
даём `max_ahrefs_per_run` по идентичному паттерну:

- `models/settings.py`: `max_ahrefs_per_run: Mapped[int] = mapped_column(Integer,
  default=50)` — новая колонка `ScoringSettings`.
- Миграция `alembic/versions/0005_ahrefs.py` (после `0004_autonomy.py`) —
  `add_column("scoring_settings", ...)`, дефолт 50 на существующей строке.
- `scoring_config.py`: `MAX_AHREFS_PER_RUN = 50` (дефолт-константа, как
  `MAX_WHOIS_PER_RUN`, для `_defaults()`/свежей БД).
- `services/settings.py`: добавить `"max_ahrefs_per_run"` в `_KEYS_NUM`, `_BOUNDS`
  (`(0, 1000)`), `_defaults()` (`cfg.MAX_AHREFS_PER_RUN`), `get_settings()` (int-каст).
  Без нижнего клампа `< 1 → 1` (в отличие от `max_whois_per_run`) — 0 здесь ЛЕГАЛЬНОЕ
  значение: полностью выключить платные Ahrefs-вызовы, оставив авторитетность только
  на фид-RD (это НЕ глушит скоринг целиком, whois/Wayback/RKN идут своим чередом).
- `api/panel.py` `settings_save()`: `max_ahrefs_per_run: int = Form(50)`, прокинуть в
  `update_settings(...)`.
- `templates/settings.html`: новая `.station` рядом с блоком whois-капа — ползунок
  `min=0 max=500 step=10`, подпись в духе "Кап Ahrefs-проверок за прогон — **платная
  капча за штуку**", hint поясняет что 0 = выключить Ahrefs-обогащение полностью.
- `score_pending()`: `ahrefs_budget = [int(st["max_ahrefs_per_run"])]` рядом с
  `whois_budget`, тем же паттерном (мутабельный `[int]`, общий на прогон,
  передаётся в `_funnel()` доп. параметром).

## Тестирование

Офлайн (`monkeypatch` на `AParserClient.ahrefs_probe`/`_call`, живая капча/прокси не
воспроизводимы в тестах):
- `_parse_ahrefs()`: юнит-тесты на все 3 живых случая (rating=число, rating="none",
  не матчится вообще → все None) + регистронезависимость "None"/"NONE".
- `_funnel()`: домен с `referring_domains` из фида → Ahrefs НЕ вызывается (мок должен
  НЕ быть вызван — assert через `Mock.assert_not_called()`); домен без RD и с живым
  `ahrefs_budget` → вызывается, `sig["dr"]`/`sig["referring_domains"]` заполняются;
  бюджет исчерпан (`ahrefs_budget[0] == 0`) → НЕ вызывается, домен НЕ уходит в
  `acquirability_unresolved` (в отличие от whois-бюджета — Ahrefs это часть скора,
  не гейт приобретаемости, просто authority/rd_proxy остаются как есть без Ahrefs-буста).
- `compute_score()`: обновить существующие фикстуры на новые веса (сумма=1.0 тест
  должен пройти как есть, он не завязан на конкретные числа); добавить кейс с
  `dr=None` (authority=0, не ломает остальной скор).

## Не делаем (non-goals)

- Не трогаем T0 (`low_rd`) — фид-RD как был, дешёвый ранний гейт до Ahrefs.
- Не вызываем Ahrefs для backorder-доменов (RD из фида уже есть — не дублируем платный
  вызов).
- Не делаем `backlinks` весовым компонентом скора — только informational.
- Не трогаем `Rank::MOZ`/`Rank::MajesticSEO` — не проверялись живьём, вне охвата.

## Риски

- Формат `resultString`/опции `oneRequest` — завязаны на текущую версию A-Parser
  (Enterprise v1.2.29.10 на скриншотах) и текущую разметку `ahrefs.com/v4/
  stGetFreeBacklinksOverview` — как и с cctld/reg_ru/sweb, при изменении верстки
  парсинг может тихо начать возвращать 0/None. `_parse_ahrefs()` при немэтче отдаёт
  все None (не бросает) — не ломает прогон, но требует присмотра (аналог diag-alert).
- `Util::Turnstile preset=RuCapcha` зависит от баланса/валидности ключа стороннего
  капча-сервиса (RuCaptcha.com) — если баланс закончится, Ahrefs снова начнёт молча
  возвращать `none` по всем полям (как в первом неудачном тесте) — это НЕ отличить
  программно от «домену не присвоен DR», `_parse_ahrefs()` не может различить эти два
  случая по одному ответу. Не решаем в этом изменении (нет дешёвого способа отличить
  без параллельного контрольного домена с заведомо известным DR).
- `APARSER_PROXY_CHECKER` — имя конкретного прокси-чекера в UI пользователя может
  измениться/протухнуть (прокси-пулы у A-Parser ротируются) — вынесено в `.env`,
  чтобы не требовало правки кода при смене.
