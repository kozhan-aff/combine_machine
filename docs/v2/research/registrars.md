# Регистраторы с API

Сбор: 2026-10-01. **[D]** — официальная дока или OpenAPI, **[?]** — пресса, сниппет или косвенно.
Позиции по РФ почти везде взяты из прессы.

## Решение

- **Основной канал на старте — NameSilo.** Почему:
  - принимает BTC на баланс;
  - через API списывает **только с баланса**, а это и есть денежный гейт;
  - в одном API регистрация, проверка доступности, цены, смена NS, баланс, список доменов,
    drop-catch .com/.net и ставки на аукционах.
- **Второй канал — Dynadot**, когда понадобятся ccTLD (.pl, .cl, .cz, .pt, .ro …) и backorder:
  принимает BTC/USDT/USDC, prepaid.
- **Namecheap** — только если у оператора уже есть рабочий аккаунт: для РФ закрыт с 2022 [?],
  API требует $50 на балансе или 20 доменов.

## Проверено вживую 2026-10-01 (NameSilo)

- Оплата: «Bitcoin» как способ пополнения Account Funds; минимум $50 для wire
  ([namesilo.com/payment-options](https://www.namesilo.com/payment-options),
  [account-funds-manager](https://www.namesilo.com/support/v2/articles/account-options/account-funds-manager)).
- `registerDomainDrop`:
  - только .com/.net;
  - только с account funds;
  - только через `/apibatch`;
  - параметры `domain`, `years`, `private`;
  - успех = код 300 и `order_amount`.
  - Дока: [namesilo.com/api-reference/pages?uid=domains/register-domain-drop](https://www.namesilo.com/api-reference/pages?uid=domains%2Fregister-domain-drop)
- **Не подтверждено:** ограничение API-ключа по IP (агент писал «до 5 IP»), точная цена drop-catch.
  Проверить при подключении.

## Сводная таблица

| Регистратор | API: регистрация / проверка (пачка) / цены / NS / баланс / список | Expired/backorder через API | Условия доступа | .com рег./продл. | Оплата через API | Крипта | РФ |
|---|---|---|---|---|---|---|---|
| **NameSilo** | ✓ / 200 / ✓ / ✓ / ✓ / ✓ [D] | `registerDomainDrop` (.com/.net) + аукционы [D] | до 5 IP [?] | $17.29; $11.05 с Discount Program [D] | баланс или карта | **BTC** [D] | нет данных |
| **Dynadot** | ✓ / 1 (100 на Bulk-аккаунте, от $500/год трат) / ✓ / ✓ / ✓ / ✓ [D] | backorder + ставки + closeouts [D] | ключ, опционально IP; 60/мин, 600/мин при $500/год | $10.88 | баланс | **BTC/USDT/USDC** | только SDN-оговорка |
| Porkbun | ✓ / 25 / ✓ / ✓ / ✓ / ✓ [D] | только покупка closeouts | email и телефон подтверждены, IP/CIDR | $11.08 | **только баланс**; `cost` в запросе, `dryRun`, `Idempotency-Key` — лучший денежный контракт | USDC через Coinbase Business (для РФ барьер [?]) | явного запрета нет |
| Name.com (Core v1) | ✓ / 50 / ✓ / ✓ / ✓ / ✓ [D] | `purchaseType=backorder` | токен, 20 rps, sandbox | $12.99 / $17.99 | Account Credit или карта | — | OFAC [?] |
| Namecheap | ✓ / 50 / ✓ / ✓ / ✓ / ✓ [D] | Auctions API, нужен баланс $100 | 20 доменов, или $50 на балансе, или $50 трат за 2 года; whitelist IPv4 | продление $18.48 | только баланс | — | **закрыт с 2022** [?] |
| GoDaddy (v3) | ✓ / 25 / ✓ / ✓ / ✗ / ✓ [D] | Auctions API (Classic-ключ) | ≥1 домен + способ оплаты | $10.49 / $14.99 | карта, Good as Gold | — | **закрыт с 2023** [?] |
| Spaceship | ✓ / 20 / ✗ / ✓ / ✗ / ✓ [D] | ✗ | ключ + секрет | $9.98 | account funds | BTC | санкц. оговорка |
| Cloudflare Registrar | бета с 2026-04-15, подмножество TLD | ✗ | NS только CF, prepaid нет, сразу карта | себестоимость | карта | — | [?] |
| Gandi | ✓ / 1 / ✓ / ✓ / ✓ / ✓ | ✗ | PAT, sandbox | €11 / €31.98 | prepaid | [?] | [?] |
| Openprovider | ✓ / массив / ✓ / ✓ / ✓ / ✓ | **дропкетч через API запрещён** (T&C §9.2) | членство от $4.16/мес | ~$10.44 [?] | prepaid | [?] | [?] |

## Схема «купил → NS на Cloudflare»

Порядок:
1. Создать зону в CF.
2. Взять назначенную пару NS.
3. Купить домен.
4. Выставить NS у регистратора.

Для .de зону лучше поднять **до** регистрации [?]. NS по API: NameSilo `changeNameServers`, Dynadot
`set_ns` (до 100 доменов за вызов), Porkbun `updateNs`.

## Python

Живые библиотеки: `python-namesilo`, `namecheap-python`, `pkb_client`. По конвенции проекта проще
написать тонкий httpx-клиент в `backend/app/integrations/`, без чужого SDK.
