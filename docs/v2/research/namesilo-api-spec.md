# Спецификация клиента NameSilo API для «Комбайна» (v2, подпроект 2)

Сбор: 2026-10-07. Метки источников:
- **[док]**: официальная страница `namesilo.com/api-reference/pages?uid=…` или support-статья NameSilo.
- **[3p]**: сторонний источник (Blesta-модуль, Go/Python-клиенты, NamePros, агрегаторы цен).
- **[live]**: моя проба с заведомо неверным ключом, без трат и без изменения состояния.
- **[?]**: не подтверждено, снимается только на аккаунте.

Код репозитория не трогал.

## Резюме (5 строк)
1. NameSilo закрывает всё нужное через GET-API: `checkRegisterAvailability` (до 200 доменов), `registerDomain`, `getPrices`, `getAccountBalance`, `changeNameServers`, `getDomainInfo`, `listDomains`. Автоматизацию обязаны слать на `/apibatch`, иначе блок и риск бана аккаунта. Ключ можно привязать к 5 IP.
2. У `registerDomain` нет ни `cost`, ни ключа идемпотентности. Денежный гейт придётся строить самим: свежий `checkRegisterAvailability.price`, проверка `premium=0`, write-ahead в БД, никаких ретраев, разбор неизвестного исхода через `getDomainInfo`. Реестр не даст зарегистрировать один домен дважды, поэтому двойного списания по одному домену быть не должно.
3. Ловля дропов: в API есть `registerDomainDrop` (только .com/.net, `/apibatch`, только баланс), но это лотерея против 750–1000+ аккредитаций DropCatch. Для .com с реальным RD (то есть заведомо востребованных) шанс близок к нулю. Единственный детерминированный путь через API — **аукционы просроченных доменов NameSilo** (`listAuctions typeId=3`, `bidAuction`): работает только для доменов, просроченных у самого NameSilo, и дата создания сохраняется.
4. Реальные коды ошибок есть в зеркале официальной таблицы (Blesta + python-namesilo, взаимно согласованы). Страницу «Response Codes» NameSilo отдаёт только SPA-чанком, прочитать её не удалось. Вживую найдено: `reply.code` приходит то числом, то строкой, а HTTP 401 бывает у части операций. Парсим тело, не статус.
5. Готовых Python-клиентов под нашу лицензию нет: `python-namesilo` под GPL-3.0, значит писать своё на httpx (~150 строк). Sandbox существует (`sandbox.namesilo.com`, ключ выдают по e-mail), но поддержку .co.uk/.mx/.nl/.si и поведение `registerDomain` при повторе проверять живым тестом.

## 1. Транспорт и аутентификация
| Пункт | Значение |
|---|---|
| Прод | `https://www.namesilo.com/api/OPERATION?version=1&type=json&key=KEY&…` [док] |
| **Автоматизация** | `https://www.namesilo.com/apibatch/OPERATION?…`, «Batch use must ONLY use /apibatch», `/api` для батча «violates ToS», режется в реальном времени, вплоть до бана API/аккаунта [док: api-automated-batch]. Ответы идентичны [3p, проба 2026-10-05] |
| Sandbox | `https://sandbox.namesilo.com/api/`: хост живой, неверный ключ даёт код 110 [live]. Креды нужны отдельные, запросить письмом в поддержку (~1 час) [док/3p]. Go-клиент знает ещё `https://ote.namesilo.com/api`, он тоже отвечает [live] |
| Метод | Только GET, https. Ключ только в query (код 120: «key must be passed as GET»). **Ключ попадает в URL: маскировать в логах httpx/Sentry** |
| Формат | `type=json` или `xml`. Версия `1` |
| Привязка по IP | До 5 IP в API Manager. Чужой IP даёт код 113. Ключ показывается один раз и не восстанавливается [док: api-manager] |
| Лимиты | Официально не заданы. Рекомендация из блога NameSilo: ≤1 запрос/с на IP, к одному домену ≤1 в 2 с, ≤5 одновременных соединений, при нагрузке лимиты могут срезать динамически [док-блог]. «Misuse of API» на быстрых сериях по Blesta #128 [3p] |
| ToS | T&C не прочитан: страница за Cloudflare-challenge. Политика API-доступа прочитана: автоматизация разрешена на `/apibatch`, API бесплатный, без платы за вызов. Мониторинг ловит повторные попытки той же операции по тому же домену и высокую долю «неуспешных» ответов [док] |

