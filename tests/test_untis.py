"""Tests for the WebUntis client (TOTP, secret login, homework parsing)."""
from __future__ import annotations

import json
from contextlib import ExitStack
from datetime import date
from unittest.mock import patch

import httpx
import pytest
import respx

from moodle_mcp import untis

BASE = "https://school.webuntis.com"

HOMEWORK_PAYLOAD = {
    "data": {
        "records": [{"homeworkId": 1, "teacherId": 7, "elementIds": [3]}],
        "homeworks": [{
            "id": 1, "lessonId": 3, "date": 20261020, "dueDate": 20261027,
            "text": "S. 45 Nr. 3", "remark": "", "completed": False, "attachments": [],
        }],
        "teachers": [{"id": 7, "name": "Mustermann"}],
        "lessons": [{"id": 3, "subject": "Mathematik", "lessonType": "Unterricht"}],
    },
}


def _configured() -> ExitStack:
    stack = ExitStack()
    stack.enter_context(patch.object(untis, "UNTIS_SERVER", "school.webuntis.com"))
    stack.enter_context(patch.object(untis, "UNTIS_SCHOOL", "school"))
    stack.enter_context(patch.object(untis, "UNTIS_USER", "max"))
    stack.enter_context(patch.object(untis, "UNTIS_SECRET", "GEZDGNBVGY3TQOJQ"))
    return stack


def test_totp_matches_rfc6238_vector() -> None:
    # RFC 6238 appendix B, SHA-1 secret "12345678901234567890", T=59 → 94287082.
    assert untis.totp("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ", at=59) == "287082"


def test_totp_ignores_spaces_and_case() -> None:
    assert untis.totp("gezd gnbv gy3t qojq gezd gnbv gy3t qojq", at=59) == "287082"


def test_totp_rejects_invalid_secret() -> None:
    with pytest.raises(untis.UntisConfigError):
        untis.totp("not base32!", at=59)


def test_base_url_accepts_host_or_https_url() -> None:
    assert untis.base_url("School.WebUntis.com") == BASE
    assert untis.base_url("https://school.webuntis.com/") == BASE


def test_base_url_rejects_paths_and_schemes() -> None:
    for bad in ["", "http://school.webuntis.com", "school.webuntis.com/WebUntis", "localhost"]:
        with pytest.raises(untis.UntisConfigError):
            untis.base_url(bad)


def test_parse_homework_resolves_subject_and_teacher() -> None:
    [hw] = untis.parse_homework(HOMEWORK_PAYLOAD["data"])
    assert hw["subject"] == "Mathematik"
    assert hw["teacher"] == "Mustermann"
    assert hw["assigned"] == date(2026, 10, 20)
    assert hw["due"] == date(2026, 10, 27)
    assert hw["completed"] is False


def test_parse_homework_tolerates_missing_lookups() -> None:
    [hw] = untis.parse_homework({"homeworks": [{"id": 9, "dueDate": "bogus"}]})
    assert hw["subject"] == ""
    assert hw["teacher"] is None
    assert hw["due"] is None


def test_configured_false_when_secret_missing() -> None:
    with _configured(), patch.object(untis, "UNTIS_SECRET", ""):
        assert untis.configured() is False


@pytest.mark.asyncio
@respx.mock
async def test_fetch_homework_logs_in_fetches_and_logs_out() -> None:
    login = respx.post(f"{BASE}/WebUntis/jsonrpc_intern.do").mock(
        return_value=httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": "moodle-mcp", "result": {}},
            headers={"Set-Cookie": "JSESSIONID=abc; Path=/WebUntis"},
        )
    )
    hw_route = respx.get(f"{BASE}/WebUntis/api/homeworks/lessons").mock(
        return_value=httpx.Response(200, json=HOMEWORK_PAYLOAD)
    )
    logout = respx.post(f"{BASE}/WebUntis/jsonrpc.do").mock(
        return_value=httpx.Response(200, json={"jsonrpc": "2.0", "result": None})
    )
    with _configured():
        async with untis.new_client() as client:
            homeworks = await untis.fetch_homework(client, date(2026, 10, 1), date(2026, 11, 9))
    assert [h["text"] for h in homeworks] == ["S. 45 Nr. 3"]
    auth = json.loads(login.calls.last.request.content)["params"][0]["auth"]
    assert auth["user"] == "max"
    assert len(auth["otp"]) == 6
    params = hw_route.calls.last.request.url.params
    assert (params["startDate"], params["endDate"]) == ("20261001", "20261109")
    assert "JSESSIONID=abc" in hw_route.calls.last.request.headers["cookie"]
    assert logout.called


@pytest.mark.asyncio
@respx.mock
async def test_fetch_homework_raises_on_bad_credentials() -> None:
    respx.post(f"{BASE}/WebUntis/jsonrpc_intern.do").mock(
        return_value=httpx.Response(200, json={
            "jsonrpc": "2.0", "id": "moodle-mcp",
            "error": {"message": "bad credentials", "code": -8504},
        })
    )
    with _configured():
        async with untis.new_client() as client:
            with pytest.raises(untis.UntisCallError, match="bad credentials"):
                await untis.fetch_homework(client, date(2026, 10, 1), date(2026, 11, 9))


@pytest.mark.asyncio
async def test_fetch_homework_requires_config() -> None:
    with patch.object(untis, "UNTIS_USER", ""):
        async with httpx.AsyncClient(base_url=BASE) as client:
            with pytest.raises(untis.UntisConfigError):
                await untis.fetch_homework(client, date(2026, 10, 1), date(2026, 11, 9))
