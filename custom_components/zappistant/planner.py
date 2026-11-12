"""Scheduling logic for the weekly charge plan.

Kept free of Home Assistant imports so it can be unit tested on its own.

A schedule has one plan per weekday (Monday first). Each plan has up to five
half-hour aligned intervals inside that calendar day and the kWh to deliver.

A day's "charge" is its intervals turned into windows, in minutes from that
day's midnight. When a day's last interval runs to 24:00 and the next day
starts with a 00:00 interval, the two form one overnight window (e.g. 22:00
to 05:00 is 1320–1740) belonging to the first day: it uses that day's kWh, and
the next day's own charge starts after it.

A "run" tracks one charge while it executes: how much energy has been
delivered since its first window began.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
import math
from typing import Any

DAYS = 7
DAY_MINUTES = 24 * 60
MAX_INTERVALS = 5
MAX_KWH = 200
# Stop treating a run as unfinished once it is this close to its target,
# since the session sensor and the zappi's own boost counter drift a little.
DONE_TOLERANCE_KWH = 0.5

STATE_DISABLED = "disabled"
STATE_IDLE = "idle"
STATE_WAITING = "waiting"
STATE_CHARGING = "charging"
STATE_OVERRUN = "overrun"
STATE_DONE = "done"
STATE_ENDED = "ended"
# Octopus mode: Octopus does the charging; Zappistant only sets its target.
STATE_OCTOPUS = "octopus"

Overrides = dict[str, dict[str, Any]]


def parse_time(value: str) -> int:
    """Convert "HH:MM" to minutes since midnight, allowing "24:00" as the end of day."""
    try:
        hours, minutes = (int(part) for part in value.split(":"))
    except (AttributeError, ValueError) as err:
        raise ValueError(f"Invalid time {value!r}, expected HH:MM") from err
    total = hours * 60 + minutes
    if minutes not in (0, 30) or not 0 <= total <= DAY_MINUTES:
        raise ValueError(f"Time {value!r} must be on the hour or half hour")
    return total


def format_time(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _percent(value: Any, field: str) -> float | None:
    if value is None:
        return None
    number = float(value)
    if not 0 <= number <= 100:
        raise ValueError(f"{field} must be between 0 and 100")
    return number


def empty_day() -> dict[str, Any]:
    return {
        "enabled": False,
        "start_percent": None,
        "target_percent": None,
        "kwh": None,
        "kwh_manual": False,
        "intervals": [],
        # Octopus mode only: when the charge must be finished, the next morning.
        "ready_by": None,
    }


def empty_schedule() -> dict[str, Any]:
    return {"enabled": False, "days": [empty_day() for _ in range(DAYS)]}


def validate_schedule(data: Any) -> dict[str, Any]:
    """Check a schedule and return it in normalized form. Raises ValueError."""
    if not isinstance(data, dict) or not isinstance(data.get("days"), list):
        raise ValueError("Schedule must have a list of days")
    if len(data["days"]) != DAYS:
        raise ValueError(f"Schedule must have {DAYS} days")

    days = []
    for index, day in enumerate(data["days"]):
        if not isinstance(day, dict):
            raise ValueError(f"Day {index} must be an object")
        kwh = day.get("kwh")
        if kwh is not None:
            kwh = float(kwh)
            if not 0 <= kwh <= MAX_KWH:
                raise ValueError(f"kWh must be between 0 and {MAX_KWH}")

        intervals: list[tuple[int, int, bool]] = []
        for interval in day.get("intervals") or []:
            start, end = parse_time(interval["start"]), parse_time(interval["end"])
            if end <= start:
                raise ValueError(f"Interval {interval['start']}–{interval['end']} must end after it starts")
            intervals.append((start, end, bool(interval.get("overrun", False))))
        intervals.sort()
        merged: list[tuple[int, int, bool]] = []
        for start, end, overrun in intervals:
            if merged and start < merged[-1][1]:
                raise ValueError("Intervals must not overlap")
            if merged and start == merged[-1][1]:
                # Back-to-back intervals are one interval.
                prev_start, _, prev_overrun = merged.pop()
                start, overrun = prev_start, prev_overrun or overrun
            merged.append((start, end, overrun))
        if len(merged) > MAX_INTERVALS:
            raise ValueError(f"At most {MAX_INTERVALS} intervals per day")
        ready_by = day.get("ready_by")
        if ready_by is not None and parse_time(ready_by) >= DAY_MINUTES:
            raise ValueError("Ready-by time must be before 24:00")

        days.append(
            {
                "enabled": bool(day.get("enabled", False)),
                "start_percent": _percent(day.get("start_percent"), "Start %"),
                "target_percent": _percent(day.get("target_percent"), "Target %"),
                "kwh": kwh,
                "kwh_manual": bool(day.get("kwh_manual", False)),
                "intervals": [
                    {"start": format_time(start), "end": format_time(end), "overrun": overrun}
                    for start, end, overrun in merged
                ],
                "ready_by": format_time(parse_time(ready_by)) if ready_by is not None else None,
            }
        )
    return {"enabled": bool(data.get("enabled", False)), "days": days}


def validate_override(data: dict[str, Any]) -> dict[str, Any]:
    """Check a one-off adjustment for a single date. Raises ValueError."""
    kwh = float(data["kwh"])
    if not 0 <= kwh <= MAX_KWH:
        raise ValueError(f"kWh must be between 0 and {MAX_KWH}")
    return {
        "start_percent": _percent(data.get("start_percent"), "Start %"),
        "target_percent": _percent(data.get("target_percent"), "Target %"),
        "kwh": kwh,
    }


def _upcoming_fields(weekly: dict[str, Any], override: dict[str, Any] | None, kwh: float) -> dict[str, Any]:
    """The percentages and amounts shown for an upcoming charge, with any one-off override applied."""
    override = override or {}
    return {
        "start_percent": override.get("start_percent", weekly["start_percent"]),
        # Overrides saved before targets could be adjusted have no target of their own.
        "target_percent": override.get("target_percent") or weekly["target_percent"],
        "kwh": kwh,
        "planned_kwh": weekly["kwh"],
        "overridden": bool(override),
        # A 0 kWh override means "skip this charge".
        "skipped": bool(override) and override["kwh"] == 0,
    }


@dataclass
class Window:
    """A charging window in minutes from its day's midnight; may run past 1440 into the next day."""

    start: int
    end: int
    overrun: bool

    def as_interval(self) -> dict[str, Any]:
        return {
            "start": format_time(self.start),
            "end": format_time(self.end if self.end <= DAY_MINUTES else self.end - DAY_MINUTES),
            "overrun": self.overrun,
            "ends_next_day": self.end > DAY_MINUTES,
        }


