# 1C MCP Toolkit

**Система интеграции AI-агентов с базами данных 1С:Предприятие через MCP и REST API.**

Прокси (Python/FastAPI) принимает запросы MCP-клиентов и REST-агентов, передаёт их в 1С по HTTP long polling и возвращает результаты. Есть и альтернативный режим: HTTP-сервер запускается прямо внутри обработки 1С — без Python и Docker.

- ✅ Не требуется изменение конфигурации 1C и публикация 1C-сервера
- ✅ Не используется COM-соединение
- ✅ Совместимость с 1С:Предприятие 8.2.13+ / 8.3.25
- ✅ **Встроенный сервер**: HTTP-сервер прямо в обработке 1С — Python не нужен
- ✅ Токенизация ПДн (анонимизация), изоляция каналов, формат TOON
- ✅ Корпус для дообучения маскирования (см. ниже)
- ✅ Поддержка Docker — полностью самодостаточный стек (прокси + Redis)

Текущая версия: **1.8.0**.

## Режимы работы

**Режим «Прокси»** — Python-сервер (FastAPI + MCP SDK). AI-агент обращается к `/mcp` или `/api/*`; обработка 1С (`build/MCP_Toolkit.epf`) получает команды через long polling (`/1c/poll`, `/1c/result`). Подходит для Docker-деплоя и мультиарендности (изоляция каналов).

**Режим «Встроенный сервер»** — HTTP-сервер запускается прямо в обработке 1С (нативная компонента `MCPHttpTransport`). Python, Docker и отдельный сервер не нужны. Анонимизация настраивается в форме обработки.

Подробное сравнение режимов — [README_FULL.md](./README_FULL.md).

## Быстрый старт

### Вариант 0: Встроенный сервер (без Python, рекомендуется)

1. Откройте `build/MCP_Toolkit.epf` в 1С:Предприятие
2. В форме выберите режим **«Встроенный сервер»**
3. Нажмите «Запустить сервер»
4. Настройте AI-агент на `http://<ip-компьютера-1С>:6003/mcp`

### Вариант 1: Docker Compose (самодостаточный стек)

```bash
# Клонировать репозиторий
git clone <repository-url>
cd 1c-mcp-toolkit

# (опционально) скопировать настройки — все значения имеют дефолты
cp .env.example .env

# Запустить
docker compose up -d
```

Состав стека:

| Сервис | Что делает |
|---|---|
| `onec-mcp-toolkit-proxy` | Прокси на `:6003`: MCP (`/mcp`), REST (`/api/*`), health (`/health`), admin UI корпуса (`/admin/corpus`) |
| `redis` | Очередь корпуса (`corpus:pending`), персистентность AOF |

Данные переживают рестарты: volume `toolkit-corpus` (JSONL корпуса, `/app/corpus`) и `redis-data`. Все переменные окружения и их дефолты — в [.env.example](./.env.example) с комментариями.

> Корпус включён в compose-профиле по умолчанию (`CORPUS_ENABLED=true`). Redis недоступен — записи пропускаются с warning, ответы инструментов не ломаются (fail-open).

> **Примечание о сборке:** в Dockerfile зафиксирован пин `mcp>=1.0,<2` — mcp 2.x переименовал `FastMCP` в `MCPServer` и несовместим с текущим кодом.

### Вариант 2: Docker Hub (режим Прокси)

```bash
docker run -d -p 6003:6003 -e ALLOW_DANGEROUS_WITH_APPROVAL=true --restart unless-stopped --name 1c-mcp-toolkit-proxy roctup/1c-mcp-toolkit-proxy
```

### Вариант 3: Прямой запуск Python

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m onec_mcp_toolkit_proxy
```

Сервер запустится по адресу `http://localhost:6003`. Для корпуса потребуется Redis (`CORPUS_REDIS_URL`); без него записи пропускаются.

## Установка обработки 1C (режим «Прокси»)

1. Скачайте готовую обработку из папки build — [MCP_Toolkit.epf](./build/MCP_Toolkit.epf)
2. Откройте обработку в 1C (Файл → Открыть)
3. Укажите:
   - URL прокси-сервера: `http://localhost:6003`
   - (опционально) ID канала для изоляции команд
