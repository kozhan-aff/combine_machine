"""Прямой whois координатора зон .RU/.РФ/.SU (whois.tcinet.ru:43). Транспорт only.

Формат ответа выверен ЖИВОЙ пробой 2026-07-19 (не по документации — дисциплина
проекта после двух дорогих ошибок разбора вслепую). См.
docs/superpowers/specs/2026-07-19-tci-whois-design.md.

Зачем мимо A-Parser: ~37 мс против секунд-минут и без расхода квоты. Зачем
ЖЁСТКО по зоне: на не-TCI домен (example.com) сервер отвечает 'No entries found'
дословно как на свободный .ru — наивная маршрутизация пометила бы каждый
занятый .com свободным.
"""
import re
import socket
from datetime import date, datetime, timezone

_HOST = "whois.tcinet.ru"
_PORT = 43
_TIMEOUT = 10.0

# Зоны, которые обслуживает этот сервер (punycode-хвосты). Всё остальное — A-Parser.
_ZONES = ("ru", "su", "xn--p1ai")

_CREATED_RE = re.compile(r"^created:\s*(\S+)", re.M)
_FREE_DATE_RE = re.compile(r"^free-date:\s*(\d{4}-\d{2}-\d{2})", re.M)
_DOMAIN_RE = re.compile(r"^domain:\s*\S+", re.M)


def _punycode(domain: str) -> str:
    """Кириллический .рф -> punycode. Сервер на кириллицу отвечает 'Invalid request.'"""
    d = domain.strip().lower().rstrip(".")
    try:
        return d.encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        return d


def _parse(text: str) -> dict:
    """Ответ whois -> {"available", "created", "free_date"}. Чистая функция, не бросает.

    available: False — есть запись `domain:`; True — 'No entries found';
    None — 'Invalid request.'/мусор (НЕ определить, не путать со свободным)."""
    created = free_date = None
    if _DOMAIN_RE.search(text):
        available = False
        m = _CREATED_RE.search(text)
        if m:
            try:
                created = datetime.fromisoformat(m.group(1).replace("Z", "+00:00"))
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
            except ValueError:
                created = None
        m = _FREE_DATE_RE.search(text)
        if m:
            try:
                free_date = date.fromisoformat(m.group(1))
            except ValueError:
                free_date = None
    elif "No entries found" in text:
        available = True
    else:
        available = None
    return {"available": available, "created": created, "free_date": free_date}


class TciWhoisClient:
    """Сырой TCP на порт 43. httpx здесь не при чём — whois не HTTP."""

    def handles(self, domain: str) -> bool:
        d = _punycode(domain)
        return any(d.endswith("." + z) for z in _ZONES)

    def query(self, domain: str) -> str:
        """Сырой ответ сервера. Сетевой сбой ПРОБРАСЫВАЕТСЯ — судит вызывающий
        (тот же контракт, что AParserClient.whois_probe)."""
        d = _punycode(domain)
        with socket.create_connection((_HOST, _PORT), timeout=_TIMEOUT) as s:
            s.sendall((d + "\r\n").encode("ascii"))
            chunks = []
            while True:
                c = s.recv(4096)
                if not c:
                    break
                chunks.append(c)
        return b"".join(chunks).decode("utf-8", "replace")

    def probe(self, domain: str) -> dict:
        return _parse(self.query(domain))

    def ping(self) -> bool:
        """Живость для /diag — запрос по заведомо занятому домену координатора."""
        return _parse(self.query("nic.ru"))["available"] is False
