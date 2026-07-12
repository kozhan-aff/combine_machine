# backorder.ru — API reference (M2 Acquisition, канал backorder)

> Статус документа: собран из **официальной документации** doc.backorder.ru + **живой проверки**
> публичных эндпойнтов (2026-07-05). Всё, что не подтверждено официально или проверкой,
> помечено **UNVERIFIED**. Эндпойнты не выдумывались: если что-то неизвестно — так и написано.

## Назначение (Purpose)

backorder.ru — российский drop-catch / backorder сервис для доменов **.RU / .РФ** (и рег. .SU/gTLD
как регистратор). Работает поверх **ISPsystem BILLmanager (billmgr), редакция Corporate** —
подтверждено ответом `func=auth` (`"copyright": "...BILLmanager-Corporate"`).

Для нашего пайплайна из сервиса нужны две вещи:

1. **Discovery / drop-list** — фид освобождающихся доменов (input для скоринга M1). Есть публичная
   выгрузка JSON/CSV с фильтром по числу доноров. **Найдено и проверено** (см. ниже).
2. **Backorder-заказ** — поставить ставку на конкретный домен по тарифу и опросить статус (М2, за
   ручным гейтом `confirmed_by_human=true`). Официально документировано, форма из тех-брифа подтверждена
   1:1.

Ценные дропы → сюда (backorder, ставка/тариф). Свободные чистые → optimizator (другой канал, см. PIPELINE.md).

---

## Base URL

| Что | URL |
|-----|-----|
| Основной сайт / поиск | `https://backorder.ru/` |
| **billmgr API** | `https://backorder.ru/manager/billmgr` |
| **Выгрузка JSON** (drop-list) | `https://backorder.ru/json/?<filter>` |
| **Выгрузка CSV** (drop-list) | `https://backorder.ru/csv/?<filter>` |
| Тарифы (price JSON) | `https://backorder.ru/manimg/userdata/json/price_ru_backorder.ru.json` |
| Личный кабинет | `https://backorder.ru/manager` |
| Документация | `https://doc.backorder.ru/servis-backorder-ru` |

---

## Авторизация (Auth)

Два независимых контура:

### A. Публичный (без авторизации)
`/json/`, `/csv/`, `price_*.json` отдаются **без авторизации** — проверено, отдают данные анонимно.
Это и есть канал discovery. Фильтры «мои заказы»/«корзина» из веб-интерфейса требуют сессию, но
для drop-list они не нужны.

### B. billmgr API (per-request basic auth)
Логин/пароль **аккаунта Сервиса** (тот же, что для входа в ЛК) передаются прямо в query как:

```
authinfo=$login:$password
```

Это HTTP-basic поверх billmgr — сессия/куки не нужны, каждый запрос самодостаточен.
(Классический billmgr также поддерживает `func=auth&username=..&password=..` → сессионный ключ `auth=<id>`,
но официальная дока backorder использует именно `authinfo=login:password` — идём по нему.)

Формат ошибки авторизации (проверено):
```json
{"error" : {"code" : "100","obj" : "","msg" : "Auth failed from 134.17.153.221"}}
{"error" : {"code" : "100","obj" : "","msg" : "access deny"}}
```
Признак ошибки — наличие ключа `error` верхнего уровня с `code`/`msg`. Это надёжный маркер для парсера.

### Общий паттерн billmgr-запроса
```
https://backorder.ru/manager/billmgr?func=<ФУНКЦИЯ>&out=json&<params>&authinfo=$login:$password
```
- `func=` — имя функции (например `accountinfo`, `clientbackorder`, `uniservice.order`).
- `out=json` — формат ответа (billmgr также умеет `out=xml`, но нам нужен json).
- Метод — **GET** (все примеры в офиц. доке — GET со всеми параметрами в query).
- **⚠ Секреты в query.** Логин/пароль уходят в URL. Не логировать «сырые» URL, не тащить в историю
  shell/прокси. httpx — параметры через `params=`, не конкатенацией строк.

---

## 1. Тарифы — price JSON (проверено)

`GET https://backorder.ru/manimg/userdata/json/price_ru_backorder.ru.json` → массив тарифов (~116 записей,
статичный JSON, без авторизации).

Нужны две вещи из тарифа при заказе: **`price_id`** и **`period_id`**.