def _plan(schedule: dict[str, Any], day: date, overrides: Overrides | None) -> dict[str, Any]:
    """The weekly plan for a date, with any one-off kWh override applied."""
    plan = schedule["days"][day.weekday()]
    override = (overrides or {}).get(day.isoformat())
    return {**plan, "kwh": override["kwh"]} if override is not None else plan


def _intervals(plan: dict[str, Any]) -> list[Window]:
    return [
        Window(parse_time(iv["start"]), parse_time(iv["end"]), iv["overrun"]) for iv in plan["intervals"]
    ]


def _runs_overnight(schedule: dict[str, Any], day: date, overrides: Overrides | None) -> bool:
    """Whether the day's charge continues into the next day's 00:00 interval.

    The day's last interval must end at 24:00 and start after 00:00, so it is the day's own
    interval rather than the tail of the previous night's charge.
    """
    plan = _plan(schedule, day, overrides)
    if not plan["enabled"] or not plan["kwh"] or not plan["intervals"]:
        return False
    last = _intervals(plan)[-1]
    return last.end == DAY_MINUTES and last.start > 0


def day_charge(schedule: dict[str, Any], day: date, overrides: Overrides | None = None) -> dict[str, Any] | None:
    """The charge a date's plan runs, as {"kwh", "windows"}, or None if there's nothing to do."""
    plan = _plan(schedule, day, overrides)
    if not plan["enabled"] or not plan["kwh"]:
        return None
    windows = _intervals(plan)
    yesterday, tomorrow = day - timedelta(days=1), day + timedelta(days=1)
    if windows and windows[0].start == 0 and _runs_overnight(schedule, yesterday, overrides):
        windows = windows[1:]  # the end of last night's charge
    if not windows:
        return None
    if _runs_overnight(schedule, day, overrides):
        next_plan = _plan(schedule, tomorrow, overrides)
        next_windows = _intervals(next_plan) if next_plan["enabled"] else []
        if next_windows and next_windows[0].start == 0:
            first = next_windows[0]
            last = windows[-1]
            windows[-1] = Window(last.start, DAY_MINUTES + first.end, last.overrun or first.overrun)
    return {"kwh": plan["kwh"], "windows": windows}


