"""Словарь служебных строк публикуемого сайта по языку (CTA, промокод, раскрытие, навигация, заголовки).

Единственное место, где живут тексты, которые сборка сайта подмешивает вокруг контента: раскрытие
партнёрства обязано быть на языке читателя (FTC/ASA), а не по-русски на сайте для de/es. Это ШОВ
для расширения языков: новый язык = новая запись в TEXTS (+ LANG_NAMES/OG_LOCALE), код не трогается.
Неизвестный язык для РЕНДЕРА -> английский (международный дефолт v2), а не русский; для ГЕНЕРАЦИИ
неподдержанный язык — отказ (`resolve_lang`): молча писать польский рынок по-английски нельзя.
"""
TEXTS = {
    "en": {
        "disclosure": ("Disclosure: this page contains affiliate links. We may earn a "
                       "commission on purchases made through them, at no extra cost to you."),
        "cta": "Go to {brand}", "promo": "Promo code",
        "nav_home": "Home", "nav_review": "Review", "nav_comparison": "Comparison",
        "nav_howto": "Setup guide", "menu": "Main menu", "footer": "Independent review site",
        "title_review": "{brand}: review and honest test",
        "title_comparison": "{brand} vs competitors", "title_howto": "How to set up {brand}",
        "review_word": "review",
        "b_privacy": "Privacy", "b_speed": "Speed", "b_access": "Global access",
        "alt_hero": "Abstract illustration for the page “{title}”",
        "facts_title": "Network size at a glance", "facts_servers": "Servers",
        "alt_chart": "Bar chart comparing the number of servers of popular VPN services",
        "q_review": "{brand} review", "q_comparison": "{brand} vs alternatives",
        "q_howto": "how to set up {brand}", "q_market": "best VPN {country}",
        "lbl_score": "Score", "lbl_for": "Best for", "lbl_not_for": "Not for",
        "lbl_pros": "Pros", "lbl_cons": "Cons",
        "lbl_steps": "Step by step", "lbl_faq": "FAQ",
    },
    "ru": {
        "disclosure": ("Раскрытие: страница содержит партнёрские ссылки. Мы можем получить "
                       "комиссию за покупки по ним — без доплаты для вас."),
        "cta": "Перейти к {brand}", "promo": "Промокод",
        "nav_home": "Главная", "nav_review": "Обзор", "nav_comparison": "Сравнение",
        "nav_howto": "Инструкция", "menu": "Главное меню", "footer": "Независимый обзорный сайт",
        "title_review": "{brand}: обзор и честный тест",
        "title_comparison": "{brand} против конкурентов", "title_howto": "Как настроить {brand}",
        "review_word": "обзор",
        "b_privacy": "Приватность", "b_speed": "Скорость", "b_access": "Доступ по всему миру",
        "alt_hero": "Абстрактная иллюстрация к странице «{title}»",
        "facts_title": "Размер сети в цифрах", "facts_servers": "Серверы",
        "alt_chart": "Столбчатая диаграмма: число серверов популярных VPN-сервисов",
        "q_review": "{brand} обзор", "q_comparison": "{brand} против конкурентов",
        "q_howto": "как настроить {brand}", "q_market": "лучший VPN {country}",
        "lbl_score": "Оценка", "lbl_for": "Кому подойдёт", "lbl_not_for": "Кому не подойдёт",
        "lbl_pros": "Плюсы", "lbl_cons": "Минусы",
        "lbl_steps": "Пошагово", "lbl_faq": "Вопросы и ответы",
    },
    "de": {
        "disclosure": ("Hinweis: Diese Seite enthält Affiliate-Links. Wir erhalten ggf. eine "
                       "Provision für Käufe über diese Links – für Sie entstehen keine Mehrkosten."),
        "cta": "Zu {brand}", "promo": "Gutscheincode",
        "nav_home": "Startseite", "nav_review": "Testbericht", "nav_comparison": "Vergleich",
        "nav_howto": "Einrichtung", "menu": "Hauptmenü", "footer": "Unabhängige Testseite",
        "title_review": "{brand}: Test und ehrliche Bewertung",
        "title_comparison": "{brand} im Vergleich zur Konkurrenz", "title_howto": "{brand} einrichten",
        "review_word": "Test",
        "b_privacy": "Privatsphäre", "b_speed": "Geschwindigkeit", "b_access": "Weltweiter Zugang",
        "alt_hero": "Abstrakte Illustration zur Seite „{title}“",
        "facts_title": "Netzwerkgröße im Überblick", "facts_servers": "Server",
        "alt_chart": "Balkendiagramm: Serveranzahl beliebter VPN-Dienste",
        "q_review": "{brand} Test", "q_comparison": "{brand} Vergleich",
        "q_howto": "{brand} einrichten", "q_market": "bester VPN {country}",
        "lbl_score": "Bewertung", "lbl_for": "Geeignet für", "lbl_not_for": "Weniger geeignet für",
        "lbl_pros": "Vorteile", "lbl_cons": "Nachteile",
        "lbl_steps": "Schritt für Schritt", "lbl_faq": "Häufige Fragen",
    },
    "es": {
        "disclosure": ("Aviso: esta página contiene enlaces de afiliado. Podemos recibir una "
                       "comisión por las compras realizadas a través de ellos, sin coste adicional para ti."),
        "cta": "Ir a {brand}", "promo": "Código promocional",
        "nav_home": "Inicio", "nav_review": "Reseña", "nav_comparison": "Comparativa",
        "nav_howto": "Guía de configuración", "menu": "Menú principal",
        "footer": "Sitio de reseñas independiente",
        "title_review": "{brand}: reseña y prueba honesta",
        "title_comparison": "{brand} frente a la competencia", "title_howto": "Cómo configurar {brand}",
        "review_word": "opiniones",
        "b_privacy": "Privacidad", "b_speed": "Velocidad", "b_access": "Acceso global",
        "alt_hero": "Ilustración abstracta para la página «{title}»",
        "facts_title": "El tamaño de la red de un vistazo", "facts_servers": "Servidores",
        "alt_chart": "Gráfico de barras: número de servidores de servicios VPN populares",
        "q_review": "{brand} opiniones", "q_comparison": "{brand} vs alternativas",
        "q_howto": "cómo configurar {brand}", "q_market": "mejor VPN {country}",
        "lbl_score": "Puntuación", "lbl_for": "Ideal para", "lbl_not_for": "No recomendado para",
        "lbl_pros": "Ventajas", "lbl_cons": "Inconvenientes",
        "lbl_steps": "Paso a paso", "lbl_faq": "Preguntas frecuentes",
    },
    "fr": {
        "disclosure": ("Information : cette page contient des liens d'affiliation. Nous pouvons "
                       "percevoir une commission sur les achats effectués via ces liens, sans frais "
                       "supplémentaires pour vous."),
        "cta": "Aller sur {brand}", "promo": "Code promo",
        "nav_home": "Accueil", "nav_review": "Test", "nav_comparison": "Comparatif",
        "nav_howto": "Guide d'installation", "menu": "Menu principal",
        "footer": "Site d'avis indépendant",
        "title_review": "{brand} : test et avis honnête",
        "title_comparison": "{brand} face à la concurrence", "title_howto": "Comment configurer {brand}",
        "review_word": "avis",
        "b_privacy": "Confidentialité", "b_speed": "Vitesse", "b_access": "Accès mondial",
        "alt_hero": "Illustration abstraite pour la page « {title} »",
        "facts_title": "La taille du réseau en un coup d'œil", "facts_servers": "Serveurs",
        "alt_chart": "Diagramme en barres : nombre de serveurs des VPN populaires",
        "q_review": "{brand} avis", "q_comparison": "{brand} comparatif",
        "q_howto": "configurer {brand}", "q_market": "meilleur VPN {country}",
        "lbl_score": "Note", "lbl_for": "Idéal pour", "lbl_not_for": "Déconseillé pour",
        "lbl_pros": "Avantages", "lbl_cons": "Inconvénients",
        "lbl_steps": "Étape par étape", "lbl_faq": "Questions fréquentes",
    },
    "nl": {
        "disclosure": ("Vermelding: deze pagina bevat affiliatelinks. Wij kunnen een commissie "
                       "ontvangen voor aankopen via deze links, zonder extra kosten voor jou."),
        "cta": "Naar {brand}", "promo": "Kortingscode",
        "nav_home": "Home", "nav_review": "Review", "nav_comparison": "Vergelijking",
        "nav_howto": "Installatiegids", "menu": "Hoofdmenu", "footer": "Onafhankelijke reviewsite",
        "title_review": "{brand}: review en eerlijke test",
        "title_comparison": "{brand} vergeleken met concurrenten", "title_howto": "{brand} instellen",
        "review_word": "review",
        "b_privacy": "Privacy", "b_speed": "Snelheid", "b_access": "Wereldwijde toegang",
        "alt_hero": "Abstracte illustratie bij de pagina “{title}”",
        "facts_title": "De netwerkgrootte in één oogopslag", "facts_servers": "Servers",
        "alt_chart": "Staafdiagram: aantal servers van populaire VPN-diensten",
        "q_review": "{brand} review", "q_comparison": "{brand} vergelijking",
        "q_howto": "{brand} instellen", "q_market": "beste VPN {country}",
        "lbl_score": "Score", "lbl_for": "Geschikt voor", "lbl_not_for": "Minder geschikt voor",
        "lbl_pros": "Voordelen", "lbl_cons": "Nadelen",
        "lbl_steps": "Stap voor stap", "lbl_faq": "Veelgestelde vragen",
    },
    "pt": {
        "disclosure": ("Aviso: esta página contém links de afiliado. Podemos receber uma comissão "
                       "pelas compras feitas através deles, sem custo adicional para si."),
        "cta": "Ir para {brand}", "promo": "Código promocional",
        "nav_home": "Início", "nav_review": "Análise", "nav_comparison": "Comparação",
        "nav_howto": "Guia de configuração", "menu": "Menu principal",
        "footer": "Site de análises independente",
        "title_review": "{brand}: análise e teste honesto",
        "title_comparison": "{brand} contra a concorrência", "title_howto": "Como configurar o {brand}",
        "review_word": "análise",
        "b_privacy": "Privacidade", "b_speed": "Velocidade", "b_access": "Acesso global",
        "alt_hero": "Ilustração abstrata para a página «{title}»",
        "facts_title": "O tamanho da rede num relance", "facts_servers": "Servidores",
        "alt_chart": "Gráfico de barras: número de servidores de VPNs populares",
        "q_review": "{brand} análise", "q_comparison": "{brand} comparação",
        "q_howto": "como configurar {brand}", "q_market": "melhor VPN {country}",
        "lbl_score": "Pontuação", "lbl_for": "Ideal para", "lbl_not_for": "Não recomendado para",
        "lbl_pros": "Vantagens", "lbl_cons": "Desvantagens",
        "lbl_steps": "Passo a passo", "lbl_faq": "Perguntas frequentes",
    },
    "it": {
        "disclosure": ("Avviso: questa pagina contiene link di affiliazione. Potremmo ricevere una "
                       "commissione sugli acquisti effettuati tramite i link, senza costi aggiuntivi per te."),
        "cta": "Vai a {brand}", "promo": "Codice promozionale",
        "nav_home": "Home", "nav_review": "Recensione", "nav_comparison": "Confronto",
        "nav_howto": "Guida alla configurazione", "menu": "Menu principale",
        "footer": "Sito di recensioni indipendente",
        "title_review": "{brand}: recensione e test onesto",
        "title_comparison": "{brand} contro la concorrenza", "title_howto": "Come configurare {brand}",
        "review_word": "recensione",
        "b_privacy": "Privacy", "b_speed": "Velocità", "b_access": "Accesso globale",
        "alt_hero": "Illustrazione astratta per la pagina «{title}»",
        "facts_title": "La dimensione della rete in sintesi", "facts_servers": "Server",
        "alt_chart": "Grafico a barre: numero di server dei VPN più diffusi",
        "q_review": "{brand} recensione", "q_comparison": "{brand} confronto",
        "q_howto": "come configurare {brand}", "q_market": "miglior VPN {country}",
        "lbl_score": "Valutazione", "lbl_for": "Ideale per", "lbl_not_for": "Sconsigliato per",
        "lbl_pros": "Vantaggi", "lbl_cons": "Svantaggi",
        "lbl_steps": "Passo dopo passo", "lbl_faq": "Domande frequenti",
    },
}
DEFAULT_LANG = "en"