- `price_id` = `elem.id` (верхнеуровневое поле `id`).
- `period_id` = `elem.period[0].id`.

Форма одной записи (backorder-тариф, `type_id=63`):
```json
{
  "id": "4769",
  "name": "Тариф 190 .RU",
  "grp": "Заказы на освобождающиеся доменные имена в .RU",
  "type": "Освобождающиеся доменные имена",
  "type_id": "63",
  "price": "190.0000 RUB / 190",
  "period": [
    { "id": "3442", "name": "190", "price": "190.0000 RUB", "price_num": "190.0000",
      "p_length": "1", "per_type": "unlimited" }
  ],
  "project_id": "14",
  "service_kind": "11",
  "order_link": "https://backorder.ru/manager/billmgr?func=register&welcomfunc=clientbackorder.order.datacenter&welcomparam=price=4769&project=14"
}
```

Ключевые поля записи: `id` (=price_id), `name`, `grp` (группа-зона), `type_id` (63 = освобождающийся домен;
3 = обычная регистрация; 20 = DNS; 28 = SSL — их для backorder не берём), `period[0].id` (=period_id),
`period[0].price_num` (цена ₽), `order_link` (ссылка на ordering-визард в ЛК).

**Фильтровать backorder-тарифы: `type_id == "63"`.** Зона — по полю `grp` («... в .RU» / «... в .РФ»).

Примеры пар price_id / period_id (важное подмножество; полный список — в JSON):

| Тариф | Зона | price_id (`id`) | period_id (`period[0].id`) | Цена ₽ |
|-------|------|-----------------|----------------------------|--------|
| Тариф 190 | .RU | 4769 | 3442 | 190 |
| Тариф 400 | .RU | 4770 | 3443 | 400 |
| Тариф 550 | .RU | 4771 | 3444 | 550 |
| Тариф 1 100 | .RU | 4772 | 3445 | 1100 |
| Тариф 2 000 | .RU | 4773 | 3446 | 2000 |
| Тариф 190 | .РФ | 4790 | 3463 | 190 |
| Тариф 400 | .РФ | 4791 | 3464 | 400 |
| Тариф 550 | .РФ | 4792 | 3465 | 550 |

Сетка тарифов сплошная — от **190 ₽** до нескольких млн ₽ (шаги 190/400/550/1100/2000/3000/4500/6000/10000…).
Цена тарифа = ставка/приоритет: чем выше тариф, тем к большему числу регистраторов Сервис отправит заказ и
тем выше вероятность перехвата (см. «Вероятность исполнения»). Не путать с фиксированной ценой домена.

> **Не хардкодить id.** Тянуть price/period_id из этого JSON на старте и кешировать — id стабильны, но
> сетка иногда пополняется (в текущем JSON видны id до 9724). По зоне+сумме выбирать нужный `id`.

---

## 2. Discovery / drop-list — выгрузка JSON/CSV (ПРОВЕРЕНО, главный вход пайплайна)

Официально: раздел «Использование API → Выгрузка данных (CSV, JSON)».
> «Любая выборка данных на сайте Сервиса может быть получена в формате JSON или CSV. Текущее значение
> строки параметров фильтров содержится в URL после знака `#`. Выгрузка … принимает аналогичную строку фильтра.»

```
GET https://backorder.ru/json/?<filter-string>
GET https://backorder.ru/csv/?<filter-string>
```
- Авторизация не нужна (публично).
- `page` и `items` в выгрузке **игнорируются**; выгрузка отдаёт весь отфильтрованный набор,
  **лимит 30000 записей**.
- JSON = массив объектов (`[]` если пусто — валидный ответ, проверено).

### Строка фильтра — параметры

Строка фильтра — это тот же query, что сайт кладёт в `#`-хеш при выставлении фильтров. Ниже —
**проверенные** флаги (эмпирически, 2026-07-05) и их семантика:

