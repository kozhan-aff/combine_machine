"""«Ключи и доступы»: ключи/адреса, которые оператор правит из панели без .env и рестарта.

Три части:
  1. БЕЛЫЙ СПИСОК (`GROUPS`) — единственное место, где решается, что вообще можно менять из панели.
     DATABASE_URL/APP_ENV/PANEL_USER/PANEL_PASS/CLOUDFLARE_SECRETS_DIR сюда не входят намеренно
     (config.NOT_EDITABLE): иначе одной опечаткой можно запереть себя из панели.
  2. КЭШ ПЕРЕОПРЕДЕЛЕНИЙ — `get_override(name)`, его зовёт `config.Settings.__getattribute__`.
     Таблица `secret_override` читается целиком раз в TTL секунд; `save()` сбрасывает кэш этого
     процесса сразу, соседний процесс (worker) подхватит правку не позже TTL. Любой сбой БД
     (нет таблицы, миграция не накачена, БД лежит) -> пусто -> работает значение из .env.
  3. ЗАПИСЬ/ОТРИСОВКА — `save()` с валидацией, `describe()` для экрана (значения секретов наружу
     не отдаются — только маска).

Секреты НЕ логируем и в тексты ошибок не кладём: исключение SQLAlchemy при сбое записи несёт
параметры запроса (то есть значение), поэтому наружу идёт только тип ошибки.
"""
import ipaddress
import json
import logging
import threading
import time
import unicodedata
from dataclasses import dataclass, replace
from urllib.parse import urlsplit

from app.config import NOT_EDITABLE, settings

log = logging.getLogger(__name__)

TTL = 5.0                      # сек: через столько соседний процесс увидит правку
MAX_LEN = 512
MAX_LEN_JSON = 8192

# Рубильник нужен тестам: фоновые потоки воронки читают settings.* конкурентно, а SQLite-харнесс
# с одним общим соединением такого не переживает. Прод всегда True.
ENABLED = True


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    hint: str                     # «где взять» — идёт в title и в раскрывающийся блок группы
    secret: bool = False          # секрет маскируется «••••1234», не-секрет виден целиком
    kind: str = "text"            # text | url | ip | choice | json
    choices: tuple = ()
    max_len: int = MAX_LEN


