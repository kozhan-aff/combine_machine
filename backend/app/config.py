"""Application settings loaded from environment (.env).

Поверх .env — переопределения из панели («Ключи и сервисы», таблица secret_override): чтение
`settings.<KEY>` для ключей из белого списка (services/api_keys.py) сначала смотрит их, потом
.env. Рестарт не нужен, backend и worker (разные процессы) видят правку в пределах TTL кэша.
Любой сбой БД/нет таблицы -> молча значение из .env.
"""
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Эти поля НИКОГДА не переопределяются из панели (запереть себя / сломать подключение к БД;
# GITHUB_REPO — смена репо + «Обновить из git» = чужой код на боксе).
# Белый список разрешённых — в services/api_keys.py; здесь только быстрый отсев до обращения к нему.
NOT_EDITABLE = frozenset({"DATABASE_URL", "APP_ENV", "PANEL_USER", "PANEL_PASS",
                          "CLOUDFLARE_SECRETS_DIR", "GITHUB_REPO"})


def _override(name: str):
    try:
        from app.services import api_keys    # лениво: на импорте config БД/модели ещё не готовы
        return api_keys.get_override(name)
    except Exception:  # noqa: BLE001 — любой сбой = значение из .env, ничего не роняем
        return None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    def __getattribute__(self, name):
        val = super().__getattribute__(name)
        if name[:1].isupper() and name not in NOT_EDITABLE:     # поля настроек — ЗАГЛАВНЫЕ
            ov = _override(name)
            if ov is not None:
                return ov
        return val

    def env_value(self, name: str):
        """Значение ТОЛЬКО из .env/окружения (мимо переопределений) — для экрана ключей."""
        return super().__getattribute__(name)

    DATABASE_URL: str = "postgresql+psycopg://portfolio:portfolio@db:5432/portfolio"
    APP_ENV: str = "dev"

    # M5: после записи страниц в docroot публикация проверяет HTTP-ответом самого домена, что
    # отдаётся именно записанная версия (метка build-id). Выключается только если домен
    # заведомо недостижим с бокса (закрытая сеть); по умолчанию страница не `published`, пока
    # не проверена.
    PUBLISH_VERIFY: bool = True

    # Ahrefs API v3 (integrations/ahrefs.py): DR-free, batch-analysis, анкоры, история трафика
    AHREFS_API_KEY: str = ""

    # backorder.ru
    BACKORDER_LOGIN: str = ""
    BACKORDER_PASSWORD: str = ""
    BACKORDER_ACCOUNT_ID: str = ""
    BACKORDER_CONTACT_ID: str = ""

    # optimizator.ru
    OPTIMIZATOR_API_KEY: str = ""
    OPTIMIZATOR_NICD: str = ""
    # http: документация провайдера канонична только для http, https у него не подтверждён живьём
    # (S3-05 — живых вызовов без разрешения не делаем). Когда оператор проверит 443 — меняет здесь
    # на https://optimizator.ru без правки кода.
    OPTIMIZATOR_BASE_URL: str = "http://optimizator.ru"

    # NameSilo (международный регистратор, канал «registrar»). Ключ ходит ТОЛЬКО в query и маскируется
    # везде (diagnostics._scrub, log_scrub). BASE_URL по умолчанию — /apibatch: автоматизация на /api
    # нарушает ToS (docs/v2/research/namesilo-api-spec.md §1). SANDBOX=true -> sandbox.namesilo.com/api.
    NAMESILO_API_KEY: str = ""
    NAMESILO_BASE_URL: str = "https://www.namesilo.com/apibatch"
    NAMESILO_SANDBOX: bool = False
    NAMESILO_CONTACT_ID: str = ""                     # contact_id профиля регистранта (contactAdd на сайте)
    NAMESILO_ALLOW_PREMIUM: bool = False              # премиум-домены (premium=1) — только по явному флагу оператора

    # M2: сколько часов живёт подтверждение выкупа (S3-07). Дальше исполнить старый confirm нельзя —
    # человек подтверждает заново (и цена/тариф перезамораживаются).
    ACQ_CONFIRM_TTL_HOURS: int = 24

    # cloudflare
    CLOUDFLARE_API_TOKEN: str = ""
    CLOUDFLARE_ACCOUNT_ID: str = ""
    CLOUDFLARE_SECRETS_DIR: str = ""  # allowlisted read-only каталог для file:BASENAME secret_ref

    # aapanel
    AAPANEL_URL: str = ""
    AAPANEL_API_KEY: str = ""
    # Optional path to the panel's cert (/www/server/panel/ssl/certificate.pem copied
    # locally) to pin TLS instead of verify=False. Recommended for remote panels.
    AAPANEL_CA_BUNDLE: str = ""
    # Режим «через SSH-туннель» (сайдкар aapanel-tunnel в docker-compose, профиль tunnel): панель
    # видит запросы с 127.0.0.1 VPS, whitelist по публичному IP бокса не нужен. Значение с экрана
    # «Ключи и сервисы» приходит строкой ("true"/"false") — разбирает integrations.aapanel.tunnel_mode().
    AAPANEL_TUNNEL: str = ""
    VPS_ORIGIN_IP: str = ""
    # M3: выпускать Cloudflare Origin CA на каждый домен и ставить его в aaPanel (SetSSL), чтобы
    # перевести зону в Full(strict). ВЫКЛ по умолчанию: ручка SetSSL и права токена («SSL and
    # Certificates: Edit») ни разу не проверены вживую (инвариант «не гадать форматы») — пока
    # выключено, провижн держит CF во flexible и честно пишет origin_https='none'.
    ORIGIN_CA_AUTO: bool = False

    @field_validator("ORIGIN_CA_AUTO", mode="before")
    @classmethod
    def _blank_flag_is_off(cls, v):
        # `ORIGIN_CA_AUTO=` (пустая строка, как велит комментарий в .env.example «пусто/0») —
        # pydantic на пустом bool падает ValidationError и роняет backend/worker/alembic при импорте.
        return False if isinstance(v, str) and not v.strip() else v

    # Индексация (M5). GSC URL Inspection — основной источник «в индексе ли страница» (service account:
    # JSON ключа целиком; аккаунт добавлен в свойство GSC сайта). GSC_API_URL — хост Search Console API.
    GSC_SERVICE_ACCOUNT_JSON: str = ""
    GSC_API_URL: str = "https://searchconsole.googleapis.com"
    # IndexNow — бесплатный пинг Bing/Yandex/др. о новых страницах. Ключа-секрета нет: ключ сайта
    # выводится из домена и лежит в корне сайта файлом <key>.txt (так задумано протоколом).
    # INDEXNOW_SECRET — секрет установки: ключ сайта = HMAC(секрет, домен). Пусто — IndexNow выключен
    # (ни файла-ключа, ни пинга): ключ без секрета угадывается и сцепляет сайты портфеля.
    INDEXNOW_SECRET: str = ""
    INDEXNOW_ENABLED: bool = True
    INDEXNOW_URL: str = "https://api.indexnow.org/indexnow"

    # llm — LiteLLM (локальный бокс, OpenAI-совместимый, без ключа)
    LLM_BASE_URL: str = "http://192.168.1.77:4000"   # ponytail: dev-box default, override via .env
    LLM_API_KEY: str = ""
    LLM_MODEL: str = "mistral"                        # mistral(=mistral-large) | mistral-small | ollama/<m>
    LLM_THINK: bool = False                           # ollama/*: False -> шлём "think": false (без рассуждений); True -> модель думает как задумано
    LLM_CLASSIFY_MODEL: str = ""                      # W5: тема/язык снимков; пусто -> LLM_MODEL
    LLM_CLASSIFY_FALLBACK_MODEL: str = ""             # W5: запасная модель при 401/403/404/429/5xx основной; пусто -> без запасной
    LLM_WRITER_MODEL: str = ""                        # M4: писатель страниц по досье; пусто -> LLM_MODEL
    LLM_CRITIC_MODEL: str = ""                        # M4: критик страниц; пусто -> LLM_MODEL
    CONTENT_GUIDES_DIR: str = ""      # пусто -> /repo/content_guides (бокс) или <репо>/content_guides
    # M4: ниша сайтов — по ней из правил письма оператора отбираются применимые (services/guides.py)
    SITE_VERTICAL: str = "VPN-сервисы: партнёрские обзоры, сравнения и инструкции по настройке"
    RESEARCH_DIR: str = "/workspace/combine/research"   # общая папка со шлюзом :3033 (скриншоты для дизайнера)
    RESEARCH_SCREENSHOTS: bool = False                  # Browserless: снимать первый экран конкурентов
    RESEARCH_MAX_AGE_DAYS: int = 30

    # searxng — free SERP (локальный бокс)
    SEARXNG_URL: str = "http://192.168.1.77:8080"    # ponytail: dev-box default, override via .env

    # browserless — скриншоты/проверка вёрстки (из контейнера backend доступен только по LAN-адресу, не 127.0.0.1)
    BROWSERLESS_URL: str = "http://192.168.1.77:3000"
    BROWSERLESS_TOKEN: str = ""

    # a-parser — whois/SERP/keywords (локальный бокс)
    APARSER_URL: str = "http://192.168.1.77:9091"
    APARSER_API_KEY: str = ""
    APARSER_PROXY_CHECKER: str = "ipv6_free"  # имя прокси-чекера в A-Parser UI, box-specific
    # Сколько A-Parser-whois (зоны без RDAP) идёт одновременно. Замер аудита F8-05: 12 параллельных =
    # 25,4 с суммарно, то есть ≈0,5 запр/с как последовательно — очередь oneRequest в A-Parser
    # конкурентности не даёт; 3 — запас на разброс задержек (p50 3,6 с, хвост до 27 с).
    WHOIS_APARSER_CONCURRENCY: int = 3
    # Куда писать оператору архивам/реестрам: уходит в User-Agent всех исходящих HTTP-клиентов.
    CONTACT_EMAIL: str = ""

    # spamhaus/surbl — нужен свой резолвер (публичные 8.8.8.8/1.1.1.1 блокируются)
    DNS_RESOLVER: str = ""
    SPAMHAUS_DQS_KEY: str = ""
    WEBRISK_API_KEY: str = ""          # Google Web Risk (замена Safe Browsing); пусто -> W3 «не настроено»
    # Списки чистоты доменов (services/domain_lists.py): базовые адреса скачивания. Менять нужно, только
    # если оператор завёл свой зеркальный сервер; ключей у обоих источников нет.
    DOMAIN_LISTS_UT1_URL: str = "https://dsi.ut-capitole.fr/blacklists/download"
    DOMAIN_LISTS_BLP_URL: str = "https://blocklistproject.github.io/Lists/alt-version"
    # Ранги доменов (services/domain_ranks.py): бесплатный заменитель Ahrefs DR. CC_RANKS_URL — прямой адрес
    # файла domain-ranks.txt.gz; пусто — свежий срез определяется по CC_GRAPHINFO_URL (список срезов
    # Common Crawl). Срез НЕ зашит в код: он устаревает каждые три месяца. MAJESTIC_URL — Majestic Million.
    CC_RANKS_URL: str = ""
    CC_GRAPHINFO_URL: str = "https://index.commoncrawl.org/graphinfo.json"
    CC_GRAPH_BASE_URL: str = "https://data.commoncrawl.org/projects/hyperlinkgraph"
    MAJESTIC_URL: str = "https://downloads.majestic.com/majestic_million.csv"

    # self-update (кнопка «Обновить из git» в панели). Токен — fine-grained PAT,
    # read-only Contents; тянем по HTTPS, чтобы не монтировать SSH-ключ в контейнер.
    GITHUB_REPO: str = "kozhan-aff/combine_machine"
    GITHUB_TOKEN: str = ""

    # panel auth — Basic-auth на ВСЮ панель/API. Панель выставлена на LAN без иной
    # защиты (см. docker-compose): задай оба, чтобы /admin/pull и пайплайн не были
    # доступны любому в сети. Пусто = auth ВЫКЛ (только для локалхост-разработки).
    PANEL_USER: str = ""
    PANEL_PASS: str = ""


settings = Settings()
