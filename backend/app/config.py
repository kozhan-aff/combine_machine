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

    # gsc
    GSC_SERVICE_ACCOUNT_JSON: str = ""

    # llm — LiteLLM (локальный бокс, OpenAI-совместимый, без ключа)
    LLM_BASE_URL: str = "http://192.168.1.77:4000"   # ponytail: dev-box default, override via .env
    LLM_API_KEY: str = ""
    LLM_MODEL: str = "mistral"                        # mistral(=mistral-large) | mistral-small | ollama/<m>
    LLM_CLASSIFY_MODEL: str = ""                      # W5: тема/язык снимков; пусто -> LLM_MODEL
    LLM_CLASSIFY_FALLBACK_MODEL: str = ""             # W5: запасная модель при 401/403/404/429/5xx основной; пусто -> без запасной

    # searxng — free SERP (локальный бокс)
    SEARXNG_URL: str = "http://192.168.1.77:8080"    # ponytail: dev-box default, override via .env

    # a-parser — whois/SERP/keywords (локальный бокс)
    APARSER_URL: str = "http://192.168.1.77:9091"
    APARSER_API_KEY: str = ""
    APARSER_PROXY_CHECKER: str = "ipv6_free"  # имя прокси-чекера в A-Parser UI, box-specific

    # spamhaus/surbl — нужен свой резолвер (публичные 8.8.8.8/1.1.1.1 блокируются)
    DNS_RESOLVER: str = ""
    SPAMHAUS_DQS_KEY: str = ""
    WEBRISK_API_KEY: str = ""          # Google Web Risk (замена Safe Browsing); пусто -> W3 «не настроено»

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