**Живые находки [live, 2026-10-07, ключ INVALID]:**
- `checkRegisterAvailability`: HTTP 200, `{"reply":{"code":"110","detail":"Invalid API Key (Permission denied)"}}`. Код **строкой**, operation заполнен.
- `getPrices` и `getAccountBalance`: **HTTP 401**, `{"request":{"operation":null,…},"reply":{"code":110,"detail":"Invalid API key"}}`. Код **числом**, текст другой.
- Неизвестная операция: HTTP 200, `{"code":"107","detail":"Invalid API operation"}`. Ключ не передан: `"109"`.
- Вывод: два бэкенда (старый и новый). Клиент обязан читать тело при любом HTTP-статусе (в том числе 401), делать `int(reply["code"])`, и не полагаться на `raise_for_status`. Перед Cloudflare возможны HTML-страницы: не-JSON тело считается ошибкой транспорта, а не ответом API.
- JSON-нормализация: один дочерний элемент приходит объектом, несколько списком (в доке `"unavailable":{"domain":…}` и `"invalid":{…}`). Нужен хелпер `as_list()`. Доки местами с опечатками (в JSON-примере `getPrices` пропущена запятая, в `whoisInfo` поле `details`).

## 2. Методы (нужные подпроекту 2)
| Метод | Параметры | Успех (из доков) | Заметки |
|---|---|---|---|
| `checkRegisterAvailability` | `domains` (через запятую, **до 200**) | `reply.available[]` = `{domain, price, premium, duration}`, `unavailable`, `invalid` | Цена в USD за `duration` лет. В живом ответе есть `renew` [3p, npm]. `premium:1` значит цена премиальная. Неподдерживаемая зона, вероятно, попадает в `invalid` [?] |
| `registerDomain` | `domain`, `years`=1–10; опц.: `payment_id`, `private` (**по умолчанию без приватности**), `auto_renew` (**по умолчанию ВКЛ**), `portfolio`, `ns1..ns13` (≥2), `coupon`, `contact_id` или `fn,ln,ad,cy,st,zp,ct,em,ph` | `{code:300, detail:"success", message:"Your domain registration was successfully processed.", domain, order_amount:7.77}` | Без `payment_id` списывает с баланса. Параметра `cost`/`max_price` **нет**. Для .us нужны `usnc`/`usap`, для .ca профиль, для .eu `eucs`; про .uk/.mx/.nl/.si/.in доки молчат [док] |
| `registerDomain` (claims) | то же + `submit_claims` (повторяемый), `submit_date` | Первый ответ: `code:210`, `claims[]` (TMCH-уведомление) | Домен не зарегистрирован, регистрация «ещё не завершена». Подтверждать только вручную [док: register-domain-claims] |
| `registerDomainDrop` | `domain`, `years`, `private`, `auto_renew`; **только `/apibatch`**, только баланс, только .com/.net | то же, что у `registerDomain` | Окно по доке **11:45–15:45 UTC** (см. §6). Контакт и NS только дефолтные |
| `getPrices` | опц. `retail_prices`, `registration_domains` | `reply.com={registration, transfer, renew}`, `reply.net={…}` (ключ = TLD) | Без `retail_prices` отдаёт цены с Discount Program. Ключ для составных зон (`co.uk`) [?]. Это же и список поддерживаемых TLD |
| `getAccountBalance` | нет | `{code:300, detail:"success", balance:355.75}` | Число с плавающей точкой, USD. Блокировка средств под ставки в баланс не вычитается (см. аукционы) |
| `changeNameServers` | `domain` (до 200 через запятую), `ns1`, `ns2` обязательны, до `ns13` | `{code:300, detail:"success"}` | Ошибка NS: 254 с деталями. Для Cloudflare обычно `x.ns.cloudflare.com` |
| `getDomainInfo` | `domain` | `created`, `expires`, `status:"Active"`, `locked`, `private`, `auto_renew`, `nameservers:[{nameserver:"NS1.NAMESILO.COM",position:1}]`, `contact_ids{registrant,administrative,technical,billing}`, `portfolio`, `traffic_type` | NS в **верхнем регистре**, сравнивать без регистра. Чужой или неактивный домен: код 200 |
| `listDomains` | опц. `portfolio`, `page`, `pageSize`, `skipExpired`, `expiredGrace`, `withBid` | `domains[]`={domain, created, expires[, maxBid]}, `pager{page,pageSize,total}` | `pageSize`/`total` в JSON строками. Максимум `pageSize` не документирован [?] |
| `contactAdd`/`contactList` | поля как у регистрации (`fn,ln,ad,cy,st,zp,ct,em,ph`; `nn` опц.) | `contact_id`; `contactList`: до 1000 за запрос, `offset` | Страну передавать кодом, телефон без кода страны. Для US/CA ровно 10 цифр [док] |
| `contactDomainAssociate` | `domain` + хотя бы одно из `registrant/administrative/billing/technical` (id контакта) | `{code:300}` | **Асинхронно**: 300 значит «принято в очередь», не «применено в реестре» [док] |
| `addPrivacy`/`removePrivacy` | `domain` | 300; уже включено: **255**, уже выключено: 256 | `private=1` в `registerDomain` бесплатный [док]. Но у зон без поддержки privacy (см. §5) не сработает |
| `addAutoRenewal`/`removeAutoRenewal` | `domain` | 300; уже так: **250**/251 (информационные) | Коды 250–253, 255, 256 — «ничего не изменено», не ошибка |
| `listOrders` / `orderDetails` | `date_from`, `date_to` / `order_number` | заказ: `order_number, order_date, method (напр. "Bitcoin"), total`; детали: `description "domain - registration"`, `price`, `status "Processed"/"Credited"` | Основной след платных операций для сверки неизвестного исхода |
| `listExpiringDomains` / `countExpiringDomains` | `daysCount`, `page`, `pageSize` | `body.entry[]` | Только **наши** домены, к дропам не относится |
| `addAccountFunds` | `amount`, `payment_id` (верифицированная карта) | `new_balance` | Крипта через API не пополняется, только через сайт [?] |

