"""Spam blacklist check (Stage E). See docs/api/blacklist.md.

DNS-based domain lists: Spamhaus DBL, SURBL. Query <domain>.<zone> for an A record;
NXDOMAIN = not listed, 127.0.x.y = listed, 127.255.255.x = lookup unavailable (public
resolver blocked / over quota) -> RAISE (never treat as clean OR as a hit).

Spamhaus blocks public resolvers (8.8.8.8/1.1.1.1): set DNS_RESOLVER to your own unbound
(then queries go through dnspython), or use SPAMHAUS_DQS_KEY. Without a private resolver
the 127.255.255.x sentinel fires and this raises — the scoring risk-guard then routes the
domain to manual `scored` instead of silently passing the blacklist gate.
"""
import socket
from app.config import settings

_ZONES = ("dbl.spamhaus.org", "multi.surbl.org")


class BlacklistClient:
    def _resolve(self, host: str) -> str | None:
        """A-record IP for host, or None on NXDOMAIN. Через свой DNS_RESOLVER, если задан
        (dnspython), иначе системный резолвер (stdlib socket)."""
        if settings.DNS_RESOLVER:
            import dns.resolver  # dnspython нужен только на этом пути
            resolver = dns.resolver.Resolver(configure=False)
            resolver.nameservers = [settings.DNS_RESOLVER]
            try:
                return resolver.resolve(host, "A")[0].address
            except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
                return None  # здесь не листнут
        try:
            return socket.gethostbyname(host)
        except socket.gaierror:
            return None  # NXDOMAIN on this zone = not listed here

    def is_blacklisted(self, domain: str) -> bool | None:
        """True = listed, False = clean on all zones, None = lookup unavailable."""
        for zone in _ZONES:
            try:
                ip = self._resolve(f"{domain}.{zone}")
            except OSError:
                return None  # системный резолвер/сеть отвалились -> unknown
            if ip is None:
                continue  # NXDOMAIN -> not listed on this zone
            if ip.startswith("127.255.255."):
                # sentinel «публичный резолвер заблокирован» — НЕ тихий None: пусть уйдёт
                # в errors, чтобы risk-guard увёл домен в scored, а не мимо гейта.
                raise RuntimeError("Spamhaus blocked public resolver — задай DNS_RESOLVER")
            if ip.startswith("127."):
                return True
        return False  # all zones NXDOMAIN -> clean

    def ping(self) -> bool:
        # control lookup: the DBL test point is listed -> should resolve to 127.0.1.2
        try:
            return socket.gethostbyname("test.dbl.spamhaus.org").startswith("127.")
        except OSError:
            return False