# Названия стран для запроса «лучший VPN {страна}» — на языке запроса. Неизвестное сочетание -> ISO-код.
COUNTRY_NAMES = {
    "ru": {"RU": "Россия", "KZ": "Казахстан", "BY": "Беларусь", "UA": "Украина", "DE": "Германия",
           "US": "США", "GB": "Великобритания"},
    "en": {"US": "United States", "GB": "UK", "DE": "Germany", "RU": "Russia", "NL": "Netherlands",
           "MX": "Mexico", "ES": "Spain"},
}

# английское название языка — для инструкции LLM («write in German»)
LANG_NAMES = {"en": "English", "ru": "Russian", "de": "German", "es": "Spanish",
              "fr": "French", "nl": "Dutch", "pt": "Portuguese", "it": "Italian"}
# og:locale
OG_LOCALE = {"en": "en_US", "ru": "ru_RU", "de": "de_DE", "es": "es_ES",
             "fr": "fr_FR", "nl": "nl_NL", "pt": "pt_PT", "it": "it_IT"}


def _base(lang: str | None) -> str:
    return (lang or "").strip().lower().replace("_", "-").split("-")[0]


def norm_lang(lang: str | None) -> str:
    """'ru-RU' / 'RU' -> 'ru'; язык без словаря -> DEFAULT_LANG (для РЕНДЕРА)."""
    base = _base(lang)
    return base if base in TEXTS else DEFAULT_LANG


def supported(lang: str | None) -> bool:
    return _base(lang) in TEXTS


def resolve_lang(*candidates: str | None) -> str:
    """Язык генерации: первый НЕПУСТОЙ кандидат (явный выбор > язык рынка домена > язык оффера).
    Ничего не задано -> DEFAULT_LANG; задан, но без словаря (pl, ja…) -> ValueError: сайт для
    польского рынка нельзя молча писать по-английски/по-русски."""
    for c in candidates:
        if c and c.strip():
            if not supported(c):
                raise ValueError(
                    f"язык «{c}» не поддержан шаблоном сайта (есть: {', '.join(sorted(TEXTS))}) — "
                    "добавь запись в services/locales.TEXTS или выбери другой язык")
            return _base(c)
    return DEFAULT_LANG


def t(lang: str | None, key: str, **fmt) -> str:
    s = TEXTS[norm_lang(lang)][key]
    return s.format(**fmt) if fmt else s


def country_name(lang: str | None, code: str | None) -> str:
    """Название страны на языке запроса; неизвестное — ISO-код как есть, пусто — ''."""
    if not code:
        return ""
    return COUNTRY_NAMES.get(norm_lang(lang), {}).get(code.upper(), code.upper())
