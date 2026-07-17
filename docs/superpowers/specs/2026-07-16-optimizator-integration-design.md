# Optimizator.ru — второй канал выкупа (M2) — дизайн

> **Как читать этот документ.** Написан соло, без пошагового диалога с оператором
> (явное разрешение оператора, 2026-07-16, ночной прогон — «нужно сделать полный
> цикл»). Решения, которые в обычном режиме задавались бы вопросом one-at-a-time,
> помечены **[РЕШЕНИЕ]** с обоснованием — чтобы утром можно было быстро проверить
> именно эти места, а не весь документ.

## Контекст

CLAUDE.md: «свободные чистые → optimizator (гарантия)» — второй канал выкупа M2,
для доменов БЕЗ конкурентной борьбы (в отличие от backorder, где тариф = ставка на
перехват). Канал был мёртв: `OPTIMIZATOR_API_KEY`/`OPTIMIZATOR_NICD` пустые в `.env`,
`OptimizatorClient.register/order_status/ping` — `raise NotImplementedError`.

**2026-07-16, за время этого диалога:** оператор получил и добавил реальный
`OPTIMIZATOR_API_KEY`; передал номер своей анкеты RU-CENTER — `OPTIMIZATOR_NICD=
5014480/NIC-D` (добавлено в `.env`). Документация сайта оказалась обманчивой
(навигационные подписи не совпадали с реальным содержимым страниц — проверено прямым
`curl` сырого HTML, не через посредника-суммаризатора) — ниже только то, что
подтверждено дословно в тексте страниц ИЛИ живым вызовом.

## Живые факты

**Формат (подтверждено на 7 страницах доки дословно):**
`GET|POST http://optimizator.ru/?a=api&sa=<action>&api_key=<KEY>&...` → JSON-**массив**
(даже для одиночных значений). Кодировка параметров — CP1251 по умолчанию, `enc=utf8`
переключает на UTF-8.

**Формат ошибки — НЕ был документирован НИГДЕ текстом на сайте; получен ЖИВЬЁМ** (вызов
`check_nicd` для нашей анкеты, которая ещё не передана в управление):
```json
[{"error": "Указанная анкета не находится под нашим управлением...", "error_id": 411}]
```

**Действия, подтверждённые ЖИВЬЁМ сегодня (2026-07-16), реальным ключом:**
- `balance` → `[{"balance": 0}]` — ключ рабочий, счёт пуст (0 ₽).
- `prices&domain=ru` → `[{"domain":"RU","price_registration":179,"price_renewal":199}]`.
- `check_nicd&nicd=5014480/NIC-D` → ошибка `error_id=411` (анкета НЕ передана в
  управление Optimizator — ручной шаг на стороне nic.ru, см. «Блокеры» ниже).

**Действия, подтверждённые ТОЛЬКО текстом документации (НЕ вызывались живьём —
`reg_domains`/`renew_domains` тратят деньги, `check_domain`/`check_order` нечего
проверять без хотя бы одного реального заказа под этой анкетой):**
- `reg_domains&nicd=...&domains=a.ru b.ru` (до 30) → `[{"order_id": N}]`.
- `check_order&order_id=N` → `[{"order_id":N,"state":"completed"}]` — **другие значения
  `state`, кроме `"completed"`, нигде не перечислены** (не гадаем, что ещё бывает).
- `check_domain&domain=a.ru` → `[{"data_end":"02.12.2016","domain":"A.RU"}]` — это
  проверка «МОЖЕТ ЛИ этот домен быть продлён», а не «свободен ли домен» (эту разницу
  чуть не перепутал сам при первом чтении доки — см. предыдущее сообщение в диалоге).
  **[РЕШЕНИЕ]** Раз `check_domain` успешен только для доменов ПОД управлением нашей
  анкеты, успешный ответ = «этот домен уже наш» — используется ниже как замена
  отсутствующему «найти заказ по домену» (см. Идемпотентность).
- `renew_domains&domains=...` → `[{"order_id": N}]`, тот же формат, что `reg_domains`.

**У API НЕТ:**
- листинга/поиска доступных для покупки доменов (`/backorder/`, `/dropped/` — только
  HTML-страницы для людей, форма шлёт `POST /index.sema`, не JSON);