## 3. Карта кодов ответа
Источник: зеркало официальной таблицы в Blesta-модуле (`NETLINK/Blesta-Namesilo config/codes.php`) и список исключений `python-namesilo`, они совпадают. **Страница «Response Codes» NameSilo прочитана не была [?]**.

| Код | Смысл | Класс для клиента |
|---|---|---|
| 300 | Успех | OK |
| 301 | Регистрация успешна, но NS недействительны, взяты дефолтные NameSilo | OK + предупреждение: выполнить `changeNameServers` и сверить |
| 302 | Успех, но контакт с ошибкой, взят дефолтный профиль | OK + предупреждение (WHOIS на дефолтном контакте) |
| 210 | Общая ошибка (детали) или TMCH-claim с `claims[]` | `NeedsHuman`/`Rejected` (на регистрации сперва проверить claims) |
| 101–109 | https/версия/тип/операция/параметры/ключ не указаны | `ClientBug` (фатальный конфиг, ничего не списано) |
| 110, 111, 112, 113 | Неверный ключ/пользователь, нет доступа субаккаунтам, **IP не разрешён** | `AuthError` (остановить модуль, оператору) |
| 114, 267 | Неверный синтаксис домена / не поддерживается зона | `Rejected` (чистый отказ) |
| 115 | Реестр не отвечает, повторить позже | `Retryable`; для денежной операции **неоднозначно** |
| 116 | Неверный sandbox-аккаунт | `ClientBug` |
| 117, 118 | Платёжный профиль не найден / не верифицирован | `Rejected` (конфиг оплаты) |
| 119 | **Недостаточно средств** | `InsufficientFunds` (чистый отказ) |
| 200 | Домен неактивен или не принадлежит пользователю | `NotOwned` (для `getDomainInfo` — нормальный ответ «не наш») |
| 201 | Внутренняя ошибка системы | `Ambiguous` на денежных операциях |
| 250–256 | Домен уже в нужном состоянии (autorenew/lock/private), изменений нет | OK (идемпотентный no-op) |
| 254 | Обновление NS не выполнено (детали) | `Rejected`/`NsError` |
| 261 | Ошибка обработки домена (детали; по поиску: «недоступен для регистрации») | `Rejected`, но перед финальным решением на `registerDomain` проверить `getDomainInfo` |
| 262 | **Домен уже активен в системе, обработать нельзя** | Для `registerDomain`: проверить `getDomainInfo`. Наш — «уже зарегистрирован», чужой — `Taken` |
| 263 | Неверное число лет | `ClientBug` |
| 264, 265, 266 | Не продлить / не перенести / нет переноса | не используем |
| 280 | Ошибка DNS-изменения | `DnsError` |
| **400** | **Предыдущий API-запрос ещё обрабатывается, повторить позже** | **`Ambiguous`**: первый запрос мог дойти до конца. Не отправлять повторно, сначала сверка |