| Параметр | Значение | Смысл | Статус |
|----------|----------|-------|--------|
| `ext` | `1` | расширенные данные записи (доноры/трафик/ИКС в ответе). **Ставить всегда.** | проверено |
| `disp` | `1` | «интересные домены» — на которые есть действующие заказы у регистраторов | проверено |
| `expired` | `1` | домены с истёкшим сроком | проверено |
| `pending_delete` | `1` | статус pendingDelete — **удаляются сегодня** | проверено (был `[]` на момент теста — сегодня пусто) |
| `tomorrow` | `1` | **удаляются завтра** | проверено (сужает выборку; см. gotcha про дату) |
| `free` | `1` | свободные для повторной регистрации | проверено (`0` на момент теста) |
| `rkn` | `1` | входят в реестр РКН | проверено |
| `judicial` | `1` | судебные домены | проверено |
| `links` | `N` (целое) | **домены с ≥ N ссылающихся доноров** (порог, не равенство) | **проверено** |
| `by` | имя колонки | сортировка: `hotness`, `yandex_tic`, `links`, `visitors`, `x_value`, `old`, `price`, `delete_date`, `domainname` | частично проверено (`hotness`,`yandex_tic` точно; остальные — по колонкам таблицы) |
| `order` | `asc` \| `desc` | направление сортировки | проверено |
| `page`, `items` | int | пагинация **на сайте**; в выгрузке игнорируются | проверено (док + тест) |

**`links=N` — порог по донорам (ключевое для M1).** Проверено на живых данных (2026-07-05):
`links=1` → 466 записей (min доноров = 1), `links=10` → 336 (min = 10), `links=1000` → 19 (min ≈ 1092).
То есть `links=N` = «доноров ≥ N». Для требования DONORS.md «≥1 донор» → **`links=1`**.

Не подтверждены/не найдены имена параметров (веб-фильтры существуют, но точный ключ query неизвестен —
**UNVERIFIED**): выбор зоны (.RU vs .РФ отдельно; пробы `zone=rf` не сработали), выбор конкретного
регистратора, диапазоны Возраст/Длина/Дата-освобождения/Трафик/тИЦ, флаги-исключения («домены с тире»,
«с цифрами», «в стоп-листе», «заказанные»). Способ узнать точно: открыть поиск на сайте, выставить фильтр,
снять query из `#`-хеша URL — это и есть строка выгрузки (так прямо сказано в офиц. доке).

### Готовые примеры (из офиц. доки)
```
# интересные истёкшие, по тИЦ
https://backorder.ru/json/?order=desc&ext=1&disp=1&expired=1&by=yandex_tic&page=1&items=50
# интересные, удаляются сегодня, по «горячести»
https://backorder.ru/json/?order=desc&ext=1&disp=1&pending_delete=1&by=hotness&page=1&items=50
# судебные
https://backorder.ru/json/?order=desc&ext=1&judicial=1&by=hotness&page=1&items=50
# свободные, в реестре РКН
https://backorder.ru/json/?order=desc&ext=1&rkn=1&free=1&by=yandex_tic&page=1&items=50
```

### Запрос под НАШ пайплайн (проверено, отдаёт данные)
«Освобождаются завтра, ≥1 донор, по убыванию доноров»:
```
https://backorder.ru/json/?ext=1&disp=1&tomorrow=1&links=1&by=links&order=desc
```
На 2026-07-05 вернул 143 записи (все с `links ≥ 1`, `delete_date` = 2026-07-07). Без `disp=1` набор шире.

### Форма записи ответа JSON (проверено на живых данных)
```json
{
  "domainname": "example.ru",
  "hotness": 0,
  "price": 190,
  "x_value": -1,
  "yandex_tic": 0,
  "links": 35,
  "visitors": -1,
  "registrar": "REGRU-RU",
  "old": 7,
  "delete_date": "2026-07-08",
  "rkn": false,
  "judicial": false,
  "block": false
}
```

