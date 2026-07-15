---
description: Прогнать оффлайн-тесты + pyflakes и коротко отчитаться (живые интеграции — через /diag на боксе)
allowed-tools: Bash
---
Прогони и коротко отчитайся (пройдено/упало), ничего не чиня:
1. `.venv/bin/python -m pytest backend/tests/ -q`
2. `.venv/bin/python -m pyflakes backend/app backend/tests`
Отчёт: число тестов pass/fail, вывод pyflakes. Живые интеграции (Cloudflare/aaPanel/SearXNG/LLM/
Wayback/РКН/A-Parser/Spamhaus/БД) — это `/diag` в самой панели, не отдельный скрипт.
