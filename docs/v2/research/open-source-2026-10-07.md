# Открытый код для звеньев цепочки — 2026-10-07

Поиск по GitHub и сети: 6 секторов, кандидаты независимо перепроверены (лицензия, живость, код). Дополняет `open-source.md`. Лицензия данных/ToS перед коммерческим использованием — отдельно читать.

## Быстрые победы

- Предохранитель в AaPanelClient (день 1, 2-4 ч): при ответе «IP validation failed» или «prohibited for 1 hour» не слать запросы 61 минуту, состояние в БД; убрать фоновый пинг aaPanel из /diag. Снимает риск бана на час (аудит S5-01/S5-03).
- SSH-сайдкар (autossh или ssh -N -L) бокс -> VPS + whitelist только 127.0.0.1 (2-4 ч): снимает проблему динамического IP, заодно даёт канал для rsync статики.
- Подать заявку на ICANN CZDS и запросить токен Catched по письму сегодня: это блокеры по времени (недели), код потом за день.
- Единый загрузчик списков UT1 + blocklistproject в таблицу domain_list (0,5-1 день): закрывает дыру многоязычной чистоты (STOPWORDS только EN/RU) на уровне самих доменов. Жёсткий отказ включать только после ручного просмотра первых 50 попаданий на реальных дропах.
- Скачивание domain-ranks.txt.gz Common Crawl + таблица domain_ranks по срезу пула (1-2 дня): бесплатный сигнал authority вместо мёртвого Ahrefs DR; Majestic Million как бонус за 2-3 часа.
- Обновить образ SearXNG на актуальный и добавить контрольный запрос в /diag (1-2 часа); индексацию переводить на GSC URL Inspection (google-api-python-client уже в requirements, 1-2 дня).
- Вынести выкладку на zip/rsync вместо поштучной записи файлов и добавить файл /.build-id для Gatus (0,5-1 день).
- Вендорить паттерны humanizer в промпт content_critic.py и в regex-пре-фильтр (3-5 часов).
- Выяснить GPU бокса (nvidia-smi/dxdiag, 10 минут): от этого зависит, берём ли локальную генерацию картинок или Pixabay.
- Получить токен Cloudflare с правом Intel Read и проверить Intel Domain API (10 минут): потенциально самый ценный источник категорий доменов.

## Рекомендуемый порядок внедрения

1. 0. Подготовка (параллельно, нулевой код): заявка CZDS, письмо в Catched за токеном, проверка GPU бокса, токен Cloudflare с Intel Read.
2. 1. Защита и транспорт (день 1-2): предохранитель AaPanelClient + убрать фоновый пинг /diag; SSH-сайдкар с whitelist 127.0.0.1; rsync/zip-выкладка. Без этого вся нижняя часть цепочки упирается в баны.
3. 2. Покупка (дни 2-4): свой httpx-клиент NameSilo (registerDomain, getAccountBalance, changeNameServers) поверх нашего AmbiguousSend/find-before-send; затем Dynadot v2; Catched по получении токена. Реальная покупка только после пополнения баланса и через ручной денежный гейт.
4. 3. Чистота истории (дни 3-5): загрузчик списков UT1 + blocklistproject (+ Phishing ACTIVE), свой многоязычный слой сигнатур, Family DNS мягким сигналом. Ручной просмотр первых 50 попаданий до жёсткого отказа.
5. 4. Скоринг без Ahrefs (дни 5-8): таблица domain_ranks из Common Crawl, Majestic как бонус, калибровка на ~200 доменах, смена порогов в /settings. Затем CZDS-diff как радар .com/.net, когда придёт доступ.
6. 5. Темы и вёрстка (дни 8-14): 3 Jinja-темы с JSON-LD/OG/hreflang/canonical, standalone Tailwind и самохост-шрифты, вариативность на сайт.
7. 6. Графика и контент (параллельно с 5): resvg-py + Pillow + geopatterns + иконки + vl-convert; паттерны humanizer в критик; сид Techlore и сверка vertical_data; sd-server или Pixabay по результату проверки GPU.
8. 7. Деплой и SSL (дни 12-15): Origin CA одним вызовом, SetSSL, проверка хэша /.build-id; гейт редактуры остаётся единственным путём к публикации.
9. 8. Индексация (дни 15-17): GSC URL Inspection, Bing GetUrlInfo, IndexNow-пинг, лог-сигнал «бот приходил»; обновить SearXNG и оставить вспомогательным.
10. 9. Мониторинг (день 17-18): Gatus с конфигом из таблицы Site (статус, хэш, TLS, срок домена), алерты в Telegram.
11. 10. После первого прогона петли (по необходимости): GraphExplorer для числа ссылающихся доменов, trafilatura вторым слоем текста (с согласования оператора), PyrateLimiter при общем бюджете, запасные cloudflared или Caddy. Воркер не заменять.

