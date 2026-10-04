"""DropCatch — список pending delete (.com/.net/.org/.cc/…), транспорт.

Снято 2026-10-01 (браузер + curl, без авторизации и капчи): страница загрузок — SPA, файл отдаётся
так: GET client.dropcatch.com/GetFileUrl?FileType=csv&RequestType=Dropping&BackorderDay=DaysOutN
-> {"result": {"fileUrl": <подписанная ссылка S3>}, "success": true} -> ZIP с одним CSV
`Domain,TLD,Type,Drop Date` (~134 тыс. строк/день, имена в смешанном регистре).
Берём DaysOut2: каждый домен виден ОДИН раз, за 2 дня до дропа (время на скоринг и решение).
ponytail: пропущенный день — потерянный день; понадобится — добавить DaysOut3.
ToS на автоматическое скачивание НЕ прочитан (страница — JS) -> источник выключен по умолчанию.
"""
import csv
import io
import zipfile
from datetime import datetime, timezone

from app.integrations.base import BaseClient

API = "https://client.dropcatch.com/GetFileUrl"


def parse_dropping_zip(raw: bytes) -> list[dict]:
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        name = next((n for n in z.namelist() if n.lower().endswith(".csv")), None)
        if name is None:
            raise ValueError("DropCatch: в архиве нет CSV")
        text = z.read(name).decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    # Смена формата не должна выглядеть как «пустой день»: законных нулей у источника нет,
    # поэтому переименованная колонка — громкая ошибка, а не молчаливый [].
    for col in ("Domain", "Drop Date"):
        if col not in (reader.fieldnames or []):
            raise ValueError(f"DropCatch: сменился формат, нет колонки {col}")
    out = []
    for row in reader:
        # Имена в фиде в смешанном регистре — канон нижний, иначе дедуп по домену промахнётся.
        d = (row.get("Domain") or "").strip().lower()
        try:
            dl = datetime.strptime((row.get("Drop Date") or "").strip(), "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            # Без даты дропа bid-лейн бессмыслен (дедлайн — основа срочности) — строку пропускаем.
            continue
        if d:
            out.append({"domain": d, "source": "dropcatch", "lane": "bid", "acquire_deadline": dl})
    return out


class DropCatchClient(BaseClient):
    def __init__(self, days_out: int = 2):
        super().__init__("", timeout=120.0)
        self.days_out = days_out

    def file_url(self) -> str:
        j = self.request("GET", API, params={"FileType": "csv", "RequestType": "Dropping",
                                             "BackorderDay": f"DaysOut{self.days_out}"}).json()
        url = (j.get("result") or {}).get("fileUrl")
        if not j.get("success") or not url:
            # В текст ошибки — только statusCode: подписанная ссылка S3 несёт подпись, её не светим.
            raise RuntimeError(f"DropCatch не отдал ссылку на файл: {j.get('statusCode')}")
        return url

    def list_dropping(self) -> list[dict]:
        return parse_dropping_zip(self.request("GET", self.file_url()).content)

    def ping(self) -> bool:
        return bool(self.file_url())
