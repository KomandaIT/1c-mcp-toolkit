"""Admin UI для корпуса: вход по email+пароль из env.

Живёт целиком внутри toolkit; при прямом доступе —
  http://<host>:6003/admin/corpus
Допустим и reverse proxy, публикующий страницу по иному пути
(см. corpus_admin.html: база API определяется по location.pathname).

Аутентификация:
  POST /admin/corpus/login — сверяет с CORPUS_ADMIN_EMAIL/CORPUS_ADMIN_PASSWORD
  (hmac.compare_digest). Успех → подписанный HMAC session-cookie (HttpOnly,
  SameSite=Lax), TTL CORPUS_ADMIN_SESSION_TTL (12ч по умолчанию).
  Если креды не заданы в env — логин отключён (страница объясняет это).

Все `/admin/corpus*` маршруты, кроме `login`, требуют валидную сессию
(иначе 401; в т.ч. скачивание и удаление файлов).
Login ограничен по числу попыток (в памяти).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import re
import time
from collections import deque
from pathlib import Path

from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    Response,
)

from . import corpus
from .config import settings

logger = logging.getLogger(__name__)

COOKIE_NAME = "corpus_admin_session"
_FILE_RE = re.compile(r"corpus-\d{4}-\d{2}-\d{2}\.jsonl")
_STATIC_HTML = Path(__file__).resolve().parent / "static" / "corpus_admin.html"

_FILES_PER_PAGE = 20

# session state (in-memory; рестарт toolkit → пользователи разлогинятся)
_login_attempts: dict[str, deque] = {}    # ip -> timestamps неудачных попыток
_login_window_sec = 300.0
_login_max_attempts = 10


def _session_secret() -> bytes:
    raw = settings.corpus_admin_session_secret
    if raw:
        return raw.encode("utf-8")
    # без CORPUS_ADMIN_SESSION_SECRET — эфемерный ключ на процесс
    # (рестарт toolkit разлогинит всех, это приемлемо и задокументировано)
    key = getattr(settings, "_corpus_admin_ephemeral_key", None)
    if key is None:
        key = os.urandom(32)
        settings._corpus_admin_ephemeral_key = key
    return key


def _sign(token: str) -> str:
    return hmac.new(
        _session_secret(), token.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _make_session(user: str) -> str:
    exp = f"{time.time() + settings.corpus_admin_session_ttl:.3f}"
    payload = f"{user}:{exp}"
    return f"{payload}:{_sign(payload)}"


def _check_session(cookie: str) -> bool:
    parts = cookie.split(":")
    if len(parts) != 3:
        return False
    user, exp, sig = parts
    try:
        if float(exp) < time.time():
            return False
    except ValueError:
        return False
    payload = f"{user}:{exp}"
    return hmac.compare_digest(_sign(payload), sig)


def _login_allowed(ip: str) -> bool:
    q = _login_attempts.get(ip)
    if not q:
        return True
    cutoff = time.time() - _login_window_sec
    while q and q[0] < cutoff:
        q.popleft()
    return len(q) < _login_max_attempts


def _login_failed(ip: str) -> None:
    _login_attempts.setdefault(ip, deque()).append(time.time())
    # LRU-эвикция вместо clear(): сброс всех ключей снимал бы лимиты
    # попыток для всех IP разом (thundering re-allow).
    while len(_login_attempts) > 10_000:
        _login_attempts.pop(next(iter(_login_attempts)), None)


def _authorized(request: Request) -> bool:
    return _check_session(request.cookies.get(COOKIE_NAME, ""))


def _unauthorized() -> JSONResponse:
    return JSONResponse({"ok": False, "error": "Требуется вход"}, status_code=401)


async def page(request: Request) -> HTMLResponse:
    """HTML-страница admin UI (логин + дашборд, логика — в JS)."""
    creds_configured = bool(
        settings.corpus_admin_email and settings.corpus_admin_password
    )
    html = _STATIC_HTML.read_text(encoding="utf-8")
    html = html.replace("__CREDS_CONFIGURED__", "true" if creds_configured else "false")
    return HTMLResponse(html)


async def login(request: Request) -> JSONResponse:
    if not settings.corpus_admin_email or not settings.corpus_admin_password:
        return JSONResponse(
            {"ok": False, "error": "Доступ не настроен (CORPUS_ADMIN_EMAIL/PASSWORD)"},
            status_code=503,
        )
    try:
        client_host = request.client.host if request.client else ""
    except Exception:
        client_host = ""
    if not _login_allowed(client_host):
        return JSONResponse(
            {"ok": False, "error": "Слишком много попыток, попробуйте позже"},
            status_code=429,
        )
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {"ok": False, "error": "Некорректное тело запроса"}, status_code=400
        )
    email = str(body.get("email", "")).strip()
    password = str(body.get("password", ""))
    ok = (
        hmac.compare_digest(email.encode(), settings.corpus_admin_email.encode())
        and hmac.compare_digest(
            password.encode(), settings.corpus_admin_password.encode()
        )
    )
    if not ok:
        _login_failed(client_host)
        return JSONResponse(
            {"ok": False, "error": "Неверный логин или пароль"}, status_code=401
        )
    token = _make_session(email)
    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        COOKIE_NAME,
        token,
        max_age=settings.corpus_admin_session_ttl,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return resp


async def logout(request: Request) -> JSONResponse:
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp


async def files(request: Request) -> JSONResponse:
    """Список JSONL-файлов корпуса (пагинация) + статус очереди."""
    if not _authorized(request):
        return _unauthorized()
    corpus_dir = Path(settings.corpus_dir)
    paths = sorted(corpus_dir.glob("corpus-*.jsonl")) if corpus_dir.is_dir() else []
    total = len(paths)
    try:
        page_no = max(int(request.query_params.get("page", "1")), 1)
    except ValueError:
        page_no = 1
    last = max(1, -(-total // _FILES_PER_PAGE))
    page_no = min(page_no, last)
    items = []
    for path in paths[(page_no - 1) * _FILES_PER_PAGE: page_no * _FILES_PER_PAGE]:
        try:
            stat = path.stat()
            # Подсчёт строк может занять секунды на большом JSONL — off-loop.
            records = await asyncio.to_thread(
                lambda p=path: sum(1 for _ in open(p, encoding="utf-8"))
            )
            items.append({
                "name": path.name,
                "size": stat.st_size,
                "mtime": stat.st_mtime,
                "records": records,
            })
        except OSError:
            continue
    pending = None
    try:
        pending = await corpus.get_redis().llen(corpus.PENDING_KEY)
    except Exception:
        pending = None
    return JSONResponse({
        "files": {"items": items, "page": page_no, "last": last, "total": total},
        "pending": pending,
        "enabled": settings.corpus_enabled,
        "retention_days": settings.corpus_retention_days,
        "corpus_dir": settings.corpus_dir,
    })


async def flush(request: Request) -> JSONResponse:
    """Выгрузить очередь Redis в JSONL сейчас."""
    if not _authorized(request):
        return _unauthorized()
    written = await corpus.flush_once()
    return JSONResponse({"ok": True, "written": written})


async def download(request: Request) -> Response:
    """Скачать JSONL-файл (имя строго corpus-YYYY-MM-DD.jsonl)."""
    if not _authorized(request):
        return _unauthorized()
    filename = request.path_params.get("filename", "")
    if not _FILE_RE.fullmatch(filename):
        return JSONResponse(
            {"ok": False, "error": "Некорректное имя файла"}, status_code=400
        )
    path = Path(settings.corpus_dir) / filename
    if not path.is_file():
        return JSONResponse({"ok": False, "error": "Файл не найден"}, status_code=404)
    return FileResponse(path, media_type="application/x-ndjson", filename=filename)


async def delete(request: Request) -> JSONResponse:
    """Удалить JSONL-файл корпуса."""
    if not _authorized(request):
        return _unauthorized()
    filename = request.path_params.get("filename", "")
    if not _FILE_RE.fullmatch(filename):
        return JSONResponse(
            {"ok": False, "error": "Некорректное имя файла"}, status_code=400
        )
    ok = await corpus.delete_file(filename)
    return JSONResponse({"ok": ok}, status_code=200 if ok else 404)
