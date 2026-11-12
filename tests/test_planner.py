"""Unit tests for the schedule planner. Run with: python3 -m unittest discover tests"""

import importlib.util
import sys
import unittest
from datetime import datetime
from pathlib import Path

# Load planner.py directly so the test doesn't need Home Assistant installed.
_PATH = Path(__file__).parent.parent / "custom_components" / "zappistant" / "planner.py"
_spec = importlib.util.spec_from_file_location("planner", _PATH)
planner = importlib.util.module_from_spec(_spec)
sys.modules["planner"] = planner  # dataclasses look their module up here
_spec.loader.exec_module(planner)

# 2026-11-10 is a Tuesday (weekday 1).
TUESDAY = 1


def schedule_with(day_index, intervals, kwh=10.0, enabled=True):
    schedule = planner.empty_schedule()
    schedule["enabled"] = enabled
    schedule["days"][day_index] = {
        **planner.empty_day(),
        "enabled": True,
        "kwh": kwh,
        "intervals": intervals,
    }
    return planner.validate_schedule(schedule)


def at(day, hour, minute=0):
    return datetime(2026, 11, day, hour, minute)


class ValidateTest(unittest.TestCase):
    def test_sorts_and_normalizes(self):
        schedule = schedule_with(
            TUESDAY,
            [{"start": "22:00", "end": "24:00"}, {"start": "01:00", "end": "02:30", "overrun": True}],
        )
        self.assertEqual(
            schedule["days"][TUESDAY]["intervals"],
            [
                {"start": "01:00", "end": "02:30", "overrun": True},
                {"start": "22:00", "end": "24:00", "overrun": False},
            ],
        )

    def test_rejects_bad_input(self):
        cases = {
            "not half hour": [{"start": "01:15", "end": "02:00"}],
            "end before start": [{"start": "03:00", "end": "02:00"}],
            "overlap": [{"start": "01:00", "end": "03:00"}, {"start": "02:00", "end": "04:00"}],
            "too many": [{"start": f"{h:02d}:00", "end": f"{h:02d}:30"} for h in range(6)],
        }
        for name, intervals in cases.items():
            with self.subTest(name), self.assertRaises(ValueError):
                schedule_with(TUESDAY, intervals)


