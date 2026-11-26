import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import asc_bark

ROOT = Path(__file__).resolve().parents[1]
SHANGHAI = ZoneInfo("Asia/Shanghai")
NEW_YORK = ZoneInfo("America/New_York")


def _fixed(moment: datetime):
    def clock() -> datetime:
        return moment

    return clock


class ScheduleParseTests(unittest.TestCase):
    def test_defaults_match_the_documented_schedule(self):
        self.assertEqual(asc_bark.DEFAULT_SCHEDULE, "09:00")
        self.assertEqual(asc_bark.DEFAULT_TIMEZONE, "Asia/Shanghai")
        schedule = asc_bark.schedule_from_env({})
        self.assertEqual(schedule.expression, "0 9 * * *")
        self.assertEqual(schedule.minutes, frozenset({0}))
        self.assertEqual(schedule.hours, frozenset({9}))
        self.assertTrue(schedule.day_any)
        self.assertTrue(schedule.weekday_any)
        self.assertEqual(asc_bark.timezone_from_env({}).key, "Asia/Shanghai")
        self.assertEqual(asc_bark.schedule_from_env({"ASC_SCHEDULE": "   "}).expression, "0 9 * * *")
        self.assertEqual(asc_bark.timezone_from_env({"TZ": "  "}).key, "Asia/Shanghai")

    def test_daily_time_and_cron_fields(self):
        daily = asc_bark.parse_schedule("09:00")
        cron = asc_bark.parse_schedule("0  9   * * *")
        self.assertEqual(daily.expression, "0 9 * * *")
        self.assertEqual(cron.expression, "0 9 * * *")
        self.assertEqual(daily.minutes, cron.minutes)
        self.assertEqual(daily.hours, cron.hours)
        self.assertEqual(asc_bark.parse_schedule("9:05").expression, "5 9 * * *")

        stepped = asc_bark.parse_schedule("*/15 9 1,15 * 1-5")
        self.assertEqual(stepped.minutes, frozenset({0, 15, 30, 45}))
        self.assertEqual(stepped.hours, frozenset({9}))
        self.assertEqual(stepped.days, frozenset({1, 15}))
        self.assertEqual(stepped.weekdays, frozenset({1, 2, 3, 4, 5}))
        self.assertFalse(stepped.day_any)
        self.assertFalse(stepped.weekday_any)

        sunday = asc_bark.parse_schedule("0 9 * * 0")
        also_sunday = asc_bark.parse_schedule("0 9 * * 7")
        both = asc_bark.parse_schedule("0 9 * * 0,7")
        self.assertEqual(sunday.weekdays, frozenset({0}))
        self.assertEqual(also_sunday.weekdays, frozenset({0}))
        self.assertEqual(both.weekdays, frozenset({0}))
        self.assertFalse(sunday.weekday_any)
        self.assertEqual(asc_bark.parse_schedule("5/15 0 * * *").minutes, frozenset({5, 20, 35, 50}))

    def test_invalid_schedule_and_timezone(self):
        invalid = [
            "",
            "   ",
            "99:00",
            "09:60",
            "9:0",
            "nope",
            "* * *",
            "0 9 * * * *",
            "60 0 * * *",
            "0 24 * * *",
            "0 0 0 * *",
            "0 0 * 13 *",
            "0 0 * * 8",
            "0 0 * * MON",
            "*/0 * * * *",
            "5-1 * * * *",
        ]
        for text in invalid:
            with self.subTest(text=text):
                with self.assertRaises(asc_bark.AscError) as caught:
                    asc_bark.parse_schedule(text)
                self.assertEqual(caught.exception.code, 2)
        with self.assertRaises(asc_bark.AscError) as caught:
            asc_bark.timezone_from_env({"TZ": "Not/AZone"})
        self.assertEqual(caught.exception.code, 2)

    def test_main_rejects_bad_schedule_before_credentials(self):
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            schedule_code = asc_bark.main(["serve"], {"ASC_SCHEDULE": "nope"})
            zone_code = asc_bark.main(["serve"], {"TZ": "Not/AZone"})
        self.assertEqual(schedule_code, 2)
        self.assertEqual(zone_code, 2)
        text = output.getvalue()
        self.assertIn("ASC_SCHEDULE", text)
        self.assertIn("时区无法识别", text)
        self.assertNotIn("缺少", text)