- проверки «свободен ли домен» (сама доки прямым текстом: «проверяем... whois
  собственными силами») — эту роль уже играет M1 (whois в T1 воронки скоринга);
- эндпоинта «список моих заказов» или «заказ по домену» (в отличие от backorder
  `client_orders()`/`find_order`) — только `check_order` по УЖЕ известному `order_id`.

## Блокеры (организационные, не код — как «баланс backorder = 0 ₽»)

1. **Баланс 0 ₽** — `reg_domains` создаст заказ, но без средств регистрация не пройдёт
   (аналогия с backorder: `paynow=on` при нуле на счету).
2. **Анкета `5014480/NIC-D` НЕ передана в управление Optimizator** (подтверждено
   `check_nicd`, `error_id=411`) — ручное действие в личном кабинете nic.ru, вне кода.
   Ни один денежный вызов (`reg_domains`/`renew_domains`) не пройдёт, пока это не
   сделано — код готовится СЕЙЧАС, включается организационно ПОЗЖЕ.

## Архитектура

### 1. `backend/app/integrations/optimizator.py` — транспорт (полностью переписывается)

Существующий класс (`__init__` уже читает `settings.OPTIMIZATOR_API_KEY`/`NICD` —
не трогаем) получает реализацию вместо `NotImplementedError`, плюс новые
исключения и методы:

```python
class OptimizatorError(Exception):
    """Провайдер вернул {"error": ..., "error_id": ...} — ЧИСТЫЙ отказ (деньги НЕ
    ушли: HTTP успешен, ответ разобран, провайдер explicitly сказал "нет"). Безопасно
    показывать человеку и безопасно позволить «↻ повторить» — деньги не потрачены."""
    def __init__(self, message: str, error_id: int | None = None):
        super().__init__(f"{message} (error_id={error_id})" if error_id else message)
        self.error_id = error_id


class OptimizatorAmbiguous(Exception):
    """Транспорт упал (timeout/5xx/соединение) ПОСЛЕ отправки денежного запроса —
    исход НЕИЗВЕСТЕН, как AmbiguousSend у backorder. НЕ давать retry вслепую."""
```

Методы (`_call` — общий GET-хелпер через `BaseClient.request`, парсит JSON-массив,
поднимает `OptimizatorError` на `{"error":...}` форму, `OptimizatorAmbiguous` на
транспортные исключения из `BaseClient`):

```python
def ping(self) -> bool:                       # balance как liveness-проверка
def balance(self) -> float:                   # [{"balance": N}] -> N
def prices(self, zone: str = "ru") -> dict:   # [{"domain":..., "price_registration":...}] -> первая запись
def check_nicd(self) -> bool:                 # True = анкета под нашим управлением, False = error_id=411 конкретно про это
def register(self, domains: list[str]) -> dict:      # reg_domains -> {"order_id": N} (РАЗВЁРНУТО из массива)
def order_status(self, order_id: int) -> dict:        # check_order -> {"order_id":N,"state":...}
def check_domain(self, domain: str) -> dict:          # data_end если наш; КАК И ВСЕ методы —
                                                       # бросает OptimizatorError/Ambiguous на отказ/
                                                       # сбой, никакого специального None-сентинела
                                                       # (нет живых данных о том, чем ИМЕННО отвечает
                                                       # API на "домен не наш" — не гадаем формат
                                                       # отказа, см. правило проекта)
```

**[РЕШЕНИЕ] `register()` идёт МИМО ретрая `BaseClient`** (как `BackorderClient.order()`,
тот же принцип — 3 ретрая = 3 попытки списания за одну команду). Реализация вызывает
`httpx` напрямую с `timeout` из `BaseClient`, а не через `self.request(...)`.

### 2. `execute_confirmed_order` (`acquisition.py:441-443`) — реальная реализация optimizator-ветки

Текущий код (`else: from ... import OptimizatorClient; res = OptimizatorClient().register([d.domain])`)
заменяется на структуру, зеркальную backorder-ветке НАД ней (idempotency-check →
отправка → различение чистого отказа/неопределённости):