class AdvanceTest(unittest.TestCase):
    def setUp(self):
        self.schedule = schedule_with(
            TUESDAY, [{"start": "01:00", "end": "03:00"}, {"start": "22:00", "end": "24:00"}]
        )

    def test_idle_before_first_interval(self):
        run, decision = planner.advance(self.schedule, None, at(10, 0, 30), 0.0)
        self.assertIsNone(run)
        self.assertEqual(decision.state, planner.STATE_IDLE)

    def test_disabled_schedule(self):
        self.schedule["enabled"] = False
        _, decision = planner.advance(self.schedule, None, at(10, 1), 0.0)
        self.assertEqual(decision.state, planner.STATE_DISABLED)

    def test_charges_in_slot_and_waits_between(self):
        run, decision = planner.advance(self.schedule, None, at(10, 1), 2.0)
        self.assertTrue(decision.run_started)
        self.assertTrue(decision.charge)
        self.assertEqual(decision.remaining, 10.0)

        run, decision = planner.advance(self.schedule, run, at(10, 3), 6.0)
        self.assertEqual(decision.state, planner.STATE_WAITING)
        self.assertFalse(decision.charge)
        self.assertEqual(decision.remaining, 6.0)  # 4 kWh delivered since the run began

        run, decision = planner.advance(self.schedule, run, at(10, 22), 6.0)
        self.assertEqual(decision.state, planner.STATE_CHARGING)
        self.assertFalse(decision.run_started)

    def test_session_reset_counts_new_energy(self):
        run, _ = planner.advance(self.schedule, None, at(10, 1), 8.0)
        run, decision = planner.advance(self.schedule, run, at(10, 1, 30), 3.0)  # car re-plugged
        self.assertEqual(run.delivered, 3.0)
        self.assertEqual(decision.remaining, 7.0)

    def test_done_when_target_reached(self):
        run, _ = planner.advance(self.schedule, None, at(10, 1), 0.0)
        run, decision = planner.advance(self.schedule, run, at(10, 2), 9.7)
        self.assertEqual(decision.state, planner.STATE_DONE)
        self.assertFalse(decision.charge)

    def test_ends_after_last_slot_without_overrun(self):
        run, _ = planner.advance(self.schedule, None, at(10, 22), 0.0)
        run, decision = planner.advance(self.schedule, run, at(11, 0, 30), 5.0)
        self.assertEqual(decision.state, planner.STATE_ENDED)
        self.assertFalse(decision.charge)

    def test_overrun_continues_past_midnight_until_kwh(self):
        schedule = schedule_with(TUESDAY, [{"start": "22:00", "end": "24:00", "overrun": True}])
        run, _ = planner.advance(schedule, None, at(10, 22), 0.0)
        run, decision = planner.advance(schedule, run, at(11, 0, 30), 5.0)
        self.assertEqual(decision.state, planner.STATE_OVERRUN)
        self.assertTrue(decision.charge)
        self.assertEqual(decision.remaining, 5.0)
        run, decision = planner.advance(schedule, run, at(11, 2), 10.0)
        self.assertEqual(decision.state, planner.STATE_DONE)

    def test_overrun_expires_after_a_day(self):
        schedule = schedule_with(TUESDAY, [{"start": "22:00", "end": "24:00", "overrun": True}])
        run, _ = planner.advance(schedule, None, at(10, 22), 0.0)
        run, decision = planner.advance(schedule, run, at(12, 9), 0.0)
        self.assertIsNone(run)
        self.assertEqual(decision.state, planner.STATE_IDLE)

    def test_next_day_plan_replaces_overrun(self):
        schedule = schedule_with(TUESDAY, [{"start": "22:00", "end": "24:00", "overrun": True}])
        schedule["days"][TUESDAY + 1] = {**schedule["days"][TUESDAY], "kwh": 4.0}
        run, _ = planner.advance(schedule, None, at(10, 22), 0.0)
        run, decision = planner.advance(schedule, run, at(11, 22), 3.0)
        self.assertTrue(decision.run_started)
        self.assertEqual(run.date, "2026-11-11")
        self.assertEqual(decision.remaining, 4.0)


