"""M4 enrichment — реальные факты VPN-вертикали для «information gain» (PLAN §2).

Проблема, которую это закрывает: без реальных данных LLM пишет «тонкий AI» —
общие фразы, которые Google давит апдейтами (scaled content abuse). Здесь курируемый
датасет проверяемых фактов по брендам (серверы, страны, протоколы, юрисдикция, аудиты,
цена, no-logs) — он подаётся в промпт генерации, и черновик опирается на конкретику,
а не на воду. Это тот самый редакторски-проверяемый фундамент из принципа §2.

ВАЖНО (ceiling): факты статичны и ДРЕЙФУЮТ — серверы/цены/аудиты меняются. Помечены
датой (`AS_OF`). Обновлять руками перед запуском новой волны сайтов; при реальном
масштабе — тянуть из фидов провайдеров/их API (Фаза 3). Здесь — честный стартовый срез,
не выдумка: только широко публикуемые бренд-факты.
"""

AS_OF = "2026-07"  # срез данных; см. docstring — обновлять перед новой волной

# ключ — нормализованный бренд (lower, без пробелов/дефисов); значения — проверяемые факты.
VPN_FACTS: dict[str, dict] = {
    "nordvpn": {
        "brand": "NordVPN", "servers": "5500+", "countries": 60,
        "jurisdiction": "Панама (вне 14 Eyes)", "protocols": ["NordLynx (WireGuard)", "OpenVPN", "IKEv2"],
        "no_logs": "да, аудит Deloitte (2022, 2023) и повторные проверки",
        "audit": "независимый аудит no-logs (Deloitte), аудит приложений (VerSprite)",
        "streaming": "Netflix, Disney+, BBC iPlayer, Amazon Prime",
        "devices": 10, "price_from": "$3.09/мес (2 года)", "refund_days": 30,
        "extras": ["Threat Protection (блок трекеров/малвари)", "двойной VPN", "Onion over VPN", "Meshnet"],
    },
    "surfshark": {
        "brand": "Surfshark", "servers": "3200+", "countries": 100,
        "jurisdiction": "Нидерланды (9 Eyes, но подтверждённый no-logs)",
        "protocols": ["WireGuard", "OpenVPN", "IKEv2"],
        "no_logs": "да, аудит Deloitte (2023), серверы в RAM-only режиме",
        "audit": "аудит no-logs (Deloitte), аудит инфраструктуры (Cure53)",
        "streaming": "Netflix (30+ библиотек), Disney+, Hulu, BBC iPlayer",
        "devices": "без лимита", "price_from": "$2.19/мес (2 года)", "refund_days": 30,
        "extras": ["CleanWeb (реклама/трекеры)", "MultiHop", "Camouflage Mode", "статичный IP"],
    },
    "protonvpn": {
        "brand": "Proton VPN", "servers": "8900+", "countries": 110,
        "jurisdiction": "Швейцария (сильные законы о приватности, вне 14 Eyes)",
        "protocols": ["WireGuard", "OpenVPN", "IKEv2", "Stealth (обход DPI)"],
        "no_logs": "да, открытый код всех приложений + независимый аудит (Securitum)",
        "audit": "open-source клиенты, аудит Securitum, прозрачный отчёт",
        "streaming": "Netflix, Disney+, BBC iPlayer (Plus-серверы)",
        "devices": 10, "price_from": "$4.49/мес (2 года)", "refund_days": 30,
        "extras": ["бесплатный тариф без лимита трафика", "Secure Core (многохоп через Швейцарию/Исландию)", "NetShield", "Tor over VPN"],
    },
    "expressvpn": {
        "brand": "ExpressVPN", "servers": "3000+", "countries": 105,
        "jurisdiction": "Британские Виргинские острова (вне 14 Eyes)",
        "protocols": ["Lightway (собственный, на wolfSSL)", "OpenVPN", "IKEv2"],
        "no_logs": "да, TrustedServer (RAM-only), многократные аудиты (KPMG, Cure53, PwC)",
        "audit": "аудиты KPMG (no-logs), Cure53 (Lightway/приложения), PwC",
        "streaming": "Netflix, Disney+, Hulu, BBC iPlayer, Amazon Prime",
        "devices": 8, "price_from": "$4.99/мес (2 года)", "refund_days": 30,
        "extras": ["Lightway — быстрый коннект", "split tunneling", "Threat Manager", "серверы во всех регионах"],
    },
    "mullvad": {
        "brand": "Mullvad", "servers": "690+", "countries": 49,
        "jurisdiction": "Швеция (14 Eyes, но радикальный no-logs без аккаунтов)",
        "protocols": ["WireGuard", "OpenVPN"],
        "no_logs": "да, анонимные аккаунты (номер, без email), аудит Cure53/Assured, открытый код",
        "audit": "открытый код, аудиты Cure53, Assured, Radically Open Security",
        "streaming": "ограниченно (не позиционируется под стриминг)",
        "devices": 5, "price_from": "€5/мес (фиксированная цена без скидок-ловушек)", "refund_days": 30,
        "extras": ["оплата наличными/крипто анонимно", "нет аккаунтов с личными данными", "DAITA (защита от анализа трафика)", "мост/обфускация"],
    },
    "cyberghost": {
        "brand": "CyberGhost", "servers": "11000+", "countries": 100,
        "jurisdiction": "Румыния (вне 14 Eyes)",
        "protocols": ["WireGuard", "OpenVPN", "IKEv2"],
        "no_logs": "да, ежеквартальные прозрачные отчёты, аудит Deloitte (2022)",
        "audit": "аудит no-logs (Deloitte), прозрачные отчёты каждый квартал",
        "streaming": "выделенные стриминг-серверы под Netflix/Disney+/BBC/Hulu",
        "devices": 7, "price_from": "$2.19/мес (2 года)", "refund_days": 45,
        "extras": ["45 дней возврата (дольше всех)", "серверы под конкретные сервисы", "выделенный IP (опц.)", "NoSpy-серверы в Румынии"],
    },
    "pia": {
        "brand": "Private Internet Access (PIA)", "servers": "35000+ (крупнейшая сеть)", "countries": 91,
        "jurisdiction": "США (5 Eyes, но no-logs доказан в суде)",
        "protocols": ["WireGuard", "OpenVPN"],
        "no_logs": "да, no-logs подтверждён в суде (дела не дали данных), аудит Deloitte",
        "audit": "no-logs проверен судебными делами + аудит Deloitte, открытый код клиентов",
        "streaming": "Netflix, Amazon Prime, работает не со всеми библиотеками",
        "devices": "без лимита", "price_from": "$2.03/мес (3 года)", "refund_days": 30,
        "extras": ["открытый код всех приложений", "настраиваемое шифрование", "MACE (блок рекламы)", "выделенный IP (опц.)"],
    },
}