def _window_at(windows: list[Window], minute: int) -> Window | None:
    return next((w for w in windows if w.start <= minute < w.end), None)


def _minute_of(now: datetime, day: date) -> int:
    """Minutes from the given day's midnight to now."""
    return (now.date() - day).days * DAY_MINUTES + now.hour * 60 + now.minute


def upcoming(schedule: dict[str, Any], now: datetime, overrides: Overrides | None = None) -> dict[str, Any] | None:
    """The next charge the schedule will run, including one already in progress."""
    if not schedule["enabled"]:
        return None
    # From yesterday, whose overnight charge may still be running, to a week ahead.
    for offset in range(-1, DAYS + 1):
        day = now.date() + timedelta(days=offset)
        override = (overrides or {}).get(day.isoformat())
        charge = day_charge(schedule, day, overrides)
        if charge is None and override is not None and override["kwh"] == 0:
            # Skipped: still show it, with the slots it would have had, so it can be undone.
            others = {d: o for d, o in (overrides or {}).items() if d != day.isoformat()}
            charge = day_charge(schedule, day, others)
            if charge is not None:
                charge = {**charge, "kwh": 0.0}
        if charge is None:
            continue
        minute = _minute_of(now, day)
        if all(w.end <= minute for w in charge["windows"]):
            continue
        return {
            "date": day.isoformat(),
            "intervals": [w.as_interval() for w in charge["windows"] if w.end > minute],
            **_upcoming_fields(schedule["days"][day.weekday()], override, charge["kwh"]),
        }
    return None


# Octopus mode: Octopus decides when to charge, so a day's plan is just how much and a
# ready-by time the next morning. Octopus's charge target is the % of the battery to add
# (it can't see the car's own %), between these limits.
OCTOPUS_MIN_PERCENT = 5
OCTOPUS_MAX_PERCENT = 100


def octopus_upcoming(
    schedule: dict[str, Any], now: datetime, overrides: Overrides | None = None
) -> dict[str, Any] | None:
    """The next charge Octopus should be set up for: the evening's, until its ready-by time next morning."""
    if not schedule["enabled"]:
        return None
    for offset in range(-1, DAYS + 1):
        day = now.date() + timedelta(days=offset)
        plan = _plan(schedule, day, overrides)
        # A 0 kWh override still counts: that night is being skipped, not handed to the next day.
        if not plan["enabled"] or not plan["ready_by"] or plan["kwh"] is None:
            continue
        deadline = datetime.combine(
            day + timedelta(days=1), time(), tzinfo=now.tzinfo
        ) + timedelta(minutes=parse_time(plan["ready_by"]))
        if now >= deadline:
            continue
        weekly = schedule["days"][day.weekday()]
        override = (overrides or {}).get(day.isoformat())
        return {
            "date": day.isoformat(),
            "intervals": [],
            "ready_by": plan["ready_by"],
            **_upcoming_fields(weekly, override, plan["kwh"]),
        }
    return None


