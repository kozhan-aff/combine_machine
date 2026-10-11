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
    assert templates.env.filters["reject_ru"]("not_acquirable") == "занят"
    assert templates.env.filters["lane_ru"]("bid") == "ставка"


def test_v2_labels():
    from app.services.labels import reject_ru, source_badge, source_ru
    assert reject_ru("spam_anchors") == "спам-ссылки" and reject_ru("legacy_ru") == "архив РФ"
    assert reject_ru("tld_closed") == "зона не наша" and reject_ru("trademark") == "чужой бренд"
    assert source_ru("nominet") == "Nominet" and source_ru("emd") == "из ключевых слов (EMD)" and source_ru(None) == ""
    # 6.2: бейдж — явная карта, не срез [:3] («вру», «reg» путался с reg.ru)
    assert [source_badge(s) for s in ("dropcatch", "nominet", "mx", "emd", "list")] == ["dc", "uk", "mx", "emd", "руч"]
    assert source_badge("backorder") == "bo" and source_badge("zzz") == "?" and source_badge(None) == "?"


def test_site_status_has_its_own_word_for_published():
    """`published` у страницы — «на сайте», у сайта — «опубликован»: ключ один, смысл разный."""
    from app.api.panel import templates
    from app.services.labels import site_status_ru, status_ru
    assert status_ru("published") == "на сайте" and site_status_ru("published") == "опубликован"
    assert site_status_ru("content") == status_ru("content") == "пишутся тексты"   # остальное — общий словарь
    assert site_status_ru("weird") == "weird" and site_status_ru(None) == ""
    assert templates.env.filters["site_status_ru"]("published") == "опубликован"
