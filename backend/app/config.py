"""Application settings loaded from environment (.env)."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    DATABASE_URL: str = "postgresql+psycopg://portfolio:portfolio@db:5432/portfolio"
    APP_ENV: str = "dev"

    # metrics
    METRICS_PROVIDER: str = "ahrefs"
    AHREFS_API_KEY: str = ""
    CHECKTRUST_API_KEY: str = ""

    # serp + keywords
    SEO_DATA_PROVIDER: str = "dataforseo"
    DATAFORSEO_LOGIN: str = ""
    DATAFORSEO_PASSWORD: str = ""
    SERPAPI_KEY: str = ""
    YANDEX_WORDSTAT_TOKEN: str = ""

    # backorder.ru
    BACKORDER_LOGIN: str = ""
    BACKORDER_PASSWORD: str = ""
    BACKORDER_ACCOUNT_ID: str = ""
    BACKORDER_CONTACT_ID: str = ""

    # optimizator.ru
    OPTIMIZATOR_API_KEY: str = ""
    OPTIMIZATOR_NICD: str = ""

    # registrar NS (.ru)
    REGRU_USERNAME: str = ""
    REGRU_PASSWORD: str = ""

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
    VPS_ORIGIN_IP: str = ""

    # gsc
    GSC_SERVICE_ACCOUNT_JSON: str = ""

    # llm — LiteLLM (локальный бокс, OpenAI-совместимый, без ключа)
    LLM_BASE_URL: str = "http://192.168.1.77:4000"   # ponytail: dev-box default, override via .env
    LLM_API_KEY: str = ""
    LLM_MODEL: str = "mistral"                        # mistral(=mistral-large) | mistral-small | ollama/<m>

    # searxng — free SERP (локальный бокс)
    SEARXNG_URL: str = "http://192.168.1.77:8080"    # ponytail: dev-box default, override via .env

    # a-parser — whois/SERP/keywords (локальный бокс)
    APARSER_URL: str = "http://192.168.1.77:9091"
    APARSER_API_KEY: str = ""
    APARSER_PROXY_CHECKER: str = "ipv6_free"  # имя прокси-чекера в A-Parser UI, box-specific

    # rkn — источник реестра (antizapret primary; z-i заморожен 2025-10)
    RKN_SOURCE_URL: str = "https://antizapret.prostovpn.org/domains-export.txt"

    # spamhaus/surbl — нужен свой резолвер (публичные 8.8.8.8/1.1.1.1 блокируются)
    DNS_RESOLVER: str = ""
    SPAMHAUS_DQS_KEY: str = ""

    # опц. локальные сервисы (тот же бокс)
    BROWSERLESS_URL: str = ""
    N8N_URL: str = ""

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
