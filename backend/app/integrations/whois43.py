"""Прямой whois по TCP:43 для зон без RDAP (.mx/.co/.nz …) — транспорт + разбор ответа.

Зачем (S1-03, S2-02, F8-05): A-Parser Net::Whois для этих зон даёт случайные вердикты — на 16 вызовах
по заведомо свободным .mx (авторитетный whois.mx: «No_Se_Encontro_El_Objeto») он ответил «занят»
10 раз, «свободен» 4, «не определил» 2, а у несуществующих .com лепит `registered: 1` без даты.
Прямой запрос к серверу зоны — ~1 с вместо 3–69 с и без очереди oneRequest.

Таблица `ZONES` расширяемая: зона -> (сервер, регэксп «свободен», регэксп даты создания). Сервер .mx
снят вживую (аудит 2026-10-07); остальные записи — по публичным описаниям формата, на живом прогоне
не сверялись: их ответ, который не распознан ни как «свободен», ни как «занят с датой», даёт
`available=None` («не определилось») — вызывающий уходит на A-Parser, а не делает вывод.
"""
import re
import socket
from datetime import datetime, timezone

# маркер «объекта нет». Регэкспы без привязки к началу — серверы добавляют шапки/дисклеймеры.
ZONES: dict[str, tuple[str, str, str]] = {
    "mx": ("whois.mx", r"no_se_encontro_el_objeto", r"created on:\s*(\d{4}-\d{2}-\d{2})"),
    "co": ("whois.nic.co", r"no data found|domain not found|not found",
           r"creation date:\s*(\d{4}-\d{2}-\d{2})"),
    "nz": ("whois.srs.net.nz", r"query_status:\s*220\s+available",
           r"domain_dateregistered:\s*(\d{4}-\d{2}-\d{2})"),
    # .de сознательно НЕТ: DENIC не публикует дату регистрации, а `Changed:` — дата последней правки записи
    # (NS/владелец/продление): возраст вышел бы молча заниженным. Зона идёт в A-Parser, пока нет честной даты.
}
MAX_RESPONSE = 64 * 1024          # whois-ответ — килобайты; больше — не наш сервер


def zone_of(domain: str) -> str:
    return domain.rsplit(".", 1)[-1].lower()


def has_whois43(domain: str) -> bool:
    return zone_of(domain) in ZONES


def parse(zone: str, text: str) -> dict:
    """{"available": True|False|None, "created": datetime|None}. Свободен — маркер зоны; занят — только
    при ДАТЕ создания (голое «ответ непустой» не доказательство: это может быть шапка/лимит запросов,
    S2-02/F8-05); иначе None."""
    _server, free_rx, created_rx = ZONES[zone]
    low = (text or "").lower()
    m = re.search(created_rx, low)
    if m:
        try:
            return {"available": False,
                    "created": datetime.strptime(m.group(1), "%Y-%m-%d").replace(tzinfo=timezone.utc)}
        except ValueError:
            return {"available": None, "created": None}
    if re.search(free_rx, low):
        return {"available": True, "created": None}
    return {"available": None, "created": None}


class Whois43Client:
    """Не httpx-клиент, поэтому не BaseClient. Счётчик `whois43_failures` — для предохранителя
    (services/whois.guarded), как у RDAP/A-Parser."""

    def __init__(self, timeout: float = 6.0):
        self.timeout = timeout
        self.whois43_failures = 0

    def _query(self, server: str, domain: str) -> str:
        with socket.create_connection((server, 43), timeout=self.timeout) as s:
            s.settimeout(self.timeout)
            s.sendall(f"{domain}\r\n".encode("ascii"))
            chunks, size = [], 0
            while size < MAX_RESPONSE:
                b = s.recv(4096)
                if not b:
                    break
                chunks.append(b)
                size += len(b)
        return b"".join(chunks).decode("utf-8", "replace")

    def whois_probe(self, domain: str) -> dict:
        """Сетевой сбой (таймаут, отказ соединения) пробрасывается — это сбой канала, а не «не определилось»."""
        zone = zone_of(domain)
        return parse(zone, self._query(ZONES[zone][0], domain))

    def ping(self) -> bool:
        return bool(self._query(ZONES["mx"][0], "nic.mx"))
