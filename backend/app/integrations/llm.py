"""LLM client — LiteLLM (OpenAI-совместимый шлюз). Transport only.

See docs/api/llm.md. Base settings.LLM_BASE_URL (LiteLLM :4000), no key on current box.
Models: mistral(=mistral-large, quality), mistral-small, ollama/* (free local).
"""
import httpx

from app.config import settings
from app.integrations.base import BaseClient


class LlmEmptyContent(RuntimeError):
    """Модель вернула только рассуждение (reasoning) без ответа: «thinking»-модель упёрлась в
    max_tokens. Явная ошибка вместо пустой строки, которую вызывающий молча пропустит (S6-10)."""


class LlmClient(BaseClient):
    def __init__(self, timeout: float = 120.0):
        # mistral-large generation blows past BaseClient's 30s default (ReadTimeout on /generate);
        # a full page can take tens of seconds, cold model more. 120s is a safe ceiling.
        super().__init__(settings.LLM_BASE_URL, timeout=timeout)
        self.model = settings.LLM_MODEL
        self.api_key = settings.LLM_API_KEY

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    def complete(self, system: str, prompt: str, **kwargs) -> str:
        """Single completion. Separate system (role/structure) from prompt (page data)."""
        body = {
            "model": kwargs.pop("model", self.model),
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            **kwargs,
        }
        r = self.request("POST", f"{self.base_url}/v1/chat/completions",
                         json=body, headers=self._headers())
        # content can be null (filtered/blocked) or the envelope may lack choices — return ""
        # rather than raising, so one bad page doesn't abort a whole generation batch.
        try:
            msg = r.json()["choices"][0]["message"]
            content = msg["content"]
        except (KeyError, IndexError, TypeError):
            return ""
        if not content and (msg.get("reasoning_content") or msg.get("reasoning")):
            raise LlmEmptyContent(f"модель {body['model']}: ответ пуст, есть только reasoning "
                                  "(thinking-модель упёрлась в max_tokens — отключи thinking или подними лимит)")
        return content or ""

    def ping(self) -> bool:
        r = self.request("GET", f"{self.base_url}/v1/models", headers=self._headers())
        return "data" in r.json()


    def probe(self, timeout: float = 15.0) -> bool:
        """Реальный микро-completion ВЫБРАННОЙ модели (max_tokens=4), а не GET /v1/models (S2-01/S6-01).

        /v1/models отвечает 200, даже когда модель закрыта тарифом (403), упёрлась в лимит (429) или
        её бэкенд (Ollama) недостижим (500) — /diag был зелёным при мёртвой генерации. Одна попытка
        без ретрая и короткий таймаут; причина — текстом в RuntimeError. Токены тратит ничтожно, но
        вызывающий (diagnostics) кэширует результат на 10 минут."""
        body = {"model": self.model, "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 4, "stream": False}
        try:
            r = self._request_once("POST", f"{self.base_url}/v1/chat/completions",
                                   json=body, headers=self._headers(), timeout=timeout)
        except httpx.HTTPStatusError as e:
            code = e.response.status_code
            label = {401: "ключ не принят", 403: "модель недоступна (тариф/права)",
                     429: "лимит запросов", 404: "модель не найдена"}.get(
                         code, "бэкенд модели недоступен" if code >= 500 else "запрос отвергнут")
            raise RuntimeError(f"модель {self.model}: HTTP {code} — {label}"
                               f"{_err_text(e.response)}") from None
        except httpx.TransportError as e:
            raise RuntimeError(f"LiteLLM недоступен: {type(e).__name__} {e}".strip()) from None
        try:
            ok = bool(r.json()["choices"])
        except (ValueError, KeyError, TypeError):
            ok = False
        if not ok:
            raise RuntimeError(f"модель {self.model}: ответ completion без choices")
        return True


def _err_text(resp) -> str:
    """Короткий текст ошибки из тела LiteLLM ({"error": {"message": ...}}) — для /diag."""
    try:
        err = resp.json().get("error")
        msg = err.get("message") if isinstance(err, dict) else err
    except (ValueError, AttributeError):
        msg = None
    return f": {' '.join(str(msg).split())[:80]}" if msg else ""


class LlmClassifyClient(LlmClient):
    """Классификация темы снимков в W5 (services/history_llm.py): короткий ответ, а не страница.

    Таймаут 60 с (локальная модель на холодном старте), без ретраев BaseClient: зависший LiteLLM
    иначе держал бы слот на ~6 минут на КАЖДЫЙ домен (120 с × 3 попытки). Единственный повтор —
    ReadTimeout у ollama-модели (холодная загрузка весов). Предохранитель «3 сбоя подряд» ставит
    воронка (scoring._topic_one, whois.guarded). Модель — LLM_CLASSIFY_MODEL (ollama-модель бокса),
    пусто -> LLM_MODEL; при 401/403/404/429/5xx — явный фолбэк LLM_CLASSIFY_FALLBACK_MODEL, а если
    он не задан или тоже отказал, — RuntimeError с кодом и текстом (оператор видит причину в
    сообщении задачи, а не немое «тема не определена» у всех доменов: S2-01).

    «Thinking»-модели ollama (qwen3.x) без `reasoning_effort=none` думают 40+ с и отдают пустой
    content при малом max_tokens (S2-09/S6-10): для ollama/* параметр добавляется сам."""
    FALLBACK_CODES = (401, 403, 404, 429)

    def __init__(self):
        super().__init__(timeout=60.0)
        self.model = settings.LLM_CLASSIFY_MODEL or settings.LLM_MODEL
        self.fallback = settings.LLM_CLASSIFY_FALLBACK_MODEL

    def request(self, method: str, url: str, **kwargs):
        return self._request_once(method, url, **kwargs)

    def complete(self, system: str, prompt: str, **kwargs) -> str:
        first = kwargs.pop("model", self.model)
        models = [first] + ([self.fallback] if self.fallback and self.fallback != first else [])
        errors = []
        for m in models:
            opts = dict(kwargs)
            local = m.startswith("ollama")
            if local:
                opts.setdefault("reasoning_effort", "none")
            for attempt in range(2 if local else 1):
                try:
                    return super().complete(system, prompt, model=m, **opts)
                except httpx.ReadTimeout:
                    if attempt == 0 and local:
                        continue                  # холодный старт модели — один повтор
                    errors.append(f"модель {m}: таймаут {self._client.timeout.read:.0f} с")
                except httpx.HTTPStatusError as e:
                    code = e.response.status_code
                    if code not in self.FALLBACK_CODES and code < 500:
                        raise
                    errors.append(f"модель {m}: HTTP {code}{_err_text(e.response)}")
                except LlmEmptyContent as e:
                    errors.append(str(e))
                break
        raise RuntimeError("; ".join(errors))
