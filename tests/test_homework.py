"""Tests for merging Moodle + WebUntis homework (filtering, sorting, markdown)."""
from __future__ import annotations

import time
from datetime import date, datetime, timezone
from unittest.mock import patch

import httpx
import pytest
import respx

from moodle_mcp import homework, untis, ws

TODAY = date(2026, 10, 26)


def _ts(year: int, month: int, day: int, hour: int = 12) -> int:
    return int(datetime(year, month, day, hour, tzinfo=timezone.utc).timestamp())


def _event(**overrides: object) -> dict:
    ev = {
        "activityname": "Anschaffung Roman",
        "name": "Anschaffung Roman ist fällig.",
        "purpose": "assessment",
        "timesort": _ts(2026, 11, 1, 17),
        "overdue": False,
        "url": "https://moodle.example.com/mod/assign/view.php?id=1",
        "course": {"fullnamedisplay": "Deutsch", "shortname": "DE"},
        "action": {"actionable": True},
    }
    ev.update(overrides)
    return ev


def _untis_hw(**overrides: object) -> dict:
    hw = {
        "id": 1, "subject": "Mathematik", "text": "S. 45\nNr. 3", "remark": "",
        "assigned": date(2026, 10, 20), "due": date(2026, 10, 27), "teacher": None,
        "completed": False,
    }
    hw.update(overrides)
    return hw


def test_moodle_items_skips_meetings_and_closed_overdue() -> None:
    events = [
        _event(),
        _event(activityname="virtueller Klassenraum", purpose="communication"),
        _event(activityname="Too late", overdue=True, action={"actionable": False}),
        _event(activityname="Still open", overdue=True, action={"actionable": True}),
    ]
    items = homework.moodle_items(events, timezone.utc)
    assert [i["title"] for i in items] == ["Anschaffung Roman", "Still open"]
    assert items[0]["course"] == "Deutsch"
    assert (items[0]["due"], items[0]["time"]) == (date(2026, 11, 1), "17:00")
    assert items[1]["overdue"] is True


def test_moodle_items_uses_local_timezone() -> None:
    [item] = homework.moodle_items([_event()], homework.ZoneInfo("Europe/Berlin"))
    assert item["time"] == "18:00"  # 17:00 UTC is 18:00 CET


def test_untis_items_filters_range_and_done() -> None:
    hws = [
        _untis_hw(),
        _untis_hw(id=2, due=date(2026, 9, 1)),
        _untis_hw(id=3, completed=True),
        _untis_hw(id=4, due=date(2026, 10, 20)),
    ]
    items = homework.untis_items(hws, date(2026, 10, 12), date(2026, 11, 9), TODAY, False)
    assert [i["due"] for i in items] == [date(2026, 10, 27), date(2026, 10, 20)]
    assert items[0]["title"] == "S. 45 Nr. 3"
    assert items[1]["overdue"] is True
    with_done = homework.untis_items(hws, date(2026, 10, 12), date(2026, 11, 9), TODAY, True)
    assert len(with_done) == 3


def test_to_markdown_puts_overdue_first_and_groups_by_day() -> None:
    items = homework.untis_items(
        [_untis_hw(), _untis_hw(id=4, due=date(2026, 10, 20), remark="Heft mitbringen")],
        date(2026, 10, 12), date(2026, 11, 9), TODAY, False,
    ) + homework.moodle_items([_event()], timezone.utc)
    md = homework.to_markdown({
        "from": date(2026, 10, 12), "to": date(2026, 11, 9),
        "sources": ["moodle", "untis"], "items": items, "notes": [],
    })
    assert md.index("## Overdue") < md.index("## Tue 27.10.") < md.index("## Sun 01.11.")
    assert "- **Mathematik** — S. 45 Nr. 3 · Tue 20.10. · Untis\n  _Heft mitbringen_" in md
    assert "17:00 · [Moodle](https://moodle.example.com/mod/assign/view.php?id=1)" in md


def test_to_markdown_reports_empty_range_and_notes() -> None:
    md = homework.to_markdown({
        "from": TODAY, "to": TODAY, "sources": [], "items": [],
        "notes": ["Untis not configured"],
    })
    assert "> Untis not configured" in md
    assert "No open homework" in md


@pytest.mark.asyncio
async def test_collect_notes_unconfigured_sources() -> None:
    with patch.object(ws, "MOODLE_TOKEN", ""), patch.object(untis, "UNTIS_SECRET", ""):
        async with httpx.AsyncClient() as client:
            result = await homework.collect(client, days_ahead=7, days_back=0, include_done=False)
    assert result["sources"] == []
    assert any("Moodle not configured" in n for n in result["notes"])
    assert any("Untis not configured" in n for n in result["notes"])


@pytest.mark.asyncio
@respx.mock
async def test_collect_keeps_moodle_when_untis_login_fails() -> None:
    respx.post("https://moodle.example.com/webservice/rest/server.php").mock(
        return_value=httpx.Response(200, json={
            "events": [_event(timesort=int(time.time()) + 2 * 86400)],
        })
    )
    respx.post("https://school.webuntis.com/WebUntis/jsonrpc_intern.do").mock(
        return_value=httpx.Response(200, json={"error": {"message": "bad credentials"}})
    )
    with patch.object(ws, "MOODLE_URL", "https://moodle.example.com"), \
         patch.object(ws, "MOODLE_TOKEN", "abc"), \
         patch.object(ws, "_is_private_ip", return_value=False), \
         patch.object(untis, "UNTIS_SERVER", "school.webuntis.com"), \
         patch.object(untis, "UNTIS_SCHOOL", "school"), \
         patch.object(untis, "UNTIS_USER", "max"), \
         patch.object(untis, "UNTIS_SECRET", "GEZDGNBVGY3TQOJQ"):
        async with httpx.AsyncClient() as client:
            result = await homework.collect(client, days_ahead=14, days_back=0, include_done=False)
    assert result["sources"] == ["moodle"]
    assert [i["title"] for i in result["items"]] == ["Anschaffung Roman"]
    assert any("bad credentials" in n for n in result["notes"])