Прочие места, где возможен `Ambiguous`: сетевой таймаут/разрыв после отправки, HTTP 5xx/408/429, Cloudflare-страница или не-JSON тело.

## 4. Денежный гейт: рекомендуемая модель исключений
Расширять иерархию `AmbiguousSend` / `Rejected` прошлых каналов (backorder, optimizator).
- **`RegistrarRejected` (чистый отказ, деньги не двигались):** 101–114, 116–119, 263, 267, 210-claims и 200 на `registerDomain`. Только после того, как ответ разобран как корректный JSON с кодом.
- **`RegistrarAmbiguous` (исход неизвестен → `maybe_sent`, отмена заказа заблокирована):**
  - таймаут чтения/записи или обрыв соединения после отправки;
  - HTTP 5xx, 408, 429;
  - не-JSON тело (Cloudflare-challenge, HTML-ошибка);
  - коды 115, 201, 400, а также 261/262/210 без claims.
  - Любой неизвестный код на `registerDomain` тоже `Ambiguous`, неизвестный код на чтении (`getPrices` и т.п.) просто ошибка.
- **Ретраи:** `registerDomain` и `registerDomainDrop` идут **мимо ретрая BaseClient** (по аналогии с backorder). Чтение (`check*`, `getPrices`, `getDomainInfo`, баланс) ретраим с бэкоффом и ≥1 с между запросами.
- **Парсинг успеха:** `code in (300,301,302)`, `domain` совпадает с заказом, `order_amount` присутствует. Если `code==300`, а `order_amount` или `domain` отсутствуют/не те — тоже `Ambiguous` (урок прошлого денежного бага).

## 5. План идемпотентной регистрации
Идемпотентность держится на **уникальности домена в реестре**: второй `registerDomain` на уже зарегистрированный домен получит отказ (предположительно 262/261 [?]), а не второе списание. Этого нет у backorder, поэтому здесь безопаснее. Всё равно: не слать повторно, пока исход не выяснен.
1. **Гейт.** `confirmed_by_human=true`, и в заказе заморожены `max_price`, `years=1`, `premium_ok`. Оператор должен знать, что зоны .online/.site дёшевы на регистрации, но в продлении ~$38.50 [3p].
2. **Pre-flight (чтение).**
   - `getAccountBalance` ≥ `max_price` + запас.
   - `checkRegisterAvailability(domain)`: домен в `available`, `price ≤ max_price`, `premium == 0` (премиум только по явному флагу оператора), `duration == years` (в доках цена бывает за 10 лет: `duration` обязателен к сверке).
   - Пакетно по 200, ≥1 с между вызовами.
3. **Adopt-проверка.** `getDomainInfo(domain)`: если ответ 300 и `status` Active, домен уже наш (ручной заказ из ЛК или прошлый заказ): помечаем `registered`, без повторной отправки. Если код 200: наш не он, идём дальше.
4. **Write-ahead.** До вызова пишем в БД состояние `sending` + `operation_id`, `price_seen`, `balance_before`, время. Падение процесса после этой записи ведёт в `maybe_sent`.
5. **Отправка.** Один запрос на `/apibatch/registerDomain`: `years=1&private=1&auto_renew=0&contact_id=…` + `ns1/ns2` Cloudflare при желании. Таймаут 60–90 с, без автоповтора. Если `ns` не прошли (код 301): `changeNameServers`.
   - **Рекомендация по auto_renew:** передавать `0` явно (дефолт ВКЛ тихо тратит баланс).
   - **Рекомендация по private:** `1` явно (дефолт без привата). Исключения: .in/.mx/.uk/…, см. ниже.
