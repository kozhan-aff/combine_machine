"""Spam blacklist check (Stage E). See docs/api/blacklist.md.

DNS-based domain lists: Spamhaus DBL, SURBL. Query <domain>.<zone> for an A record;
NXDOMAIN = not listed, 127.0.x.y = listed, 127.255.255.x = lookup unavailable (public
resolver blocked / over quota) -> return None (never treat as clean OR as a hit).

v1 uses stdlib socket (system resolver). For volume, run own unbound and set DNS_RESOLVER,
or use SPAMHAUS_DQS_KEY.
# ponytail: stdlib resolver; swap to dnspython+custom resolver when volume needs it.
"""
import socket

_ZONES = ("dbl.spamhaus.org", "multi.surbl.org")


class BlacklistClient:
    def is_blacklisted(self, domain: str) -> bool | None:
        """True = listed, False = clean on all zones, None = lookup unavailable."""
        for zone in _ZONES:
            try:
                ip = socket.gethostbyname(f"{domain}.{zone}")
            except socket.gaierror:
                continue  # NXDOMAIN on this zone = not listed here
            except OSError:
                return None  # resolver/network error -> unknown
            if ip.startswith("127.255."):
                return None  # error/blocked-resolver sentinel -> unknown, not a hit
            if ip.startswith("127."):
                return True
        return False  # all zones NXDOMAIN -> clean

    def ping(self) -> bool:
        # control lookup: the DBL test point is listed -> should resolve to 127.0.1.2
        try:
            return socket.gethostbyname("test.dbl.spamhaus.org").startswith("127.")
        except OSError:
            return False