GROUPS = [
    ("m1", "Поиск и проверка доменов",
     "Откуда берутся данные о доменах и проверки чистоты. Нет ключа — проверка пропускается, а домен "
     "помечается «не проверено»; это видно в Диагностике.",
     [Field("AHREFS_API_KEY", "Ahrefs API", "Платный API Ahrefs v3 (ahrefs.com → Account → API). "
            "Без ключа проверки ссылок и спам-ссылок пропускаются.", secret=True),
      Field("WEBRISK_API_KEY", "Google Web Risk", "Google Cloud Console → проект → включить Web Risk API → "
            "Credentials → API key. Без ключа проверка риска пропускается, домен помечается «риск не проверен».",
            secret=True),
      Field("SPAMHAUS_DQS_KEY", "Spamhaus · ключ DQS", "Ключ Spamhaus Data Query Service (бесплатная регистрация "
            "на spamhaus.com). С ним запросы не блокируются, как обычные публичные.",
            secret=True),
      Field("DNS_RESOLVER", "DNS-сервер для Spamhaus", "IP своего DNS-сервера: публичные 8.8.8.8/1.1.1.1 "
            "Spamhaus блокирует. Пусто — системный.", kind="ip"),
      Field("DOMAIN_LISTS_UT1_URL", "Списки чистоты · UT1 (адрес)", "Адрес скачивания UT1 blacklists "
            "(CC BY-SA 4.0), по умолчанию https://dsi.ut-capitole.fr/blacklists/download. Ключа нет. "
            "Менять только для своего зеркала.", kind="url"),
      Field("DOMAIN_LISTS_BLP_URL", "Списки чистоты · blocklistproject (адрес)", "Адрес "
            "blocklistproject (Unlicense), по умолчанию https://blocklistproject.github.io/Lists/alt-version. "
            "Ключа нет. Менять только для своего зеркала.", kind="url"),
      Field("CC_RANKS_URL", "Ранги доменов · Common Crawl (адрес файла)", "Прямой адрес файла "
            "…-domain-ranks.txt.gz нужного среза Common Crawl. Пусто — свежий срез берётся сам. "
            "Ключа нет.", kind="url"),
      Field("CC_GRAPHINFO_URL", "Ранги доменов · список срезов Common Crawl", "Адрес graphinfo.json со "
            "списком срезов, по умолчанию https://index.commoncrawl.org/graphinfo.json. Менять "
            "только для своего зеркала.", kind="url"),
      Field("CC_GRAPH_BASE_URL", "Ранги доменов · папка файлов Common Crawl", "Адрес папки с файлами, по "
            "умолчанию https://data.commoncrawl.org/projects/hyperlinkgraph. Менять только для зеркала.",
            kind="url"),
      Field("MAJESTIC_URL", "Ранги доменов · Majestic Million (адрес)", "Список топ-миллиона Majestic "
            "(CC BY 3.0), по умолчанию https://downloads.majestic.com/majestic_million.csv. Ключа нет. "
            "Нужен только для необязательного бонуса к авторитетности.", kind="url"),
      Field("APARSER_URL", "A-Parser · адрес", "Адрес A-Parser, например http://192.168.1.77:9091.",
            kind="url"),
      Field("APARSER_API_KEY", "A-Parser · пароль API", "Пароль API из настроек A-Parser.", secret=True)]),
    ("m2", "Покупка",
     "Смена этих ключей ничего не запускает и ничего не покупает: заказ уходит провайдеру только после "
     "твоего подтверждения на экране «Покупка».",
     [Field("BACKORDER_LOGIN", "backorder.ru · логин", "Логин от личного кабинета backorder.ru.", secret=True),
      Field("BACKORDER_PASSWORD", "backorder.ru · пароль", "Пароль от личного кабинета backorder.ru.",
            secret=True),
      Field("BACKORDER_ACCOUNT_ID", "backorder.ru · ID счёта", "ID лицевого счёта backorder. Берётся из "
            "ответа их API, не придумывается."),
      Field("BACKORDER_CONTACT_ID", "backorder.ru · ID контакта", "ID контакта-заказчика backorder — "
            "только из их API, не вручную."),
      Field("OPTIMIZATOR_API_KEY", "optimizator.ru · ключ API", "Ключ API из личного кабинета optimizator.ru.",
            secret=True),
      Field("OPTIMIZATOR_NICD", "optimizator.ru · анкета (nic-d)", "Номер анкеты nic.ru вида 5014480/NIC-D."),
      Field("NAMESILO_API_KEY", "NameSilo · ключ API", "namesilo.com → Account → API Manager → Generate. "
            "Ключ привязывается к IP нашего сервера (до 5 IP) и показывается один раз. В логах и ошибках "
            "скрывается. Проверка связи только читает баланс — денег не тратит.",
            secret=True),
      Field("NAMESILO_BASE_URL", "NameSilo · адрес API", "По умолчанию https://www.namesilo.com/apibatch. "
            "Менять не нужно.", kind="url"),
      Field("NAMESILO_SANDBOX", "NameSilo · тестовый режим", "true — учебный сервер NameSilo без настоящих "
            "покупок (ключ для него выдают отдельно, письмом в поддержку). По умолчанию false.", kind="choice",
            choices=("false", "true")),
      Field("NAMESILO_CONTACT_ID", "NameSilo · ID контакта", "ID профиля владельца домена (создаётся на "
            "сайте NameSilo: Account → Contact Profiles). Пусто — профиль по умолчанию."),
      Field("NAMESILO_ALLOW_PREMIUM", "NameSilo · премиум-домены", "true — разрешить покупку премиум-доменов "
            "(цена в десятки раз выше обычной). По умолчанию false: такой домен — отказ.",
            kind="choice", choices=("false", "true"))]),
    ("m3", "Хостинг и DNS",
     "Нужны, чтобы поднять сайт: зона и DNS в Cloudflare, сайт и HTTPS на сервере (aaPanel).",
     [Field("CLOUDFLARE_API_TOKEN", "Cloudflare · API-токен", "dash.cloudflare.com → My Profile → API Tokens "
            "→ Create Token (права на зоны и DNS).", secret=True),
      Field("CLOUDFLARE_ACCOUNT_ID", "Cloudflare · ID аккаунта", "ID аккаунта: правая колонка на странице "
            "любой зоны в кабинете Cloudflare."),
      Field("AAPANEL_URL", "aaPanel · адрес", "Адрес панели с портом, например https://185.201.252.187:18839.",
            kind="url"),
      Field("AAPANEL_API_KEY", "aaPanel · ключ API", "Это token_crypt из /www/server/panel/config/api.json "
            "на сервере, а не поле token. IP нашего сервера должен быть в белом списке API aaPanel.",
            secret=True),
      Field("AAPANEL_CA_BUNDLE", "aaPanel · сертификат (путь)", "Путь к certificate.pem панели внутри "
            "контейнера: по нему проверяется, что отвечает именно наша панель. Пусто — без проверки."),
      Field("AAPANEL_TUNNEL", "aaPanel · через SSH-туннель", "true — ходить в панель через SSH-туннель "
            "(контейнер aapanel-tunnel: docker compose --profile tunnel up -d). Адрес aaPanel тогда "
            "https://aapanel-tunnel:18839, белый список по IP не нужен. Нужен сертификат (поле выше). "
            "Инструкция: docs/v2/aapanel-tunnel-runbook.md.", kind="choice", choices=("false", "true")),
      Field("VPS_ORIGIN_IP", "IP сервера с сайтами", "IPv4 сервера с aaPanel: на него Cloudflare направляет "
            "посетителей.", kind="ip")]),
    ("m45", "Тексты и индексация",
     "Модели, которые пишут и вычитывают тексты, поиск для проверки индексации, скриншоты конкурентов.",
     [Field("LLM_BASE_URL", "LLM · адрес", "OpenAI-совместимый адрес (LiteLLM), например "
            "http://192.168.1.77:4000.", kind="url"),
      Field("LLM_API_KEY", "LLM · ключ", "Ключ LiteLLM или провайдера. На нашем сервере ключ не нужен.",
            secret=True),
      Field("LLM_MODEL", "LLM · модель для текстов", "Имя модели в LiteLLM: mistral, mistral-small, ollama/…. "
            "Рекомендуется ollama/qwen3.5:9b-q8_0 (есть и ollama/hf.co/unsloth/Qwen3.8-27B-GGUF:Q3_K_M — имя сверь по /v1/models LiteLLM)."),
      Field("LLM_THINK", "LLM · режим рассуждений (ollama)", "Выкл (по умолчанию) — модели ollama отвечают "
            "сразу: ~1 с вместо ~30 с и пустого текста. Вкл — модель сначала рассуждает.",
            kind="choice", choices=("false", "true")),
      Field("LLM_CLASSIFY_MODEL", "LLM · модель для проверки истории", "Определяет тему и язык прошлого сайта "
            "по снимкам веб-архива. Пусто — берётся модель для текстов. Через шлюз :3033 — haiku; напрямую LiteLLM — ollama/qwen3.5:9b-q8_0."),
      Field("LLM_CLASSIFY_FALLBACK_MODEL", "LLM · запасная модель для проверки истории", "Куда уходит проверка "
            "истории, если основная модель отвечает ошибкой или не успевает (например, истёк вход Claude CLI в шлюзе :3033). "
            "Рекомендуется ollama/qwen3.5:9b-q8_0. Пусто — без запасной."),
      Field("LLM_WRITER_MODEL", "LLM · модель-писатель", "Пишет страницы по разбору конкурентов. Ответ "
            "длинный (1–2 тыс. слов), поэтому нужна самая сильная из доступных. "
            "Имя — как в LiteLLM (сверь по /v1/models). Пусто — берётся модель для текстов."),
      Field("LLM_CRITIC_MODEL", "LLM · модель-критик", "Вычитывает написанную страницу по списку проверок. "
            "Имя — как в LiteLLM (сверь по /v1/models). Пусто — берётся модель для текстов."),
      Field("SITE_VERTICAL", "Ниша сайтов", "О чём сайты, одной фразой. По ней из твоих правил письма "
            "отбираются применимые: остальное писателю и критику не идёт. После смены ниши сожми "
            "правила заново: экран «Правила письма» → «Сжать правила» с галочкой «все файлы заново»."),
      Field("SEARXNG_URL", "SearXNG · адрес", "Свой поиск, например http://192.168.1.77:8080. "
            "Нужен для проверки индексации.", kind="url"),
      Field("BROWSERLESS_URL", "Browserless · адрес", "Браузер для скриншотов на нашем сервере, например "
            "http://192.168.1.77:3000.", kind="url"),
      Field("BROWSERLESS_TOKEN", "Browserless · токен", "Если контейнер запущен с TOKEN=…; пусто — без токена.",
            secret=True),
      Field("RESEARCH_SCREENSHOTS", "Разбор конкурентов · скриншоты", "true — при разборе конкурентов Browserless "
            "снимает первый экран каждой страницы. false (по умолчанию) — только текст и оформление.",
            kind="choice", choices=("false", "true")),
      Field("RESEARCH_DIR", "Разбор конкурентов · папка скриншотов", "Внутри контейнера backend; по умолчанию "
            "/workspace/combine/research — та же папка подключена к шлюзу :3033 как /workspace/combine."),
      Field("GSC_SERVICE_ACCOUNT_JSON", "Google Search Console · JSON сервис-аккаунта",
            "Содержимое JSON-ключа сервис-аккаунта целиком (Google Cloud → IAM → Service accounts → Keys). "
            "Аккаунт нужно добавить в Search Console каждого сайта. В Firefox текст виден при вводе. "
            "Это основной способ проверки индексации (2000 запросов в сутки на сайт); пусто — проверка "
            "идёт через SearXNG.",
            secret=True, kind="json", max_len=MAX_LEN_JSON),
      Field("GSC_API_URL", "Google Search Console · адрес API", "По умолчанию "
            "https://searchconsole.googleapis.com. Менять не нужно.", kind="url"),
      Field("INDEXNOW_ENABLED", "IndexNow · сообщать о новых страницах", "true (по умолчанию) — после публикации "
            "сообщать поисковикам о новых страницах. Ключ сайта лежит в его корне файлом <ключ>.txt и "
            "выкладывается вместе с сайтом.", kind="choice", choices=("true", "false")),
      Field("INDEXNOW_SECRET", "IndexNow · секрет", "Любая длинная случайная строка "
            "(например openssl rand -hex 32). Из неё и домена получается ключ каждого сайта, поэтому по "
            "ключам нельзя связать наши сайты между собой. Не менять после запуска: ключи сайтов "
            "сменятся. Пусто — IndexNow выключен.", secret=True),
      Field("INDEXNOW_URL", "IndexNow · адрес API", "По умолчанию https://api.indexnow.org/indexnow. "
            "Менять не нужно.", kind="url")]),
    ("infra", "Прочее",
     "Обновление программы.",
     [Field("GITHUB_TOKEN", "GitHub · токен", "Токен GitHub (fine-grained) с правом чтения Contents: Settings → "
            "Developer settings. Нужен для кнопки «Обновить программу» в Диагностике.", secret=True)]),
]