```python
else:
    from app.integrations.optimizator import OptimizatorClient, OptimizatorError, OptimizatorAmbiguous
    c = OptimizatorClient()
    # ИДЕМПОТЕНТНОСТЬ. У API нет «список заказов»/«заказ по домену» (в отличие от
    # backorder.find_order) — единственная замена: check_domain успешен ТОЛЬКО для
    # доменов под нашей анкетой. Успех здесь = «уже наш», второй reg_domains не шлём.
    # check_domain, КАК И ВСЕ методы клиента, бросает на отказ/сбой (нет отдельного
    # None-сентинела — нет живых данных о формате "домен не наш", не гадаем). Любое
    # исключение здесь — «подтвердить существующую регистрацию не удалось» — ЭТА
    # проверка денег не тратит, поэтому безопасно просто продолжить к register():
    # если домен и правда уже наш, reg_domains — вопрос к провайдеру, не к нам
    # (OptimizatorError оттуда упадёт в общий except ниже так же честно).
    try:
        existing = c.check_domain(d.domain)
    except (OptimizatorError, OptimizatorAmbiguous):
        existing = None
    saved.pop("maybe_sent", None)
    if existing is not None:
        o.status = "ordered"
        o.result = {**saved, "note": "домен уже под нашей анкетой (check_domain) — "
                                     "второй reg_domains не шлём", "data_end": existing.get("data_end")}
        o.ordered_at = o.ordered_at or datetime.now(timezone.utc)
        db.commit()
        return {"order_id": order_id, "status": o.status, "result": o.result}
    try:
        res = c.register([d.domain])
    except OptimizatorAmbiguous as e:
        o.status = "failed"
        o.result = {**saved, "error": f"исход неизвестен: {e}", "maybe_sent": True}
        db.commit()
        return {"order_id": order_id, "status": "failed", **o.result}
    # OptimizatorError (чистый отказ) падает в общий except Exception ниже — это
    # ПРАВИЛЬНО: деньги не ушли, "↻ повторить" безопасен, сообщение читаемое
    # (__str__ уже несёт error_id).
```

`res` здесь — уже развёрнутый `{"order_id": N}` (не список) — существующий код на
строке 445 (`res.get("order_id") if isinstance(res, dict)`) остаётся БЕЗ изменений и
продолжает работать as-is.

### 3. `poll_orders()` — застрявший `ordering` для optimizator (закрывает пробел из комментария acquisition.py:501-507)

Текущий докстринг честно говорит: «ВЫХОД ИЗ 'ordering' — ТОЛЬКО ДЛЯ backorder...
как только у канала появится транспорт — ему понадобится свой разбор». Транспорт
появляется этим планом — значит и разбор нужен ЗДЕСЬ ЖЕ, а не отдельным треском.

**[РЕШЕНИЕ]** Добавить после существующего backorder-цикла отдельный проход по
`AcquisitionOrder`, где `provider == "optimizator"`, `status == "ordering"`,
`_claim_expired(o)` истёк: для каждого — `check_domain(d.domain)` в try/except
(`OptimizatorError`/`OptimizatorAmbiguous` → трактуем как «домен НЕ наш», см. пункт 2
про единообразие с execute); успех (домен наш) → `caught`-путь недоступен (нет
автослежения за поимкой, в отличие от backorder — регистрация свободного домена либо
УЖЕ произошла, либо нет, нет фазы «поймали, но не наши» вовсе) → сразу `ordered`,
`d.status` НЕ трогаем (уже `purchasing`, человек сам переводит в `purchased` через
`mark_caught`, симметрично backorder); исключение/ответа нет → `failed`,
`claimed_at=None`, деньги не считаем потраченными (никогда не подтверждено, что они
списываются на отправке, а не на успехе — см. «Что НЕ входит»).

**[РЕШЕНИЕ]** В отличие от backorder-цикла (который различает «заказ есть у
провайдера» от «заказа нет» одним `client_orders()`-ответом), здесь `check_domain`
даёт ТОЛЬКО «домен наш» или «нет» (включая транспортные сбои — неотличимы от
«нет данных»). Это осознанно консервативнее: застрявший `ordering`, где реально
проверить нельзя, уходит в `failed`, а не зависает — человек увидит его в `/queue`
и решит (в т.ч. вручную сверив на сайте optimizator.ru), а не потеряет из виду.

