# Octa Oracle

Telegram Mini App с простым LLM-чатом. Клиент — React SPA, backend — FastAPI,
история хранится в PostgreSQL, а `DeepSeek-R1-0528-Qwen3-8B` обслуживается vLLM.

## Быстрый локальный запуск без GPU

Требуются Docker Desktop и Docker Compose.

```powershell
Copy-Item .env.example .env
docker compose up --build
```

Откройте [http://localhost:8088](http://localhost:8088). В этом режиме включены
безопасная только для development dev-auth и потоковый mock LLM. PostgreSQL,
миграции, API и Nginx SPA запускаются в контейнерах.

Полезные адреса:

- приложение: `http://localhost:8088`;
- OpenAPI в development: `http://localhost:8001/api/docs`;
- readiness: `http://localhost:8001/health/ready`.

## Запуск настоящей модели на Windows

Нужны Docker Desktop с WSL2 backend, актуальный NVIDIA driver и GPU с достаточным
объёмом VRAM. Сначала проверьте GPU passthrough:

```powershell
wsl --update
docker run --rm --gpus all nvidia/cuda:12.9.0-base-ubuntu22.04 nvidia-smi
```

В `.env` измените:

```dotenv
LLM_MODE=vllm
VLLM_API_KEY=replace-with-local-random-value
```

Затем запустите GPU profile:

```powershell
docker compose --profile gpu up --build
```

Первый запуск скачивает веса модели и может занять продолжительное время.
Состояние видно через `docker compose logs -f vllm`.

Если vLLM работает на другом GPU-сервере, профиль `gpu` локально не включайте,
а задайте `VLLM_BASE_URL` и `VLLM_API_KEY` этого сервера. Source модели для
локального контейнера настраивается переменной `VLLM_MODEL_SOURCE`; по умолчанию
используется исходный BF16-репозиторий, указанный в архитектуре.

### NVIDIA GTX 1070 и другие Pascal GPU

Для 8 ГБ VRAM используйте отдельный override: он запускает CUDA 12 сборку
`llama.cpp` с GGUF Q4_K_M (около 5 ГБ), контекстом 4096 и одним ответом до 1024
токенов. CUDA 13 для Pascal не подходит.

```powershell
docker compose -f compose.yaml -f compose.legacy-gpu.yaml up -d --build --wait --wait-timeout 1800
docker compose -f compose.yaml -f compose.legacy-gpu.yaml logs -f llama-cpp
```

Первая команда завершится только после готовности модели; первый запуск скачает
её в отдельный persistent volume. При нехватке VRAM
уменьшите `LEGACY_LLM_CONTEXT_SIZE`, а затем при необходимости выберите
`LEGACY_LLM_MODEL_SOURCE=bartowski/deepseek-ai_DeepSeek-R1-0528-Qwen3-8B-GGUF:Q3_K_M`.

## Подключение Telegram

Для проверки внутри Telegram нужен публичный HTTPS URL. Укажите его и секреты в
`.env`:

```dotenv
PUBLIC_BASE_URL=https://your-domain.example
MINI_APP_URL=https://your-domain.example
CORS_ORIGINS=https://your-domain.example
DEV_AUTH=false
TELEGRAM_BOT_TOKEN=replace-me
TELEGRAM_WEBHOOK_SECRET=replace-with-random-secret
```

Настройте webhook и Menu Button одной командой после запуска API:

```powershell
docker compose exec api python -m app.telegram_setup
```

Токен бота используется только backend-контейнером и не попадает в frontend.

## Production override

`compose.prod.yaml` отключает dev-auth, закрывает прямые порты API/vLLM и
оставляет web gateway доступным для внешнего TLS reverse proxy:

```powershell
docker compose -f compose.yaml -f compose.prod.yaml --profile gpu up -d --build
```

Перед production зафиксируйте Docker images по версиям/digest, замените все
локальные пароли, настройте HTTPS, backup PostgreSQL и внешний secret storage.

## Разработка без Docker

Backend:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".\apps\api[dev]"
$env:DEV_AUTH="true"
$env:DB_AUTO_CREATE="true"
uvicorn app.main:app --app-dir apps/api --reload --port 8001
```

Frontend:

```powershell
Set-Location apps/web
npm install
npm run dev
```

## Проверки

```powershell
.\.venv\Scripts\python -m pytest apps/api/tests
.\.venv\Scripts\ruff check apps/api/app apps/api/tests migrations
Set-Location apps/web
npm run typecheck
npm run build
```

Архитектурные решения подробно описаны в [docs/architecture.md](docs/architecture.md).