# Смена адреса сервиса отправит ключ на новый хост — говорим об этом в подсказке КАЖДОГО url-поля
_URL_WARN = " Внимание: смена адреса отправит ключ на новый хост."
GROUPS = [(g, t, n, [replace(f, hint=f.hint + _URL_WARN) if f.kind == "url" else f for f in fs])
          for g, t, n, fs in GROUPS]

EDITABLE = {f.key: f for _, _, _, fields in GROUPS for f in fields}
assert not (set(EDITABLE) & NOT_EDITABLE), "в белый список попал запретный ключ"
SECRET_KEYS = tuple(k for k, f in EDITABLE.items() if f.secret)


# ---------- кэш переопределений ----------

_cache: dict | None = None     # текущий снимок; None = нет снимка (старт или после invalidate) -> читатель ждёт
_last_good: dict | None = None  # последний УСПЕШНО загруженный снимок: на нём живём при сбое БД
_loaded_at = 0.0
_gen = 0                       # поколение: invalidate() его двигает, загрузка, начатая до него, не публикуется
_lock = threading.Lock()       # только выбор «кто обновляет»; чтение снимка замка не берёт
_failed = False                # прошлая загрузка упала — не спамим предупреждением каждые TTL секунд
_tls = threading.local()       # защита от рекурсии: загрузка из БД сама может прочитать settings.*


