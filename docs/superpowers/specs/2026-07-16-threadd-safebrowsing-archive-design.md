# Тред D — SafeBrowsing hard-reject + Archive pre-gate для Wayback — дизайн

## Контекст

CLAUDE.md, «Тред D — дешёвые критерии скоринга»: живая проба A-Parser форматов перед
кодом (проект уже дважды обжёгся на угадывании — whois-возраст, дата дропа cctld).
Проверено вживую на боксе 2026-07-16 восемь кандидатов из категории «History /
cleanliness / blacklist» (docs/api/aparser.md). Рабочими и надёжными оказались два:
`SE::Google::SafeBrowsing` и `Rank::Archive`. Остальные шесть — нерабочие на этом
инстансе (SecurityTrails/Yandex/Cloudflare::Radar — 100% ReadTimeout; TrustCheck/
Compromised — recaptcha блокирует прокси-пул, 0–50% успеха; Check::BackLink —
`Invalid query`, формат запроса не опознан) — в эту итерацию не входят.

## Живые факты (проверено, не гадаем)

**`SE::Google::SafeBrowsing`** — `resultString` формата `"{domain}: {0|1}\n"`, `1` =
зафлагован Google как вредоносный/фишинговый. Быстрый, надёжный, ни разу не упёрся в
recaptcha на пробе (не SERP-скрейпинг, прямой lookup).

**`Rank::Archive`** — `resultString` формата
`"{domain}: {first} - {last} ({N} times)\n"` (даты `dd.mm.yyyy`, отсутствие — `none`).
**Критично:** пресет `default` СЛОМАН на этом инстансе — `useproxy: 1` (подтверждено
`getParserPreset`), а archive.org's `__wb/sparkline` эндпоинт отвечает **502 на каждый
прокси без исключения** (подтверждено логами `oneRequest`) — итог: `none - none (none
times)` даже на `google.com`/`wikipedia.org`/`yandex.ru`, то есть на одних из самых
заархивированных доменов интернета. Пресет **`no_proxy`** (создан на боксе 2026-07-16)
чинит это полностью — живьём подтверждено на `google.com` (19 936 104 снимка),
`wikipedia.org` (393 880), `yandex.ru` (506 844) и на обоих тестовых доменах
(`dswjcndwijnwld23234212djf.ru` → честно `none`; `zudpopo.ru` → реальные 19 снимков
2023–2026). **Код обязан звать `Rank::Archive` ТОЛЬКО с `preset: "no_proxy"`.**

## Решённое в диалоге

1. **SafeBrowsing = жёсткий отказ**, как РКН/Spamhaus. Новый `reject_reason =
   "safebrowsing"`.
2. **Archive — НЕ сигнал чистоты.** Инвариант проекта (аудит F2, `scoring.py:50-57`):
   пустой Wayback даёт `wayback_checked=False` → `history_verdict()` = `"unknown"`
   (не `"clean"`) → домен уходит в ручной `scored`, никогда не auto-approve. Archive
   используется ТОЛЬКО чтобы дешевле прийти к тому же самому исходу: если Archive
   подтвердил 0 снимков — дорогой T3-фетч (скачать+распарсить nh3, ~60с/домен) не
   запускается, но `wayback_checked` всё равно ставится `False`, `sampled=0`,
   `history_evidence=[]` — ИДЕНТИЧНО сегодняшнему поведению при честно-пустом
   Wayback-ответе. Скор и вердикт не меняются НИ НА ЙОТУ — экономится только время.

## Архитектура

**`backend/app/integrations/aparser.py`** — два новых метода на `AParserClient`,
рядом с `ahrefs_probe` (тот же стиль: `_call` → `_result_string` → регэксп-парсер,
немэтч → `None`, не исключение):