## Чего не брать

- python-namesilo (GPLv3, py<=3.12, релиз 2024-10, из метаданных PyPI, код не читали): свой httpx-клиент проще и без копилефта.
- maelgangloff/domain-watchdog как зависимость: AGPL, PHP, нет наших регистраторов, свой денежный гейт несовместим с нашим.
- nvwyk/dropcatch и develanet/dropcatch как код: 0-2 звезды, TypeScript, второй мёртв 23 мес. Первый только как референс денежного контракта.
- ExpiredDomains.net и любой скрейпинг: ToS закрывает аккаунт.
- Tranco (в смеси CC BY-NC от Cloudflare Radar, пакет не обновлялся с 2024-04), Umbrella, Radar: это популярность, а не ссылки.
- fasttext-predict: исходный репозиторий 404, релиз 23 мес, на коротком тексте уверенный мусор; модель LID-218 некоммерческая.
- waybackpy (мёртв с 2022), EDGI/wayback (наш wayback.py лучше), cdx_toolkit/Common Crawl как второй источник истории (на 4 дропах 0 снимков против 69 и 19 у Wayback).
- Fast-DetectGPT, Binoculars, slop-forensics, STORM, Loki, SAFE: устаревший исследовательский код или не про нашу задачу; детекторы дадут ложную уверенность.
- Pelican (AGPL), hugo-theme-stack и Astroship (GPL-3.0), stackbase (нет LICENSE и архитектура «95% общего кода»), Tabi (по умолчанию шлёт POST на сторонний supabase-хост, нет JSON-LD).
- Astro/Eleventy как рантайм-генератор: лишний Node-тулчейн (Astro Node>=22, Eleventy в ребрендинге 4.0-alpha); брать из AstroWind только вёрстку.
- hagezi/dns-blocklists внутрь репозитория (GPL-3.0); допустимо лишь скачивание в рантайме, а при наличии UT1 он не нужен.
- Quad9 для проверки риска: NXDOMAIN неотличим от несуществующего домена. URLhaus и OpenPhish Community: коммерческое использование ограничено.
- oiramix/domainsifter и discover-domain-finder как код: нет LICENSE / один коммит и судит по URL; только идеи.
- index-now-for-python: жёсткие пины requests и lxml, py>=3.11. Свой POST на httpx проще.
- Whoogle (архивирован, финальный релиз 2026-04-15), sshtunnel (последний релиз 2021), BTPanel-API-SDK (мёртв с 2021).
- Coolify/Dokploy/CapRover, Terraform-провайдер Cloudflare, Prefect/Dagster/Temporal/ARQ: избыточно или не про статические vhost; перенос на внешний оркестратор рискует обойти жёсткие гейты.
- Unsplash API (обязательный хотлинк, «неавтоматизированный» характер), DiceBear с человеческими стилями (фейковые лица авторов), Satori и CairoSVG (лишний Node/системные библиотеки).
- Блокировать готовую тему целиком на несколько сайтов: одинаковая тема, хеш CSS и иконки между сайтами портфеля создают footprint.
- Что НЕ проверено и поэтому не рекомендуется без пробы: запуск GraphExplorer и корреляция CC-ранга с DR, ToU Common Crawl целиком, формат ответов Catched, цена и условия ключа Keywords Everywhere, поведение Tailscale из Docker Desktop, 4get, uptime-kuma-api2 на Kuma 2.x.

## По звеньям

### 1. Источники доноров (международные дропы)

Бесплатный или дешёвый поток освобождающихся .com/.net/.org/.uk/.mx без Ahrefs и без скрейпинга ExpiredDomains

**Вывод:** BUILD-OWN + VENDOR. Списки остаются прежними: официальные реестры .uk/.mx/.cl/.pl/.se, DropCatch CSV, GoDaddy inventory, Nominet, registry.mx (все живы 2026-10-07 по проверке сектора). OSS-охотников за доменами нет: все мертвы, закрыты ToS или тривиальны (expired-domain-finder: только NXDOMAIN). Берём (1) CZDS как ранний радар для .com/.net: ВЕНДОРИТЬ ~40 строк после NameSilo, заявку подать сегодня; (2) GET /domains Catched как источник ccTLD, когда придёт токен. Изменилось против прошлого анализа: у Catched есть публичная OpenAPI. Не проверено: качество и полнота списка Catched, ToU CZDS по коммерческому использованию.