4. Нажмите кнопку «Подключиться»

## Настройка AI-агента

### Kiro IDE

Добавьте в файл `.kiro/settings/mcp.json`:

```json
{
  "mcpServers": {
    "onec-mcp-toolkit-proxy": {
      "url": "http://localhost:6003/mcp",
      "transport": "http",
      "type": "streamable-http",
      "disabled": false,
      "autoApprove": ["execute_query", "get_metadata"]
    }
  }
}
```

### Claude Desktop

> `claude_desktop_config.json` поддерживает только stdio-серверы — запись с `url`/`transport` игнорируется. Используйте один из вариантов:

**Мост mcp-remote (нужен Node.js):**

```json
{
  "mcpServers": {
    "onec-mcp-toolkit-proxy": {
      "command": "npx",
      "args": [
        "-y", "mcp-remote",
        "http://127.0.0.1:6003/mcp",
        "--allow-http",
        "--transport", "http-only"
      ]
    }
  }
}
```

Используйте `127.0.0.1`, а не `localhost` (надёжнее с точки зрения IPv4/IPv6), флаг `--allow-http` разрешает HTTP без TLS.

**Custom Connector (без Node.js, платные планы):** Settings → Connectors → Add custom connector, URL `http://localhost:6003/mcp`.

### Аутентификация по токену (встроенный сервер)

По умолчанию встроенный сервер не требует аутентификации. Чтобы включить, задайте на форме поле **«Токен доступа»** (или нажмите «Сгенерировать») и добавьте заголовок `Authorization: Bearer <token>` в конфигурацию клиента. Пустой токен = аутентификация выключена. Подробнее — [README_FULL.md](./README_FULL.md).

## MCP-инструменты

| Инструмент | Описание |
|-----------|----------|
| **execute_query** | Выполнение запросов на языке запросов 1C |
| **execute_code** | Выполнение произвольного кода 1C |
| **get_metadata** | Информация о структуре метаданных базы |
| **get_event_log** | Записи из журнала регистрации |
| **get_object_by_link** | Получение объекта по навигационной ссылке |
| **get_link_of_object** | Генерация навигационной ссылки на объект |
| **find_references_to_object** | Поиск всех ссылок на объект |
| **get_access_rights** | Права доступа к объектам метаданных |
| **get_bsl_syntax_help** | Справочник по встроенному языку BSL |
| **get_screenshot** | Снимок активного окна 1С, base64 PNG (только Windows) |
| **submit_for_deanonymization** | Отправка финального ответа для деанонимизации (только при включённой анонимизации) |
| **restart_1c_session** | Перезапуск текущей сессии 1С |
| **close_1c_session** | Закрытие сессии 1С с командой запуска новой |

## REST API (альтернатива MCP)

Для агентов без MCP-поддержки — те же возможности через HTTP. Базовый URL: `http://localhost:6003/api/`.

| Эндпоинт | Метод | Описание |
|----------|-------|----------|
| `/api/execute_query` | POST | Выполнение запросов 1C |
| `/api/execute_code` | POST | Выполнение кода 1C |
| `/api/get_metadata` | GET/POST | Получение метаданных |
| `/api/get_event_log` | POST | Журнал регистрации |
| `/api/get_object_by_link` | POST | Получить объект по ссылке |
| `/api/get_link_of_object` | POST | Генерация ссылки на объект |
| `/api/find_references_to_object` | POST | Поиск ссылок на объект |
| `/api/get_access_rights` | POST | Права доступа |
| `/api/get_bsl_syntax_help` | POST | Справочник по языку BSL |
| `/api/submit_for_deanonymization` | POST | Текст для деанонимизации (только при включённой анонимизации) |
| `/api/restart_1c_session` | POST | Перезапуск сессии 1С |
| `/api/close_1c_session` | POST | Закрыть сессию и получить команду запуска новой |

**Формат ответов:** успех — `{"success": true, "data": <результат>}`; ошибка — `{"success": false, "error": "Описание ошибки"}`. Исключение: `submit_for_deanonymization` возвращает `{"received": true}`.