| Поле | Тип | Смысл | В наш пайплайн (DONORS.md) |
|------|-----|-------|----------------------------|
| `domainname` | str | имя домена (для .РФ — кириллица, НЕ punycode) | ключ кандидата |
| `links` | int | **число ссылающихся доноров (referring sites)** | прокси RD для стадии A/B; фильтр «≥1 донор» |
| `yandex_tic` | int | тИЦ (Яндекс, историч.); `0`/пусто = нет данных | вспом. сигнал |
| `x_value` | int | ИКС (Яндекс X); `-1` = нет данных | вспом. сигнал |
| `visitors` | int | трафик/визиты; `-1` = не измерено | прокси орг-трафика |
| `hotness` | int | «горячесть»/спрос (внутр. метрика Сервиса) | приоритезация очереди |
| `old` | int | возраст (лет; проверить единицу) — **UNVERIFIED единица** | стадия D (возраст/непрерывность) |
| `delete_date` | str `YYYY-MM-DD` | ожидаемая дата освобождения/удаления | планирование ставки |
| `registrar` | str | регистратор-держатель (напр. `REGRU-RU`, `RU-CENTER-RU`, `DOMENUS-RU`, `REGTIME-RF`) | контекст |
| `price` | int | минимальный тариф ₽, с которого можно заказать | стоимость ставки |
| `rkn` | bool | в реестре РКН | стадия E → reject (`rkn_listed`) |
| `judicial` | bool | судебный домен | юр-риск |
| `block` | bool | в стоп-листе / заблокирован к регистрации | reject |

`-1` и пустая строка в метриках = **«нет данных»**, не ноль. Обрабатывать как NULL.

### CSV-вариант (те же данные, разделитель `;`)
Заголовок (проверено):
```
domainname;hotness;price;x_value;yandex_tic;links;visitors;registrar;old;delete_date;rkn;judicial;block;
```
JSON предпочтительнее (типизирован, `-1`/`false` явные). CSV — если нужен стрим/большой объём.

---

## 3. Вспомогательные billmgr-функции (для заказа) — официально документировано

Все — `GET .../manager/billmgr?func=...&out=json&authinfo=$login:$password`.

**Обёртка верхнего уровня — ПРОВЕРЕНО ЖИВЬЁМ (2026-07-11, боевой аккаунт):** это `{"elem": [ ... ]}`,
НЕ `{"doc": {...}}`. Ошибка приходит как `{"error": {"code": "...", "msg": "..."}}`. Пути `elem[0].id` /
`elem[n].id` из доки подтверждены. Реализация — `integrations/backorder.py::_billmgr()`.

### 3.1 Баланс/лицевой счёт → `account_id`
```
GET https://backorder.ru/manager/billmgr?func=accountinfo&out=json&authinfo=$login:$password
```
`account_id = elem[0].id` — ID лицевого счёта. Нужен для `payfrom` при заказе.

Форма живого ответа (проверено 2026-07-11): `{"elem":[{"id":"<account_id>","name":"BackOrder.ru",
"currency":"Russian Ruble","balance":"0.00","creditlimit":"0.0000"}]}`. **`balance` — критично:**
`uniservice.order` идёт с `paynow=on`, поэтому при нулевом балансе заказ создаётся, но повисает в
`id_status=2` «Не оплачен» и домен НЕ перехватывается. Панель показывает баланс в шапке `/queue`
до отправки (`BackorderClient.balance()`). Значение для `.env` — брать из своего живого ответа.

### 3.2 Контакты администратора → `contact_id`
```
GET https://backorder.ru/manager/billmgr?func=domaincontact&out=json&authinfo=$login:$password
```
`contact_id = elem[n].id` — ID контакта администратора домена. Нужен для `contact` при заказе.
Контакт («Контакт доменов») и плательщика надо предварительно создать в ЛК; данные должны быть
**достоверными** — на них регистрируется домен при успешном перехвате и их проверяет регистратор.

Форма живого ответа (проверено 2026-07-11): `{"elem":[{"id":"<contact_id>","name":"<имя>",
"type":"Персональный"}]}`.
⚠ Значение НЕ угадывать: в `.env` лежал неверный `BACKORDER_CONTACT_ID` (2026-07-11) — заказ с чужим
контактом отбивается регистратором. Брать строго из этого вызова.

### 3.3 Список заказов → `elid` и статус
```
GET https://backorder.ru/manager/billmgr?func=clientbackorder&out=json&authinfo=$login:$password
```
Возвращает заказы клиента; `elid = elem[n].id` — ID зарегистрированного заказа. **Это же — источник
статуса заказа** (поле статуса — см. §5, id_status/clear_status). Именно этот `func` опрашиваем для
поллинга «поймали / не поймали».

---

## 4. Постановка backorder-заказа — `uniservice.order` (официально; форма подтверждена 1:1)