| репо | лицензия | последний коммит | fit | как | риски |
|---|---|---|---|---|---|
| [icann/czds-api-client-python](https://github.com/icann/czds-api-client-python) | BSD-3-Clause (LICENSE; GitHub показывает NOASSERTION) | 2025-03-23 (~18,5 мес, граница красного  | partial | vendor-snippet | Это скрипт, а не библиотека (код на уровне модуля), как зависимость не ставится. Diff зоны НЕ равен списку дропов, дата дропа оценочная (~35 дней). Доступ CZDS одобряется неделями/месяцами и требует обоснования; .com/.net снимки порядка ГБ. Токен печатается в stdout (убрать). |
| [api.catched.com (REST-бэкордер, не OSS)](https://api.catched.com/documentation) | Проприетарный сервис; клиентского SDK нет | спека живая 2026-10-07, версия API 0.1 | partial | separate-service | Версия 0.1, api_token в query string (маскировать в логах). Форматы ответов не проверены (нужен токен: без него 401). Баланса и оплаты в API нет. Не закрывает .com/.net. ToS не читал. В спеке 7 путей, а не 9. |
| [maelgangloff/domain-watchdog](https://github.com/maelgangloff/domain-watchdog) | AGPL-3.0 | 2026-05-21 | weak | separate-service | AGPL (только отдельный процесс), PHP/Symfony чужой стек, нет NameSilo/Dynadot/Porkbun, свой денежный гейт несовместим с нашим. Звёзды из прошлого анализа, повторно не проверены. |

### 2. Покупка доменов (регистратор / бэкордер)

Канал покупки международных доменов с идемпотентностью, балансом, сменой NS и нашим денежным гейтом

**Вывод:** BUILD-OWN. Готового Python-клиента NameSilo/Dynadot/Porkbun с идемпотентной регистрацией в открытом доступе НЕТ (проверено по GitHub и PyPI). Поправка к open-source.md: «Python-SDK мертвы» уточняется, жив namecheap-python, но он про Namecheap и без денежного контракта. Порядок: (1) свой тонкий httpx-клиент NameSilo (registerDomain + getAccountBalance + changeNameServers), затем Dynadot v2, ~60-80 строк на регистратора, поверх нашего AmbiguousSend/find-before-send; (2) REFERENCE: docs/registrars/{namesilo,dynadot}.md из registrar-client читать как источник проверенных деталей; (3) Catched как канал ccTLD после токена. Новое: NameSilo registerDomainDrop работает только в окне 11:45-15:45 UTC, только .com/.net, только через /apibatch; перед включением проверить на аккаунте условие оплаты (в доке расхождение), денежный гейт обязан требовать pendingDelete с датой дропа в окне. Блокеры не кодовые: пополнение баланса, токен Catched, одобрение CZDS, ограничения РФ у Namecheap/GoDaddy. Идею nvwyk/dropcatch использовать как независимую сверку денежного кода.

| репо | лицензия | последний коммит | fit | как | риски |
|---|---|---|---|---|---|
| [aoxborrow/registrar-client](https://github.com/aoxborrow/registrar-client) | MIT | 2026-10-07 (v0.9.1, 116 коммитов) | partial | reference-only | Node/TS не встраивается в Python. Автор сам пишет, что платные необратимые операции частично проверены только в песочнице. Один автор. Бэкордера и drop-catch нет. |
| [adriangalilea/namecheap-python](https://github.com/adriangalilea/namecheap-python) | MIT | 2026-08-09 (3.1.0, py>=3.12) | partial | vendor-snippet | Намекается закрытость Namecheap для РФ (по прессе, не проверено), нужен баланс и whitelist IPv4. Нет тестов, код частично AI-генерирован. ApiKey попадает в GET-query и DEBUG-лог. Нет идемпотентности и разбора неоднозначного исхода. Покупка идёт GET-запросом: НИКОГДА через ретрай-слой. |
| [nvwyk/dropcatch](https://github.com/nvwyk/dropcatch) | MIT | 2026-09-23 (v0.2.0) | weak | reference-only | 0 звёзд, неделя истории, один автор, 14 тыс. строк TS. Идея у нас уже есть (AmbiguousSend в backorder). |

### 3. Скоринг без Ahrefs (авторитетность / ссылочный профиль)

Бесплатный заменитель DR и числа ссылающихся доменов

**Вывод:** ADOPT (данные) + SERVICE позже. Основной сигнал authority вместо Ahrefs: Common Crawl web graph. Шаг 1 (1-2 дня): раз в месяц стримить domain-ranks.txt.gz, класть только срез по пулу кандидатов в таблицу domain_ranks, вес authority = f(наличие, pr_val, n_hosts), не harmonic. Шаг 2 (3-5 дней, по необходимости): число ссылающихся доменов через GraphExplorer или DuckDB. До смены порогов калибровка на ~200 доменах с известным Ahrefs DR (public-domain-rating-free). Majestic Million (2-3 часа) только как положительный бонус. OpenPageRank как резерв для тонкого API-запроса выживших после T0-T2 (не вендорить данные). Честно: готового PBN-детектора с репутацией нет, токсичность профиля бесплатно не оценить.

| репо | лицензия | последний коммит | fit | как | риски |
|---|---|---|---|---|---|
| [commoncrawl/cc-webgraph (+ данные web graph cc-main-2026-jul-aug-sep)](https://github.com/commoncrawl/cc-webgraph) | Код Apache-2.0; данные по Common Crawl Terms of Use (читать  | 2026-09-21 (по atom; push 2026-10-01 не  | strong | separate-service | Не Ahrefs: отсутствие в графе = «ссылок в краулинге не нашли». Хвост harmonic почти плоский: различать по наличию, in-degree и pr_val. Спам не фильтруется, токсичность/анкоры не видны. Нужна калибровка против DR на ~200 доменах. Ключ: punycode в нижнем регистре в обратной нотации с учётом PSL. Стать |
| [Majestic Million (downloads.majestic.com/majestic_million.csv)](https://majestic.com/reports/majestic-million) | CC BY 3.0 (атрибуция; на сайте рядом упоминается и 4.0, пере | 2026-10-07 05:00 GMT (ежедневно) | partial | vendor-snippet | Только топ-1M: на 11 фикстурных дропах совпадений 0. Годится как положительный флаг, не как фильтр. Tranco не брать (в смеси CC BY-NC). |
| [OpenPageRank 10M dataset / API](https://openpagerank.keywordseverywhere.com/top-10-million-domains) | Собственные Terms, права на редистрибуцию не прописаны | CSV 2026-09-24 (по заявке; повторно путь | partial | reference-only | ИЗМЕНИЛОСЬ: free-регистрация снова доступна, но через ключ Keywords Everywhere (условия ключа не проверены). 30k/мес меньше суточного потока воронки. Тот же граф CC с чужой нормализацией: первоисточник надёжнее. |

### 4. История и риск (чистота, язык, блэклисты)

Языконезависимый слой «домен числился в gambling/adult/phishing/malware» и закрытие дыры STOPWORDS только EN/RU

**Вывод:** ADOPT (списки) + BUILD-OWN (сигнатуры). Найдена дыра, не закрытая прошлым анализом: STOPWORDS в wayback.py только EN и RU, а v2 нацелен на .mx/.de/.com.br, где испанское или португальское казино и аптека проходят словарь, а мягкий сигнал LLM по инварианту отказов не ставит. Что делать: (1) единый ночной загрузчик UT1 + blocklistproject (+ Phishing ACTIVE) в таблицу domain_list; жёсткий отказ только по gambling и adult, мягкий флаг для phishing/malware, мягкий плюс для vpn. Перед включением жёсткого отказа прогнать по реальным дропам и руками просмотреть первые 50 попаданий (recall не измерен). (2) Свой детерминированный слой многоязычных сигнатур (идеи domainsifter: границы слов, CJK-подстрока, персистентный denylist), код не копировать. (3) Family DNS 1.1.1.2/.3 только мягким предфильтром перед Web Risk. (4) trafilatura как второй слой текста: жёсткий отказ при попаданиях в её тексте, попадания только в nh3 понижать до ручного обзора; только с согласования оператора. (5) Язык: py3langid, можно отложить. hagezi (GPL-3.0) только как скачиваемый в рантайме файл, при наличии UT1 не нужен. Бесплатной замены Google Web Risk нет, он остаётся. Предупреждение по ToS: Spamhaus DQS бесплатный только для некоммерческого использования, оператор должен это осознанно принять. Не проверено: Cloudflare Intel Domain API (токен без права Intel Read: самый ценный источник категорий, если выпустить токен).

| репо | лицензия | последний коммит | fit | как | риски |
|---|---|---|---|---|---|
| [olbat/ut1-blacklists (источник dsi.ut-capitole.fr/blacklists)](https://dsi.ut-capitole.fr/blacklists/) | CC BY-SA 4.0 (файл в архиве; на GitHub лицензия не показана) | архив 2026-10-06; зеркало ~2026-10-05 | strong | vendor-snippet | Recall на прошлых казино не измерен. malware/phishing шумные. Хранит больше мёртвых доменов (36-45% не резолвятся), что для дропов плюс. Качать официальный архив, а не зеркало с Ruby-скриптами. |
| [blocklistproject/Lists](https://github.com/blocklistproject/Lists) | Unlicense (провенанс данных gambling неясен) | заголовки файлов 2026-07-18/20; pushed_a | partial | vendor-snippet | Еженедельно вычищает мёртвые домены, а дроп это ровно мёртвый домен: recall на прошлых казино ниже, чем у UT1. Это домен-флаг, не история страниц. |
| [Phishing-Database/Phishing.Database](https://github.com/Phishing-Database/Phishing.Database) | MIT | 2026-10-02 | partial | vendor-snippet | ЗАЯВКА О ИСТОРИИ НЕ ПОДТВЕРДИЛАСЬ: phishing-domains-INACTIVE.txt пуст (0 байт), ALL-tar.gz даёт 404, зеркало phish.co.za отвечает 503. Рабочий только ACTIVE (текущий фишинг, у освободившегося домена маловероятен). phishing-links-INACTIVE.txt (URL, ~97,6 тыс. хостов) обновлялся 2026-07-24. |
| [Cloudflare Family DNS 1.1.1.2 / 1.1.1.3](https://developers.cloudflare.com/1.1.1.1/setup/) | Сервис; условия массового коммерческого использования не опр | живой сервис, проверен 2026-10-07 | partial | separate-service | Серая зона ToS, нет SLA. Gambling не блокируется (bet365 резолвится). Покрытие ~половина списочных доменов. Утечка списка кандидатов Cloudflare. Только мягкий сигнал; сбой = «сигнала нет», не «чисто». |
| [adbar/trafilatura](https://github.com/adbar/trafilatura) | Apache-2.0 | 2026-10-06 (2.3.1) | partial | dependency | Тянет lxml, courlan, htmldate, justext, dateparser (~30 МБ). На тонких страницах и JS-оболочках отдаёт пусто, поэтому заменять nh3 нельзя, только дополнять. Изменение жёсткого гейта требует согласования оператора. |
| [adbar/py3langid](https://github.com/adbar/py3langid) | BSD-3-Clause | релиз 0.4.0 от 2026-09-02 | partial | dependency | Тянет numpy>=2. На смешанном тексте ошибся. Нужен свой порог. Ценность невысока, т.к. LLM уже возвращает lang; pycld2 (Apache-2.0) точнее, но релиз 19 мес. fasttext-predict понижен: исходный репозиторий 404, релиз 23 мес. |
| [oiramix/domainsifter](https://github.com/oiramix/domainsifter) | Файла LICENSE нет (README пишет MIT): вендорить нельзя | 2026-10-07 (автокоммиты данных, ~2,5 нед | partial | reference-only | Нет LICENSE, pre-launch, код с ИИ. Safe Browsing вне ToS, зависит от CZDS. Брать только идеи. |

### 5. Генерация сайта и шаблоны

Темы с CSS, OG, JSON-LD, hreflang; вариативность footprint между сайтами портфеля

**Вывод:** BUILD-OWN (Jinja) + VENDOR (вёрстка). Поправка к прошлому выводу «Hugo/Astro лишний тулчейн»: для Astro подтверждаю (Node>=22, десятки зависимостей), для Hugo смягчаю: один бинарник, 500 страниц за 0,27 с, остаётся честным запасным вариантом. Ни одна проверенная тема не содержит Review/Product/ItemList-разметки, её пишем сами. План: (1) 3-5 собственных Jinja-тем, вёрстку компонентов портировать из AstroWind (MIT) и PaperMod (MIT); (2) вариативность на сайт: своя тема из пула, свой набор страниц, свои @theme-токены и сборка CSS через standalone Tailwind, шрифты самохостом (fontsource, OFL), никаких CDN, убрать метки генератора; (3) оценка: 4-6 дней на 3 темы + 1 день Tailwind/шрифты. Из programmatic-seo (MIT, 0 звёзд, неделя истории) перенять только паттерны: JSON-LD в Python и порог схожести страниц.

| репо | лицензия | последний коммит | fit | как | риски |
|---|---|---|---|---|---|
| [pallets/jinja](https://github.com/pallets/jinja) | BSD-3-Clause | 2025-06-14 (3.1.6; стабильная библиотека | strong | dependency | Формально ~16 мес без коммитов, но это стабильный Pallets. Реальная работа сектора: content.render_html сейчас отдаёт голый HTML без CSS/OG/JSON-LD/hreflang. |
| [onwidget/astrowind](https://github.com/onwidget/astrowind) | MIT | 2026-09-12 (v1.0.0-beta.65) | partial | vendor-snippet | Бета, Node>=22, Astro в Python-конвейер не тащить, только вёрстку. Демо ходит на Unsplash (убрать). Нет Review/Product-схемы, hreflang, i18n. |
| [adityatelange/hugo-PaperMod](https://github.com/adityatelange/hugo-PaperMod) | MIT | 2026-08-02 | partial | vendor-snippet | Блог-тема, не review. Метки темы: «Powered by Hugo & PaperMod», generator-метатег, одинаковый хеш CSS; все убираются настройками, иначе cookie-cutter. |
| [tailwindlabs/tailwindcss (standalone CLI)](https://github.com/tailwindlabs/tailwindcss) | MIT | 2026-09-25 (v4.3.3) | strong | separate-service | ~80 МБ. Вариативность значений, не структуры: имена классов одинаковы, для структурной вариативности нужны разные HTML-шаблоны. Сверять sha256 релиза. |
| [gohugoio/hugo](https://github.com/gohugoio/hugo) | Apache-2.0 | 2026-10-07 (v0.167.0) | partial | separate-service | Две системы шаблонов. Нужно disableHugoGeneratorInject. Гейт edited остаётся в нашей БД: в Hugo уходят только edited-страницы. Версию пинить. |

### 6. Графика (hero/OG, иконки, графики, фото)

Картинки без платных API и без браузера в рантайме, уникальные на сайт

**Вывод:** ADOPT (набор зависимостей) + SERVICE (локальная генерация). Без нейросети: Jinja-SVG + geopatterns (вендорить) + иконки Tabler/Lucide (вендорить) + resvg-py + Pillow (WebP/AVIF) + vl-convert для графиков. Ловушка со шрифтами resvg обязательна к тесту. Фото: сначала выяснить GPU бокса (nvidia-smi/dxdiag), затем stable-diffusion.cpp с весами только Apache-2.0; без GPU фолбэк на Pixabay. Не брать: Satori, DiceBear, CairoSVG (Node/системные библиотеки ради уже закрытого), Unsplash, ComfyUI/Fooocus (GPL, избыточно). Не создавать фейковые лица авторов. Оценка блока 1-2 дня плюс 1-2 дня на sd-server.

| репо | лицензия | последний коммит | fit | как | риски |
|---|---|---|---|---|---|
| [baseplate-admin/resvg-py](https://github.com/baseplate-admin/resvg-py) | MIT (resvg Apache-2.0) | 2026-10-01 (0.5.0) | strong | dependency | ЛОВУШКА: без шрифтов текст молча пропадает, в slim-образе системных шрифтов нет. Вшить TTF с лицензией OFL, передавать font_files + skip_system_fonts=True; тест на размер PNG. Один мейнтейнер, 0.x. Автопереноса строк нет (Pillow getlength). |
| [python-pillow/Pillow](https://pypi.org/project/pillow/) | MIT-CMU (HPND) | релиз 12.3.0 от 2026-07-01 | strong | dependency | В requirements сейчас нет (новая зависимость). Windows/Linux-колёса проверить features.check в образе бокса. |
| [bryanveloso/geopatterns](https://github.com/bryanveloso/geopatterns) | MIT | 2026-10-06 (0.1.0, 1 коммит) | strong | vendor-snippet | Пакет возрождён вчера, доверия нет: вендорить 631 строку, не пинить. Узнаваемый стиль GitHub-2014, одного мало для брендинга. Тайл, не широкий hero. |
| [vega/vl-convert](https://github.com/vega/vl-convert) | BSD-3-Clause | 2026-10-01 (1.9.0.post1) | strong | dependency | Wheel ~30 МБ. Только inline-данные, allowed_base_urls пустой (иначе возможен сетевой доступ). Мажор 2.0 на подходе: пинить. |
| [tabler/tabler-icons + lucide-icons/lucide](https://github.com/tabler/tabler-icons) | MIT (Tabler); ISC + MIT для части (Lucide, копировать LICENS | 2026-10-05 (Tabler v3.49.0); 2026-10-07  | strong | vendor-snippet | Один набор на всех сайтах = общий отпечаток. Репозиторий Tabler 1,1 ГБ: тянуть tarball тега. simple-icons: логотипы брендов остаются товарными знаками, брать креативы из материалов партнёрки. |
| [leejet/stable-diffusion.cpp](https://github.com/leejet/stable-diffusion.cpp) | MIT (код); веса: Z-Image-Turbo, FLUX.2-klein-4B, FLUX.1-schn | 2026-10-06 (master-945) | strong | separate-service | GPU бокса не подтверждена (по косвенным признакам VRAM у Ollama есть, модель неизвестна). Docker Desktop даёт GPU только NVIDIA, иначе нативно на Windows через Vulkan. Активно меняется API. Скорость на карте не мерена. |
| [Pixabay API (не OSS)](https://pixabay.com/api/docs/) | Pixabay Content License, атрибуция не обязательна | сервис | partial | separate-service | 100 запросов/60 с, кэш 24 ч, постоянный хотлинк запрещён. Одни и те же кадры на нескольких сайтах = отпечаток. Нужен ключ. Pexels требует заметную ссылку на Pexels, Unsplash не брать. |

### 7. Контент и фактическая база (тексты, фактчекинг)

Тексты без «AI-воды» и с проверяемыми фактами по VPN

**Вывод:** VENDOR + BUILD-OWN. Паттерны humanizer в промпт критика и детерминированный пре-фильтр (английский; для es свои). Фактчекинг делаем сами: факты структурно в датасете, LLM получает только их, код детерминированно проверяет, что числа в тексте есть в данных. Живых фактчекеров нет (Loki и SAFE мертвы с 2024). AI-детекторы (Fast-DetectGPT, Binoculars) не интегрировать. Techlore как сид, цены и скорости собирать самим. Гейт редактуры нигде не обходится.

| репо | лицензия | последний коммит | fit | как | риски |
|---|---|---|---|---|---|
| [blader/humanizer](https://github.com/blader/humanizer) | MIT | 2026-09-27 (v3.1.0) | strong | vendor-snippet | Только английский: для испанского нужны свои паттерны. Не продавать как обход детекторов. Это промпт-скилл, не библиотека. |
| [Techlore VPN Finder dataset (vpn.techlore.tech/vpn-chart/vpns.json)](https://vpn.techlore.tech/vpn-chart/vpns.json) | CC BY 4.0 (кредит Techlore) | версия v2026.09 | strong | vendor-snippet | Нет цен, скоростей, стриминга. Вторичный источник: серверы сверять с первичной страницей провайдера. Атрибуция = внешняя ссылка с каждого сайта (политика независимости). Хранить снимок с датой. Наш vertical_data.py расходится (Proton 8900+ против 20559, Nord 5500+ против 8400). |
| [assafelovic/gpt-researcher](https://github.com/assafelovic/gpt-researcher) | Apache-2.0 (на PyPI заявлено MIT) | 2026-09-26 | partial | separate-service | Тяжёлый (70 строк зависимостей, py>=3.12), может галлюцинировать: только с URL для человека на гейте редактуры. SearXNG на боксе сейчас неработоспособен. |

### 8. Деплой и транспорт к aaPanel (динамический IP бокса)

Стабильный доступ к aaPanel без правки whitelist при смене домашнего IP и без банов (20 неудач = бан на час)

**Вывод:** SERVICE + BUILD-OWN. Готового живого Python-клиента aaPanel нет (BTPanel-API-SDK мёртв с 2021, остальные PHP/MCP). Свой клиент оставляем. Основной транспорт: SSH-сайдкар (autossh или ssh -N -L) с бокса на VPS, whitelist только 127.0.0.1, порт 18839 наружу закрыть; тот же SSH-канал для rsync --checksum статики (не заливать файлы через CreateFile aaPanel). Запасной: cloudflared + Access service token (новое против прошлого анализа). Tailscale третьим: не проверена маршрутизация из контейнеров Docker Desktop. Независимо от транспорта обязателен предохранитель в AaPanelClient: при ответе «IP validation failed»/«prohibited for 1 hour» не слать запросы 61 минуту, состояние в БД (общее для backend и worker), и убрать фоновый пинг aaPanel из /diag. Origin CA одним вызовом, без целого SDK. Прочее (Coolify/Dokploy/CapRover, Terraform-провайдер) не брать.

| репо | лицензия | последний коммит | fit | как | риски |
|---|---|---|---|---|---|
| [jnovack/autossh](https://github.com/jnovack/autossh) | MIT | 2026-04-01 (v2.2.0-alpha1) | strong | separate-service | Релиз alpha: лучше стабильный тег или обычный ssh -N -L с ServerAliveInterval. Один оператор. Имя в сертификате не совпадёт, пиннинг по отпечатку. Живьём против нашего VPS не поднимали. |
| [cloudflare/cloudflared](https://github.com/cloudflare/cloudflared) | Apache-2.0 | 2026-10-05 (2026.10.0) | strong | separate-service | Зависимость от Zero Trust, ротация токена, нужен служебный домен вне портфеля, noTLSVerify для loopback. Лимиты Free-плана на своём аккаунте не проверены. Запасной вариант. |
| [cloudflare/cloudflare-python](https://github.com/cloudflare/cloudflare-python) | Apache-2.0 | 2026-10-03 (5.9.0) | partial | vendor-snippet | Генерируемая поверхность меняется каждые недели; SDK целиком не оправдан, вызвать POST /certificates через наш httpx-клиент. Статус Origin CA Key (отключение 30.09.2026) не перепроверен. |
| [caddyserver/caddy](https://github.com/caddyserver/caddy) | Apache-2.0 | 2026-10-07 (v2.11.7) | partial | separate-service | Заменяет aaPanel, а не дополняет; управлять придётся через тот же SSH. Делать только если aaPanel и дальше капризничает. |

### 9. Индексация и проверка

Замена мёртвого SearXNG site:-скрейпа (results=0)

**Вывод:** ADOPT (уже в стеке) + BUILD-OWN. Скрейп-путь site: признан непригодным как источник правды (изменилось против прошлого анализа). Источники правды: (1) GSC URL Inspection через google-api-python-client; (2) Bing Webmaster GetUrlInfo (свой клиент); (3) IndexNow-пинг на публикацию (свой POST, не библиотека); (4) собственный сигнал «бот приходил» из логов nginx/Cloudflare по User-Agent. Статусы: не обходили, обходили, в индексе. SearXNG обновить и оставить слабым вспомогательным сигналом с контрольным запросом, снять нагрузку 60 запросов/час с автопилота.

| репо | лицензия | последний коммит | fit | как | риски |
|---|---|---|---|---|---|
| [googleapis/google-api-python-client](https://github.com/googleapis/google-api-python-client) | Apache-2.0 | 2026-10-06 (2.201.0) | strong | dependency | Лимит 2000 запросов/сутки и 600/мин на сайт. Один сервис-аккаунт связывает портфель в один GSC-владелец: группировать сайты по аккаунтам. Верификацию end-to-end не проверяли. |
| [Bing Webmaster API (GetUrlInfo)](https://learn.microsoft.com/en-us/bingwebmaster/getting-started) | Нет кода (веб-API Microsoft) | документация 2023-11-14 | partial | reference-only | Документация старая, обёртка d и даты /Date()/. Нужен API-ключ и верификация сайта в Bing. Квоты не найдены. Не путать с закрытым Bing Search API v7. |
| [IndexNow (indexnow.org)](https://www.indexnow.org/documentation) | Открытый протокол | протокол | partial | reference-only | Библиотека index-now-for-python жёстко пиннит requests и lxml, py>=3.11: не брать. Это пинг, а не подтверждение индексации. Ключ на домен свой, не общий на портфель. |
| [searxng/searxng](https://github.com/searxng/searxng) | AGPL-3.0 | 2026-10-07 | partial | separate-service | AGPL: только отдельный процесс. site: через скрейп даёт «попало в выдачу сегодня», а не «проиндексировано». Whoogle архивирован, 4get не проверен. |

### 10. Оркестрация, мониторинг, лимитеры

Надёжность выкладки и сертификатов; нужно ли заменять самописный воркер (APScheduler + job_run)

**Вывод:** SERVICE (Gatus) + REFERENCE (остальное). Воркер НЕ заменять: Prefect/Dagster/Temporal/ARQ отклонены (избыточно, нужен Redis или кластер, риск обойти жёсткие гейты редактуры и выкупа). Procrastinate единственный совместимый кандидат, но только если понадобятся параллельные ретраи по сайтам. Gatus для проверки выкладки (хэш /.build-id), TLS и срока домена, алерты в Telegram. PyrateLimiter только если нужен общий бюджет между процессами; текущие самописные предохранители (TCI/A-Parser/SafeBrowsing) оставить: они покрыты детерминированными тестами. Uptime Kuma только если нужен UI (программно через неофициальную Socket.IO-обёртку). Не проверено: запуск Gatus на ccTLD, лимиты Bing API.

| репо | лицензия | последний коммит | fit | как | риски |
|---|---|---|---|---|---|
| [TwiN/gatus](https://github.com/TwiN/gatus) | Apache-2.0 | 2026-10-05 (v5.37.0) | strong | separate-service | Конфиг файловый (генерировать). Не проверяет индексацию. DOMAIN_EXPIRATION на ccTLD (.mx и др.) не проверен. Страницу статуса закрыть Basic-auth или туннелем (footprint). |
| [vutran1710/PyrateLimiter](https://github.com/vutran1710/PyrateLimiter) | MIT | 2026-09-23 (4.5.0) | partial | dependency | Выгода только при общем бюджете между процессами. Мажоры менялись. Postgres-корзину проверить под конкурентностью 12/12/4/2. |
| [procrastinate-org/procrastinate](https://github.com/procrastinate-org/procrastinate) | MIT | 2026-10-05 (3.10.0) | partial | dependency | Заменять воркер сейчас не нужно: APScheduler + job_run (single-flight, отмена, стадии, ~944 теста) закрывает задачу, вторая схема в БД добавит второй источник истины. |