Подробные примеры — [README_FULL.md](./README_FULL.md).

## Анонимизация данных

Автоматическое маскирование персональных и конфиденциальных данных в ответах 1С. Реальные значения заменяются стабильными токенами (`[ORG-00001]`, `[PER-00001]`, `[INN-00001]` и т.д.); агент может передавать токены обратно — сервер подставит реальные значения перед исполнением.

**Режим «Встроенный сервер»:** настройка через форму обработки 1С (дерево полей метаданных, словарь из справочников, регулярные выражения).

**Режим «Прокси»:** переменные окружения (`ANONYMIZATION_ENABLED=true`); дополнительно — Natasha NER, изоляция токенов по каналам, умная анонимизация псевдонимов колонок.

Полная документация: **[ANONYMIZATION.md](./ANONYMIZATION.md)**.

## Корпус для дообучения маскирования

Прокси может собирать **raw-ответы инструментов 1С** (до анонимизации, без аргументов запросов) в JSONL-файлы — датасет для дообучения/калибровки моделей маскирования ПДн. Захват выполняется в единственной точке исполнения команды 1С, поэтому в записи попадают и исходный, и (при включённой анонимизации) токенизированный вариант ответа.

**Формат записи JSONL:**

```json
{"ts": 1760000000.0, "tool": "execute_query", "channel": "c1",
 "anonymization_applied": true,
 "raw_result_text": "...",
 "anonymized_result_text": "..."}
```

Файлы создаются по дням: `CORPUS_DIR/corpus-YYYY-MM-DD.jsonl`. Записи сначала попадают в Redis-очередь (`corpus:pending`), затем фоновый воркер прокси раз в `CORPUS_FLUSH_INTERVAL_SEC` выгружает их в JSONL и удаляет файлы старше `CORPUS_RETENTION_DAYS`.

**Включение:**

```bash
cp .env.example .env   # CORPUS_ENABLED=true уже стоит по умолчанию
docker compose up -d   # поднимает прокси + Redis (очередь корпуса)
```

Без Redis корпус работает в fail-open режиме: записи пропускаются с warning в лог, ответы инструментов не ломаются.

**⚠️ Приватность:** raw-ответы содержат персональные данные. Каталог корпуса имеет права 0700 и не публикуется наружу; инструменты `submit_for_deanonymization` и `get_screenshot` (base64-скриншоты) никогда не записываются (`CORPUS_EXCLUDE_TOOLS`); слишком большие записи обрезаются (`truncated: true`).

**Admin UI:** `http://<host>:6003/admin/corpus` — просмотр файлов, счётчик очереди, ручной flush, скачивание/удаление JSONL. Вход по email+паролю из `CORPUS_ADMIN_EMAIL`/`CORPUS_ADMIN_PASSWORD` (обоих оставить пустыми = доступ не настроен, логин отклоняется).

### Переменные корпуса

| Переменная | По умолчанию | Описание |
|---|---|---|
| `CORPUS_ENABLED` | `true` (в compose) | Сборка корпуса включена |
| `CORPUS_REDIS_URL` | `redis://redis:6379/2` | Redis-очередь записей |
| `CORPUS_DIR` | `/app/corpus` | Каталог JSONL-файлов (volume) |
| `CORPUS_FLUSH_INTERVAL_SEC` | `3600` | Период flush Redis → JSONL |
| `CORPUS_MAX_RECORD_BYTES` | `1048576` | Лимит размера записи (обрезка) |
| `CORPUS_REDIS_MAX_RECORDS` | `100000` | Потолок очереди (свыше — пропуск с warning) |
| `CORPUS_RETENTION_DAYS` | `30` | Ретеншн JSONL-файлов, дней |
| `CORPUS_EXCLUDE_TOOLS` | `submit_for_deanonymization,get_screenshot` | Инструменты-исключения |
| `CORPUS_ADMIN_EMAIL` / `CORPUS_ADMIN_PASSWORD` | `admin@toolkit.local` / `change-me-corpus-admin` | Креды admin UI (смените в проде!) |
| `CORPUS_ADMIN_SESSION_TTL` | `43200` | TTL session-cookie, сек |
| `CORPUS_ADMIN_SESSION_SECRET` | пусто | Секрет подписи cookie (пусто — эфемерный ключ) |

