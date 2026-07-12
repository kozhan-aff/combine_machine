# Макеты M1 из Claude Design

Сюда кладём PNG из claude.ai/design. Имена файлов — по состоянию, а не по номеру:

- `station-idle.png` / `station-running.png` / `station-failed.png` — станция M1 в трёх состояниях
- `inbox-row.png` / `inbox-blind.png` / `inbox-urgent.png` — строка инбокса: обычная / «оценён вслепую» / срочный дроп
- `inbox-bulk.png` — пакетное одобрение (порог + счётчик «попадёт N»)
- `dash-busy.png` / `dash-idle.png` — Пульт, блок «Машина сейчас»: несколько задач / простой
- `funnel-reasons.png` — воронка с раскрытым разбором причин отказа
- `header-bar.png` — тонкая полоса прогресса в шапке

Токены держим свои: светлый нейтральный фон, один холодный синий акцент `#2563c9`,
IBM Plex Sans (UI) / JetBrains Mono (числа, домены). Тёмное и тёплое/оранжевое — отвергнуты.

Источник истины по дизайн-системе — `docs/DESIGN.md`. Спека, которую эти макеты питают —
`docs/superpowers/specs/2026-07-12-panel-m1-observability-design.md`.
