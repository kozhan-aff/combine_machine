# Сайдкар SSH-туннеля к aaPanel (профиль compose «tunnel»). Нужен, чтобы панель видела запросы
# с 127.0.0.1 VPS и whitelist по публичному IP бокса не требовался. См. docs/v2/aapanel-tunnel-runbook.md
FROM alpine:3.20
RUN apk add --no-cache openssh-client autossh
COPY entrypoint.sh /entrypoint.sh
# CRLF из Windows-чекаута ломает shebang — срезаем при сборке
RUN sed -i 's/\r$//' /entrypoint.sh && chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
