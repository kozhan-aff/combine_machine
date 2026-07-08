---
description: Прогнать оффлайн-тесты + pyflakes + smoke-скрипт и коротко отчитаться
allowed-tools: Bash
---
Прогони и коротко отчитайся (пройдено/упало), ничего не чиня:
1. `.venv/bin/python -m pytest backend/tests/ -q`
2. `.venv/bin/python -m pyflakes backend/app backend/tests`
3. `.venv/bin/python backend/scripts/smoke.py` (если файл есть)
Отчёт: число тестов pass/fail, вывод pyflakes (если есть), результат smoke.