**Важно (нашёл при чтении существующего кода перед планом):** запись исхода ОБЯЗАНА
идти через существующий `_settle(db, o, **values)` (строка 117), НЕ прямым
`o.status = ...; db.commit()`. `_settle` — общий, provider-agnostic снимок-гард
(ABA-защита, аудит F11/F12): без него параллельный `execute` (если человек нажмёт
«↻ повторить» ровно в момент, пока поллинг решает) может переписать чужой
только что поставленный `maybe_sent`/`provider_order_id`. Новый цикл для optimizator
— это ТОТ ЖЕ цикл по форме, что и backorder-цикл (строки 540-661): свой SELECT по
`provider == "optimizator"`, тот же пропуск свежего `ordering` (`_claim_expired`),
тот же вызов `_settle` для записи, тот же `except IntegrityError` ремень поверх.
Не изобретаем новый механизм записи — переиспользуем существующий, только источник
правды другой (`check_domain` вместо `client_orders()`).

### 4. `confirm_order` — фиксация ЦЕНЫ для не-backorder провайдеров

Сегодня `confirm_order` замораживает `o.cost` только для `provider == "backorder"`
(через `pick_tariff`). Для optimizator у человека на экране `/queue` сегодня не было
бы ВООБЩЕ никакой цифры перед подтверждением — тратить деньги вслепую противоречит
самому принципу денежного гейта («человек видит, сколько»).

**[РЕШЕНИЕ]** Добавить параллельную ветку:
```python
elif provider == "optimizator":
    from app.integrations.optimizator import OptimizatorClient
    zone = domain.rsplit(".", 1)[-1] if domain else "ru"
    price = OptimizatorClient().prices(zone)          # сетевой вызов, вне транзакции — как pick_tariff
    # price_id/period_id: None — это backorder-специфичные поля (тир сетки), у
    # optimizator их физически нет. НИЖЕ, в SessionLocal-блоке, существующий код
    # (`if tier is not None: o.result = {..., "price_id": tier["price_id"], ...}`)
    # обращается к tier["price_id"]/tier["period_id"] БЕЗУСЛОВНО — без этих двух
    # ключей (даже пустых) он бы упал KeyError на каждом optimizator-confirm.
    tier = {"price": price["price_registration"], "price_id": None, "period_id": None}
```
`bid_rub` для optimizator остаётся необязательным (существующая проверка
`if provider == "backorder" and not bid_rub: raise ValueError(...)` уже НЕ требует
его для optimizator — не трогаем). Проверено чтением существующего кода
`confirm_order` (см. блок `if tier is not None: o.cost = tier["price"]; o.result =
{..., "price_id": tier["price_id"], "period_id": tier["period_id"]}`) — это НЕ новая
ветка кода, а обязательная форма данных для уже существующей.

### 5. UI — панель

**`domains.html:254`/`pool.html:130`** (кнопка «＋ в очередь выкупа») — форма сегодня
шлёт только `POST /domains/{id}/queue` без `provider` (роут по умолчанию берёт
`"backorder"`). Добавить `<select name="provider">` с двумя опциями
(`backorder`/`optimizator`), **[РЕШЕНИЕ]** preselected по `d.lane` (`"free"` →
optimizator, `"bid"`/`None` → backorder — тот самый принцип из CLAUDE.md: «ценные
дропы → backorder, свободные чистые → optimizator»), человек волен переключить.

**`queue.html:83-93`** (форма подтверждения) — сегодня БЕЗУСЛОВНО рендерит
`<select name="bid_rub">` с сеткой backorder-тарифов — для optimizator-заказа это
показывало бы бессмысленный выбор тарифа, которого не существует. Разделить на
`{% if o.provider == "backorder" %}` (как сейчас) / `{% else %}` (фиксированная цена
из `o.cost` текстом + кнопка подтверждения без `bid_rub`, или скрытое поле с тем же
значением — `confirm_order` его просто проигнорирует для не-backorder).

### 6. `/diag` (опционально, малая правка)

**[РЕШЕНИЕ]** Добавить `OptimizatorClient().ping()` (баланс-вызов, read-only, бесплатно)
рядом с существующим backorder-пингом — тот же принцип «pings ЧЕСТНО, не skip».
Если бюджет времени сегодня ночью не позволит — не блокер, можно вынести в отдельный
маленький follow-up (это не денежный путь, чистая наблюдаемость).