```
GET https://backorder.ru/manager/billmgr?func=uniservice.order&out=json
    &period=$period_id
    &price=$price_id
    &domainname=$DOMAIN
    &itype=63
    &sok=ok
    &payfrom=account$account_id
    &contact=$contact_id
    &paynow=on
    &clientbackorder=yes
    &authinfo=$login:$password
```
(в один URL, без переносов). Форма из BUILD_SPEC совпадает с офиц. докой дословно.

| Параметр | Значение | Пояснение |
|----------|----------|-----------|
| `func` | `uniservice.order` | функция размещения заказа |
| `out` | `json` | формат ответа |
| `period` | `$period_id` | = `period[0].id` выбранного тарифа (§1) |
| `price` | `$price_id` | = `id` выбранного тарифа (§1); задаёт ставку/приоритет |
| `domainname` | `$DOMAIN` | заказываемый домен (для .РФ — кириллица) |
| `itype` | `63` | тип услуги = освобождающийся домен (совпадает с `type_id=63` тарифа) |
| `sok` | `ok` | подтверждение формы (submit ok) — не менять |
| `payfrom` | `account$account_id` | **литерал `account` + ID счёта слитно**: `account_id=123456` → `payfrom=account123456` |
| `contact` | `$contact_id` | ID контакта администратора (§3.2) |
| `paynow` | `on` | оплатить сразу с баланса |
| `clientbackorder` | `yes` | пометка «клиентский backorder-заказ» |
| `authinfo` | `$login:$password` | авторизация (§B) |

> Офиц. дока: «остальные параметры строки менять не нужно». Меняем только `period`, `price`,
> `domainname`, `payfrom`(account_id), `contact`, `authinfo`.

**Гейт проекта:** этот вызов = трата денег с баланса (`paynow=on`). Пускать ТОЛЬКО при
`confirmed_by_human=true` (PLAN.md §2, правило 2). Никакого автопилота на `uniservice.order`.

**Апселл тарифа:** при заказе Сервис может предложить тариф выше выбранного (выше тариф → выше
вероятность перехвата: по офиц. примеру тариф «150» даёт всего ~4%). Решение о повышении — тоже
через человека (это про деньги).

### 4.1 Изменить тариф уже размещённого заказа — `clientbackorder.edit`
```
GET https://backorder.ru/manager/billmgr?func=clientbackorder.edit&out=json&elid=$elid_id&price=$price_id&sok=ok
```
`elid` = ID заказа (из §3.3), `price` = новый `price_id`. (authinfo так же требуется.)

---

## 5. Order flow: разместить → опросить статус

```
1. GET accountinfo   → account_id            (один раз, кешировать)
2. GET domaincontact → contact_id            (один раз, кешировать)
3. price JSON        → выбрать price_id + period_id по зоне и сумме ставки
   --- ГЕЙТ: confirmed_by_human == true ---
4. GET uniservice.order (§4)  → заказ создан, деньги списаны с баланса
5. POLL: GET clientbackorder → найти запись по domainname/elid → читать статус
```

### Статусы заказа (API) — проверено по офиц. доке
У статусов есть `id_status` (число) и `clear_status` (смысл):

| id_status | clear_status | Трактовка для нас |
|-----------|--------------|-------------------|
| 2 | Не оплачен | ожидает оплаты (баланс?) |
| 3 | Перекрыт (заказ не может быть исполнен) | fail — не исполнится |
| 4 | Ожидает исполнения (активен) | активен, ждёт окна удаления |
| 5 | Принят | принят в работу |
| 6 | Аннулирован (домен продлён) | fail — владелец продлил домен |
| 7 | Аннулирован (неудачная попытка регистрации) | **fail — не поймали** |
| 8 | Готов (домен в процессе передачи) | почти успех — идёт передача |
| 9 | Аннулирован (удалён) | fail — заказ снят |
| 10 | Исполнение (активен) | идёт попытка перехвата |
| 11 | **Завершён** | **успех — домен пойман/зарегистрирован** |
| 12 | Аннулирован (приём заказа закрыт) | fail — приём закрыт |
| 13 | В обработке | промежуточный |
| 14 | Аннулирован | fail — отменён |

Маппинг для машины состояний M2:
- **caught / success:** `11` (и `8` — «в процессе передачи», ведёт к 11).
- **failed:** `3`, `6`, `7`, `9`, `12`, `14`.
- **pending / in-flight:** `2`, `4`, `5`, `10`, `13`.

