"""LLM-критик редактуры (Спека 4, 2026-07-18): второй, более дешёвый LLM-вызов
оценивает черновик страницы ДО того, как человек его откроет — advisory-слой, НЕ
гейт. mark_edited (content.py) работает независимо от полей этого модуля.

Формат ответа LLM (простой построчный, НЕ строгий JSON — см. design doc) НЕ проверен
вживую: LiteLLM (192.168.1.77:4000) недоступен в этой итерации (тот же бокс, что и
A-Parser/панель). Парсер `_parse_critique` НАМЕРЕННО defensive — любой неожиданный
ввод даёт score=None/issues=[], никогда не бросает исключение и никогда не подставляет
0 как «оценено плохо». Первый живой прогон ОБЯЗАН сверить реальный формат и поправить
промпт/парсер при расхождении — см. docs/superpowers/specs/2026-07-18-editorial-critic-design.md.
"""
import re

_SCORE_RE = re.compile(r"БАЛЛ:\s*(\d+)", re.I)


def _parse_critique(text: str) -> dict:
    """Построчный ответ критика -> {"score": float|None в [0,1], "issues": [str]}.
    Никогда не бросает исключение — на любой неразбираемый текст даёт score=None."""
    score = None
    m = _SCORE_RE.search(text or "")
    if m:
        raw = int(m.group(1))
        score = max(0, min(100, raw)) / 100.0
    issues = [line[2:].strip() for line in (text or "").splitlines()
              if line.strip().startswith("- ") and line[2:].strip()]
    return {"score": score, "issues": issues}


_SYSTEM_PROMPT = (
    "Ты — редактор VPN-сайта. Оцени черновик страницы по пяти критериям: "
    "(1) тема соответствует бренду/офферу, (2) есть конкретные факты/цифры "
    "вертикали, а не только общие фразы, (3) язык текста соответствует "
    "заявленному, (4) есть пометка о партнёрской ссылке (disclosure), "
    "(5) текст не выглядит как общая AI-вода без содержания. "
    "Ответь СТРОГО в формате: первая строка 'БАЛЛ: <число от 0 до 100>', "
    "затем каждое замечание отдельной строкой, начинающейся с '- '. "
    "Никакого другого текста."
)


def _critique_prompt(body: str, lang: str, brand: str | None) -> str:
    return (
        f"Бренд/оффер: {brand or 'не указан'}\n"
        f"Ожидаемый язык: {lang}\n"
        f"Текст черновика:\n{body}"
    )


def critique_page(page_id: int) -> dict:
    """Оценить черновик страницы вторым LLM-вызовом (advisory, НЕ гейт — status не
    трогается). Пишет critic_score/critic_notes/critic_checked_at, коммитит сама.
    Возвращает {"score": float|None, "issues": [str], "error": str|None}."""
    from datetime import datetime, timezone
    from app.db import SessionLocal
    from app.models.site import Page
    from app.models.offer import Offer
    from app.integrations.llm import LlmClient

    with SessionLocal() as db:
        page = db.get(Page, page_id)
        if page is None:
            raise ValueError(f"page {page_id} not found")
        brand = None
        if page.offer_id:
            offer = db.get(Offer, page.offer_id)
            brand = offer.brand if offer else None

        error = None
        try:
            text = LlmClient().complete(
                _SYSTEM_PROMPT, _critique_prompt(page.body or "", page.lang or "ru", brand))
        except Exception as e:  # noqa: BLE001 — критик advisory, сбой не должен падать наружу
            text = ""
            error = f"{type(e).__name__}: {e}"

        parsed = _parse_critique(text)
        if not text.strip() and error is None:
            error = "пустой ответ LLM (фильтр/blocked) — оценка недоступна"

        page.critic_score = parsed["score"]
        page.critic_notes = {"issues": parsed["issues"]} if parsed["issues"] else None
        page.critic_checked_at = datetime.now(timezone.utc)
        db.commit()
        return {"score": parsed["score"], "issues": parsed["issues"], "error": error}