6. **Успех (300/301/302).** Фиксируем `order_amount` (сверка с `price_seen`: расхождение выше допуска даёт тревогу, а не откат), затем `getDomainInfo`: `Active`, `nameservers`, `private`, `auto_renew`. Домен переходит в `registered`. 302 пишем как warning «контакт дефолтный».
7. **Неизвестный исход → `maybe_sent`.** Не слать повторно. Цикл сверки (поллинг, не отправка): `getDomainInfo` через 30 с, 2, 5, 15 мин:
   - ответ 300, Active, домен у нас: `registered`, исход подтверждён;
   - код 200 после всей серии + `listOrders(date_from=момент отправки)` не содержит `"<domain> - registration"` + `getAccountBalance` совпадает с `balance_before`: помечаем «не зарегистрирован (выверено)», **повторная отправка только новым подтверждением человека**;
   - любое расхождение (баланс упал, а домена нет, неполный заказ): остаётся `maybe_sent`, ручной разбор, отмена заблокирована.
8. **Привязка NS/контактов.** `contactDomainAssociate` асинхронен (300 только «принято»): после него поллим `getDomainInfo.contact_ids`. Смена NS: сначала `changeNameServers`, затем сверка `getDomainInfo.nameservers` без регистра; NS-пара CF отдельно ждёт делегирования.