```python
def safebrowsing_check(self, domain: str) -> bool | None:
    """SE::Google::SafeBrowsing: True = зафлагован, False = чист, None = формат не распознан
    (вызывающий код трактует как «не проверено», НЕ как «чисто» — см. _funnel)."""
    res = self._call("oneRequest", {"query": domain, "parser": "SE::Google::SafeBrowsing",
                                    "configPreset": "default", "preset": "default"})
    return _parse_safebrowsing(self._result_string(res))

def archive_probe(self, domain: str) -> dict:
    """Rank::Archive, ОБЯЗАТЕЛЬНО preset=no_proxy (default бьётся в archive.org через
    прокси-пул -> 502, см. дизайн-документ). times=0 -> вызывающий код вправе
    пропустить дорогой Wayback-фетч; times=None -> формат не распознан/сбой, судить
    нельзя, фолбэк на реальный Wayback как раньше."""
    res = self._call("oneRequest", {"query": domain, "parser": "Rank::Archive",
                                    "configPreset": "default", "preset": "no_proxy"})
    return _parse_archive(self._result_string(res))
```

Парсеры-хелперы (module-level, как `_parse_ahrefs`):

```python
_RE_SAFEBROWSING = re.compile(r":\s*([01])\s*$")
_RE_ARCHIVE = re.compile(r":\s*(?P<first>none|\d{2}\.\d{2}\.\d{4})\s*-\s*"
                         r"(?P<last>none|\d{2}\.\d{2}\.\d{4})\s*\((?P<times>none|\d+)\s*times\)")

def _parse_safebrowsing(text: str) -> bool | None:
    m = _RE_SAFEBROWSING.search(text or "")
    return None if not m else m.group(1) == "1"

def _parse_archive(text: str) -> dict:
    m = _RE_ARCHIVE.search(text or "")
    if not m:
        return {"times": None, "first": None, "last": None}
    times = m.group("times")
    return {"times": None if times == "none" else int(times),
            "first": None if m.group("first") == "none" else m.group("first"),
            "last": None if m.group("last") == "none" else m.group("last")}
```

**`backend/app/services/scoring.py`** — `_funnel`, две вставки:

1. Внутри стадии `"risk"` (рядом с РКН/blacklist, ДО `jobs.report(run, stage="echo")`):
```python
try:
    sig["safebrowsing_flagged"] = c["aparser"].safebrowsing_check(d.domain)
    if sig["safebrowsing_flagged"] is True:
        return "safebrowsing"
except Exception as e:  # noqa: BLE001
    sig["errors"].append(f"safebrowsing:{type(e).__name__}")
if sig.get("safebrowsing_flagged") is None and "safebrowsing_flagged" in sig:
    sig["errors"].append("safebrowsing:unavailable")   # формат не распознан -> risk-guard
```
   (тот же паттерн, что уже есть для `blacklisted is None`, строка ~482.)

2. В начале стадии `"history"`, ДО вызова `classify_history`:
```python
jobs.report(run, stage="history")
skip_wayback = False
try:
    arch = c["aparser"].archive_probe(d.domain)
    sig["archive_times"] = arch.get("times")
    skip_wayback = arch.get("times") == 0
except Exception as e:  # noqa: BLE001
    sig["errors"].append(f"archive:{type(e).__name__}")

if skip_wayback:
    # Archive подтвердил 0 снимков — эквивалент честно-пустого Wayback СЕГОДНЯ:
    # wayback_checked=False, история остаётся 'unknown', авто-approve не светит
    # (гард compute_score/_decide не меняется и не обходится).
    sig["wayback_checked"] = False
    sig["sampled"] = 0
    sig["history_evidence"] = []
else:
    try:
        hist = c["wayback"].classify_history(d.domain)
        # ...остальное тело блока БЕЗ ИЗМЕНЕНИЙ...
    except Exception as e:
        sig["errors"].append(f"wayback:{type(e).__name__}")
```

**`backend/app/services/labels.py`** — `REJECT_RU["safebrowsing"] = "Google Safe Browsing"`.