# синонимы/варианты написания → канонический ключ
_ALIASES = {
    "nord": "nordvpn", "nordvpn": "nordvpn",
    "surfshark": "surfshark", "surf": "surfshark",
    "proton": "protonvpn", "protonvpn": "protonvpn",
    "express": "expressvpn", "expressvpn": "expressvpn",
    "mullvad": "mullvad",
    "cyberghost": "cyberghost", "ghost": "cyberghost",
    "pia": "pia", "privateinternetaccess": "pia",
}


def _norm(brand: str) -> str:
    return "".join(ch for ch in (brand or "").lower() if ch.isalnum())


def facts_for(brand: str) -> dict | None:
    """Факты по бренду или None, если бренда нет в датасете (тогда генерим без vertical_data)."""
    key = _norm(brand)
    key = _ALIASES.get(key, key)
    return VPN_FACTS.get(key)


def vertical_block(brand: str) -> str | None:
    """Готовый текстовый блок для промпта генерации, или None если бренда нет в датасете.

    Возвращает компактный список проверяемых фактов — LLM обязан на них опираться,
    а не выдумывать. Формат человекочитаемый (LLM лучше усваивает, чем сырой JSON).
    """
    f = facts_for(brand)
    if not f:
        return None
    proto = ", ".join(f["protocols"])
    extras = "; ".join(f["extras"])
    return (
        f"Бренд: {f['brand']} (данные на {AS_OF}, использовать как факты, не выдумывать цифры).\n"
        f"- Серверы: {f['servers']} в {f['countries']} странах.\n"
        f"- Юрисдикция: {f['jurisdiction']}.\n"
        f"- Протоколы: {proto}.\n"
        f"- No-logs: {f['no_logs']}.\n"
        f"- Независимые аудиты: {f['audit']}.\n"
        f"- Стриминг: {f['streaming']}.\n"
        f"- Устройств одновременно: {f['devices']}.\n"
        f"- Цена от: {f['price_from']}; возврат в течение {f['refund_days']} дней.\n"
        f"- Ключевые фичи: {extras}."
    )


def known_brands() -> list[str]:
    return [v["brand"] for v in VPN_FACTS.values()]


if __name__ == "__main__":  # self-check: датасет + резолвинг брендов, без сети
    assert facts_for("NordVPN")["countries"] == 60
    assert facts_for("nord vpn")["brand"] == "NordVPN"        # алиас + пробел + регистр
    assert facts_for("Proton")["jurisdiction"].startswith("Швейцария")
    assert facts_for("PIA")["brand"].startswith("Private")
    assert facts_for("НесуществующийБренд") is None           # неизвестный → None (генерим без данных)
    block = vertical_block("Surfshark")
    assert "3200+" in block and "Нидерланды" in block and "WireGuard" in block, block
    assert vertical_block("unknown-xyz") is None
    # инвариант: у каждого бренда заполнены все ключи, что читает vertical_block
    need = {"brand", "servers", "countries", "jurisdiction", "protocols", "no_logs",
            "audit", "streaming", "devices", "price_from", "refund_days", "extras"}
    for k, v in VPN_FACTS.items():
        assert need <= set(v), f"{k}: не хватает {need - set(v)}"
    print(f"vertical_data ok: {len(VPN_FACTS)} брендов, срез {AS_OF} —", ", ".join(known_brands()))
