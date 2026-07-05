"""Phase 0 connectivity check — pings free/local integrations (no creds) and
credentialed ones when keys are present. Green = reachable.

    python scripts/smoke.py
    docker compose run --rm backend python scripts/smoke.py
"""
from app.config import settings


def check(name: str, fn) -> None:
    try:
        ok = fn()
        print(f"[{'OK  ' if ok else 'FAIL'}] {name}")
    except NotImplementedError:
        print(f"[TODO] {name}  (ping не реализован)")
    except Exception as e:  # noqa: BLE001
        print(f"[FAIL] {name}: {type(e).__name__}: {e}")


def skip(name: str, why: str) -> None:
    print(f"[SKIP] {name}  ({why})")


def main() -> None:
    from app.integrations.searxng import SearxngClient
    from app.integrations.llm import LlmClient
    from app.integrations.wayback import WaybackClient
    from app.integrations.rkn import RknClient
    from app.integrations.backorder import BackorderClient
    from app.integrations.openpagerank import OpenPageRankClient
    from app.integrations.aparser import AParserClient

    print("— free / локальные (Фаза 0, без кредов) —")
    check("searxng", lambda: SearxngClient().ping())
    check("litellm (llm)", lambda: LlmClient().ping())
    check("wayback", lambda: WaybackClient().ping())
    check("rkn (antizapret)", lambda: RknClient().ping())
    check("backorder discovery", lambda: BackorderClient().ping())
    if settings.APARSER_API_KEY:
        check("a-parser", lambda: AParserClient().ping())
    else:
        skip("a-parser", "нет APARSER_API_KEY")
    if settings.OPENPAGERANK_API_KEY:
        check("openpagerank", lambda: OpenPageRankClient().ping())
    else:
        skip("openpagerank", "нет OPENPAGERANK_API_KEY (free-ключ, 2 мин)")

    print("— credentialed (реализуются позже, при наличии кредов) —")
    for name in ("cloudflare", "aapanel", "optimizator", "registrar"):
        skip(name, "ping/креды — Фаза 2+")
    skip("gsc", "исключён из v1")


if __name__ == "__main__":
    main()