## Изоляция каналов

При подключении нескольких клиентов 1C к одному прокси потоки команд изолируются по ID канала. Один и тот же канал указывается в обработке 1С и в URL клиента.

**MCP** — параметр `?channel=<id>`:

```json
{
  "mcpServers": {
    "onec-dev": {
      "url": "http://localhost:6003/mcp?channel=dev-environment",
      "transport": "http",
      "type": "streamable-http"
    },
    "onec-prod": {
      "url": "http://localhost:6003/mcp?channel=prod-environment",
      "transport": "http",
      "type": "streamable-http"
    }
  }
}
```

**REST API** — тот же параметр запроса:

```bash
curl -X POST "http://localhost:6003/api/execute_query?channel=dev-environment" \
  -H "Content-Type: application/json" \
  -d '{"query": "ВЫБРАТЬ 1"}'
```

**Важно:** укажите тот же ID канала в настройках обработки 1C для соответствующего окружения.

## Переменные окружения

Полный список с комментариями — в [.env.example](./.env.example). Основные группы:

| Группа | Ключевые переменные |
|---|---|
| Базовые | `PORT` (6003), `TIMEOUT` (180), `POLL_TIMEOUT` (0), `LOG_LEVEL` (INFO), `DEBUG`, `RESPONSE_FORMAT` (toon/json), `ALLOW_DANGEROUS_WITH_APPROVAL` |
| Анонимизация | `ANONYMIZATION_ENABLED`, `ANONYMIZATION_RADICAL_MODE`, `ANONYMIZATION_NER_ENABLED`, `ANONYMIZATION_DICTIONARY_*`, `ANONYMIZATION_TOKENMAP_MAX` |
| Корпус | `CORPUS_ENABLED`, `CORPUS_REDIS_URL`, `CORPUS_DIR`, `CORPUS_FLUSH_INTERVAL_SEC`, `CORPUS_RETENTION_DAYS`, `CORPUS_ADMIN_*` |

Детальные таблицы — в [README_FULL.md](./README_FULL.md) (базовые) и [ANONYMIZATION.md](./ANONYMIZATION.md) (анонимизация).

## Безопасность

- Опасные операции `execute_code` блокируются чёрным списком ключевых слов (`DANGEROUS_KEYWORDS`)
- `ALLOW_DANGEROUS_WITH_APPROVAL=true` включает режим подтверждения операций пользователем в 1С
- `autoApprove` у агента настраивайте только для безопасных инструментов
- Каталог корпуса — права 0700, наружу не публикуется; порт 6003 при деплое за платформой не выставляется в интернет
- Health-check: `GET /health`

## Структура репозитория

```
1c-mcp-toolkit/
├── onec_mcp_toolkit_proxy/   # Python-прокси (FastAPI): MCP, REST, анонимизация,
│   │                         #   корпус, admin UI
│   └── anonymizer/           # Модули токенизации: словарь, regex, NER, token map
├── 1c/                       # Исходники обработки 1С (MCPToolkit)
├── build/                    # Готовые сборки: MCP_Toolkit.epf (+ x86)
├── native_components/        # Нативные компоненты (MCPHttpTransport и др.)
├── docker-compose.yml        # Самодостаточный стек: прокси + Redis
├── .env.example              # Все переменные окружения с комментариями
├── Dockerfile                # Образ прокси (пин mcp>=1.0,<2)
├── README.md                 # Этот документ
├── README_FULL.md            # Полная документация
└── ANONYMIZATION.md          # Документация анонимизации
```

## Документация

- **[README_FULL.md](./README_FULL.md)** — полная документация: режимы, каналы, все переменные, инструменты, REST API, обработка ошибок
- **[ANONYMIZATION.md](./ANONYMIZATION.md)** — анонимизация: режимы, словарь, NER, тонкая настройка
- **[.env.example](./.env.example)** — все переменные окружения с комментариями