def _load() -> dict:
    from app.db import SessionLocal
    from app.models.secret import SecretOverride
    with SessionLocal() as db:
        return {r.key: r.value for r in db.query(SecretOverride).all() if r.key in EDITABLE}


def _snapshot() -> dict:
    """Stale-while-revalidate: пока снимок есть (пусть протухший), читатели его отдают и НЕ ждут;
    обновляет ровно один поток (замок берётся без ожидания). Ждут только когда снимка нет вовсе
    (первый вызов, сразу после save) — там без свежих данных не обойтись."""
    global _cache, _last_good, _loaded_at, _failed
    c = _cache
    if c is not None and time.monotonic() - _loaded_at < TTL:
        return c
    if getattr(_tls, "busy", False):                 # реентерабельный вызов из самой загрузки
        return c if c is not None else (_last_good or {})
    if c is not None:
        if not _lock.acquire(blocking=False):
            return c                                 # кто-то уже обновляет — отдаём устаревший
    else:
        _lock.acquire()
    try:
        if _cache is not None and time.monotonic() - _loaded_at < TTL:
            return _cache                            # пока ждали, соседний поток уже загрузил
        gen = _gen
        _tls.busy = True
        try:
            data = _load()
            _failed = False
        except Exception as e:  # noqa: BLE001 — нет таблицы/БД лежит; в лог только тип (не текст)
            if not _failed:
                log.warning("secret_override недоступна (%s) — держим последний снимок/.env", type(e).__name__)
            _failed = True
            data = None
        finally:
            _tls.busy = False
        if data is None:
            # сбой: НЕ сбрасываем в {} — ключи (особенно BACKORDER_*) не должны внезапно «пропасть»
            # на время недоступности БД. На .env откатываемся, только если успешной загрузки не было.
            _cache = _last_good if _last_good is not None else {}
            _loaded_at = time.monotonic()            # пауза перед повтором
            return _cache
        if gen != _gen:
            return data       # save() случился во время загрузки: данные могли устареть — не публикуем
        _cache, _last_good, _loaded_at = data, data, time.monotonic()
        return data
    finally:
        _lock.release()