class OvernightTest(unittest.TestCase):
    """Tuesday 22:00–24:00 plus Wednesday 00:00–05:00 is one charge, using Tuesday's kWh."""

    def setUp(self):
        self.schedule = schedule_with(TUESDAY, [{"start": "22:00", "end": "24:00"}], kwh=10)
        self.schedule["days"][TUESDAY + 1] = {
            **planner.empty_day(),
            "enabled": True,
            "kwh": 30,
            "intervals": [
                {"start": "00:00", "end": "05:00", "overrun": False},
                {"start": "22:00", "end": "24:00", "overrun": False},
            ],
        }

    def test_merges_adjacent_intervals_when_validating(self):
        schedule = schedule_with(
            TUESDAY, [{"start": "20:00", "end": "22:00"}, {"start": "22:00", "end": "24:00", "overrun": True}]
        )
        self.assertEqual(
            schedule["days"][TUESDAY]["intervals"], [{"start": "20:00", "end": "24:00", "overrun": True}]
        )

    def test_upcoming_shows_one_overnight_slot(self):
        upcoming = planner.upcoming(self.schedule, at(10, 18))
        self.assertEqual(upcoming["date"], "2026-11-10")
        self.assertEqual(
            upcoming["intervals"],
            [{"start": "22:00", "end": "05:00", "overrun": False, "ends_next_day": True}],
        )
        self.assertEqual(upcoming["kwh"], 10)
        # Still Tuesday's charge at 01:00 on Wednesday; then Wednesday's own evening charge.
        self.assertEqual(planner.upcoming(self.schedule, at(11, 1))["date"], "2026-11-10")
        wednesday = planner.upcoming(self.schedule, at(11, 6))
        self.assertEqual(wednesday["date"], "2026-11-11")
        self.assertEqual(wednesday["kwh"], 30)
        self.assertEqual(wednesday["intervals"][0]["start"], "22:00")

    def test_one_run_across_midnight(self):
        run, decision = planner.advance(self.schedule, None, at(10, 22), 0.0)
        self.assertTrue(decision.run_started)
        run, decision = planner.advance(self.schedule, run, at(11, 1), 4.0)
        self.assertEqual(run.date, "2026-11-10")
        self.assertFalse(decision.run_started)
        self.assertEqual(decision.state, planner.STATE_CHARGING)
        self.assertEqual(decision.remaining, 6.0)  # Tuesday's 10 kWh, not Wednesday's 30
        run, decision = planner.advance(self.schedule, run, at(11, 5), 7.0)
        self.assertEqual(decision.state, planner.STATE_ENDED)
        run, decision = planner.advance(self.schedule, run, at(11, 22), 7.0)
        self.assertTrue(decision.run_started)
        self.assertEqual(decision.remaining, 30.0)

    def test_starts_overnight_run_after_midnight(self):
        # E.g. Home Assistant restarted, or the schedule was saved at 01:00.
        run, decision = planner.advance(self.schedule, None, at(11, 1), 0.0)
        self.assertEqual(run.date, "2026-11-10")
        self.assertEqual(decision.remaining, 10.0)

    def test_no_merge_when_evening_day_is_off(self):
        self.schedule["days"][TUESDAY]["enabled"] = False
        run, decision = planner.advance(self.schedule, None, at(11, 1), 0.0)
        self.assertEqual(run.date, "2026-11-11")
        self.assertEqual(decision.remaining, 30.0)

    def test_overrun_on_merged_slot_applies_at_its_end(self):
        self.schedule["days"][TUESDAY + 1]["intervals"][0]["overrun"] = True
        run, _ = planner.advance(self.schedule, None, at(10, 22), 0.0)
        run, decision = planner.advance(self.schedule, run, at(11, 5, 30), 4.0)
        self.assertEqual(decision.state, planner.STATE_OVERRUN)


class OverrideTest(unittest.TestCase):
    def setUp(self):
        self.schedule = schedule_with(TUESDAY, [{"start": "22:00", "end": "24:00"}], kwh=20)
        self.schedule["days"][TUESDAY]["start_percent"] = 45
        self.schedule["days"][TUESDAY]["target_percent"] = 80

    def test_override_replaces_kwh_for_that_date_only(self):
        overrides = {"2026-11-10": planner.validate_override({"kwh": 16, "start_percent": 52})}
        _, decision = planner.advance(self.schedule, None, at(10, 22), 0.0, overrides)
        self.assertEqual(decision.remaining, 16)
        # A week later the weekly plan applies again.
        _, decision = planner.advance(self.schedule, None, at(17, 22), 0.0, overrides)
        self.assertEqual(decision.remaining, 20)

    def test_zero_kwh_override_skips_the_charge(self):
        overrides = {"2026-11-10": planner.validate_override({"kwh": 0})}
        run, decision = planner.advance(self.schedule, None, at(10, 22), 0.0, overrides)
        self.assertIsNone(run)
        self.assertEqual(decision.state, planner.STATE_IDLE)

    def test_upcoming_today_then_next_week(self):
        upcoming = planner.upcoming(self.schedule, at(10, 18))
        self.assertEqual(upcoming["date"], "2026-11-10")
        self.assertEqual(upcoming["start_percent"], 45)
        self.assertEqual(upcoming["target_percent"], 80)
        self.assertEqual(upcoming["kwh"], 20)
        self.assertFalse(upcoming["overridden"])
        # While tonight's slot runs it's still the upcoming charge; afterwards it's next Tuesday.
        self.assertEqual(planner.upcoming(self.schedule, at(10, 23))["date"], "2026-11-10")
        self.assertEqual(planner.upcoming(self.schedule, at(11, 0, 30))["date"], "2026-11-17")

    def test_upcoming_shows_override(self):
        overrides = {"2026-11-10": planner.validate_override({"kwh": 16, "start_percent": 52})}
        upcoming = planner.upcoming(self.schedule, at(10, 18), overrides)
        self.assertEqual(upcoming["kwh"], 16)
        self.assertEqual(upcoming["planned_kwh"], 20)
        self.assertEqual(upcoming["start_percent"], 52)
        self.assertTrue(upcoming["overridden"])

    def test_upcoming_shows_adjusted_target(self):
        overrides = {"2026-11-10": planner.validate_override({"kwh": 30, "start_percent": 45, "target_percent": 100})}
        upcoming = planner.upcoming(self.schedule, at(10, 18), overrides)
        self.assertEqual((upcoming["start_percent"], upcoming["target_percent"], upcoming["kwh"]), (45, 100, 30))

    def test_skipped_charge_stays_upcoming_until_its_slot_ends(self):
        overrides = {"2026-11-10": planner.validate_override({"kwh": 0})}
        upcoming = planner.upcoming(self.schedule, at(10, 18), overrides)
        self.assertEqual(upcoming["date"], "2026-11-10")
        self.assertTrue(upcoming["skipped"])
        self.assertEqual(upcoming["kwh"], 0)
        self.assertEqual(upcoming["intervals"][0]["start"], "22:00")
        # Nothing is charged, and once the slot is over the next week's charge is upcoming.
        _, decision = planner.advance(self.schedule, None, at(10, 22), 0.0, overrides)
        self.assertFalse(decision.charge)
        self.assertEqual(planner.upcoming(self.schedule, at(11, 0, 30), overrides)["date"], "2026-11-17")

    def test_no_upcoming_when_disabled(self):
        self.schedule["enabled"] = False
        self.assertIsNone(planner.upcoming(self.schedule, at(10, 18)))


