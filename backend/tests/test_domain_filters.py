"""Чистые фильтры v2: белый список зон, чужие VPN-бренды, генератор EMD."""
from app.services.domain_filters import brand_hit, canonical_domain, emd_candidates, tld_match


def test_tld_match_takes_registrable_zone_only():
    assert tld_match("foo.co.uk", ["uk", "co.uk"]) == "co.uk"
    assert tld_match("foo.co.uk", ["uk"]) is None        # зона foo.co.uk — co.uk, не uk
    assert tld_match("x.com.mx", ["mx"]) is None          # .com.mx NameSilo не продаёт — не наша
    assert tld_match("x.mx", ["mx"]) == "mx"
    assert tld_match("fooco.uk", ["co.uk"]) is None       # сравнение по полной метке
    assert tld_match("a.com", [" .COM "]) == "com"        # мусор в списке оператора нормализуется
    assert tld_match("a.com", []) is None


def test_tld_match_is_case_insensitive_for_the_domain():
    """Финальное ревью: домен в смешанном регистре (ручной список, W0 по имени из БД) — тот же матч,
    что и в нижнем: иначе `Foo.CO.UK` ушёл бы в tld_closed, хотя зона в белом списке."""
    assert tld_match("Foo.CO.UK", ["uk", "co.uk"]) == "co.uk"
    assert tld_match("WerKleittechnik.COM", ["com"]) == "com"
    assert tld_match("X.Com.MX", ["mx"]) is None


def test_brand_hit_substring_only_for_vpn_or_long_tokens():
    tokens = ["nordvpn", "surfshark", "pia", "avast", "norton"]
    # с «vpn» внутри или от 7 символов — подстрокой в имени без дефисов
    assert brand_hit("bestnordvpndeals.com", tokens) == "nordvpn"
    assert brand_hit("mynordvpn.com", tokens) == "nordvpn"
    assert brand_hit("nordvpn-deals.com", tokens) == "nordvpn"
    assert brand_hit("my-surf-shark.net", tokens) == "surfshark"   # дефисы не спасают
    # короткие без «vpn» — только целой частью между дефисами
    assert brand_hit("best-avast-deal.com", tokens) == "avast"
    assert brand_hit("pia-vpn.com", tokens) == "pia"
    assert brand_hit("javastudio.com", tokens) is None             # j-AVAST-udio — обычное слово
    assert brand_hit("nortonville.com", tokens) is None            # топоним, не бренд
    assert brand_hit("utopia.com", tokens) is None
    assert brand_hit("clean-vpn.com", tokens) is None


def test_emd_candidates_order_dedupe_brands_and_idn():
    sets = [{"market": "es-MX", "lang": "es",
             "keywords": ["mejor vpn", "  ", "nordvpn gratis", "vpn"],
             "tlds": ["com", ".MX"]},
            {"market": "es-CO", "lang": "es", "keywords": ["vpn"], "tlds": ["com"]}]
    out = emd_candidates(sets, ["nordvpn"])
    assert [c["domain"] for c in out] == [
        "mejorvpn.com", "mejorvpn.mx", "mejor-vpn.com", "mejor-vpn.mx", "vpn.com", "vpn.mx"]
    assert out[0] == {"domain": "mejorvpn.com", "market": "es-MX", "lang": "es"}
    idn = emd_candidates([{"keywords": ["vpn grátis"], "tlds": ["com"]}], [])
    assert idn[0]["domain"].startswith("xn--") and idn[0]["domain"].endswith(".com")


def test_emd_candidates_string_instead_of_list_is_one_item():
    # строка вместо списка — ОДИН ключ и ОДНА зона, а не набор букв (m.com, e.com, j.com…)
    out = emd_candidates([{"market": "es-MX", "lang": "es", "keywords": "mejor vpn", "tlds": "com"}], [])
    assert [c["domain"] for c in out] == ["mejorvpn.com", "mejor-vpn.com"]


def test_canonical_domain_still_reexported_from_discovery():
    from app.services import discovery
    assert discovery.canonical_domain is canonical_domain
    assert canonical_domain("WerKleittechnik.com") == "werkleittechnik.com"