class NextOccurrenceTests(unittest.TestCase):
    def test_daily_time_waits_until_the_next_wall_clock_slot(self):
        schedule = asc_bark.parse_schedule("09:00")
        before = datetime(2026, 11, 26, 8, 30, 15, tzinfo=SHANGHAI)
        nxt = asc_bark.next_occurrence(schedule, before, SHANGHAI)
        self.assertEqual(nxt.isoformat(timespec="seconds"), "2026-11-26T09:00:00+08:00")

        exactly = datetime(2026, 11, 26, 9, 0, tzinfo=SHANGHAI)
        self.assertEqual(
            asc_bark.next_occurrence(schedule, exactly, SHANGHAI).isoformat(timespec="seconds"),
            "2026-11-27T09:00:00+08:00",
        )
        just_after = datetime(2026, 11, 26, 9, 0, 1, tzinfo=SHANGHAI)
        self.assertEqual(
            asc_bark.next_occurrence(schedule, just_after, SHANGHAI).isoformat(timespec="seconds"),
            "2026-11-27T09:00:00+08:00",
        )

        utc_morning = datetime(2026, 11, 26, 0, 30, tzinfo=timezone.utc)
        self.assertEqual(
            asc_bark.next_occurrence(schedule, utc_morning, SHANGHAI).isoformat(timespec="seconds"),
            "2026-11-26T09:00:00+08:00",
        )
        same = asc_bark.next_occurrence(asc_bark.parse_schedule("0 9 * * *"), before, SHANGHAI)
        self.assertEqual(same, nxt)

    def test_weekdays_sunday_and_dom_or_dow(self):
        self.assertEqual(date(2026, 11, 26).weekday(), 3)  # Thursday, cron weekday 4
        weekday = asc_bark.parse_schedule("0 9 * * 1-5")
        thursday_morning = datetime(2026, 11, 26, 8, 0, tzinfo=SHANGHAI)
        self.assertEqual(
            asc_bark.next_occurrence(weekday, thursday_morning, SHANGHAI).date(),
            date(2026, 11, 26),
        )
        thursday_after = datetime(2026, 11, 26, 10, 0, tzinfo=SHANGHAI)
        self.assertEqual(
            asc_bark.next_occurrence(weekday, thursday_after, SHANGHAI).date(),
            date(2026, 11, 27),
        )
        friday_after = datetime(2026, 11, 27, 10, 0, tzinfo=SHANGHAI)
        self.assertEqual(
            asc_bark.next_occurrence(weekday, friday_after, SHANGHAI).date(),
            date(2026, 11, 30),
        )

        for expression in ("0 9 * * 0", "0 9 * * 7"):
            sunday = asc_bark.next_occurrence(
                asc_bark.parse_schedule(expression),
                thursday_morning,
                SHANGHAI,
            )
            self.assertEqual(sunday.date(), date(2026, 11, 29))
            self.assertEqual(asc_bark._cron_weekday(sunday.date()), 0)

        either = asc_bark.parse_schedule("0 9 1 * 5")
        self.assertEqual(
            asc_bark.next_occurrence(either, thursday_after, SHANGHAI).date(),
            date(2026, 11, 27),
        )
        after_friday = datetime(2026, 11, 27, 10, 0, tzinfo=SHANGHAI)
        self.assertEqual(
            asc_bark.next_occurrence(either, after_friday, SHANGHAI).date(),
            date(2026, 12, 1),
        )
        after_first = datetime(2026, 12, 1, 10, 0, tzinfo=SHANGHAI)
        self.assertEqual(
            asc_bark.next_occurrence(either, after_first, SHANGHAI).date(),
            date(2026, 12, 4),
        )

    def test_month_end_and_leap_day_and_impossible_date(self):
        month_end = asc_bark.parse_schedule("0 9 31 * *")
        after_january = datetime(2026, 1, 31, 10, 0, tzinfo=SHANGHAI)
        march = asc_bark.next_occurrence(month_end, after_january, SHANGHAI)
        self.assertEqual(march.date(), date(2026, 3, 31))

        leap = asc_bark.parse_schedule("0 9 29 2 *")
        before_leap = datetime(2024, 2, 28, 10, 0, tzinfo=SHANGHAI)
        first = asc_bark.next_occurrence(leap, before_leap, SHANGHAI)
        self.assertEqual(first.date(), date(2024, 2, 29))
        on_leap = datetime(2024, 2, 29, 9, 0, tzinfo=SHANGHAI)
        self.assertEqual(
            asc_bark.next_occurrence(leap, on_leap, SHANGHAI).date(),
            date(2028, 2, 29),
        )

        with self.assertRaises(asc_bark.AscError) as caught:
            asc_bark.next_occurrence(
                asc_bark.parse_schedule("0 0 31 2 *"),
                datetime(2026, 1, 1, tzinfo=SHANGHAI),
                SHANGHAI,
            )
        self.assertEqual(caught.exception.code, 2)

    def test_daylight_saving_gap_and_repeated_hour(self):
        gap = asc_bark.parse_schedule("30 2 * * *")
        before_gap = datetime(2026, 3, 8, 1, 0, tzinfo=NEW_YORK)
        nxt = asc_bark.next_occurrence(gap, before_gap, NEW_YORK)
        self.assertEqual(nxt.date(), date(2026, 3, 9))
        self.assertEqual((nxt.hour, nxt.minute), (2, 30))

        repeated = asc_bark.parse_schedule("30 1 * * *")
        first = datetime(2026, 11, 1, 1, 30, tzinfo=NEW_YORK, fold=0)
        second = asc_bark.next_occurrence(repeated, first, NEW_YORK)
        self.assertEqual(second.date(), date(2026, 11, 1))
        self.assertEqual((second.hour, second.minute, second.fold), (1, 30, 1))
        self.assertEqual(second.utcoffset(), timedelta(hours=-5))
        self.assertEqual(second.timestamp() - first.timestamp(), 3600)
        following = asc_bark.next_occurrence(repeated, second, NEW_YORK)
        self.assertEqual(following.date(), date(2026, 11, 2))

        delays = []
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = asc_bark.serve_loop(
                repeated,
                NEW_YORK,
                lambda: asc_bark.Result(0, "已推送。"),
                clock=lambda: first,
                wait=lambda delay: delays.append(delay) or True,
            )
        self.assertEqual(code, 0)
        self.assertAlmostEqual(delays[0], 3600, delta=1)
        self.assertIn("01:30:00-05:00", stdout.getvalue())

        every_minute = asc_bark.parse_schedule("* * * * *")
        nxt_minute = asc_bark.next_occurrence(every_minute, first, NEW_YORK)
        self.assertEqual(nxt_minute.timestamp() - first.timestamp(), 60)
        self.assertEqual((nxt_minute.hour, nxt_minute.minute, nxt_minute.fold), (1, 31, 0))
        self.assertEqual(nxt_minute.utcoffset(), timedelta(hours=-4))

    def test_immediate_cycle_runs_once_before_the_first_wait(self):
        moment = datetime(2026, 11, 1, 1, 30, tzinfo=NEW_YORK, fold=0)
        runs = []
        triggers = []
        delays = []

        def wait(delay: float) -> bool:
            delays.append(delay)
            return True

        output = io.StringIO()
        with redirect_stdout(output):
            code = asc_bark.serve_loop(
                asc_bark.parse_schedule("30 1 * * *"),
                NEW_YORK,
                lambda: runs.append("ran") or asc_bark.Result(0, "已推送。"),
                clock=lambda: moment,
                wait=wait,
                run_immediately=True,
                before_run=triggers.append,
            )
        self.assertEqual(code, 0)
        self.assertEqual(runs, ["ran"])
        self.assertEqual(triggers, ["startup"])
        self.assertEqual(len(delays), 1)
        self.assertAlmostEqual(delays[0], 3600, delta=1)
        self.assertIn("已推送。", output.getvalue())
        self.assertIn("01:30:00-05:00", output.getvalue())

    def test_steps_do_not_catch_up_missed_slots(self):
        schedule = asc_bark.parse_schedule("*/15 * * * *")
        now = datetime(2026, 11, 26, 10, 0, 1, tzinfo=SHANGHAI)
        first = asc_bark.next_occurrence(schedule, now, SHANGHAI)
        self.assertEqual(first.isoformat(timespec="seconds"), "2026-11-26T10:15:00+08:00")
        later = datetime(2026, 11, 26, 10, 50, tzinfo=SHANGHAI)
        second = asc_bark.next_occurrence(schedule, later, SHANGHAI)
        self.assertEqual(second.isoformat(timespec="seconds"), "2026-11-26T11:00:00+08:00")


class ServeLoopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        self.schedule = asc_bark.parse_schedule("09:00")
        self.now = datetime(2026, 11, 26, 8, 0, tzinfo=SHANGHAI)

    def tearDown(self):
        self.tmp.cleanup()

    def test_wait_first_and_stop_before_any_run(self):
        delays = []
        runs = []

        def wait(delay: float) -> bool:
            delays.append(delay)
            return True

        output = io.StringIO()
        with redirect_stdout(output):
            code = asc_bark.serve_loop(
                self.schedule,
                SHANGHAI,
                lambda: runs.append("ran") or asc_bark.Result(0, "已推送。"),
                clock=_fixed(self.now),
                wait=wait,
            )
        self.assertEqual(code, 0)
        self.assertEqual(runs, [])
        self.assertEqual(len(delays), 1)
        self.assertAlmostEqual(delays[0], 3600, delta=1)
        self.assertIn("2026-11-26T09:00:00+08:00", output.getvalue())

    def test_does_not_exit_after_success_or_failure(self):
        calls = {"n": 0}

        def run_once() -> asc_bark.Result:
            calls["n"] += 1
            if calls["n"] == 1:
                return asc_bark.Result(0, "已推送。")
            if calls["n"] == 2:
                return asc_bark.Result(1, "未成功")
            if calls["n"] == 3:
                raise asc_bark.AscError("Bark 推送失败，未记录为已推送。")
            raise RuntimeError("network down bark-device-key-0001")

        def wait(_delay: float) -> bool:
            return calls["n"] >= 4

        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = asc_bark.serve_loop(
                self.schedule,
                SHANGHAI,
                run_once,
                clock=_fixed(self.now),
                wait=wait,
                secrets=["bark-device-key-0001"],
            )
        self.assertEqual(code, 0)
        self.assertEqual(calls["n"], 4)
        self.assertIn("已推送。", stdout.getvalue())
        errors = stderr.getvalue()
        self.assertIn("未成功", errors)
        self.assertIn("本次未成功，将等到下一个日程。", errors)
        self.assertIn("Bark 推送失败", errors)
        self.assertIn("本次运行失败，将等到下一个日程。", errors)
        self.assertIn("network down", errors)
        self.assertNotIn("bark-device-key-0001", stdout.getvalue() + errors)
        self.assertIn("[redacted]", errors)

    def test_shutdown_after_the_current_run_does_not_start_another(self):
        state = {"runs": 0, "stop": False, "in_flight": 0, "peak": 0}

        def run_once() -> asc_bark.Result:
            state["in_flight"] += 1
            state["peak"] = max(state["peak"], state["in_flight"])
            try:
                state["runs"] += 1
                state["stop"] = True
                self.assertEqual(state["in_flight"], 1)
                return asc_bark.Result(0, "已推送。")
            finally:
                state["in_flight"] -= 1

        def wait(_delay: float) -> bool:
            return state["stop"]

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = asc_bark.serve_loop(
                self.schedule,
                SHANGHAI,
                run_once,
                clock=_fixed(self.now),
                wait=wait,
            )
        self.assertEqual(code, 0)
        self.assertEqual(state["runs"], 1)
        self.assertEqual(state["peak"], 1)

    def test_missed_slots_are_not_replayed(self):
        moments = iter(
            [
                datetime(2026, 11, 26, 10, 0, 1, tzinfo=SHANGHAI),
                datetime(2026, 11, 26, 10, 50, tzinfo=SHANGHAI),
            ]
        )
        seen = []

        def clock() -> datetime:
            return next(moments)

        def wait(delay: float) -> bool:
            seen.append(round(delay))
            return len(seen) >= 2

        schedule = asc_bark.parse_schedule("*/15 * * * *")
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = asc_bark.serve_loop(
                schedule,
                SHANGHAI,
                lambda: asc_bark.Result(0, "已推送。"),
                clock=clock,
                wait=wait,
            )
        self.assertEqual(code, 0)
        self.assertIn("10:15:00+08:00", stdout.getvalue())
        self.assertIn("11:00:00+08:00", stdout.getvalue())
        self.assertNotIn("10:30:00+08:00", stdout.getvalue())
        self.assertEqual(seen[0], 15 * 60 - 1)
        self.assertEqual(seen[1], 10 * 60)
        self.assertNotIn(0, seen)

    def test_lock_failure_is_logged_and_the_loop_continues(self):
        held = asc_bark.acquire_lock(self.data / "asc-bark.lock")
        config = asc_bark.Config(
            issuer_id="issuer-example-0001",
            key_id="KEYEXAMPLE1",
            app_id="1000000001",
            private_key="x" * 16,
            key_path="",
            bark_key="bark-device-key-0001",
            bark_server="https://bark.test",
            data_dir=self.data,
        )
        state = {"waits": 0}

        def wait(_delay: float) -> bool:
            state["waits"] += 1
            return state["waits"] >= 3

        stdout = io.StringIO()
        stderr = io.StringIO()
        try:
            with redirect_stdout(stdout), redirect_stderr(stderr):
                result = asc_bark.cmd_serve(
                    config,
                    session=object(),
                    schedule=self.schedule,
                    tz=SHANGHAI,
                    clock=_fixed(self.now),
                    wait=wait,
                )
        finally:
            asc_bark.release_lock(held)
        self.assertEqual(result.code, 0)
        self.assertEqual(result.message, "已停止。")
        self.assertEqual(state["waits"], 3)
        self.assertIn("正在运行", stderr.getvalue())
        self.assertIn("将等到下一个日程", stderr.getvalue())
        self.assertGreaterEqual(stdout.getvalue().count("下次运行"), 3)

    def test_unreachable_schedule_does_not_run(self):
        def runner():
            raise AssertionError("runner should not be called")

        config = asc_bark.Config(
            issuer_id="issuer-example-0001",
            key_id="KEYEXAMPLE1",
            app_id="1000000001",
            private_key="x" * 16,
            key_path="",
            bark_key=None,
            bark_server="https://bark.test",
            data_dir=self.data,
        )
        with self.assertRaises(asc_bark.AscError) as caught:
            asc_bark.cmd_serve(
                config,
                session=object(),
                schedule=asc_bark.parse_schedule("0 0 31 2 *"),
                tz=SHANGHAI,
                clock=_fixed(self.now),
                wait=lambda _delay: True,
                runner=runner,
            )
        self.assertEqual(caught.exception.code, 2)
        self.assertFalse((self.data / "serve.json").exists())

    def _config(self) -> asc_bark.Config:
        return asc_bark.Config(
            issuer_id="issuer-example-0001",
            key_id="KEYEXAMPLE1",
            app_id="1000000001",
            private_key="x" * 16,
            key_path="",
            bark_key="bark-device-key-0001",
            bark_server="https://bark.test",
            data_dir=self.data,
        )

    def _serve(self, runner, wait):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = asc_bark.cmd_serve(
                self._config(),
                session=object(),
                schedule=self.schedule,
                tz=SHANGHAI,
                clock=_fixed(self.now),
                wait=wait,
                runner=runner,
            )
        return result, stdout.getvalue(), stderr.getvalue()

    def _record(self) -> dict:
        return json.loads((self.data / "serve.json").read_text(encoding="utf-8"))

    def test_first_start_runs_once_then_restart_waits(self):
        runs = []
        delays = []

        def runner():
            record = self._record()
            self.assertEqual(record["runs"], 1)
            self.assertEqual(record["first_trigger"], "startup")
            self.assertNotIn("bark-device-key-0001", json.dumps(record))
            runs.append("startup")
            return asc_bark.Result(0, "已推送。")

        def wait(delay: float) -> bool:
            delays.append(delay)
            return True

        result, stdout, stderr = self._serve(runner, wait)
        self.assertEqual(result.code, 0)
        self.assertEqual(result.message, "已停止。")
        self.assertEqual(runs, ["startup"])
        self.assertEqual(len(delays), 1)
        self.assertAlmostEqual(delays[0], 3600, delta=1)
        self.assertIn("还没有 serve 触发过的运行记录", stdout)
        self.assertIn("先抓取一次", stdout)
        self.assertIn("2026-11-26T09:00:00+08:00", stdout)
        self.assertNotIn("不会立刻抓取", stdout)
        self.assertEqual(stderr, "")
        self.assertEqual(self._record()["runs"], 1)
        self.assertEqual(self._record()["first_started_at"], "2026-11-26T08:00:00+08:00")
        self.assertEqual((self.data / "serve.json").stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.data / "state.json").exists())

        runs.clear()
        result, stdout, stderr = self._serve(
            lambda: runs.append("restart") or asc_bark.Result(0, "已推送。"),
            lambda _delay: True,
        )
        self.assertEqual(result.code, 0)
        self.assertEqual(result.message, "已停止。")
        self.assertEqual(runs, [])
        self.assertIn("已有 serve 运行记录", stdout)
        self.assertIn("不会立刻抓取", stdout)
        self.assertIn("2026-11-26T09:00:00+08:00", stdout)
        self.assertEqual(self._record()["runs"], 1)
        self.assertEqual(self._record()["last_trigger"], "startup")
        self.assertEqual(stderr, "")

        asc_bark.save_state(
            self.data / "state.json",
            {"version": asc_bark.STATE_VERSION, "instances": {}, "pushed": {}},
        )
        (self.data / "state.json").unlink()
        runs.clear()
        result, stdout, stderr = self._serve(
            lambda: runs.append("state") or asc_bark.Result(0, "已推送。"),
            lambda _delay: True,
        )
        self.assertEqual(runs, [])
        self.assertIn("不会立刻抓取", stdout)
        self.assertEqual(self._record()["runs"], 1)

        (self.data / "serve.json").unlink()
        runs.clear()
        result, stdout, stderr = self._serve(
            lambda: runs.append("again") or asc_bark.Result(0, "已推送。"),
            lambda _delay: True,
        )
        self.assertEqual(result.code, 0)
        self.assertEqual(runs, ["again"])
        self.assertIn("先抓取一次", stdout)
        self.assertEqual(self._record()["runs"], 1)
        self.assertEqual(self._record()["first_trigger"], "startup")

    def test_scheduled_cycle_keeps_the_first_serve_record(self):
        waits = {"n": 0}
        runs = []

        def wait(_delay: float) -> bool:
            waits["n"] += 1
            return waits["n"] >= 2

        def runner():
            runs.append(self._record()["last_trigger"])
            return asc_bark.Result(0, "已推送。")

        result, stdout, _stderr = self._serve(runner, wait)
        self.assertEqual(result.code, 0)
        self.assertEqual(runs, ["startup", "schedule"])
        record = self._record()
        self.assertEqual(record["runs"], 2)
        self.assertEqual(record["first_trigger"], "startup")
        self.assertEqual(record["last_trigger"], "schedule")
        self.assertEqual(record["first_started_at"], "2026-11-26T08:00:00+08:00")
        self.assertEqual(record["version"], asc_bark.SERVE_RECORD_VERSION)
        self.assertEqual(stdout.count("2026-11-26T09:00:00+08:00"), 2)

    def test_failed_startup_cycle_does_not_exit_or_repeat_on_restart(self):
        calls = {"n": 0}

        def runner():
            calls["n"] += 1
            self.assertEqual(self._record()["first_trigger"], "startup")
            if calls["n"] == 1:
                raise asc_bark.AscError("Bark 推送失败，未记录为已推送。")
            return asc_bark.Result(1, "未成功")

        result, stdout, stderr = self._serve(runner, lambda _delay: True)
        self.assertEqual(result.code, 0)
        self.assertEqual(result.message, "已停止。")
        self.assertEqual(calls["n"], 1)
        self.assertIn("下次运行：2026-11-26T09:00:00+08:00", stdout)
        self.assertIn("本次运行失败，将等到下一个日程。", stderr)
        self.assertIn("Bark 推送失败", stderr)
        self.assertEqual(self._record()["runs"], 1)

        result, stdout, stderr = self._serve(runner, lambda _delay: True)
        self.assertEqual(result.code, 0)
        self.assertEqual(calls["n"], 1)
        self.assertIn("不会立刻抓取", stdout)
        self.assertNotIn("本次运行失败", stderr)
        self.assertEqual(self._record()["runs"], 1)

    def test_unsuccessful_startup_result_keeps_waiting(self):
        def runner():
            return asc_bark.Result(1, "未成功")

        result, _stdout, stderr = self._serve(runner, lambda _delay: True)
        self.assertEqual(result.code, 0)
        self.assertEqual(result.message, "已停止。")
        self.assertIn("未成功", stderr)
        self.assertIn("本次未成功，将等到下一个日程。", stderr)
        self.assertEqual(self._record()["first_trigger"], "startup")

    def test_one_shot_state_does_not_count_as_a_serve_run(self):
        asc_bark.save_state(
            self.data / "state.json",
            {
                "version": asc_bark.STATE_VERSION,
                "instances": {"instance-1": {"report": "downloads"}},
                "pushed": {"2024-03-30": {"fingerprint": "abc", "pushed_at": "2024-04-01T00:00:00+00:00"}},
            },
        )
        runs = []

        def runner():
            runs.append("startup")
            return asc_bark.Result(0, "已推送。")

        result, stdout, _stderr = self._serve(runner, lambda _delay: True)
        self.assertEqual(result.code, 0)
        self.assertEqual(runs, ["startup"])
        self.assertIn("先抓取一次", stdout)
        self.assertEqual(self._record()["first_trigger"], "startup")
        state = json.loads((self.data / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["pushed"]["2024-03-30"]["fingerprint"], "abc")
        self.assertNotIn("serve", state)

    def test_zero_runs_still_starts_immediately(self):
        (self.data / "serve.json").write_text(
            json.dumps({"version": asc_bark.SERVE_RECORD_VERSION, "runs": 0}) + "\n",
            encoding="utf-8",
        )
        runs = []
        result, stdout, _stderr = self._serve(
            lambda: runs.append("startup") or asc_bark.Result(0, "已推送。"),
            lambda _delay: True,
        )
        self.assertEqual(result.code, 0)
        self.assertEqual(runs, ["startup"])
        self.assertIn("先抓取一次", stdout)
        self.assertEqual(self._record()["runs"], 1)
        self.assertEqual(self._record()["first_trigger"], "startup")

    def test_unreadable_serve_record_waits_and_is_not_overwritten(self):
        samples = {
            "truncated": "{",
            "unknown-version": json.dumps({"version": 99, "runs": 1}),
            "boolean-runs": json.dumps({"version": asc_bark.SERVE_RECORD_VERSION, "runs": True}),
        }
        for name, payload in samples.items():
            with self.subTest(name=name):
                path = self.data / "serve.json"
                path.write_text(payload, encoding="utf-8")
                runs = []
                waits = {"n": 0}

                def wait(_delay: float) -> bool:
                    waits["n"] += 1
                    return waits["n"] >= 2

                def runner():
                    runs.append("schedule")
                    return asc_bark.Result(0, "已推送。")

                result, stdout, stderr = self._serve(runner, wait)
                self.assertEqual(result.code, 0)
                self.assertEqual(runs, ["schedule"])
                self.assertEqual(path.read_text(encoding="utf-8"), payload)
                self.assertIn("不立刻抓取", stdout)
                self.assertNotIn("先抓取一次", stdout)
                self.assertIn("未覆盖它", stderr)
                self.assertIn("未能写入 serve 运行记录，本次抓取仍会继续。", stderr)

    def test_commands_still_parse(self):
        self.assertEqual(asc_bark.parse_args(["serve"]).command, "serve")
        self.assertTrue(asc_bark.parse_args(["run", "--dry-run"]).dry_run)
        self.assertEqual(asc_bark.parse_args(["init"]).command, "init")
        self.assertTrue(asc_bark.parse_args(["init", "--snapshot"]).snapshot)
        self.assertEqual(asc_bark.parse_args(["test-notification"]).command, "test-notification")


class DeployContractTests(unittest.TestCase):
    def test_compose_and_image_workflow_keep_the_service_running(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        example = (ROOT / ".env.example").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        workflow = (ROOT / ".github/workflows/docker-publish.yml").read_text(encoding="utf-8")

        self.assertIn('CMD ["serve"]', dockerfile)
        self.assertIn("ASC_SCHEDULE=09:00", dockerfile)
        self.assertIn("restart: unless-stopped", compose)
        self.assertIn('command: ["serve"]', compose)
        self.assertIn("ghcr.io/feong/asc-bark:${ASC_IMAGE_TAG:-latest}", compose)
        self.assertIn("ASC_SCHEDULE=09:00", example)
        self.assertIn("TZ=Asia/Shanghai", example)
        self.assertIn("docker compose up -d", readme)
        self.assertIn("可以删掉那条任务", readme)

        self.assertIn("ghcr.io/feong/asc-bark", workflow)
        self.assertIn("packages: write", workflow)
        self.assertIn("contents: read", workflow)
        self.assertIn("secrets.GITHUB_TOKEN", workflow)
        self.assertNotIn("secrets.", workflow.replace("secrets.GITHUB_TOKEN", ""))
        self.assertIn("type=raw,value=latest,enable={{is_default_branch}}", workflow)
        self.assertIn("type=ref,event=tag", workflow)
        self.assertIn('- "v*"', workflow)
        self.assertIn("visibility == 'public'", workflow)
        self.assertIn("/token?service=", workflow)
        self.assertNotIn("try_public", workflow)
        self.assertNotIn("gh api --method PATCH", workflow)
        self.assertIn("PLATFORMS: linux/amd64,linux/arm64/v8", workflow)
        self.assertEqual(workflow.count("platforms: ${{ env.PLATFORMS }}"), 2)
        self.assertIn("docker/setup-qemu-action@v4.4.0", workflow)
        self.assertIn("outputs: type=oci,dest=/tmp/asc-bark-image.tar", workflow)
        self.assertIn('{"os": "linux", "architecture": "arm64", "variant": "v8"}', workflow)
        self.assertIn('item["architecture"] == "arm64" and variant in ("", "v8")', workflow)
        self.assertIn("linux/amd64", readme)
        self.assertIn("linux/arm64/v8", readme)
        self.assertIn("serve.json", readme)
        self.assertIn("不会因为容器重启又立刻抓一次", readme)
        self.assertIn("不会写入 `serve.json`", readme)


class ShutdownSignalTests(unittest.TestCase):
    def test_sigterm_and_sigint_stop_the_wait(self):
        script = r"""
import json
import shutil
import signal
import tempfile
import threading
import time
from pathlib import Path

import asc_bark

data = Path(tempfile.mkdtemp())
try:
    (data / "serve.json").write_text(
        json.dumps(
            {
                "version": 1,
                "runs": 1,
                "first_trigger": "startup",
                "first_started_at": "2026-11-26T00:00:00+00:00",
                "last_trigger": "startup",
                "last_started_at": "2026-11-26T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    config = asc_bark.Config(
        issuer_id="issuer-example-0001",
        key_id="KEYEXAMPLE1",
        app_id="1000000001",
        private_key="x" * 16,
        key_path="",
        bark_key="bark-device-key-0001",
        bark_server="https://bark.test",
        data_dir=data,
    )

    def once(sig):
        runs = []

        def runner():
            runs.append(sig)
            return asc_bark.Result(0, "已推送。")

        def poke():
            time.sleep(0.3)
            signal.raise_signal(sig)

        threading.Thread(target=poke, daemon=True).start()
        result = asc_bark.cmd_serve(
            config,
            object(),
            schedule=asc_bark.parse_schedule("00:00"),
            tz=asc_bark.ZoneInfo("UTC"),
            runner=runner,
        )
        if result.code != 0 or result.message != "已停止。" or runs:
            raise SystemExit(f"sig {sig} result={result} runs={runs}")

    once(signal.SIGTERM)
    once(signal.SIGINT)
    print("stopped")
finally:
    shutil.rmtree(data, ignore_errors=True)
"""
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT)
        proc = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(ROOT),
            env=env,
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("stopped", proc.stdout)
        self.assertIn("下次运行", proc.stdout)
        self.assertIn("不会立刻抓取", proc.stdout)

    def test_sigterm_during_startup_cycle_finishes_then_exits(self):
        script = r"""
import json
import shutil
import signal
import tempfile
import threading
import time
from pathlib import Path

import asc_bark

data = Path(tempfile.mkdtemp())
try:
    config = asc_bark.Config(
        issuer_id="issuer-example-0001",
        key_id="KEYEXAMPLE1",
        app_id="1000000001",
        private_key="x" * 16,
        key_path="",
        bark_key="bark-device-key-0001",
        bark_server="https://bark.test",
        data_dir=data,
    )
    runs = []
    started = threading.Event()

    def runner():
        runs.append("startup")
        started.set()
        time.sleep(2)
        return asc_bark.Result(0, "已推送。")

    def poke():
        if not started.wait(5):
            raise SystemExit("runner did not start")
        signal.raise_signal(signal.SIGTERM)

    threading.Thread(target=poke, daemon=True).start()
    result = asc_bark.cmd_serve(
        config,
        object(),
        schedule=asc_bark.parse_schedule("00:00"),
        tz=asc_bark.ZoneInfo("UTC"),
        runner=runner,
    )
    record = json.loads((data / "serve.json").read_text(encoding="utf-8"))
    if result.code != 0 or result.message != "已停止。" or runs != ["startup"]:
        raise SystemExit(f"result={result} runs={runs}")
    if record["runs"] != 1 or record["first_trigger"] != "startup":
        raise SystemExit(f"record={record}")
    print("finished-startup")
finally:
    shutil.rmtree(data, ignore_errors=True)
"""
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT)
        proc = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(ROOT),
            env=env,
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("finished-startup", proc.stdout)
        self.assertIn("先抓取一次", proc.stdout)
        self.assertIn("下次运行", proc.stdout)
