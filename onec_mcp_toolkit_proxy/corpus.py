"""Corpus: ответы инструментов 1С для дообучения маскирования.

Корпус целиком живёт в toolkit: захват выполняется в
mcp_handler._execute_1c_command — единственной точке, где доступны и raw
(до анонимизации), и токенизированный результат.

Формат записи JSONL:
  {"ts": ..., "tool": ..., "channel": ..., "anonymization_applied": bool,
   "raw_result_text": "...",            # всегда
   "anonymized_result_text": "..."}     # только при включённой анонимизации

Пайплайн:
  capture → RPUSH corpus:pending (Redis)
  → flush worker раз в CORPUS_FLUSH_INTERVAL_SEC: LRANGE батчем → append в
   CORPUS_DIR/corpus-YYYY-MM-DD.jsonl (день по ts записи, UTC) → LTRIM
  → ретеншн файлов старше CORPUS_RETENTION_DAYS.

Приватность: только ответы (без аргументов); исключения —
`CORPUS_EXCLUDE_TOOLS` (по умолчанию `submit_for_deanonymization`
и `get_screenshot`). Каталог 0700, не публикуется наружу.
Fail-open: любые ошибки Redis/диска не ломают ответы (запись пропускается).
Возможен дубликат записи при падении между append и LTRIM — допустимо.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path

from .config import settings

logger = logging.getLogger("corpus")

PENDING_KEY = "corpus:pending"
_FLUSH_BATCH = 1000
_MAX_FLUSH_LOOPS = 100  # ≤ _FLUSH_BATCH * _MAX_FLUSH_LOOPS записей за один цикл

_client = None


def get_redis():
    """Lazy Redis-клиент (точка подмены в тестах через fakeredis)."""
    global _client
    if _client is None:
        import redis.asyncio as aioredis

        _client = aioredis.from_url(settings.corpus_redis_url, decode_responses=True)
    return _client


def set_redis(client) -> None:
    """Подмена клиента (тесты)."""
    global _client
    _client = client


def is_tool_recordable(tool: str) -> bool:
    """Включён ли корпус и не попадает ли инструмент в исключения."""
    if not settings.corpus_enabled:
        return False
    return tool not in settings.corpus_exclude_tools_set


def extract_result_text(result) -> str:
    """Текст ответа MCP: конкатенация result.content[*].text, иначе JSON."""
    if not isinstance(result, dict):
        return ""
    content = result.get("content")
    if isinstance(content, list):
        texts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                texts.append(str(item.get("text", "")))
        if texts:
            return "\n".join(texts)
    try:
        return json.dumps(result, ensure_ascii=False)
    except (TypeError, ValueError):
        return ""


def build_record(
    *,
    tool: str,
    channel: str,
    raw_result,
    anonymized_result=None,
) -> dict:
    """Запись корпуса: raw всегда, токенизированный дубликат при анонимизации."""
    raw_text = extract_result_text(raw_result)
    anonymization_applied = anonymized_result is not None
    record = {
        "ts": time.time(),
        "tool": tool,
        "channel": channel,
        "anonymization_applied": anonymization_applied,
        "raw_result_text": raw_text,
    }
    if anonymization_applied:
        record["anonymized_result_text"] = extract_result_text(anonymized_result)
    encoded = json.dumps(record, ensure_ascii=False)
    if len(encoded.encode("utf-8")) > settings.corpus_max_record_bytes:
        # Обрезаем текст до лимита (≈3 байта на символ в UTF-8 — с запасом).
        limit = max(settings.corpus_max_record_bytes // 3, 100)
        record["raw_result_text"] = raw_text[:limit]
        if anonymization_applied:
            record["anonymized_result_text"] = (
                record["anonymized_result_text"][:limit]
            )
        record["truncated"] = True
    return record


async def add_record(record: dict) -> bool:
    """RPUSH записи в Redis-очередь. False — запись пропущена (fail-open)."""
    if not record:
        return False
    try:
        client = get_redis()
        llen = await client.llen(PENDING_KEY)
        if llen >= settings.corpus_redis_max_records:
            logger.warning("corpus queue full (%d), record dropped", llen)
            return False
        await client.rpush(PENDING_KEY, json.dumps(record, ensure_ascii=False))
        return True
    except Exception:  # корпус никогда не ломает ответы
        logger.warning("corpus add failed, record dropped")
        return False


def _corpus_dir() -> Path:
    path = Path(settings.corpus_dir)
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:  # pragma: no cover
        pass
    return path


def _file_for(ts: float) -> Path:
    day = datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d")
    return _corpus_dir() / f"corpus-{day}.jsonl"


async def flush_once() -> int:
    """Выгрузить накопленное из Redis в JSONL. Возвращает число записей."""
    client = get_redis()
    written = 0
    for _ in range(_MAX_FLUSH_LOOPS):
        batch = await client.lrange(PENDING_KEY, 0, _FLUSH_BATCH - 1)
        if not batch:
            break
        by_file: dict[Path, list[str]] = {}
        for raw in batch:
            try:
                record = json.loads(raw)
                ts = float(record.get("ts") or time.time())
            except (ValueError, TypeError):
                ts = time.time()
                record = {"ts": ts, "tool": "unknown", "raw": str(raw)[:2000]}
            by_file.setdefault(_file_for(ts), []).append(raw)
        # Файловый I/O (до ~100k строк батча) — в потоковом пуле, чтобы
        # не блокировать event loop toolkit'а.
        await asyncio.to_thread(_append_files, by_file)
        written += len(batch)
        # Очистить обработанный диапазон (возможен дубликат при падении —
        # допустимо, см. docstring).
        await client.ltrim(PENDING_KEY, len(batch), -1)
    return written


def _append_files(by_file: dict[Path, list[str]]) -> None:
    for path, lines in by_file.items():
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")


async def retention_cleanup() -> int:
    """Удалить файлы старше CORPUS_RETENTION_DAYS. Возвращает число удалённых."""
    cutoff = time.time() - settings.corpus_retention_days * 86400
    removed = 0
    try:
        def _cleanup() -> int:
            count = 0
            for path in _corpus_dir().glob("corpus-*.jsonl"):
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    count += 1
            return count

        removed = await asyncio.to_thread(_cleanup)
    except OSError as exc:
        logger.warning("corpus retention failed: %s", exc)
    return removed


async def delete_file(filename: str) -> bool:
    """Удалить JSONL-файл корпуса (имя строго corpus-YYYY-MM-DD.jsonl)."""
    import re

    if not re.fullmatch(r"corpus-\d{4}-\d{2}-\d{2}\.jsonl", filename):
        return False
    path = _corpus_dir() / filename
    try:
        path.unlink()
        return True
    except OSError as exc:
        logger.warning("corpus delete failed: %s", exc)
        return False


async def flush_worker_forever() -> None:
    """Фоновый воркер toolkit (lifespan): flush + ретеншн раз в интервал."""
    logger.info(
        "corpus worker started (interval=%ss, dir=%s)",
        settings.corpus_flush_interval_sec, settings.corpus_dir,
    )
    while True:
        try:
            count = await flush_once()
            if count:
                logger.info("corpus flushed %d records", count)
            await retention_cleanup()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - fail-open
            logger.warning("corpus flush failed: %s", exc)
        await asyncio.sleep(settings.corpus_flush_interval_sec)
