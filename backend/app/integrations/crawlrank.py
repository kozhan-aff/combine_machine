"""Ранги доменов — транспорт: Common Crawl web graph (domain-ranks.txt.gz) и Majestic Million.

ЛИЦЕНЗИИ: данные Common Crawl — по Terms of Use Common Crawl (читать перед боевым запуском); Majestic Million —
CC BY 3.0 (атрибуция есть на /settings). Файлы НЕ вендорятся и на диск не пишутся: они читаются потоком и
из них остаётся только срез по пулу кандидатов (services/domain_ranks.py).

Файл domain-ranks.txt.gz — гигабайты (≈133 млн строк), поэтому распаковка идёт потоково
(zlib.decompressobj по кускам ответа): ни сжатый, ни распакованный файл целиком в памяти и на диске не живут.
Обрыв потока (gzip не дочитан до конца) — исключение: усечённый файл не должен выглядеть «полным»
и объявить все домены пула отсутствующими в графе.

Формат строки CC (табуляция; в шапке `#harmonicc_pos #harmonicc_val #pr_pos #pr_val #host_rev #n_hosts`) и адрес
файла `<база>/<срез>/domain/<срез>-domain-ranks.txt.gz` взяты из исследования и живым запросом НЕ сверялись
(ключей/доступа в сессии не было): парсер читает колонки по ИМЕНАМ шапки, а не по позициям.
"""
import re
import zlib
from collections.abc import Iterator

import httpx

from app.integrations.base import BaseClient

_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}


def release_key(rel_id: str) -> tuple | None:
    """Ключ порядка среза по идентификатору `cc-main-2026-jul-aug-sep` -> (2026, 7). Берётся ПОСЛЕДНИЙ
    месяц среза с учётом перехода года; нераспознанный id -> None (вызывающий откатится на порядок списка)."""
    m = re.search(r"(\d{4})(?:-(\d{2}))?((?:-[a-z]{3})+)", rel_id.lower())
    if not m:
        return None
    months = [_MONTHS[x] for x in m.group(3).strip("-").split("-") if x in _MONTHS]
    if not months:
        return None
    year = int(m.group(1))
    # срез dec-jan-feb стартует в декабре year и заканчивается в феврале year+1
    end_year = year + (1 if months[-1] < months[0] else 0)
    if m.group(2) and months[-1] < months[0]:
        end_year = year // 100 * 100 + int(m.group(2))      # «2024-25-dec-jan-feb»: год конца дан явно
    return (end_year, months[-1])


def pick_release(entries) -> str | None:
    """Самый свежий срез из graphinfo.json (список словарей с `id` или список строк). Порядок списка
    не гарантирован — сортируем по дате из id; не вышло распознать ни одного — берём первый (так отдаёт CC)."""
    ids = [e.get("id") if isinstance(e, dict) else e for e in (entries or [])]
    ids = [i for i in ids if isinstance(i, str) and i]
    if not ids:
        return None
    keyed = [(release_key(i), i) for i in ids]
    known = [k for k in keyed if k[0] is not None]
    return max(known)[1] if known else ids[0]


def rev_to_domain(host_rev: str) -> str:
    """Обратная нотация CC `com.example` -> `example.com` (`uk.co.example` -> `example.co.uk`)."""
    return ".".join(reversed(host_rev.strip().lower().split(".")))


def domain_to_rev(domain: str) -> str:
    return rev_to_domain(domain)          # разворот симметричен


class RankClient(BaseClient):
    def __init__(self):
        super().__init__("", timeout=httpx.Timeout(300.0, connect=10.0))

    def latest_ranks_url(self) -> tuple[str, str]:
        """(адрес файла, идентификатор среза). CC_RANKS_URL задан — берём его (срез = имя файла); пусто —
        свежий срез из graphinfo.json."""
        from app.config import settings
        if settings.CC_RANKS_URL:
            name = settings.CC_RANKS_URL.rstrip("/").rsplit("/", 1)[-1]
            return settings.CC_RANKS_URL, re.sub(r"-domain-ranks\.txt\.gz$", "", name)
        r = self._client.get(settings.CC_GRAPHINFO_URL)
        r.raise_for_status()
        rel = pick_release(r.json())
        if not rel:
            raise ValueError("graphinfo.json: нет ни одного среза — формат изменился")
        base = settings.CC_GRAPH_BASE_URL.rstrip("/")
        return f"{base}/{rel}/domain/{rel}-domain-ranks.txt.gz", rel

    def iter_gz_lines(self, url: str) -> Iterator[str]:
        """Строки gzip-файла по адресу — потоком. Усечённый поток -> EOFError."""
        d = zlib.decompressobj(16 + zlib.MAX_WBITS)
        open_member, tail = False, b""         # open_member: в текущий gzip-член подали байты, но конца не было
        with self._client.stream("GET", url) as r:
            r.raise_for_status()
            for chunk in r.iter_bytes(1 << 16):
                while chunk:
                    open_member = True
                    data = d.decompress(chunk)
                    if d.eof:                  # конец gzip-члена; файл может быть из нескольких
                        chunk, open_member = d.unused_data, False
                        d = zlib.decompressobj(16 + zlib.MAX_WBITS)
                    else:
                        chunk = b""
                    *lines, tail = (tail + data).split(b"\n")
                    for ln in lines:
                        yield ln.decode("utf-8", errors="replace")
        if open_member:
            raise EOFError("gzip оборван: поток закончился раньше конца файла")
        if tail:
            yield tail.decode("utf-8", errors="replace")

    def iter_text_lines(self, url: str) -> Iterator[str]:
        with self._client.stream("GET", url) as r:
            r.raise_for_status()
            yield from r.iter_lines()

    def ping(self) -> bool:
        raise NotImplementedError("ранги качаются раз в месяц; отдельного пинга нет")
