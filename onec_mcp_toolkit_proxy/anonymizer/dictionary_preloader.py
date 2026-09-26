"""
Preloader: fetches names from 1C catalogs and builds DictionaryMatcher per channel.

Design (non-blocking, cache-backed):

- ``get_matcher`` NEVER blocks the calling request. If the matcher is not
  ready yet it schedules a background load task and returns ``None`` —
  the response is anonymized without the dictionary until it is built.
- Expensive 1C catalog fetches are cached on disk per channel. On startup
  the matcher is rebuilt from the cache in seconds without touching 1C
  (stale-while-revalidate: a stale cache is served immediately and
  refreshed in the background).
- The cached version is replaced ONLY after a full successful fetch of
  ALL sources. If any source fails during revalidation, the previous
  cache and matcher stay untouched and the fetch is retried after a
  backoff delay.
"""
import asyncio
import hashlib
import json
import logging
import os
import re as _re
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..command_queue import channel_command_queue
from ..config import settings
from .dictionary_matcher import DictionaryMatcher
from .anonymization_defaults import DEFAULT_DICTIONARY_SOURCES

logger = logging.getLogger(__name__)

_CACHE_VERSION = 1
_RETRY_DELAY_SEC = 60.0


class DictionaryPreloader:
    """Manages per-channel DictionaryMatcher loading (non-blocking + disk cache)."""

    def __init__(self) -> None:
        self._matchers: Dict[str, DictionaryMatcher] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()
        self._tasks: Dict[str, asyncio.Task] = {}
        self._reval_tasks: Dict[str, asyncio.Task] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_matcher(self, channel: str) -> Optional[DictionaryMatcher]:
        """Get matcher for channel without ever blocking the caller.

        Returns the matcher when ready; otherwise schedules a background
        load/revalidation and returns ``None`` (anonymization proceeds
        without the dictionary for this request).
        Returns None if dictionary feature is disabled.
        """
        if not settings.anonymization_dictionary_enabled:
            return None

        matcher = self._matchers.get(channel)
        if matcher is not None:
            return matcher

        self._schedule_load(channel)
        return None

    def invalidate(self, channel: Optional[str] = None) -> None:
        """Drop in-memory matcher, cancel background tasks and delete cache file(s)."""
        channels = (
            list(self._matchers.keys()) + [
                ch for ch in self._tasks.keys() if ch not in self._matchers
            ]
            if channel is None else [channel]
        )
        for ch in channels:
            self._matchers.pop(ch, None)
            for tasks in (self._tasks, self._reval_tasks):
                task = tasks.pop(ch, None)
                if task is not None and not task.done():
                    task.cancel()
            if settings.anonymization_dictionary_cache_enabled:
                try:
                    self._cache_path(ch).unlink(missing_ok=True)
                except OSError as e:
                    logger.warning(f"Dictionary cache remove failed for '{ch}': {e}")

    def is_loaded(self, channel: str) -> bool:
        """True if the matcher for channel is ready."""
        return channel in self._matchers

    # ------------------------------------------------------------------
    # Background loading
    # ------------------------------------------------------------------

    def _schedule_load(self, channel: str) -> None:
        existing = self._tasks.get(channel)
        if existing is not None and not existing.done():
            return  # load/revalidation already in progress
        self._tasks[channel] = asyncio.create_task(self._load(channel))

    async def _get_channel_lock(self, channel: str) -> asyncio.Lock:
        async with self._global_lock:
            if channel not in self._locks:
                self._locks[channel] = asyncio.Lock()
            return self._locks[channel]

    async def _load(self, channel: str) -> None:
        """Initial load for a channel: cache-first, fetch on miss."""
        try:
            lock = await self._get_channel_lock(channel)
            async with lock:
                if channel in self._matchers:
                    return  # loaded by a concurrent path

                started = time.monotonic()
                cached = await asyncio.to_thread(self._read_cache, channel)
                if cached is not None:
                    terms, age = cached
                    # Сборка Aho-Corasick на 50k термах — CPU: off-loop.
                    matcher = await asyncio.to_thread(
                        self._build_matcher, channel, terms
                    )
                    self._matchers[channel] = matcher
                    logger.info(
                        f"Dictionary for channel '{channel}' loaded from cache: "
                        f"{matcher.term_count} terms, cache age {age:.0f}s, "
                        f"built in {time.monotonic() - started:.2f}s"
                    )
                    if self._cache_is_stale(age):
                        self._schedule_revalidate(channel, 0.0)
                    return

                # No usable cache — fetch from 1C. Lenient mode: cached if at
                # least one source responded (a catalog may be absent in a
                # given configuration). Replaced later by strict revalidation.
                await self._fetch_store_loop(channel, strict=False)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception(f"Dictionary load task failed for channel '{channel}': {e}")

    def _schedule_revalidate(self, channel: str, delay: float) -> None:
        existing = self._reval_tasks.get(channel)
        if existing is not None and not existing.done():
            return
        self._reval_tasks[channel] = asyncio.create_task(
            self._revalidate_loop(channel, delay)
        )

    async def _revalidate_loop(self, channel: str, first_delay: float) -> None:
        """Periodically refresh the dictionary. Strict: replaces the cached
        version and the live matcher ONLY after ALL sources fetched OK."""
        try:
            if first_delay > 0:
                await asyncio.sleep(first_delay)
            lock = await self._get_channel_lock(channel)
            while True:
                async with lock:
                    try:
                        started = time.monotonic()
                        terms, all_ok = await self._fetch_terms(channel)
                        if not all_ok:
                            raise RuntimeError(
                                "some dictionary sources failed; keeping previous version"
                            )
                        if not terms:
                            raise RuntimeError("no dictionary terms fetched")
                        # Heavy: fetch уже off-loop-безопасен (await), build —
                        # CPU на 50k термах, запись кэша — файловый I/O.
                        matcher = await asyncio.to_thread(
                            self._build_matcher, channel, terms
                        )
                        self._matchers[channel] = matcher
                        await asyncio.to_thread(self._write_cache, channel, terms)
                        logger.info(
                            f"Dictionary revalidated for channel '{channel}': "
                            f"{matcher.term_count} terms in "
                            f"{time.monotonic() - started:.2f}s"
                        )
                        delay = float(settings.anonymization_dictionary_cache_ttl)
                        if delay <= 0:
                            return  # TTL disabled — single refresh was enough
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        logger.warning(
                            f"Dictionary revalidation failed for channel '{channel}': {e}. "
                            f"Keeping previous version, retry in {_RETRY_DELAY_SEC:.0f}s."
                        )
                        delay = _RETRY_DELAY_SEC
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            pass

    async def _fetch_store_loop(self, channel: str, *, strict: bool) -> None:
        """Fetch terms and store matcher+cache; retry with backoff on failure."""
        while True:
            started = time.monotonic()
            try:
                terms, all_ok = await self._fetch_terms(channel)
                if strict and not all_ok:
                    raise RuntimeError(
                        "some dictionary sources failed; keeping previous version"
                    )
                if not terms:
                    raise RuntimeError("no dictionary terms fetched")
                matcher = await asyncio.to_thread(
                    self._build_matcher, channel, terms
                )
                self._matchers[channel] = matcher
                await asyncio.to_thread(self._write_cache, channel, terms)
                logger.info(
                    f"Dictionary loaded for channel '{channel}': "
                    f"{matcher.term_count} terms in {time.monotonic() - started:.2f}s"
                )
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(
                    f"Dictionary preload failed for channel '{channel}': {e}. "
                    f"Retry in {_RETRY_DELAY_SEC:.0f}s."
                )
                await asyncio.sleep(_RETRY_DELAY_SEC)

    # ------------------------------------------------------------------
    # 1C fetching
    # ------------------------------------------------------------------

    async def _fetch_terms(self, channel: str) -> Tuple[Dict[str, str], bool]:
        """Execute 1C queries and collect {canonical_term: category}.

        Returns ``(terms, all_ok)`` where ``all_ok`` is True only when every
        source responded successfully.
        """
        sources = self._get_sources()
        terms: Dict[str, str] = {}  # canonical_term → category, first wins
        all_ok = True

        for source in sources:
            catalog = source.get("from", "")
            category = source.get("category", "ORG")
            extra_fields = source.get("extra_fields", [])
            fields = ["Наименование"] + [f for f in extra_fields if f != "Наименование"]

            try:
                names = await self._fetch_catalog_names(channel, catalog, fields)
                for name in names:
                    if name and name.strip() and name.strip() not in terms:
                        terms[name.strip()] = category
            except Exception as e:
                all_ok = False
                logger.warning(
                    f"Dictionary preload failed for '{catalog}' on channel '{channel}': {e}"
                )

        return terms, all_ok

    async def _fetch_catalog_names(
        self, channel: str, catalog: str, fields: List[str]
    ) -> List[str]:
        """Execute 1C query to fetch names, with fallback to Наименование only."""
        limit = int(settings.anonymization_dictionary_preload_limit) or 1
        field_sql = ", ".join(f"T.{f}" for f in fields)
        query = f"ВЫБРАТЬ ПЕРВЫЕ {limit} РАЗЛИЧНЫЕ {field_sql} ИЗ {catalog} КАК T"
        params = {"query": query, "limit": limit}

        try:
            result = await self._execute_raw(channel, params)
            return self._extract_names(result, fields)
        except Exception as e:
            if len(fields) > 1:
                logger.info(
                    f"Retrying '{catalog}' with Наименование only "
                    f"(original error: {e})"
                )
                query = f"ВЫБРАТЬ ПЕРВЫЕ {limit} РАЗЛИЧНЫЕ T.Наименование ИЗ {catalog} КАК T"
                params = {"query": query, "limit": limit}
                result = await self._execute_raw(channel, params)
                return self._extract_names(result, ["Наименование"])
            raise

    async def _execute_raw(
        self, channel: str, params: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Execute query via command queue, bypassing anonymization."""
        command_id = await channel_command_queue.add_command(
            channel, "execute_query", params
        )
        result = await channel_command_queue.wait_for_result(
            command_id, timeout=float(settings.timeout)
        )
        if isinstance(result, dict) and result.get("success") is False:
            raise RuntimeError(result.get("error", "unknown 1C error"))
        return result

    @staticmethod
    def _extract_names(result: Dict[str, Any], fields: List[str]) -> List[str]:
        """Extract name strings from query result rows."""
        names: List[str] = []
        data = result.get("data", [])
        if not isinstance(data, list):
            return names
        for row in data:
            if not isinstance(row, dict):
                continue
            for field in fields:
                val = row.get(field)
                if isinstance(val, str) and val.strip():
                    names.append(val.strip())
        return names

    def _get_sources(self) -> List[Dict]:
        """Return effective sources.

        If SOURCES_OVERRIDE is set — replaces defaults entirely.
        SOURCES_ADD always appended on top (to override or defaults).
        """
        override = settings.anonymization_dictionary_sources_override
        sources = list(override) if override is not None else list(DEFAULT_DICTIONARY_SOURCES)
        add = settings.anonymization_dictionary_sources_add
        if add:
            sources.extend(add)
        return sources

    # ------------------------------------------------------------------
    # Disk cache
    # ------------------------------------------------------------------

    @staticmethod
    def _build_matcher(channel: str, terms: Dict[str, str]) -> DictionaryMatcher:
        matcher = DictionaryMatcher()
        matcher.build(terms)
        return matcher

    @staticmethod
    def _cache_is_stale(age_sec: float) -> bool:
        ttl = float(settings.anonymization_dictionary_cache_ttl)
        if ttl <= 0:
            return False  # TTL disabled — cache never expires by age
        return age_sec > ttl

    def _fingerprint(self) -> str:
        """Hash of sources+limit: cache auto-invalidates on config change."""
        payload = json.dumps(
            {
                "sources": self._get_sources(),
                "limit": settings.anonymization_dictionary_preload_limit,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def _cache_dir(self) -> Path:
        base = Path(settings.anonymization_dictionary_cache_dir)
        if not base.is_absolute():
            # Relative paths resolve against the proxy package parent dir
            # (i.e. /app in the default container layout → /app/.dict_cache).
            base = Path(__file__).resolve().parent.parent / base
        return base

    def _cache_path(self, channel: str) -> Path:
        safe = _re.sub(r"[^A-Za-z0-9_-]", "_", channel) or "default"
        return self._cache_dir() / f"{safe}-{self._fingerprint()}.json"

    def _read_cache(self, channel: str) -> Optional[Tuple[Dict[str, str], float]]:
        """Read cached terms. Returns (terms, age_seconds) or None."""
        path = self._cache_path(channel)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        if data.get("version") != _CACHE_VERSION:
            return None
        if data.get("fingerprint") != self._fingerprint():
            return None
        raw_terms = data.get("terms")
        if not isinstance(raw_terms, dict) or not raw_terms:
            return None
        try:
            built_at = float(data.get("built_at", 0))
        except (TypeError, ValueError):
            return None
        terms = {}
        for term, category in raw_terms.items():
            if isinstance(term, str) and term.strip() and isinstance(category, str) and category:
                terms[term.strip()] = category
        if not terms:
            return None
        age = max(0.0, time.time() - built_at)
        return terms, age

    def _write_cache(self, channel: str, terms: Dict[str, str]) -> None:
        """Atomically persist terms (tmp file + os.replace)."""
        if not settings.anonymization_dictionary_cache_enabled:
            return
        path = self._cache_path(channel)
        payload = {
            "version": _CACHE_VERSION,
            "fingerprint": self._fingerprint(),
            "built_at": time.time(),
            "terms": terms,
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(
                dir=str(path.parent), prefix=".tmp-dict-", suffix=".json"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False)
                os.replace(tmp, path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError as e:
            logger.warning(f"Dictionary cache write failed for '{channel}': {e}")


# Global singleton
dictionary_preloader = DictionaryPreloader()