def octopus_percent(kwh: float, battery_kwh: float) -> int:
    """The "% to add" for Octopus that delivers about this many kWh, within Octopus's limits."""
    percent = math.ceil(kwh / battery_kwh * 100 - 1e-9)
    return min(OCTOPUS_MAX_PERCENT, max(OCTOPUS_MIN_PERCENT, percent))


def next_interval(schedule: dict[str, Any], now: datetime, overrides: Overrides | None = None) -> dict[str, Any] | None:
    """The next window that hasn't started yet, today or in a charge that's under way."""
    charge = upcoming(schedule, now, overrides)
    if charge is None:
        return None
    day = date.fromisoformat(charge["date"])
    windows = day_charge(schedule, day, overrides)["windows"]  # type: ignore[index]
    minute = _minute_of(now, day)
    return next((w.as_interval() for w in windows if w.start > minute), None)


def _count_session(tracker: Run | Boost, session_kwh: float | None) -> None:
    """Add energy the session counter gained since last time. It resets when the car is
    plugged in again, in which case everything it shows is new."""
    if session_kwh is None:
        return
    if tracker.last_session is not None:
        tracker.delivered += (
            session_kwh - tracker.last_session if session_kwh >= tracker.last_session else session_kwh
        )
    tracker.last_session = session_kwh


@dataclass
class Run:
    """Progress of one day's charge."""

    date: str
    kwh: float
    delivered: float = 0.0
    last_session: float | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> Run | None:
        return cls(**data) if data else None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


BOOST_MANUAL = "manual"
BOOST_SMART = "smart"


@dataclass
class Boost:
    """A boost started from the card, tracked so it can be shown in the charging plan."""

    kind: str
    kwh: float
    started: str
    # Smart boost only: when the charge must be finished.
    ready_by: str | None = None
    delivered: float = 0.0
    last_session: float | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Boost:
        return cls(**data)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def remaining(self) -> float:
        return max(0.0, self.kwh - self.delivered)

    def track(self, session_kwh: float | None) -> None:
        _count_session(self, session_kwh)

    def finished(self, now: datetime) -> bool:
        if self.remaining <= DONE_TOLERANCE_KWH:
            return True
        return self.ready_by is not None and now >= datetime.fromisoformat(self.ready_by)

    def slot(self, now: datetime, rate_kw: float) -> tuple[datetime, datetime, float] | None:
        """When this boost is expected to charge, as (start, end, kWh)."""
        if self.finished(now):
            return None
        duration = timedelta(hours=self.remaining / rate_kw)
        if self.ready_by is not None:
            # myenergi doesn't say when a smart boost will charge; assume as late as it can.
            ready_by = datetime.fromisoformat(self.ready_by)
            start = max(now, ready_by - duration)
            return start, start + duration, self.remaining
        return now, now + duration, self.remaining


def ready_by_time(now: datetime, value: str) -> datetime:
    """The next time "HH:MM" comes round: today, or tomorrow if it has already passed."""
    hours, minutes = (int(part) for part in value.split(":")[:2])
    when = now.replace(hour=hours, minute=minutes, second=0, microsecond=0)
    return when if when > now else when + timedelta(days=1)


def _window_datetimes(day: date, window: Window, tzinfo: Any) -> tuple[datetime, datetime]:
    midnight = datetime.combine(day, time(), tzinfo=tzinfo)
    return midnight + timedelta(minutes=window.start), midnight + timedelta(minutes=window.end)


def _charge_slots(
    day: date, windows: list[Window], kwh: float, now: datetime, rate_kw: float, in_progress: bool
) -> list[tuple[datetime, datetime, float]]:
    """Split a charge's kWh over its windows, charging at full rate from the start of each one.

    in_progress: the charge's run has started, so an overrun window that has already ended
    means it is charging past its slot right now.
    """
    slots = []
    remaining = kwh
    for window in windows:
        if remaining <= DONE_TOLERANCE_KWH:
            break
        start, end = _window_datetimes(day, window, now.tzinfo)
        if end <= now:
            if window.overrun and in_progress:
                # Past an overrun window: kWh, not time, decides; it keeps charging from now.
                slots.append((now, now + timedelta(hours=remaining / rate_kw), remaining))
                break
            continue
        start = max(start, now)
        kwh_here = min(remaining, rate_kw * (end - start).total_seconds() / 3600)
        slots.append((start, start + timedelta(hours=kwh_here / rate_kw), kwh_here))
        remaining -= kwh_here
        if window.overrun and remaining > DONE_TOLERANCE_KWH:
            slots.append((end, end + timedelta(hours=remaining / rate_kw), remaining))
            break
    return slots


