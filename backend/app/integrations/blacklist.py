"""Spam blacklist check (Stage E). See docs/api/blacklist.md.

Spamhaus DBL: query <domain>.dbl.spamhaus.org for an A record; NXDOMAIN = not listed,
127.0.x.y = listed, 127.255.255.x = lookup unavailable (public resolver blocked / over
quota) -> RAISE (never treat as clean OR as a hit).

With SPAMHAUS_DQS_KEY set, the query moves to <domain>.<key>.dbl.dq.spamhaus.net instead
— DQS is keyed (authenticates by account, not source IP), so it is not subject to the
open-resolver/residential-IP block that the free public zone enforces (same 127.* codes).

SURBL is DISABLED (2026-07-08): the free public `multi.surbl.org` zone returns
`127.0.0.1` (access blocked) from a residential egress IP exactly like Spamhaus used to
before DQS — but SURBL has no free keyed alternative (only a paid data feed, see
docs/api/blacklist.md). Querying it would just poison every domain's risk-guard into
permanent manual review for a check that can never succeed here. Re-enable if a clean
egress (e.g. routed via a hosting-provider VPS) or a paid SURBL feed becomes available.

Fail-closed control probe: many public resolvers don't return the 127.255.255.x sentinel
at all for Spamhaus zones — they just NXDOMAIN everything under dbl.spamhaus.org, including
the always-listed test point. That silently reads as "clean" unless we explicitly verify the
resolver can see Spamhaus at all first: the permanent test entry is guaranteed listed
(127.0.1.2); if OUR resolver can't resolve it to a 127.* address, the zone is unreachable and
we must not trust any "not listed" result from it -> RAISE (fail-closed), scoring routes the
domain to manual `scored` instead of silently passing the gate.

Spamhaus blocks public resolvers (8.8.8.8/1.1.1.1) AND residential/generic-rDNS egress IPs
on its free public zone: set DNS_RESOLVER to your own unbound (fixes the public-resolver
case only), or set SPAMHAUS_DQS_KEY (free, lifts both restrictions).
"""
import socket
import threading
from app.config import settings

_TESTPOINT = "test"     # DBL-домен, всегда листнут (127.0.1.2) — контроль доступности


class BlacklistClient:
    _control_ok: bool | None = None       # кэш контроля на процесс
    # Волновая архитектура (2026-07-20): is_blacklisted() зовётся конкурентно из _wave_risk (до
    # 12 потоков). Без лока несколько потоков одновременно видят _control_ok в исходном None и
    # КАЖДЫЙ шлёт свой DNS-запрос тест-поинта — не только лишняя нагрузка на резолвер, а реальная
    # гонка результата: если ОДИН из этих параллельных запросов транзиентно не резолвится (пакет
    # потерян), его домен ловит RuntimeError и уходит в ручной обзор ("оценён вслепую"), хотя
    # СОСЕДНИЙ поток тем же миллисекундами резолвит тест-поинт успешно — резолвер живой, просто
    # не повезло с таймингом. Лок сериализует контроль: один реальный запрос на всех, детерминированно.
    _control_lock = threading.Lock()

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

    def _dbl_host(self, label: str) -> str:
        """label — домен на проверку либо литерал _TESTPOINT. С SPAMHAUS_DQS_KEY уходит
        в приватную keyed-зону вместо публичной (не блокируется как public/residential)."""
        if settings.SPAMHAUS_DQS_KEY:
            return f"{label}.{settings.SPAMHAUS_DQS_KEY}.dbl.dq.spamhaus.net"
        return f"{label}.dbl.spamhaus.org"

    def _ensure_control(self) -> None:
        """Тест-поинт Spamhaus всегда листнут; если наш резолвер его не видит —
        публичный резолвер заблокирован, проверка бессмысленна → RAISE (fail-closed).

        Кэшируем ТОЛЬКО положительный результат (контроль прошёл — на процесс). Отрицательный
        НЕ кэшируем: транзиентный сбой резолвера сразу после старта воркера иначе навсегда
        (до рестарта контейнера) загонял бы КАЖДЫЙ последующий домен в путь «история не
        проверена», хотя Spamhaus восстановился через секунду. RknClient уже делает так же —
        при неудачной загрузке `_loaded_at` не выставляется, и следующий вызов ретраит."""
        with BlacklistClient._control_lock:
            if BlacklistClient._control_ok:
                return
            try:
                ip = self._resolve(self._dbl_host(_TESTPOINT))
            except OSError:
                ip = None
            ok = bool(ip and ip.startswith("127."))
            if ok:
                BlacklistClient._control_ok = True     # кэшируем только успех
                return
            raise RuntimeError(
                "blacklist: резолвер не видит Spamhaus DBL (тест-поинт не листнут) — "
                "задай DNS_RESOLVER или SPAMHAUS_DQS_KEY")

    def is_blacklisted(self, domain: str) -> bool | None:
        """True = листнут в Spamhaus DBL, False = чист, None = транзиентный сбой резолвера.
        SURBL не проверяется — см. докстринг модуля."""
        self._ensure_control()
        try:
            ip = self._resolve(self._dbl_host(domain))
        except OSError:
            return None                       # транзиент -> None -> scoring уведёт в errors
        if ip is None:
            return False
        if ip.startswith("127.255.255."):
            raise RuntimeError(
                "Spamhaus заблокировал запрос (public resolver / DQS-ключ невалиден) — "
                "проверь DNS_RESOLVER/SPAMHAUS_DQS_KEY")
        return ip.startswith("127.")

    def ping(self) -> bool:
        """Тест-поинт через _resolve() — тот же DNS_RESOLVER/DQS-путь, что и реальный гейт
        (is_blacklisted), иначе на боксе с публичным системным DNS Spamhaus блокирует
        голый socket.gethostbyname и /diag врёт «down» при исправном гейте."""
        try:
            ip = self._resolve(self._dbl_host(_TESTPOINT))
        except OSError:
            return False
        return bool(ip and ip.startswith("127."))
