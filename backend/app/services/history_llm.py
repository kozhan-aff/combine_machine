"""W5: язык и тема прошлого сайта по видимому тексту снимков Wayback — через LLM (LiteLLM).

МЯГКИЙ сигнал (инвариант 3 v2): ничего не отклоняет и никого не «ослепляет». Жёсткие отказы по
истории — только детерминированный wayback._classify_text. Зачем тема вообще: политика Google
«expired domain abuse» — примеры в ней все про СМЕНУ темы (казино на бывшем сайте школы);
оператор обязан видеть, чем домен был, прежде чем делать из него VPN-сайт (инвариант 4).
"""
import json
import math
import re

_SYSTEM = (
    "Ты классификатор истории веб-сайтов. По тексту снимков сайта из веб-архива определи для "
    "КАЖДОГО снимка: язык (ISO 639-1, две латинские буквы), тему (до 6 слов по-английски) и "
    "признак припаркованного домена (заглушка продажи/парковки/ошибки). Затем общую тему сайта и "
    "насколько она близка к VPN, приватности, кибербезопасности, софту, интернет-сервисам или "
    "стримингу — число от 0 до 1. Ответь ТОЛЬКО JSON без пояснений: "
    '{"snapshots":[{"year":2019,"lang":"es","topic":"...","parked":false}],'
    '"topic_summary":"...","vpn_adjacent":0.0}')
_LANG = re.compile(r"^[a-z]{2}$")
_MAX_SNAPS, _MAX_CHARS = 5, 2000


def _year(x: dict) -> int:
    y = str(x.get("year") or "")[:4]
    return int(y) if y.isdigit() else 0


def _clean_topic(v) -> str:
    """Тема из ОТВЕТА МОДЕЛИ (недоверенные данные) -> строка, безопасная для PostgreSQL/JSONB.
    Только `str` (dict/число не превращаем в "{'evil': 1}"); NUL, управляющие и одиночные
    суррогаты (\\ud800) режем: psycopg3 на них падает, а при temperature=0 тема воспроизводится и
    коммит всей волны падал бы на каждом прогоне."""
    if not isinstance(v, str):
        return ""
    s = "".join(c for c in v if c.isprintable()).strip()[:120]
    try:
        s.encode("utf-8")
    except UnicodeEncodeError:              # страховка: суррогат, проскочивший фильтр
        return ""
    return s


def parse_answer(raw: str) -> dict | None:
    """Тотальная обёртка над `_parse`: любой мусор в ответе — None, не исключение (иначе непригодный
    ответ модели засчитался бы предохранителю как сбой транспорта)."""
    try:
        return _parse(raw)
    except (ValueError, TypeError, OverflowError, RecursionError):
        return None


def _parse(raw: str) -> dict | None:
    """Строгий разбор (находка 4.2). JSON-объект, даже если LLM обернула его прозой;
    `vpn_adjacent` — только конечное число (не строка, не bool, не NaN/Infinity), клампится к 0..1;
    `topic_summary` обязателен; `snapshots` — только список, парковка — только литерал true.
    None — ответ непригоден: тема останется «не определена»."""
    s = raw or ""
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        data = json.loads(s[i:j + 1])
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    adj = data.get("vpn_adjacent")
    if isinstance(adj, bool) or not isinstance(adj, (int, float)) or not math.isfinite(adj):
        return None
    topic = _clean_topic(data.get("topic_summary"))
    if not topic:
        return None
    raw_snaps = data.get("snapshots")
    snaps = [x for x in raw_snaps if isinstance(x, dict)] if isinstance(raw_snaps, list) else []
    live = [x for x in snaps if x.get("parked") is not True]
    lang = next((str(x.get("lang") or "").strip().lower() for x in sorted(live, key=_year, reverse=True)
                 if _LANG.match(str(x.get("lang") or "").strip().lower())), None)
    return {"market_lang": lang, "topic": topic, "topical_relevance": max(0.0, min(1.0, float(adj))),
            "parked_share": round(1 - len(live) / len(snaps), 2) if snaps else None}


def build_prompt(domain: str, texts: list) -> str:
    parts = [f"Домен: {domain}"]
    for t in texts[:_MAX_SNAPS]:
        parts.append(f"--- снимок {str(t.get('timestamp') or '')[:4]} ---\n"
                     f"{str(t.get('text') or '')[:_MAX_CHARS]}")
    return "\n\n".join(parts)


def classify_topics(domain: str, texts: list, llm) -> dict | None:
    """Один вызов LLM на домен. Модель выбирает клиент (LlmClassifyClient: LLM_CLASSIFY_MODEL,
    пусто -> LLM_MODEL). Исключение транспорта пробрасывается — его считает предохранитель
    воронки; непригодный ответ — None, не исключение."""
    if not texts:
        return None
    return parse_answer(llm.complete(_SYSTEM, build_prompt(domain, texts), temperature=0))
