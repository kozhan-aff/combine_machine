"""LLM client — LiteLLM (OpenAI-совместимый шлюз). Transport only.

See docs/api/llm.md. Base settings.LLM_BASE_URL (LiteLLM :4000), no key on current box.
Models: mistral(=mistral-large, quality), mistral-small, ollama/* (free local).
"""
from app.config import settings
from app.integrations.base import BaseClient


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
            content = r.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            return ""
        return content or ""

    def ping(self) -> bool:
        r = self.request("GET", f"{self.base_url}/v1/models", headers=self._headers())
        return "data" in r.json()


class LlmClassifyClient(LlmClient):
    """Классификация темы снимков в W5 (services/history_llm.py): короткий ответ, а не страница.

    Таймаут 30 с и ОДНА попытка, без ретраев BaseClient: зависший LiteLLM иначе держал бы слот
    волны истории ~6 минут на КАЖДЫЙ домен (120 с × 3 попытки). Предохранитель «3 сбоя подряд»
    ставит воронка (scoring._topic_one, whois.guarded). Модель — LLM_CLASSIFY_MODEL (ollama-модель
    бокса), пусто -> LLM_MODEL."""
    def __init__(self):
        super().__init__(timeout=30.0)
        self.model = settings.LLM_CLASSIFY_MODEL or settings.LLM_MODEL

    def request(self, method: str, url: str, **kwargs):
        return self._request_once(method, url, **kwargs)