def invalidate() -> None:
    """Сбросить кэш этого процесса (после save; соседние процессы обновятся по TTL)."""
    global _cache, _gen
    _gen += 1
    _cache = None


def get_override(name: str):
    """Значение из БД для разрешённого ключа или None (-> читать из .env)."""
    if not ENABLED or name not in EDITABLE:
        return None
    v = _snapshot().get(name)
    return v if v else None          # пустая строка в БД = как будто переопределения нет


def current(key: str) -> tuple[str, str]:
    """(эффективное значение, источник): 'db' | 'env' | 'unset'."""
    ov = get_override(key)
    if ov is not None:
        return ov, "db"
    v = settings.env_value(key) or ""
    return v, ("env" if v else "unset")


# ---------- валидация и запись ----------

def _bad_chars(v: str, allow_ws: bool) -> bool:
    for ch in v:
        if allow_ws and ch in "\n\r\t":
            continue
        if unicodedata.category(ch) in ("Cc", "Zl", "Zp"):
            return True
    return False


def validate(f: Field, raw: str) -> str:
    """Вернуть очищенное значение или поднять ValueError с русским текстом (БЕЗ самого значения)."""
    v = raw.strip()
    if _bad_chars(v, allow_ws=f.kind == "json"):
        raise ValueError(f"{f.key}: в значении недопустимы управляющие символы и переводы строк")
    if len(v) > f.max_len:
        raise ValueError(f"{f.key}: слишком длинное значение (максимум {f.max_len} символов)")
    if f.kind == "url":
        try:
            host = urlsplit(v).hostname
        except ValueError:                           # напр. «http://[::1» — битый IPv6-литерал
            host = None
        if v.split("://", 1)[0].lower() not in ("http", "https") or "://" not in v or not host:
            raise ValueError(f"{f.key}: адрес должен начинаться с http:// или https:// и содержать хост")
    elif f.kind == "ip":
        try:
            ipaddress.ip_address(v)
        except ValueError:
            raise ValueError(f"{f.key}: нужен IP-адрес (например 192.168.1.10)") from None
    elif f.kind == "choice":
        if v not in f.choices:
            raise ValueError(f"{f.key}: допустимо одно из: {', '.join(f.choices)}")
    elif f.kind == "json":
        try:
            ok = isinstance(json.loads(v), dict)
        except (ValueError, RecursionError):         # глубоко вложенный JSON роняет разбор рекурсией
            ok = False
        if not ok:
            raise ValueError(f"{f.key}: нужен JSON-объект (содержимое ключа сервис-аккаунта целиком)")
    return v