## 6. Ловля дропов и аукционы (главный вопрос)
**Что есть в самом NameSilo:**
1. **Аукционы просроченных доменов** (`listAuctions` с `typeId=3`, `statusId=2`; `viewAuction`, `viewAuctions`, `bidAuction`, `bulkBidAuction` ≤100 ставок, `buyNowAuction`, `watchAuction`) [док]. Домен выставляется на **5-й день** после истечения. Аукцион идёт до дня 41, владелец может продлить до дня 30 включительно (тогда аукцион отменяется). У каждого лота есть **максимальная ставка** (выше нельзя), победитель платит ставку + год продления. Для ставки на балансе должно хватать суммы ставки и продления, лимит ставок = баланс + кредитный лимит − активные ставки. **Дата создания домена сохраняется** [док: Expired Domain Auction FAQ].
   - Для нас это единственный детерминированный механизм через API. Охват ограничен доменами, зарегистрированными у NameSilo. Размер витрины: ~218 тыс. просроченных лотов на момент замера в [3p serpcompany#83], это сторонняя оценка.
   - Для M1 это бесплатный источник discovery (уже с датой создания и текущей ставкой). Для M2 ставка: денежное действие, тот же ручной гейт, ставка с `proxyBid` блокирует средства до конца аукциона.
2. **`registerDomainDrop`** (.com/.net) [док]: окно «11:45 – 15:45 UTC», только баланс, только `/apibatch`, дефолтный контакт и NS. **Расхождение:** в более старой версии этой же страницы окно звучало как «10:45–12:15 PT» [3p], что совпадает с дропом Verisign (14:00 ET ≈ 18:00 UTC летом); 11:45–15:45 UTC этот дроп не покрывает. Какое окно реально действует, не известно: снять на аккаунте.
3. **Catch.club** (бэкордер, запущен NameSilo в 2020 [3p]): раздаёт заказ партнёрам (NameJet, DropCatch, Name.com, Sav…), дедлайн бэкордера 09:00 UTC в день дропа, при единственном заказе фиксированная плата ~$60–$90, при нескольких приватный аукцион 3–7 дней, неуспех не тарифицируется [3p]. Публичного API для Catch.club я не нашёл [?]. Ссылка на него стоит в меню «Backorder a Domain».

**Шанс поймать .com только `registerDomain`/polling `checkRegisterAvailability`:** прямых цифр нет ни у кого. Качественная картина из источников:
- DropCatch держит 750–1000+ аккредитаций и «захватывает как минимум половину всех пойманных дропов» [3p: Domain Name Wire, 2021]. У нас одна.
- Профессиональные ловцы работают в миллисекундах; по свидетельству NamePros (2016), API NameSilo «hammered» в момент дропа популярных .com, процент успеха «minimal»; `registerDomainDrop` добавляет проверку у реестра и тем теряет ещё микросекунды [3p].
- NameSilo сам рекомендует для конкурентных доменов платформы дроп-кетчинга, а публичные списки — для остального: ценные домены «rarely remain available long enough» [док-блог].
- **Вывод (оценка, не измерение):** для любого .com/.net с реальным RD/бэклинками (а мы именно их отбираем) шанс через API-регистрацию практически нулевой. Остаётся шанс на «ничьи» дропы, которые никто не заказал, но они обычно малоценны. Поллинг `checkRegisterAvailability` в момент дропа бесполезен: задержка запроса на порядки выше конкуренции, плюс нарушает рекомендацию ≤1 запрос/с.
- **Рекомендация:** .com/.net-дропы с ценностью брать на внешних ловцах (DropCatch/NameJet/SnapNames/Catch.club, вручную, ордер вне API), а NameSilo-API использовать для аукционов их собственных просроченных доменов, для обычной регистрации свободных и для «лотерейного билета» `registerDomainDrop` без ожиданий. Для ccTLD (.co.uk/.nl/.mx/.si/.in) модель дропа иная и у NameSilo под неё нет отдельных методов [?].

## 7. TLD из белого списка
Цены [3p, namebeta.com, 2026-10]: живой источник правды — `getPrices` / `checkRegisterAvailability`.
| TLD | Рег./продл., USD | Заметки |
|---|---|---|
| com | 17.29 / 17.29 | У оператора прежний анализ: $11.05 с Discount Program [prev] |
| net | 15.95 / 15.95 | |
| org | 14.99 / 14.99 | |
| online, site | 2.99 / **38.50** | Ловушка продления |
| xyz | 2.79 / 17.29 | |
| co.uk | 6.49 / 6.49 | Без ограничений по резиденции. **Whois Privacy не поддерживается для `.uk`** [док-блог] |
| mx | 39.99 / 43.99 | **Privacy не поддерживается** [док-блог]. Доп. поля/контакт: не документировано [?] |
| nl | 6.98 / 7.99 | В официальных страницах NameSilo поддержка не подтверждена [?] |
| si | 24.99 / 24.99 | Аналогично [?] |
| in | 7.95 / 7.95 | **Privacy не поддерживается** [док-блог] |

Официальный список зон без privacy: `.ac .am .asia .at .ca* .de .eu .film .in .it .mx .nyc .pro .sh .top .travel .uk .us .vote .ws`. `.nl`/`.si` в нём нет, значит привата у них вероятно можно. `private=1` для зон из списка может дать ошибку или тихий no-op [?]. Премиум-домены: флаг `premium="1"` и цена в `available`, отдельного параметра подтверждения в `registerDomain` нет, поэтому гейтить на нашей стороне.

## 8. Python-клиенты
| Клиент | Лицензия | Живость | Вывод |
|---|---|---|---|
| `goranvrbaski/python-namesilo` (PyPI 1.7.0) | **GPL-3.0** | пуш 2025-12-31, 13 звёзд | **Не вендорить** (копилефт). Полезен как справочник кодов/методов |
| `nrdcg/namesilo` (Go) | MPL-2.0 | пуш 2026-09-01 | Эталон маппинга кодов (success = 300/301/302), три эндпоинта |
| прочие (`namesilo-py`, DDNS-скрипты) | разные/нет | DNS-only | Не подходят |

Решение: писать своё на httpx (транспорт без ретраев для денежных вызовов, `as_list()`-нормализация, парсинг тела при любом HTTP-статусе).

## 9. Депозит и баланс
- API видит только `getAccountBalance` (USD float). Пополнение: `addAccountFunds` по верифицированной карте (`payment_id`) [док]; BTC и wire только через сайт. В примере `orderDetails` `method:"Bitcoin"` [док], минимум для wire $50 [prev, статья account-funds-manager сейчас за Cloudflare-challenge].
- Платежи API идут с баланса при отсутствии `payment_id`; `119` при нехватке. Рекомендация: держать на балансе только нужную сумму, ключ ограничить 5 IP.
- Аукционный баланс: ставки блокируют лимит (`outstandingCommitments`), но не вычитаются из `balance` [док]. Учитывать при расчёте доступной суммы.

## 10. Открытые вопросы (снимаются только живым тестом)
1. **Повтор `registerDomain` на уже зарегистрированный (наш и чужой) домен:** какой код (262 / 261 / 210) и какой текст? Двойного списания не должно быть, но это не подтверждено.
2. **Нехватка средств при `registerDomain`:** точно ли 119 и не создаётся ли «висящий» неоплаченный заказ (урок backorder `id_status=2`)?
3. **Премиум-домен через `registerDomain`:** что будет без доп. параметров — отказ, списание премиум-цены или цены обычной зоны?
4. **Идёт ли `registerDomain` по `/apibatch` с теми же лимитами, и действительно ли ответы на `/api` и `/apibatch` идентичны для денежных операций.** Лимит частоты: точное значение и формат ответа при превышении («Misuse of API»: код, HTTP-статус).
5. **Sandbox:** отдаёт ли он поддержку .co.uk/.mx/.nl/.si/.in, имитирует ли ошибки 119/262/400? Как быстро выдают sandbox-ключ?
6. **Поддержка зон .nl и .si и составные ключи `getPrices`** (`co.uk` в XML/JSON). Что возвращает `checkRegisterAvailability` для неподдерживаемой зоны: `invalid` или код 267?
7. **TLD-специфика регистрации:** нужны ли доп. поля/контакт для .co.uk, .mx, .nl, .si, .in (документированы только .us/.ca/.eu)? Как ведёт себя `private=1` на .uk/.mx/.in?
8. **Передача `ns1/ns2` в `registerDomain` для сторонних NS (Cloudflare):** действительно ли «must already exist at the requisite registry» касается только дочерних хостов, или внешние тоже дают 301?
9. **Окно `registerDomainDrop`:** 11:45–15:45 UTC или старое 10:45–12:15 PT; что именно в него попадает. Есть ли лимит запросов в окне.
10. **Появляется ли домен в `getDomainInfo`/`listOrders` сразу после ответа 300** и через сколько после ответа 400 «still processing»?
11. **Охват `listAuctions typeId=3`:** какие зоны попадают в аукционы, максимальный `pageSize`, нужен ли `/apibatch` (в [3p] использовался `/public/api/listAuctions`, статус для `/apibatch` неизвестен).
12. **Условия T&C** (автоматическая массовая регистрация, domain tasting): страница не прочитана. И официальная таблица кодов (страница Response Codes) — сверить с зеркалом.
13. **Есть ли API у Catch.club** и можно ли разместить бэкордер программно.

## Источники
- Официальные страницы NameSilo: [api-reference](https://www.namesilo.com/api-reference), [register-domain](https://www.namesilo.com/api-reference/pages?uid=domains%2Fregister-domain), [register-domain-drop](https://www.namesilo.com/api-reference/pages?uid=domains%2Fregister-domain-drop), [check-register-availability](https://www.namesilo.com/api-reference/pages?uid=domains%2Fcheck-register-availability), [get-prices](https://www.namesilo.com/api-reference/pages?uid=account%2Fget-prices), [change-nameserver](https://www.namesilo.com/api-reference/pages?uid=nameserver%2Fchange-nameserver), [list-auctions](https://www.namesilo.com/api-reference/pages?uid=auctions%2Flist-auctions), [API Automated Batch Processing](https://www.namesilo.com/support/v2/articles/account-options/api-automated-batch), [API Manager](https://www.namesilo.com/support/v2/articles/account-options/api-manager), [Expired Domain Auction FAQ](https://www.namesilo.com/Support/Expired-Domain-Auction-Frequently-Asked-Questions), [Rate Limits & Best Practices (блог)](https://www.namesilo.com/blog/en/domain-names/api-driven-domain-management), [TLDs without WHOIS privacy](https://www.namesilo.com/blog/en/guide-top-level-domains-tlds-without-whois-privacy-support-on-namesilo).
- Сторонние: [Blesta codes.php](https://github.com/NETLINK/Blesta-Namesilo/blob/master/config/codes.php), [nrdcg/namesilo](https://pkg.go.dev/github.com/nrdcg/namesilo), [python-namesilo](https://github.com/goranvrbaski/python-namesilo), [Blesta issue #128](https://github.com/blesta/module-namesilo/issues/128), [external-dns-namesilo-webhook PR #14](https://github.com/internetliquid/external-dns-namesilo-webhook/pull/14), [serpcompany issue #83](https://github.com/serpcompany/auction-domain-aggregator-v4/issues/83), [NamePros: API для дропов](https://www.namepros.com/threads/registrars-that-offer-an-api-service-to-catch-dropped-domains.909722/), [Domain Name Wire 2021](https://domainnamewire.com/2021/03/18/comparing-expired-domain-name-platforms/), [NameSilo Catch.Club](https://domaininvesting.com/namesilo-announces-catch-club/), [цены namebeta](https://namebeta.com/registrars/namesilo.com).
- Прошлый анализ: `/Users/kozhan/Documents/PROJECTS/combine_machine/docs/v2/research/registrars.md`. Он говорил «до 5 IP [?]» — теперь подтверждено [док]; про drop-catch `.com/.net` и ставки на аукционах — подтверждено [док].