## Идемпотентность денег — сводка решения

| Событие | backorder | optimizator (это решение) |
|---|---|---|
| Проверка «уже ли заказано» ДО отправки | `find_order` (список заказов у провайдера) | `check_domain` (успех = «уже наш» — единственная доступная замена, API не даёт листинг) |
| Отправка упала неопределённо (timeout/5xx) | `AmbiguousSend` → `maybe_sent=True`, retry заблокирован | `OptimizatorAmbiguous` → `maybe_sent=True`, retry заблокирован (тот же контракт) |
| Провайдер ЧЕСТНО отказал (нет анкеты/денег/домен занят) | `RuntimeError` из `_billmgr` | `OptimizatorError` — падает в тот же generic `except Exception`, retry безопасен |
| Убили процесс между claim и ответом (`ordering` навечно) | `poll_orders` спрашивает `client_orders()` | `poll_orders` спрашивает `check_domain()` (по одному домену, не листингом) |

## Что осознанно НЕ входит

- **Реальный вызов `register()` с настоящими деньгами** — организационно заблокирован
  (баланс 0 ₽, анкета не передана) И архитектурно защищён денежным гейтом
  (`confirmed_by_human` — решение человека в реальном времени, не автопилот). Этой
  ночью транспорт и гейты ГОТОВЯТСЯ, не исполняются.
- **Момент списания денег** (на отправке `reg_domains` vs по факту успеха) —
  документация об этом молчит, а тестировать живым платежом нельзя. Код трактует
  ЛЮБОЙ неопределённый исход (`OptimizatorAmbiguous`) консервативно — как «возможно
  списано» (`maybe_sent=True`), не предполагая обратного.
- **Механизм передачи анкеты в управление Optimizator** — ручной шаг в личном
  кабинете nic.ru, вне кода и вне этого плана.
- **Пополнение баланса** — человеческие способы оплаты (см. `texts/3.html`), не API.
- **Пополнение через API/OAuth для получения `api_key`** — ключ уже получен и добавлен
  оператором вручную через саппорт, повторно этот процесс не описывается.
- **Пересмотр `_PROVIDERS`/модели `AcquisitionOrder`** — они уже поддерживают второй
  провайдер (`_PROVIDERS = {"backorder", "optimizator"}`, `o.provider` — рабочее
  поле), новых колонок/миграций не требуется.

## Критерии приёмки

- `OptimizatorClient.ping()`/`balance()`/`prices()`/`check_nicd()` живьём отработали
  сегодня в этой сессии в РУЧНОМ curl-тесте (см. «Живые факты») — код должен
  парсить РОВНО эти форматы, не придуманные.
- `register()`/`order_status()`/`check_domain()` реализованы по формату из ДОКУМЕНТАЦИИ
  (не проверены живым заказом — денег нет и анкета не передана), но структура ответа
  (`[{...}]`, разворачивание в dict) идентична уже подтверждённым `balance`/`prices`/
  `check_nicd` — тот же провайдер, тот же конверт, разумно доверять форме.
- Хард-гейт не ослаблен: `execute_confirmed_order` для optimizator по-прежнему
  проверяет `confirmed_by_human` (существующая проверка строкой раньше, не трогается),
  `dirty_reason`/`refuse_dirty` — тоже (существующий код над веткой provider, общий
  для обоих).
- `poll_orders()` для optimizator НЕ переводит домен в `purchased` автоматически
  (это по-прежнему делает только `mark_caught`, ЧЕЛОВЕК) — только `ordering→ordered|failed`,
  симметрично тому, что комментарий в коде обещал как будущий разбор.
- UI: заказ на optimizator можно завести, увидеть его фиксированную цену на confirm,
  подтвердить БЕЗ бессмысленного тарифного селектора backorder.
- Тесты — герметичные (никакой живой сети), мокают `OptimizatorClient` так же, как
  существующие тесты мокают `BackorderClient` (см. `test_m23_fixes.py`,
  `test_order_recovery.py`, `test_backorder_order.py` — паттерн переносится 1:1).
