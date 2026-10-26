"""Merge open homework from Moodle and WebUntis into one due-date list.

Moodle side reads the calendar timeline (core_calendar_get_action_events_by_timesort),
the same "to do" list the Moodle dashboard shows. WebUntis side reads homework
entered in the class book. A source that is unconfigured or fails is reported
in `notes` instead of raising, so one broken login never hides the other list.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, tzinfo
from datetime import time as dt_time
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from . import untis, ws
from .config import HOMEWORK_TZ

# The Untis API filters by the lesson the homework was given in, not by due
# date, so look this far back to catch homework that is due inside the range.
UNTIS_LOOKBACK_DAYS = 28
# Upper bound accepted by core_calendar_get_action_events_by_timesort.
MOODLE_EVENT_LIMIT = 50
_WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def local_tz() -> tzinfo | None:
    """HOMEWORK_TZ, or None (system local time) when the zone is unknown."""
    try:
        return ZoneInfo(HOMEWORK_TZ)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def moodle_items(events: list[dict[str, Any]], tz: tzinfo | None) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for ev in events:
        # The timeline also lists video meetings; those aren't homework.
        if ev.get("purpose") == "communication":
            continue
        # Overdue and no longer submittable: nothing left to do.
        if ev.get("overdue") and not (ev.get("action") or {}).get("actionable"):
            continue
        when = datetime.fromtimestamp(int(ev.get("timesort") or 0), tz)
        course = ev.get("course") or {}
        items.append({
            "source": "moodle",
            "course": course.get("fullnamedisplay") or course.get("fullname") or "",
            "title": ev.get("activityname") or ev.get("name") or "",
            "due": when.date(),
            "time": when.strftime("%H:%M"),
            "url": ev.get("url"),
            "note": None,
            "done": False,
            "overdue": bool(ev.get("overdue")),
        })
    return items


def untis_items(
    homeworks: list[dict[str, Any]], first: date, last: date, today: date,
    include_done: bool,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for hw in homeworks:
        due = hw.get("due")
        if due is None or not first <= due <= last:
            continue
        done = bool(hw.get("completed"))
        if done and not include_done:
            continue
        items.append({
            "source": "untis",
            "course": hw.get("subject") or "",
            "title": " ".join((hw.get("text") or "").split()),
            "due": due,
            "time": None,
            "url": None,
            "note": " ".join((hw.get("remark") or "").split()) or None,
            "done": done,
            "overdue": due < today and not done,
        })
    return items


async def collect(
    client: httpx.AsyncClient, *, days_ahead: int, days_back: int, include_done: bool,
) -> dict[str, Any]:
    tz = local_tz()
    today = datetime.now(tz).date()
    first = today - timedelta(days=days_back)
    last = today + timedelta(days=days_ahead)
    items: list[dict[str, Any]] = []
    sources: list[str] = []
    notes: list[str] = []

    if ws.configured():
        try:
            payload = await ws.call(client, "core_calendar_get_action_events_by_timesort", {
                "timesortfrom": int(datetime.combine(first, dt_time.min, tz).timestamp()),
                "timesortto": int(datetime.combine(last, dt_time.max, tz).timestamp()),
                "limitnum": MOODLE_EVENT_LIMIT,
            })
            events = payload.get("events") if isinstance(payload, dict) else None
            items += moodle_items(events or [], tz)
            sources.append("moodle")
        except (ws.WSConfigError, ws.WSCallError) as e:
            notes.append(f"Moodle failed: {e}")
    else:
        notes.append("Moodle not configured — set MOODLE_URL and MOODLE_TOKEN.")

    if untis.configured():
        try:
            async with untis.new_client() as uclient:
                homeworks = await untis.fetch_homework(
                    uclient, first - timedelta(days=UNTIS_LOOKBACK_DAYS), last,
                )
            items += untis_items(homeworks, first, last, today, include_done)
            sources.append("untis")
        except (untis.UntisConfigError, untis.UntisCallError, httpx.HTTPError) as e:
            notes.append(f"Untis failed: {e}")
    else:
        notes.append(
            "Untis not configured — set UNTIS_SERVER, UNTIS_SCHOOL, UNTIS_USER and UNTIS_SECRET."
        )

    items.sort(key=lambda i: (i["due"], i["time"] or "", i["source"], i["course"]))
    return {
        "today": today,
        "from": first,
        "to": last,
        "sources": sources,
        "items": items,
        "notes": notes,
    }


def _day(d: date) -> str:
    return f"{_WEEKDAYS[d.weekday()]} {d:%d.%m.}"


def _line(item: dict[str, Any], *, with_day: bool = False) -> str:
    parts = [f"- **{item['course'] or '?'}** — {item['title'] or '(no text)'}"]
    if with_day:
        parts.append(_day(item["due"]))
    if item["time"]:
        parts.append(item["time"])
    parts.append(f"[Moodle]({item['url']})" if item["url"] else item["source"].capitalize())
    if item["done"]:
        parts.append("done")
    line = " · ".join(parts)
    if item["note"]:
        line += f"\n  _{item['note']}_"
    return line


def to_markdown(result: dict[str, Any]) -> str:
    md = [f"# Homework {_day(result['from'])} – {_day(result['to'])}", ""]
    md.append(f"Sources: {', '.join(result['sources']) or 'none'}")
    md.extend(f"> {note}" for note in result["notes"])
    items = result["items"]
    if not items:
        md += ["", "No open homework in this range."]
        return "\n".join(md)
    overdue = [i for i in items if i["overdue"]]
    if overdue:
        md += ["", "## Overdue", ""]
        md.extend(_line(i, with_day=True) for i in overdue)
    current: date | None = None
    for item in items:
        if item["overdue"]:
            continue
        if item["due"] != current:
            current = item["due"]
            md += ["", f"## {_day(current)}", ""]
        md.append(_line(item))
    return "\n".join(md)