class OctopusTest(unittest.TestCase):
    """Octopus mode: Tuesday evening's charge must be ready by 07:00 on Wednesday."""

    def setUp(self):
        self.schedule = planner.empty_schedule()
        self.schedule["enabled"] = True
        self.schedule["days"][TUESDAY] = {
            **planner.empty_day(),
            "enabled": True,
            "start_percent": 45,
            "target_percent": 80,
            "kwh": 21,
            "ready_by": "07:00",
        }
        self.schedule = planner.validate_schedule(self.schedule)

    def test_rejects_ready_by_at_midnight(self):
        self.schedule["days"][TUESDAY]["ready_by"] = "24:00"
        with self.assertRaises(ValueError):
            planner.validate_schedule(self.schedule)

    def test_upcoming_until_ready_by_next_morning(self):
        upcoming = planner.octopus_upcoming(self.schedule, at(10, 18))
        self.assertEqual(upcoming["date"], "2026-11-10")
        self.assertEqual(upcoming["ready_by"], "07:00")
        self.assertEqual(upcoming["kwh"], 21)
        # Still Tuesday's charge overnight; after 07:00 on Wednesday it's next Tuesday's.
        self.assertEqual(planner.octopus_upcoming(self.schedule, at(11, 6, 30))["date"], "2026-11-10")
        self.assertEqual(planner.octopus_upcoming(self.schedule, at(11, 7))["date"], "2026-11-17")

    def test_zero_kwh_override_keeps_the_night(self):
        overrides = {"2026-11-10": planner.validate_override({"kwh": 0})}
        upcoming = planner.octopus_upcoming(self.schedule, at(10, 18), overrides)
        self.assertEqual((upcoming["date"], upcoming["kwh"]), ("2026-11-10", 0))

    def test_percent_to_add(self):
        self.assertEqual(planner.octopus_percent(20.3, 61), 34)
        self.assertEqual(planner.octopus_percent(30.5, 61), 50)  # exactly 50%, no rounding up
        self.assertEqual(planner.octopus_percent(0, 61), planner.OCTOPUS_MIN_PERCENT)
        self.assertEqual(planner.octopus_percent(80, 61), planner.OCTOPUS_MAX_PERCENT)