Поллинг: дёргать `func=clientbackorder` вокруг `delete_date` (день удаления). После разрешения статуса
в `11` (успех) — домен зарегистрирован на `contact_id`, дальше M3 (Cloudflare NS + aaPanel).

> Точное имя поля статуса в JSON `clientbackorder` — **UNVERIFIED** (нет тестового аккаунта). По логике
> billmgr это `elem[n].status` / `id_status`; сверить на первом реальном заказе. Смысловые значения —
> из офиц. таблицы выше.

---

## 6. Rate limits / стоимость

- **Тарифы от 190 ₽**, сплошная сетка вверх (§1). Тариф = приоритет/ставка, списывается **с баланса
  лицевого счёта** (`payfrom=account…`, `paynow=on`).
- Возврат средств возможен (раздел «Возврат средств» / «Аннулирован (…)» → средства). Условия — в
  офиц. доке `oformlenie-zakaza/vozvrat-sredstv`.
- Выгрузка JSON/CSV: лимит **30000 записей** на запрос; `page`/`items` игнорируются. Явных численных
  rate-limit не опубликовано — **UNVERIFIED**; вести себя вежливо: кеш price/list, бэкофф на httpx,
  не дёргать выгрузку чаще, чем меняется дроп-лист (реально — раз в день/несколько часов; дропы
  публикуются ежедневно кроме сб/вс/праздников, >10000 доменов/день).
- Есть **Telegram-бот** и бонусная программа (для мониторинга/уведомлений; не API как таковой).

---

## 7. Gotchas (грабли)

1. **`payfrom=account$account_id` слитно** — литерал `account` + число без разделителя (`account123456`),
   не `payfrom=123456`. Легко ошибиться.
2. **Неизвестные фильтры выгрузки молча игнорируются.** Опечатка в имени параметра (`donors=1`,
   `pending_tomorrow=1`, `zone=rf`) НЕ даёт ошибку — сервис возвращает дефолтный набор (в тесте — 882
   записи). Всегда проверять, что фильтр реально сузил выборку, а не «прошёл мимо». Инвариант для кода:
   после запроса убедиться, что все записи удовлетворяют фильтру (напр. все `links ≥ N`).
3. **`tomorrow=1` vs `delete_date`.** На 2026-07-05 `tomorrow=1` дал `delete_date=2026-07-07` (не 07-06).
   Похоже, «удаление завтра» и «дата освобождения» (`delete_date`) сдвинуты на день (домен освобождается
   на следующий день после удаления). **Не** приравнивать `tomorrow` к «`delete_date == сегодня+1`».
   Фильтроваться по флагу (`tomorrow`/`pending_delete`), а не пересчитывать дату руками.
4. **`-1` и пустая строка = нет данных**, не ноль (`visitors`, `x_value`, `yandex_tic`). Иначе испортите
   скоринг (домен без данных о трафике выглядел бы как «0 трафика»).
5. **.РФ домены — кириллица, не punycode** в `domainname` (и в выдаче, и, предположительно, в заказе;
   для заказа .РФ проверить, не нужен ли punycode — **UNVERIFIED**).
6. **Секреты в URL.** `authinfo=login:password` в query → не логировать сырые URL, httpx `params=`.
7. **`links` ≠ качество.** Это только число доноров (referring sites) от Сервиса — грубый прокси RD.
   Реальную оценку доноров (анкоры, live/lost, тематика, спам) делает M1 по DONORS.md, не по этому полю.
   Использовать `links` только как дешёвый pre-filter стадии A.
8. **Апселл тарифа** при заказе (Сервис предложит дороже) — это решение о деньгах, за человеческий гейт.
9. **`out=json` обёртка.** Ошибки — плоский `{"error":{...}}`. Успешные authed-ответы, вероятно,
   в `{"doc":{...}}` (типично для billmgr) — распарсить по факту на первом заказе.

---

## 8. Open questions / low-confidence (**UNVERIFIED**)