def save(updates: dict, resets=()) -> dict:
    """Записать переопределения и/или сбросить к .env. Всё или ничего: любая ошибка валидации —
    ValueError, в БД ничего не пишется. Возвращает {'changed': [...], 'reset': [...]} (имена ключей).
    Пустое значение в `updates` = «не менять» (отфильтровывается ДО валидации)."""
    resets = set(resets)
    clean: dict[str, str] = {}
    for key, raw in updates.items():
        if key not in EDITABLE:
            raise ValueError(f"{key}: этот параметр нельзя менять из панели")
        v = (raw or "").strip()
        if not v:
            continue
        if key in resets:
            raise ValueError(f"{key}: нельзя одновременно вписать значение и вернуть из .env")
        clean[key] = validate(EDITABLE[key], v)
    for key in resets:
        if key not in EDITABLE:
            raise ValueError(f"{key}: этот параметр нельзя менять из панели")
    if not clean and not resets:
        return {"changed": [], "reset": []}

    from app.db import SessionLocal
    from app.models.secret import SecretOverride
    reset_done = []
    try:
        with SessionLocal() as db:
            for key, v in clean.items():
                db.merge(SecretOverride(key=key, value=v))
            for key in resets:
                row = db.get(SecretOverride, key)
                if row is not None:
                    db.delete(row)
                    reset_done.append(key)
            db.commit()
    except Exception as e:  # noqa: BLE001 — текст исключения SQLAlchemy несёт параметры (значения!)
        raise RuntimeError(f"не удалось записать в базу ({type(e).__name__}); проверь, что программа "
                           "обновлена до конца (миграция 0026_secret_override)") from None
    finally:
        invalidate()
    return {"changed": sorted(clean), "reset": sorted(reset_done)}


# ---------- отрисовка ----------

def mask(v: str) -> str:
    """«••••1234» по последним 4 символам; у коротких секретов хвост не показываем вовсе."""
    if not v:
        return ""
    return "••••" + v[-4:] if len(v) >= 12 else "••••"


def describe() -> list[dict]:
    """Группы -> ключи с текущим (замаскированным) значением и источником. Сырой секрет сюда
    не попадает: шаблон физически не получает того, что нельзя показывать."""
    out = []
    for gid, title, note, fields in GROUPS:
        rows = []
        for f in fields:
            val, src = current(f.key)
            rows.append({"key": f.key, "label": f.label, "hint": f.hint, "secret": f.secret,
                         "kind": f.kind, "choices": f.choices, "source": src,
                         "shown": mask(val) if f.secret else val,
                         "max_len": f.max_len})
        out.append({"id": gid, "title": title, "note": note, "rows": rows})
    return out
