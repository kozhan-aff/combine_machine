"""labels.py — одна точка правды переводов статусов/reject/лейнов для панели."""


def test_status_ru_covers_domain_lifecycle():
    from app.services.labels import status_ru
    for s in ["discovered", "scored", "approved", "rejected",
              "purchasing", "purchased", "live"]:
        assert status_ru(s) and status_ru(s) != s   # переведён, не сырой


def test_status_ru_covers_order_site_page():
    from app.services.labels import status_ru
    for s in ["pending_confirm", "ordered", "caught", "failed", "cancelled",  # заказ M2
              "provisioning", "content", "published",                          # сайт
              "draft", "edited"]:                                              # страница
        assert status_ru(s) and status_ru(s) != s


def test_reject_ru_covers_all_reasons():
    from app.services.labels import reject_ru
    for r in ["low_rd", "feed_flag", "too_young", "rkn", "blacklist",
              "history_dirty", "low_score", "not_acquirable"]:
        assert reject_ru(r) and reject_ru(r) != r


def test_lane_and_fallback_and_none():
    from app.services.labels import status_ru, reject_ru, lane_ru
    assert lane_ru("bid") == "ставка" and lane_ru("free") == "свободный"
    assert status_ru("weird_unknown") == "weird_unknown"      # неизвестный → сырой
    assert status_ru(None) == "" and reject_ru(None) == "" and lane_ru(None) == ""
    assert status_ru("") == ""


def test_filters_registered_on_templates():
    from app.api.panel import templates
    assert templates.env.filters["status_ru"]("approved") == "одобрен"
    assert templates.env.filters["reject_ru"]("not_acquirable") == "нельзя купить"
    assert templates.env.filters["lane_ru"]("bid") == "ставка"