def planned_slots(
    schedule: dict[str, Any],
    run: Run | None,
    now: datetime,
    rate_kw: float,
    overrides: Overrides | None = None,
    boosts: list[Boost] | None = None,
    horizon: timedelta = timedelta(hours=48),
) -> list[tuple[datetime, datetime, float]]:
    """Expected charging over the coming hours, from the schedule and tracked boosts, as (start, end, kWh)."""
    slots: list[tuple[datetime, datetime, float]] = []
    if schedule["enabled"]:
        for offset in range(-1, horizon.days + 2):
            day = now.date() + timedelta(days=offset)
            charge = day_charge(schedule, day, overrides)
            if charge is None:
                continue
            in_progress = run is not None and run.date == day.isoformat()
            kwh = max(0.0, run.kwh - run.delivered) if in_progress else charge["kwh"]  # type: ignore[union-attr]
            slots += _charge_slots(day, charge["windows"], kwh, now, rate_kw, in_progress)
    for boost in boosts or []:
        if slot := boost.slot(now, rate_kw):
            slots.append(slot)
    return sorted(s for s in slots if s[0] < now + horizon and s[1] > s[0])


@dataclass
class Decision:
    """What the charger should be doing right now."""

    state: str
    charge: bool = False
    remaining: float = 0.0
    interval: dict[str, Any] | None = None
    run_started: bool = False


def advance(
    schedule: dict[str, Any],
    run: Run | None,
    now: datetime,
    session_kwh: float | None,
    overrides: Overrides | None = None,
) -> tuple[Run | None, Decision]:
    """Update the run for the current time and session energy, and decide whether to charge."""
    if not schedule["enabled"]:
        return None, Decision(STATE_DISABLED)

    today = now.date()
    run_started = False
    # A window is open now: today's, or yesterday's overnight one. Its charge takes over
    # from any earlier run.
    for day in (today, today - timedelta(days=1)):
        charge = day_charge(schedule, day, overrides)
        if charge is None or not _window_at(charge["windows"], _minute_of(now, day)):
            continue
        if run is None or run.date < day.isoformat():
            run = Run(date=day.isoformat(), kwh=charge["kwh"], last_session=session_kwh)
            run_started = True
        break
    if run is None:
        return None, Decision(STATE_IDLE)

    _count_session(run, session_kwh)

    run_date = date.fromisoformat(run.date)
    charge = day_charge(schedule, run_date, overrides)
    # Drop runs whose plan was switched off, and overruns that have gone on for over a day.
    if charge is None or (today - run_date).days > 1:
        return None, Decision(STATE_IDLE)
    run.kwh = charge["kwh"]  # follow edits made while the run is in progress
    remaining = max(0.0, run.kwh - run.delivered)

    if remaining <= DONE_TOLERANCE_KWH:
        return run, Decision(STATE_DONE, run_started=run_started)

    minute = _minute_of(now, run_date)
    windows = charge["windows"]
    if in_slot := _window_at(windows, minute):
        return run, Decision(STATE_CHARGING, True, remaining, in_slot.as_interval(), run_started)
    # Once an overrun window has ended, kWh rather than time decides when to stop.
    if any(w.overrun and w.end <= minute for w in windows):
        return run, Decision(STATE_OVERRUN, True, remaining, None, run_started)
    if any(w.start > minute for w in windows):
        return run, Decision(STATE_WAITING, False, remaining, None, run_started)
    return run, Decision(STATE_ENDED, False, remaining, None, run_started)
