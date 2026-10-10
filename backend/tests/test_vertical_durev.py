"""Факты Durev VPN в датасете: сняты живьём 2026-10-10; неподтверждённое в блок не попадает."""
from app.services import vertical_data as vd
from app.services.brief import allowed_numbers


def test_durev_block_has_live_facts_and_no_unknowns():
    block = vd.vertical_block("Durev VPN")
    assert block.startswith("Бренд: Durev VPN (данные на 2026-10")
    assert "- Серверы: 200+ в 50 странах." in block and "Казахстан" in block
    assert "289 руб/мес" in block and "328 руб/мес" in block and "493 руб/мес" in block
    assert "неизвестно" not in block.lower()
    for gone in ("Протоколы", "аудиты", "Стриминг", "возврат"):      # сервис этого не называет
        assert gone not in block, gone


def test_durev_aliases_and_other_brands_keep_global_date():
    assert vd.facts_for("durev")["brand"] == vd.facts_for("DurevVPN")["brand"] == "Durev VPN"
    assert vd.facts_for("durevpn")["brand"] == "Durev VPN"            # домен пишется без второй v
    assert f"данные на {vd.AS_OF}" in vd.vertical_block("NordVPN")


def test_durev_numbers_become_allowed_for_the_critic():
    allowed = allowed_numbers([], vd.vertical_block("Durev VPN"))
    assert {"289", "328", "493", "250", "650", "30", "300", "50", "200"} <= allowed
