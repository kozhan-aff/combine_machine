"""Настройки v2: дефолты, нормализация списков, валидация наборов EMD, пол остатка units."""
import pytest

from app.services import scoring_config as cfg
from app.services.settings import get_settings, update_settings


def test_v2_defaults():
    s = get_settings()
    assert s["min_dr"] == 5.0 and s["max_links_per_run"] == 500 and s["max_deep_per_run"] == 20
    assert s["spam_anchor_max"] == 0.2 and s["emd_sets"] == []
    assert s["tld_allowlist"] == cfg.TLD_ALLOWLIST and "nordvpn" in s["brand_tokens"]
    assert s["units_floor"] == cfg.UNITS_FLOOR == 300000


def test_lists_from_textarea_are_normalized():
    s = update_settings(tld_allowlist=" .CO.UK, com\nmx com ;", brand_tokens="NordVPN\n\nsurfshark")
    assert s["tld_allowlist"] == ["co.uk", "com", "mx"]
    assert s["brand_tokens"] == ["nordvpn", "surfshark"]


def test_emd_sets_valid_json_saved_invalid_rejected_without_losing_old():
    ok = '[{"market":"es-MX","lang":"ES","keywords":["mejor vpn"," "],"tlds":["com",".MX"]},{"keywords":[]}]'
    s = update_settings(emd_sets=ok)
    assert s["emd_sets"] == [{"market": "es-MX", "lang": "es", "keywords": ["mejor vpn"], "tlds": ["com", "mx"]}]
    with pytest.raises(ValueError):
        update_settings(emd_sets="{not json")
    assert get_settings()["emd_sets"] == s["emd_sets"]


def test_emd_sets_string_instead_of_list_is_one_keyword():
    # строка вместо списка: ключ — ОДИН (не m, e, j…), зоны строкой разбираются как textarea
    s = update_settings(emd_sets='[{"market":"es-MX","lang":"es","keywords":"mejor vpn","tlds":"com .MX"}]')
    assert s["emd_sets"] == [{"market": "es-MX", "lang": "es", "keywords": ["mejor vpn"], "tlds": ["com", "mx"]}]


def test_numeric_bounds():
    s = update_settings(min_dr=500, max_links_per_run=0, max_deep_per_run=9999, spam_anchor_max=3)
    assert s["min_dr"] == 100.0 and s["max_links_per_run"] == 1
    assert s["max_deep_per_run"] == 500 and s["spam_anchor_max"] == 1.0


def test_units_floor_bounds():
    assert update_settings(units_floor=5_000_000)["units_floor"] == 2_000_000
    assert update_settings(units_floor=-1)["units_floor"] == 0               # 0 = пола нет
    assert update_settings(units_floor=150000)["units_floor"] == 150000


def test_emd_non_string_items_are_dropped_not_stringified():
    # str(None) -> «none», str(5) -> «5»: молча стали бы ключами/зонами
    s = update_settings(emd_sets='[{"market":"x","lang":"es","keywords":["mejor vpn",null,5,true],'
                                 '"tlds":["com",null,7,false]}]')
    assert s["emd_sets"] == [{"market": "x", "lang": "es", "keywords": ["mejor vpn"], "tlds": ["com"]}]
    assert update_settings(tld_allowlist=["com", None, 5, True, "mx"])["tld_allowlist"] == ["com", "mx"]


def test_emd_dotted_keyword_rejected_without_losing_old():
    # best.vpn + com = best.vpn.com — не регистрируемое имя
    ok = update_settings(emd_sets='[{"keywords":["mejor vpn"],"tlds":["com"]}]')["emd_sets"]
    with pytest.raises(ValueError, match="точк"):
        update_settings(emd_sets='[{"keywords":["best.vpn"],"tlds":["com"]}]')
    assert get_settings()["emd_sets"] == ok


@pytest.mark.parametrize("bad", ['{"keywords":5,"tlds":["com"]}', '{"keywords":["a"],"tlds":true}',
                                 '{"keywords":{"a":1},"tlds":["com"]}'])
def test_emd_scalar_instead_of_list_is_value_error(bad):
    with pytest.raises(ValueError):
        update_settings(emd_sets="[" + bad + "]")


def test_list_setting_scalar_is_value_error():
    with pytest.raises(ValueError):
        update_settings(tld_allowlist=True)
    with pytest.raises(ValueError):
        update_settings(brand_tokens=5)
