"""Spam blacklist check (Stage E). See docs/api/blacklist.md.

DNS-based domain lists: Spamhaus DBL, SURBL. Query <domain>.<zone> for an A record;
NXDOMAIN = not listed, 127.0.x.y = listed, 127.255.255.x = lookup unavailable (public
resolver blocked / over quota) -> RAISE (never treat as clean OR as a hit).

Fail-closed control probe: many public resolvers don't return the 127.255.255.x sentinel
at all for Spamhaus zones — they just NXDOMAIN everything under dbl.spamhaus.org, including
the always-listed test point. That silently reads as "clean" unless we explicitly verify the
resolver can see Spamhaus at all first: `test.dbl.spamhaus.org` is guaranteed listed
(127.0.1.2); if OUR resolver can't resolve it to a 127.* address, the zone is unreachable and
we must not trust any "not listed" result from it -> RAISE (fail-closed), scoring routes the
domain to manual `scored` instead of silently passing the gate.

Spamhaus blocks public resolvers (8.8.8.8/1.1.1.1): set DNS_RESOLVER to your own unbound
(then queries go through dnspython), or use SPAMHAUS_DQS_KEY.
"""
import socket
from app.config import settings

_ZONES = ("dbl.spamhaus.org", "multi.surbl.org")
_TESTPOINT = "test.dbl.spamhaus.org"     # всегда листнут (127.0.1.2) — контроль доступности


class BlacklistClient:
    _control_ok: bool | None = None       # кэш контроля на процесс

    def _resolve(self, host: str) -> str | None:
        """A-запись IP или None на NXDOMAIN. Через свой DNS_RESOLVER, если задан
        (dnspython), иначе системный резолвер (stdlib socket). Транзиентный сбой
        (не NXDOMAIN) — RAISE, чтобы не трактовать недоступность как «чисто»."""
        if settings.DNS_RESOLVER:
            import dns.resolver  # dnspython нужен только на этом пути
            resolver = dns.resolver.Resolver(configure=False)
            resolver.nameservers = [settings.DNS_RESOLVER]
            try:
                return resolver.resolve(host, "A")[0].address
            except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
                return None
        try:
            return socket.gethostbyname(host)
        except socket.gaierror as e:
            if e.errno in (socket.EAI_NONAME, getattr(socket, "EAI_NODATA", socket.EAI_NONAME)):
                return None                   # настоящий NXDOMAIN — не листнут в этой зоне
            raise                             # резолвер/сеть отвалились — вверх, не «чисто»

    def _ensure_control(self) -> None:
        """Тест-поинт Spamhaus всегда листнут; если наш резолвер его не видит —
        публичный резолвер заблокирован, проверка бессмысленна → RAISE (fail-closed)."""
        if BlacklistClient._control_ok is None:
            try:
                ip = self._resolve(_TESTPOINT)
            except OSError:
                ip = None
            BlacklistClient._control_ok = bool(ip and ip.startswith("127."))
        if not BlacklistClient._control_ok:
            raise RuntimeError(
                "blacklist: резолвер не видит Spamhaus (тест-поинт не листнут) — задай DNS_RESOLVER")

    def is_blacklisted(self, domain: str) -> bool | None:
        """True = листнут, False = чист на всех зонах, None = транзиентный сбой резолвера."""
        self._ensure_control()
        for zone in _ZONES:
            try:
                ip = self._resolve(f"{domain}.{zone}")
            except OSError:
                return None                   # транзиент -> None -> scoring уведёт в errors
            if ip is None:
                continue
            if ip.startswith("127.255.255."):
                raise RuntimeError("Spamhaus blocked public resolver — задай DNS_RESOLVER")
            if ip.startswith("127."):
                return True
        return False

    def ping(self) -> bool:
        """Тест-поинт через _resolve() — тот же DNS_RESOLVER-путь, что и реальный гейт
        (is_blacklisted), иначе на боксе с публичным системным DNS Spamhaus блокирует
        голый socket.gethostbyname и /diag врёт «down» при исправном гейте."""
        try:
            ip = self._resolve(_TESTPOINT)
        except OSError:
            return False
        return bool(ip and ip.startswith("127."))
