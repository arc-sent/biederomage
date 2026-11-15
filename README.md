# biederomage

## Публикация в VK

- `VK_MIN_POST_INTERVAL_SECONDS=90` задаёт паузу после успешного поста до следующей попытки публикации с того же аккаунта. При положительном значении публикации аккаунта выполняются последовательно. При нуле применяется `VK_PUBLISH_CONCURRENCY`.
- `VK_FLOOD_RETRIES=3` задаёт число попыток при коде VK 9, включая первую. `VK_FLOOD_RETRY_DELAY=120` задаёт паузу всего аккаунта в секундах после такого ответа. Для других повторяемых ошибок действуют `VK_PUBLISH_RETRIES` и `VK_RETRY_BASE_DELAY`.
- Недоступный ролик исключается из планирования на `UNAVAILABLE_RETRY_DAYS=7` дней. Следующий кандидат того же правила ставится через `REPLACEMENT_DELAY_SECONDS=60` секунд, если в квоте на этот день есть место. `MAX_REPLACEMENTS_IN_ROW=2` ограничивает последовательные замены до следующего успешного поста правила.

## Прокси для скачивания TikTok через VLESS

В `.env` в корне проекта задайте ключ или HTTPS-адрес подписки.

```dotenv
VLESS_URL=vless://…
COMPOSE_PROFILES=vless
DOWNLOAD_PROXY=socks5h://xray:1080
```

Соберите конфиг Xray на хосте и запустите Compose.

```bash
python3 vless_config.py
docker compose up -d --build
```

После смены ключа или подписки снова выполните `python3 vless_config.py` и `docker compose restart xray`. Для подписки `VLESS_PICK` и `VLESS_SKIP` принимают части названий серверов через запятую. Если серверов несколько, Xray проверяет их через observatory и выбирает рабочий сервер с меньшей задержкой (leastPing). До первых результатов используется `proxy-0`.

TikTok сначала скачивается напрямую. При ошибке скачивание повторяется через `DOWNLOAD_PROXY`. VLESS обслуживает только скачивание TikTok. SOCKS5 доступен внутри Docker-сети, а файл `xray/config.json` с ключом исключён из Git.
