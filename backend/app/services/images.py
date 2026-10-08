"""Графика сайта: ШОВ ImageProvider + единственный провайдер «svg-local».

Решение «чем генерировать картинки» (внешние API, стоимость, лицензии) принимает ОПЕРАТОР — здесь
оно не принято: есть интерфейс и локальный детерминированный провайдер без зависимостей и сети.
Новый провайдер = класс с теми же методами + запись в PROVIDERS; сборка сайта (site_builder)
знает только `get_provider(name)`.

svg-local: всё — чистый SVG-текст по сиду (домен) и палитре сайта, одинаковый вход -> побайтово
одинаковый выход (идемпотентный деплой). Ограничение: og:image — SVG; соцсети (Facebook/Telegram/X)
SVG в превью не рисуют, им нужен PNG/JPG. Растеризация в чистом Python без зависимостей не делается
сознательно (Pillow/cairosvg — решение оператора), шов — метод `og`.
"""
import hashlib
import random
from typing import Protocol
from xml.sax.saxutils import escape

_NS = 'xmlns="http://www.w3.org/2000/svg"'


def _rng(seed: str, salt: str = "") -> random.Random:
    return random.Random(int(hashlib.sha256(f"{seed}|{salt}".encode()).hexdigest()[:12], 16))


class ImageProvider(Protocol):
    name: str

    def hero(self, seed: str, pal: dict) -> str: ...
    def og(self, seed: str, pal: dict, title: str) -> str: ...
    def favicon(self, seed: str, pal: dict, letter: str) -> str: ...
    def icon(self, kind: str, pal: dict) -> str: ...
    def bar_chart(self, title: str, items: list[tuple[str, int]], pal: dict) -> str: ...


def _art(seed: str, pal: dict, w: int, h: int, salt: str) -> str:
    """Абстрактная геометрия в палитре сайта: градиентный фон, круги и волна."""
    r = _rng(seed, salt)
    out = [f'<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
           f'<stop offset="0" stop-color="{pal["accent"]}"/><stop offset="1" stop-color="{pal["accent2"]}"/>'
           f'</linearGradient></defs><rect width="{w}" height="{h}" fill="url(#g)"/>']
    for _ in range(r.randint(5, 8)):
        out.append(f'<circle cx="{r.randint(0, w)}" cy="{r.randint(0, h)}" r="{r.randint(h // 12, h // 3)}" '
                   f'fill="{pal["bg"]}" fill-opacity="{r.choice((0.08, 0.12, 0.18, 0.25))}"/>')
    y = r.randint(h // 2, h * 3 // 4)
    a = r.randint(h // 8, h // 4)
    out.append(f'<path d="M0 {y} Q{w // 4} {y - a} {w // 2} {y} T{w} {y} V{h} H0Z" '
               f'fill="{pal["bg"]}" fill-opacity="0.22"/>')
    for _ in range(r.randint(2, 4)):
        x, s = r.randint(0, w - 80), r.randint(40, h // 3)
        yy = r.randint(0, h - s)
        out.append(f'<rect x="{x}" y="{yy}" width="{s}" height="{s}" rx="{s // 6}" fill="none" '
                   f'stroke="{pal["bg"]}" stroke-opacity="0.35" stroke-width="3"/>')
    return "".join(out)


_ICONS = {
    # 24x24 контуры: щит / молния / глобус
    "privacy": '<path d="M12 2 4 5v6c0 5 3.4 9.4 8 11 4.6-1.6 8-6 8-11V5z"/><path d="m9 12 2 2 4-4"/>',
    "speed": '<path d="M13 2 4 14h7l-1 8 9-12h-7z"/>',
    "access": '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c3 3 3 15 0 18M12 3c-3 3-3 15 0 18"/>',
}


class SvgLocalProvider:
    name = "svg-local"

    def hero(self, seed, pal):
        return (f'<svg {_NS} viewBox="0 0 1200 360" width="1200" height="360" role="img">'
                f'{_art(seed, pal, 1200, 360, "hero")}</svg>')

    def og(self, seed, pal, title):
        t = escape((title or "")[:70])
        return (f'<svg {_NS} viewBox="0 0 1200 630" width="1200" height="630">'
                f'{_art(seed, pal, 1200, 630, "og")}'
                f'<rect x="60" y="400" width="1080" height="170" rx="24" fill="{pal["bg"]}" fill-opacity="0.92"/>'
                f'<text x="100" y="500" font-family="Arial,Helvetica,sans-serif" font-size="46" '
                f'font-weight="700" fill="{pal["fg"]}">{t}</text></svg>')

    def favicon(self, seed, pal, letter):
        ch = escape((letter or "?")[:1].upper())
        return (f'<svg {_NS} viewBox="0 0 64 64" width="64" height="64">'
                f'<rect width="64" height="64" rx="14" fill="{pal["accent"]}"/>'
                f'<text x="32" y="44" text-anchor="middle" font-family="Arial,Helvetica,sans-serif" '
                f'font-size="36" font-weight="700" fill="{pal["accent_fg"]}">{ch}</text></svg>')

    def icon(self, kind, pal):
        d = _ICONS.get(kind, _ICONS["access"])
        return (f'<svg {_NS} viewBox="0 0 24 24" width="48" height="48" fill="none" stroke="{pal["accent"]}" '
                f'stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">{d}</svg>')

    def bar_chart(self, title, items, pal):
        """Горизонтальные столбцы: items = [(подпись, число)]."""
        top = max((v for _, v in items), default=1) or 1
        row, left, width = 34, 150, 520
        h = 56 + row * len(items)
        out = [f'<svg {_NS} viewBox="0 0 760 {h}" width="760" height="{h}" role="img" '
               f'font-family="Arial,Helvetica,sans-serif" font-size="14">',
               f'<rect width="760" height="{h}" rx="10" fill="{pal["surface"]}"/>',
               f'<text x="20" y="30" font-size="16" font-weight="700" fill="{pal["fg"]}">{escape(title)}</text>']
        for i, (label, v) in enumerate(items):
            y = 50 + i * row
            w = max(4, round(width * v / top))
            out.append(f'<text x="{left - 10}" y="{y + 17}" text-anchor="end" fill="{pal["fg"]}">{escape(label)}</text>'
                       f'<rect x="{left}" y="{y}" width="{w}" height="22" rx="4" fill="{pal["accent"]}"/>'
                       f'<text x="{left + w + 8}" y="{y + 17}" fill="{pal["muted"]}">{v}</text>')
        out.append("</svg>")
        return "".join(out)


PROVIDERS: dict[str, type] = {"svg-local": SvgLocalProvider}


def get_provider(name: str = "svg-local") -> ImageProvider:
    try:
        return PROVIDERS[name]()
    except KeyError:
        raise ValueError(f"провайдер графики «{name}» не зарегистрирован "
                         f"(есть: {', '.join(PROVIDERS)})") from None