- **Имена query-параметров для тонких фильтров выгрузки:** зона (.RU/.РФ по отдельности), конкретный
  регистратор, диапазоны Возраст/Длина/Дата-освобождения/Трафик/тИЦ, флаги-исключения. Проверено только
  подмножество (`ext,disp,expired,pending_delete,tomorrow,free,rkn,judicial,links,by,order`). Точные имена
  снять из `#`-хеша сайта при ручном выставлении фильтров. **Это единственный существенный пробел**, но
  главный нужный фильтр — `links=N` (доноры ≥ N) и `tomorrow=1` — уже подтверждён.
- **Точная JSON-обёртка и имена полей** ответов `accountinfo` / `domaincontact` / `clientbackorder`
  (`doc.elem[]`? имя поля статуса?) — нет тестового аккаунта. Пути `elem[0].id` / `elem[n].id` — по офиц. доке.
- **Единица `old`** (лет? записей истории?) — по контексту «Возраст», предположительно лет.
- **`hotness`** — точная формула «горячести» не документирована.
- **Заказ .РФ:** кириллица или punycode в `domainname` при `uniservice.order`.
- **Численные rate-limit** billmgr/выгрузки — не опубликованы.
- **Полный список `by=`** значений сверх проверенных `hotness`/`yandex_tic`.

### Смежный сервис (не backorder.ru, но родственная инфраструктура) — на заметку
Веб-поиск выявил **expired.ru** (тоже drop-catcher на billmgr) с более «чистым» API-неймингом:
`https://api.expired.ru/billmgr` c функциями `backorder.api.list`, `backorder.api.prices`,
`backorder.api.set`, `backorder.api.delete`. Это **другой сервис** (не backorder.ru), но если у
backorder.ru discovery-фильтры окажутся неудобны, expired.ru — кандидат на второй источник дропов.
Не проверялось в этой сессии — **UNVERIFIED**, вынести в отдельное исследование при необходимости.

---

## 9. Источники (Source URLs)

Официальная документация (Wiki.js, doc.backorder.ru):
- Оглавление руководства: https://doc.backorder.ru/servis-backorder-ru
- **Выгрузка данных (CSV, JSON):** https://doc.backorder.ru/servis-backorder-ru/ispolzovanie-api/vygruzka-dannyh
- **Заказ домена (uniservice.order):** https://doc.backorder.ru/servis-backorder-ru/ispolzovanie-api/zakaz-domena
- Изменение тарифа заказа: https://doc.backorder.ru/servis-backorder-ru/ispolzovanie-api/izmenenie-tarifa-zakaza
- **Статусы заказа API:** https://doc.backorder.ru/servis-backorder-ru/ispolzovanie-api/statusy-zakaza-api
- Фильтры расширенного поиска: https://doc.backorder.ru/servis-backorder-ru/sistem-poiska/filtry-rasshirennogo-poiska
- Фильтры быстрого поиска: https://doc.backorder.ru/servis-backorder-ru/sistem-poiska/filtry-poiska-i-ih-svojstva
- Вкладка «Доноры»: https://doc.backorder.ru/servis-backorder-ru/sistem-poiska/vkladka-donory
- Необходимо для заказа: https://doc.backorder.ru/servis-backorder-ru/oformlenie-zakaza/neobhodimo-dlya-zakaza
- Регистрация заказа / апселл тарифа: https://doc.backorder.ru/servis-backorder-ru/oformlenie-zakaza/registraciya-zakaza
- Вероятность исполнения заказа: https://doc.backorder.ru/servis-backorder-ru/oformlenie-zakaza/veroyatnost-ispolneniya

Живые эндпойнты (проверены 2026-07-05):
- Price JSON: https://backorder.ru/manimg/userdata/json/price_ru_backorder.ru.json
- Выгрузка JSON: https://backorder.ru/json/?ext=1&disp=1&tomorrow=1&links=1&by=links&order=desc
- Выгрузка CSV: https://backorder.ru/csv/?ext=1&disp=1&tomorrow=1&links=1&by=links&order=desc
- billmgr (envelope/ошибки): https://backorder.ru/manager/billmgr?func=accountinfo&out=json

Вторичные / контекст:
- О проекте: https://backorder.ru/about/
- Хабр «Как настроить мониторинг освобождающихся ru и рф доменов»: https://habr.com/ru/articles/787196/
- Смежный сервис expired.ru (API на billmgr): https://expired.ru/faq/
