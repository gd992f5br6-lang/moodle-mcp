"""WebUntis client for homework lookups.

Logs in with the per-user secret behind the Untis Mobile QR code
(WebUntis → Profil → Freigaben → "Zugriff über Untis Mobile"), so it also
works for accounts that sign in through SSO and have no Untis password.
Activated by setting `UNTIS_SERVER`, `UNTIS_SCHOOL`, `UNTIS_USER` and
`UNTIS_SECRET` in the environment.

Each lookup opens its own short-lived session and logs out afterwards, as
Untis asks API clients to do.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import re
import struct
import time
from datetime import date, datetime
from typing import Any

import httpx

from .config import (
    DEFAULT_TIMEOUT,
    UNTIS_SCHOOL,
    UNTIS_SECRET,
    UNTIS_SERVER,
    UNTIS_USER,
    USER_AGENT,
)

_HOST_RE = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$")
_RPC_ID = "moodle-mcp"


class UntisConfigError(RuntimeError):
    """UNTIS_* env vars missing or malformed."""


class UntisCallError(RuntimeError):
    """WebUntis rejected the login or returned an unexpected payload."""


def configured() -> bool:
    return bool(UNTIS_SERVER and UNTIS_SCHOOL and UNTIS_USER and UNTIS_SECRET)


def base_url(server: str) -> str:
    """Return https://<host> for a bare host or https URL, else raise UntisConfigError."""
    host = server.strip().lower().removeprefix("https://").rstrip("/")
    if not _HOST_RE.match(host):
        raise UntisConfigError(
            f"UNTIS_SERVER must be a hostname like 'myschool.webuntis.com', got {server!r}"
        )
    return f"https://{host}"


def totp(secret: str, at: float | None = None) -> str:
    """RFC 6238 one-time code (SHA-1, 6 digits, 30 s) — what Untis Mobile sends."""
    raw = re.sub(r"\s", "", secret).upper()
    try:
        key = base64.b32decode(raw + "=" * (-len(raw) % 8))
    except ValueError as e:
        raise UntisConfigError("UNTIS_SECRET is not a valid base32 key.") from e
    counter = int((time.time() if at is None else at) // 30)
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    code = (struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{code:06d}"


def new_client() -> httpx.AsyncClient:
    """A fresh client per lookup so the session cookie never outlives it."""
    return httpx.AsyncClient(
        base_url=base_url(UNTIS_SERVER),
        timeout=DEFAULT_TIMEOUT,
        headers={"User-Agent": USER_AGENT},
    )


def _json(resp: httpx.Response) -> dict[str, Any]:
    try:
        resp.raise_for_status()
        payload = resp.json()
    except httpx.HTTPError as e:
        raise UntisCallError(f"HTTP error from WebUntis: {e}") from e
    except ValueError as e:
        raise UntisCallError(f"Non-JSON response from WebUntis: {e}") from e
    if not isinstance(payload, dict):
        raise UntisCallError("Unexpected response shape from WebUntis.")
    return payload


async def login(client: httpx.AsyncClient) -> None:
    """Start a session; WebUntis keeps it in the client's JSESSIONID cookie."""
    resp = await client.post(
        "/WebUntis/jsonrpc_intern.do",
        params={"m": "getUserData2017", "school": UNTIS_SCHOOL, "v": "i2.2"},
        json={
            "id": _RPC_ID,
            "method": "getUserData2017",
            "params": [{"auth": {
                "clientTime": int(time.time() * 1000),
                "user": UNTIS_USER,
                "otp": totp(UNTIS_SECRET),
            }}],
            "jsonrpc": "2.0",
        },
    )
    payload = _json(resp)
    if payload.get("error"):
        err = payload["error"]
        raise UntisCallError(f"WebUntis login failed: {err.get('message')} ({err.get('code')})")
    if "JSESSIONID" not in client.cookies:
        raise UntisCallError("WebUntis login returned no session cookie.")


async def logout(client: httpx.AsyncClient) -> None:
    with contextlib.suppress(httpx.HTTPError):
        await client.post(
            "/WebUntis/jsonrpc.do",
            params={"school": UNTIS_SCHOOL},
            json={"id": _RPC_ID, "method": "logout", "params": {}, "jsonrpc": "2.0"},
        )


async def fetch_homework(
    client: httpx.AsyncClient, start: date, end: date,
) -> list[dict[str, Any]]:
    """Homework entered in lessons between start and end (inclusive)."""
    if not configured():
        raise UntisConfigError(
            "UNTIS_SERVER, UNTIS_SCHOOL, UNTIS_USER and UNTIS_SECRET must be set."
        )
    await login(client)
    try:
        resp = await client.get(
            "/WebUntis/api/homeworks/lessons",
            params={"startDate": start.strftime("%Y%m%d"), "endDate": end.strftime("%Y%m%d")},
        )
        payload = _json(resp)
    finally:
        await logout(client)
    data = payload.get("data")
    if not isinstance(data, dict) or "homeworks" not in data:
        raise UntisCallError("WebUntis returned no homework data.")
    return parse_homework(data)


def parse_homework(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten the /api/homeworks/lessons payload into one dict per homework."""
    lessons = {lesson.get("id"): lesson for lesson in data.get("lessons") or []}
    teachers = {t.get("id"): t.get("name") for t in data.get("teachers") or []}
    teacher_of = {
        r.get("homeworkId"): teachers.get(r.get("teacherId"))
        for r in data.get("records") or []
    }
    out: list[dict[str, Any]] = []
    for hw in data.get("homeworks") or []:
        lesson = lessons.get(hw.get("lessonId")) or {}
        out.append({
            "id": hw.get("id"),
            "subject": lesson.get("subject") or "",
            "text": (hw.get("text") or "").strip(),
            "remark": (hw.get("remark") or "").strip(),
            "assigned": _untis_date(hw.get("date")),
            "due": _untis_date(hw.get("dueDate")),
            "teacher": teacher_of.get(hw.get("id")),
            "completed": bool(hw.get("completed")),
        })
    return out


def _untis_date(value: Any) -> date | None:
    try:
        return datetime.strptime(str(value), "%Y%m%d").date()
    except ValueError:
        return None
