"""Nominet — официальный список дропов .uk/.co.uk, транспорт.

Живой факт 2026-10-01: это ВСЁ расписание (~266 тыс. строк, drop_time на 65 дней вперёд,
~4 тыс. в день), колонки `roid,domain,drop_time` (ISO с Z). Берём окно [сейчас, сейчас+N дней]:
дальние дропы ещё не время скорить, прошедшие — уже не поймать.
"""
import csv
import gzip
import io
from datetime import datetime, timedelta, timezone

from app.integrations.base import BaseClient, conditional_get

URL = "https://droplists.nominet.uk/current/uk.csv.gz"


def parse_droplist(text: str, now: datetime, lookahead_days: int) -> list[dict]:
    hi = now + timedelta(days=lookahead_days)
    reader = csv.DictReader(io.StringIO(text))
    # Переименованная колонка — громкая ошибка, а не тихий [] (иначе смена формата = «пустой день»).
    for col in ("domain", "drop_time"):
        if col not in (reader.fieldnames or []):
            raise ValueError(f"Nominet: сменился формат, нет колонки {col}")
    out = []
    for row in reader:
        d = (row.get("domain") or "").strip().lower()
        try:
            dt = datetime.fromisoformat((row.get("drop_time") or "").strip().replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt.tzinfo is None:
            # Сравнение naive с aware бросает TypeError и валит весь список — считаем UTC.
            dt = dt.replace(tzinfo=timezone.utc)
        if d and now <= dt <= hi:
            out.append({"domain": d, "source": "nominet", "lane": "bid", "acquire_deadline": dt})
    return out


class NominetClient(BaseClient):
    def __init__(self, lookahead_days: int = 3):
        super().__init__("", timeout=120.0)
        self.lookahead_days = lookahead_days
        self.validators: dict | None = None   # ETag/Last-Modified с прошлого забора (S1-11)

    def list_dropping(self) -> list[dict]:
        """Файл обновляется раз в сутки (~03:01Z), а автопилот гоняет discovery ежечасно: при
        заданных `validators` и 304 бросает `base.NotModified` — 4 МБ не качаем."""
        raw = conditional_get(self, URL).content
        return parse_droplist(gzip.decompress(raw).decode("utf-8", errors="replace"),
                              datetime.now(timezone.utc), self.lookahead_days)

    def ping(self) -> bool:
        return self.request("HEAD", URL).status_code == 200