**`backend/app/services/transitions.py`** — `DIRTY_REASONS` пополняется:
`frozenset({"rkn", "blacklist", "history_dirty", "feed_flag", "safebrowsing"})`. Это
единственная строка, которая реально запирает купленный-но-грязный домен от кассы —
`dirty_reason()` уже вызывается из `acquisition.py`/`orchestrator.py` (не только из UI),
см. `scoring.py:198` («новое основание "нельзя" обязано пройти через единый предикат»).

**Согласованность с уже существующим риск-гардом (нашёл на самопроверке спеки).**
SafeBrowsing — риск-проверка того же СОРТА, что РКН/блэклист: если она упала и мы не
знаем ответа, домен не имеет права авто-одобриться так, будто его проверили. Два места
в `scoring.py`, которые уже это делают для rkn/blacklist, должны получить и
`safebrowsing`:

1. `_decide()` (риск-гард, ~строка 222-224): `any(e.startswith(("rkn:", "blacklist:"))
   for e in ...)` → добавить `"safebrowsing:"` в кортеж префиксов.
2. `_BLIND_RU` (~строка 58-70, словарь для бейджа «оценён вслепую»): добавить
   `"safebrowsing": "Google Safe Browsing НЕ проверен: сервис не ответил"` — тем же
   ключом, что кладёт `sig["errors"].append(f"safebrowsing:{type(e).__name__}")`
   (`blind_reason()` берёт префикс до `:` и смотрит в этот словарь).

Без этих двух правок падение SafeBrowsing тихо вело бы себя иначе, чем падение РКН —
ровно тот разъезд, который уже дважды ловили аудитом (F2, F6): новая проверка ОБЯЗАНА
проходить через тот же контракт «сбой = не подтверждено = не одобряем автоматически»,
а не изобретать своё поведение.

## Что осознанно НЕ входит

- Никакого нового тумблера в `/settings` — RKN/Spamhaus тоже безусловные стадии, не
  капаются и не выключаются по отдельности; fail-open уже есть через `sig["errors"]`.
- Никакого изменения `WEIGHTS`/`compute_score` — `history_cleanliness` считается
  ровно как сегодня (`1.0 if wayback_checked else 0.5`), Archive в эту формулу не
  входит вообще.
- Никакого нового `FUNNEL_STAGES`-пункта — обе проверки живут внутри существующих
  стадий `risk`/`history` (прогресс-степпер панели не меняется).
- Проверка Check::BackLink/TrustCheck/Compromised/SecurityTrails/Yandex/Radar — не
  готовы (см. «Живые факты» выше), в этот тред не входят.

## Критерии приёмки

- `SE::Google::SafeBrowsing` вернул `1` → домен `rejected`, `reject_reason='safebrowsing'`,
  `dirty_reason()` его ловит, `acquisition.py`/`orchestrator.py` не пускают к кассе.
- `SafeBrowsing` вернул `0` — воронка идёт дальше как раньше.
- `SafeBrowsing` упал (сеть/формат) — `errors` содержит `safebrowsing:...`, risk-guard в
  `_decide` (расширенный кортеж префиксов) не даёт auto-approve, `_BLIND_RU` даёт
  бейджу «оценён вслепую» человекочитаемую причину — ТЕ ЖЕ гарантии, что уже есть у
  РКН/блэклиста, ни одной веткой меньше.
- `Rank::Archive` (`preset=no_proxy`) подтвердил `times=0` → `classify_history` НЕ
  вызывается, `wayback_checked=False`, `sampled=0` — домен ведёт себя ИДЕНТИЧНО
  сегодняшнему честно-пустому Wayback (не auto-approve, `history_verdict='unknown'`).
- `times>0` или Archive упал/дал `None` → T3 Wayback запускается как раньше, без
  единого изменения в этом пути.
- Пресет `no_proxy` — обязательный литерал в коде, не рантайм-настройка (он специфичен
  для конкретного A-Parser-инстанса; если пресет отсутствует на другом боксе — вызов
  упадёт, `archive:...` в `errors`, фолбэк на реальный Wayback — безопасно, не тихо).