class PlannedSlotsTest(unittest.TestCase):
    """Charging slots published for Predbat, at 7 kW."""

    RATE = 7.0

    def slots(self, schedule, now, run=None, boosts=None):
        return [
            (start.strftime("%d %H:%M"), end.strftime("%d %H:%M"), round(kwh, 2))
            for start, end, kwh in planner.planned_slots(schedule, run, now, self.RATE, boosts=boosts)
        ]

    def test_fills_windows_in_order_until_kwh(self):
        # 10 kWh at 7 kW: the first 2-hour window takes it all, ending after 1h26m.
        schedule = schedule_with(TUESDAY, [{"start": "01:00", "end": "03:00"}, {"start": "22:00", "end": "24:00"}])
        self.assertEqual(self.slots(schedule, at(10, 0)), [("10 01:00", "10 02:25", 10.0)])

    def test_spills_into_later_windows(self):
        schedule = schedule_with(
            TUESDAY, [{"start": "01:00", "end": "02:00"}, {"start": "22:00", "end": "24:00"}], kwh=10
        )
        self.assertEqual(
            self.slots(schedule, at(10, 0)), [("10 01:00", "10 02:00", 7.0), ("10 22:00", "10 22:25", 3.0)]
        )

    def test_overrun_extends_past_window(self):
        schedule = schedule_with(TUESDAY, [{"start": "22:00", "end": "23:00", "overrun": True}], kwh=10)
        self.assertEqual(
            self.slots(schedule, at(10, 12)), [("10 22:00", "10 23:00", 7.0), ("10 23:00", "10 23:25", 3.0)]
        )

    def test_running_charge_uses_remaining_kwh_from_now(self):
        schedule = schedule_with(TUESDAY, [{"start": "22:00", "end": "24:00"}], kwh=10)
        run = planner.Run(date="2026-11-10", kwh=10, delivered=6.5)
        self.assertEqual(self.slots(schedule, at(10, 22, 30), run), [("10 22:30", "10 23:00", 3.5)])

    def test_running_overrun_past_its_window(self):
        schedule = schedule_with(TUESDAY, [{"start": "22:00", "end": "23:00", "overrun": True}], kwh=10)
        run = planner.Run(date="2026-11-10", kwh=10, delivered=7)
        self.assertEqual(self.slots(schedule, at(10, 23, 30), run), [("10 23:30", "10 23:55", 3.0)])

    def test_overnight_charge_and_window_limit(self):
        # Tuesday 22:00 to Wednesday 05:00 is one 30 kWh charge: 4h17m at 7 kW, across midnight.
        schedule = schedule_with(TUESDAY, [{"start": "22:00", "end": "24:00"}], kwh=30)
        schedule["days"][TUESDAY + 1] = {
            **planner.empty_day(),
            "enabled": True,
            "intervals": [{"start": "00:00", "end": "05:00", "overrun": False}],
        }
        self.assertEqual(self.slots(schedule, at(10, 12)), [("10 22:00", "11 02:17", 30.0)])
        # Without the morning slot only 2 hours fit, so only 14 kWh is planned.
        schedule["days"][TUESDAY + 1]["enabled"] = False
        self.assertEqual(self.slots(schedule, at(10, 12)), [("10 22:00", "11 00:00", 14.0)])

    def test_nothing_when_disabled(self):
        schedule = schedule_with(TUESDAY, [{"start": "22:00", "end": "24:00"}], enabled=False)
        self.assertEqual(self.slots(schedule, at(10, 12)), [])

    def test_boosts(self):
        schedule = planner.empty_schedule()
        now = at(10, 18)
        manual = planner.Boost(kind=planner.BOOST_MANUAL, kwh=7, started=now.isoformat())
        smart = planner.Boost(
            kind=planner.BOOST_SMART, kwh=14, started=now.isoformat(),
            ready_by=planner.ready_by_time(now, "07:00").isoformat(),
        )
        self.assertEqual(
            self.slots(schedule, now, boosts=[manual, smart]),
            [("10 18:00", "10 19:00", 7.0), ("11 05:00", "11 07:00", 14.0)],
        )
        manual.track(0.0)
        manual.track(7.0)
        self.assertTrue(manual.finished(now))
        self.assertTrue(smart.finished(at(11, 7)))


if __name__ == "__main__":
    unittest.main()
