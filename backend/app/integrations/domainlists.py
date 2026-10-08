"""Списки чистоты доменов — транспорт скачивания (UT1 blacklists, blocklistproject).

ЛИЦЕНЗИИ (проверено по docs/v2/research/open-source-2026-10-07.md §4; сами файлы не вендорятся — они
скачиваются в рантайме и живут только в таблице domain_list, в репозиторий не попадает ничего):
  * UT1 blacklists (Université Toulouse 1 Capitole, dsi.ut-capitole.fr/blacklists) — CC BY-SA 4.0,
    требует указания источника: подпись есть на экране /settings. Формат — tar.gz, внутри
    `<категория>/domains` (по домену в строке).
  * blocklistproject/Lists — Unlicense (провенанс данных gambling неясен). Берём alt-version
    `<имя>-nl.txt` — домены без префикса hosts-формата.
  * hagezi/dns-blocklists (GPL-3.0) НЕ используется ни как код, ни как файл.
Ключей у обоих источников нет. Адреса форматов взяты из исследования и живыми запросами НЕ сверялись
(ключей/доступа в сессии не было): первый боевой прогон — глазами в /settings (счётчики по спискам).

Файлы большие (adult у UT1 — десятки МБ), поэтому тело пишется на диск потоком (`stream`), а не в память;
условный GET (ETag / Last-Modified) делает ежедневное обновление почти бесплатным: 304 — ничего не качали.
"""
import os
import tempfile

import httpx

from app.integrations.base import BaseClient, NotModified


class DomainListClient(BaseClient):
    def __init__(self):
        super().__init__("", timeout=httpx.Timeout(120.0, connect=10.0))

    def download(self, url: str, validators: dict | None = None) -> tuple[str, dict]:
        """Скачать `url` во временный файл. -> (путь, {etag, last_modified}). 304 -> NotModified.
        Файл удаляет вызывающий. Любая не-2xx — исключение (пустой/битый файл не должен выглядеть
        как «список очистили»)."""
        v = validators or {}
        headers = {}
        if v.get("etag"):
            headers["If-None-Match"] = v["etag"]
        if v.get("last_modified"):
            headers["If-Modified-Since"] = v["last_modified"]
        fd, path = tempfile.mkstemp(prefix="domain_list_")
        try:
            with os.fdopen(fd, "wb") as out, self._client.stream("GET", url, headers=headers) as r:
                if r.status_code == 304:
                    raise NotModified(url)
                r.raise_for_status()
                for chunk in r.iter_bytes(1 << 16):
                    out.write(chunk)
                new = {"etag": r.headers.get("ETag"), "last_modified": r.headers.get("Last-Modified")}
        except BaseException:
            try:
                os.unlink(path)
            except OSError:
                pass
            raise
        return path, new

    def ping(self) -> bool:
        raise NotImplementedError("списки чистоты качаются раз в сутки; отдельного пинга нет")
