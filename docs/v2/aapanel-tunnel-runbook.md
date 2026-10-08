# aaPanel через SSH-туннель (без whitelist по публичному IP)

Зачем: whitelist API aaPanel сверяет публичный IP звонящего точным совпадением, а IP бокса
меняется. Туннель заставляет панель видеть запросы с `127.0.0.1` самого VPS: в whitelist
достаточно одной постоянной записи.

Схема: контейнер `aapanel-tunnel` (сеть combine) держит `ssh -L` на VPS; backend/worker ходят на
`https://aapanel-tunnel:18839`. Порт наружу не публикуется. Сайдкар включается профилем `tunnel`
и от него ничего не зависит: прямой режим продолжает работать без изменений.

Все команды ниже — PowerShell на боксе, из каталога `D:\combine_machine`.

## 1. SSH-ключ для туннеля

Отдельный ключ без пароля (контейнер стартует без человека), только для туннеля.

```
New-Item -ItemType Directory -Force secrets\tunnel
ssh-keygen -t ed25519 -N '""' -C aapanel-tunnel -f secrets\tunnel\id_ed25519
```

Каталог `secrets\` в `.gitignore`. Приватный ключ не отправляй никуда.

## 2. Пользователь и ключ на VPS

На VPS (под root) создай отдельного пользователя без shell и добавь туда публичный ключ,
с ограничением: только проброс на 127.0.0.1:18839.

```
useradd -m -s /usr/sbin/nologin tunnel
mkdir -p /home/tunnel/.ssh
```

Строка в `/home/tunnel/.ssh/authorized_keys` (содержимое `secrets\tunnel\id_ed25519.pub` вставь
вместо KEY):

```
restrict,port-forwarding,permitopen="127.0.0.1:18839" ssh-ed25519 KEY aapanel-tunnel
```

Права: `chown -R tunnel:tunnel /home/tunnel/.ssh`, затем `chmod 700 /home/tunnel/.ssh` и
`chmod 600 /home/tunnel/.ssh/authorized_keys`. Публичный ключ покажет
`Get-Content secrets\tunnel\id_ed25519.pub`.

Если у пользователя `nologin` и вход не проходит, проверь, что в `sshd_config` не стоит
`AllowUsers` без `tunnel`.

## 3. known_hosts (защита от подмены VPS)

Контейнер использует `StrictHostKeyChecking=yes`: незнакомый хост = отказ.

```
ssh-keyscan -t ed25519 185.201.252.187 | Out-File -Encoding ascii secrets\tunnel\known_hosts
```

Сверь отпечаток с тем, что показывает сам VPS (`ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`
на VPS) и с `ssh-keygen -lf secrets\tunnel\known_hosts` на боксе. Не совпало — стоп, не запускай.

## 4. Whitelist в aaPanel

aaPanel: Settings, API, в IP whitelist добавь `127.0.0.1` (старый публичный IP бокса можно
оставить на время перехода, потом убрать). `api_sk` не меняется.

## 5. Сертификат панели

Серт самоподписанный (CN=`*.aapanel.com`), проверка идёт по пину файла, а не по имени.
Скопируй с VPS `/www/server/panel/ssl/certificate.pem` на бокс в `backend\aapanel.pem`
(через `scp` или вставкой). Файл уже gitignored (`*.pem`), `git pull` его не трогает. `backend\`
монтируется как `/app` и в backend, и в worker (провижн идёт из worker), поэтому в `.env`:

```
AAPANEL_CA_BUNDLE=/app/aapanel.pem
```

## 6. Настройки

В `.env` на боксе (или на экране «Ключи и сервисы»):

```
TUNNEL_VPS_HOST=185.201.252.187
TUNNEL_VPS_USER=tunnel
TUNNEL_VPS_SSH_PORT=22
TUNNEL_PORT=18839
AAPANEL_TUNNEL=1
AAPANEL_URL=https://aapanel-tunnel:18839
AAPANEL_CA_BUNDLE=/app/aapanel.pem
```

Порт `TUNNEL_PORT` должен совпадать с портом панели на VPS. `AAPANEL_TUNNEL=1` без
`AAPANEL_CA_BUNDLE` клиент отвергнет (без пина TLS не проверяется), как и `AAPANEL_URL` с
публичным адресом (туннель был бы обойдён).

## 7. Запуск и проверка

```
docker compose --profile tunnel up -d --build aapanel-tunnel
docker compose --profile tunnel ps
docker compose logs aapanel-tunnel
```

Статус `healthy` значит, что SSH-авторизация прошла и порт проброшен (слушатель появляется только
после неё). Затем открой `/diag`: строка aaPanel должна быть зелёной. Если нет:

- в логах «нет ключа» или «нет known_hosts» — контейнер вышел с кодом 78, повтори шаги 1 и 3;
- `Permission denied (publickey)` — ключ не попал в `authorized_keys` или не те права (шаг 2);
- `Host key verification failed` — `known_hosts` не от этого VPS (шаг 3);
- `/diag`: «SSH-туннель не отвечает» — сайдкар не запущен или не healthy;
- `/diag`: «IP validation failed» — в whitelist панели нет `127.0.0.1` (шаг 4). Повторные отказы
  копят счётчик бана (20 подряд = час), клиент сам ставит паузу.

## Откат на прямой режим

`AAPANEL_TUNNEL=` (пусто), `AAPANEL_URL=https://IP-VPS:18839`, в whitelist панели снова публичный
IP бокса. Сайдкар остановить: `docker compose --profile tunnel stop aapanel-tunnel`.
