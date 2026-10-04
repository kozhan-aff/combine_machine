# Сайты, контент, индексация

Сбор: 2026-10-01. **[✓]** — первоисточник, **[~]** — вторичный, **[?]** — интерпретация или не проверено.
Это вход для подпроекта 3 «Контент».

## Движок: свой Jinja → статический HTML (уже есть)

Сравнение:
- **WordPress на aaPanel.**
  - Установка — только WP-CLI по SSH (one-click API нет).
  - 11 тыс. уязвимостей в 2025 году, 91% из них в плагинах [~ Patchstack].
  - CWV проходят у 45% WP-сайтов [✓ Web Almanac].
  - Гейт редактуры уехал бы в роли WP.
- **Hugo/Astro:** лишний тулчейн. Имеет смысл только для тысяч страниц.
- **Свой рендер (`publish.py`):**
  - гейт `edited` остаётся в нашей БД;
  - на origin почти нечего атаковать;
  - нет WP-следа;
  - узкое место — поштучная запись файлов (решение: zip + `UnZip` или rsync).

**Решение: остаёмся на своём рендере.**

**Footprint.** Google прямо называет злоупотреблением «Creating multiple sites with the intent of
hiding the scaled nature of the content». Значит, маскировка вёрсткой не спасает, если сайты по сути
одинаковы. Что реально нужно:
- 3–5 действительно разных тем;
- разный набор страниц;
- разные авторы и «о нас»;
- **никакой перелинковки сайтов портфеля** (link spam policy).

## Политики Google (developers.google.com/search/docs/essentials/spam-policies, обновлено 2026-08-28)

- **Scaled content abuse** [✓] — массовая генерация страниц ради позиций, «no matter whether content
  is produced through automation, human efforts, or some combination».
- **Expired domain abuse** [✓] — покупка expired-домена ради позиций с малоценным контентом.
  Разрешено: «new, original site that's designed to serve people first».
- **Thin affiliation** [✓] — «cookie-cutter sites or templates… across multiple domains». Хороший
  аффилиат по Google — это «original product reviews, rigorous testing and ratings».
- **ИИ сам по себе не запрещён** [✓]: «Appropriate use of AI or automation is not against our
  guidelines». Рекомендовано раскрывать, кто, как и зачем писал.

## Структура VPN-аффилиата

- **Типы страниц:**
  - обзор;
  - сравнение X vs Y;
  - топ «лучшие VPN для…»;
  - гайды (настройка, гео, стриминг);
  - цены;
  - методология тестирования.
- **Требования Google к обзорам** [✓]:
  - доказательства собственного опыта;
  - количественные замеры;
  - для «best» — объяснение почему, с собственными доказательствами.
- **Для VPN first-hand данные можно честно автоматизировать:** замеры скорости и утечек DNS/WebRTC
  с бокса. Это главное отличие от выдачи конкурентов.
- **Schema.org:**
  - FAQPage rich results отключены с 07.05.2026 [✓];
  - Product → Review с `positiveNotes`/`negativeNotes` для редакционных обзоров ПО работает [✓];
  - Article с автором-Person, Organization, BreadcrumbList.
- **Обязательные страницы:** about (реальная редакция + методология), affiliate disclosure (на сайт и
  у ссылок), privacy.

## Контентный пайплайн (подпроект 3)

1. **Бриф:** топ-10 выдачи → H2 и вопросы конкурентов (`competitor.py` уже есть; ходит через A-Parser
   `SE::Google` → `Net::HTTP`, не через SearXNG) → LLM-бриф.
2. **Слой данных:** цены, юрисдикции, аудиты, протоколы — строки с `source_url` и `checked_at`.
   Таблицы рендерит шаблон из данных, LLM их не пишет.
3. **Черновик на языке рынка:** mistral-large (LiteLLM); Claude API — опционально.
4. **Подстрочный перевод на русский для оператора** (решение оператора: редактирует сам, по переводу).
5. **Автопроверки:**
   - каждое число в тексте найдено в данных;
   - disclosure на месте;
   - шинглы-сходство с другими сайтами портфеля.
6. **LLM-критик** (`content_critic.py`) — только подсказка.
7. **Человек:** `draft → edited` (гейт), затем публикация.

Узкое место масштаба — пропускная способность редактуры.

## Индексация

- **IndexNow** — Bing, Yandex, Naver, Seznam, Yep. **Google не участвует.** До 10 тыс. URL за POST [✓].
- **Пинг sitemap в Google отключён** (404) [✓].
- **GSC полностью по API** [✓ по доке, end-to-end не проверено]:
  1. Site Verification `getToken` (DNS_TXT);
  2. TXT через CF API;
  3. `webResource.insert`;
  4. `sites.add("sc-domain:…")`;
  5. `sitemaps.submit`.
- **URL Inspection API** — 2000 запросов в сутки на свойство, только чтение статуса.
- **Indexing API** — только JobPosting/BroadcastEvent, нам не подходит.
- Подключать ли весь портфель к одному сервис-аккаунту GSC — вопрос footprint, официально не
  описан [?]. Решить в подпроекте 3.
