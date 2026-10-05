import time
import unittest
from datetime import datetime, timedelta

import _util  # noqa: F401  (sys.path)

from loop_agents import (find_exe, parse_reset_after, parse_reset_text, parse_try_again,
                         split_command, unsafe_cmd_args, IS_WINDOWS)


def at(hours_from_now: float) -> datetime:
    return datetime.now() + timedelta(hours=hours_from_now)


def clock(dt: datetime) -> str:
    """'3:05pm' style, the way Claude words a reset."""
    return f"{dt.hour % 12 or 12}:{dt.minute:02d}{'pm' if dt.hour >= 12 else 'am'}"


class ResetTextTest(unittest.TestCase):
    def test_local_time_ahead(self):
        target = at(5).replace(second=0, microsecond=0)
        epoch, how = parse_reset_text(f"You've hit your limit · resets {clock(target)}")
        self.assertEqual(epoch, target.timestamp())
        self.assertEqual(how, "local time (no zone given)")

    def test_just_passed_probes(self):
        if datetime.now().hour < 2:
            self.skipTest("an hour ago was yesterday")
        epoch, how = parse_reset_text(f"resets {clock(at(-1))}")
        self.assertIsNone(epoch)
        self.assertIn("just passed", how)

    def test_long_passed_is_tomorrow(self):
        target = at(-6).replace(second=0, microsecond=0)
        epoch, _ = parse_reset_text(f"resets {clock(target)}")
        self.assertEqual(epoch, (target + timedelta(days=1)).timestamp())

    def test_twelve_am(self):
        epoch, how = parse_reset_text("resets 12am")
        if epoch is None:  # run in the 3 hours after midnight: midnight "just passed"
            self.assertIn("just passed", how)
        else:
            self.assertEqual(datetime.fromtimestamp(epoch).hour, 0)

    def test_none(self):
        self.assertEqual(parse_reset_text("nothing here"), (None, "no reset time in the message"))


class TryAgainTest(unittest.TestCase):
    def test_full_date(self):
        epoch, _ = parse_try_again("try again at Sep 22nd, 2099 9:51 PM.")
        self.assertEqual(epoch, datetime(2099, 9, 22, 21, 51).timestamp())

    def test_past_date_probes(self):
        epoch, how = parse_try_again("try again at Jan 1st, 2001 9:51 AM.")
        self.assertIsNone(epoch)
        self.assertIn("already passed", how)

    def test_clock_only(self):
        target = at(4).replace(second=0, microsecond=0)
        epoch, _ = parse_try_again(f"try again at {target.hour % 12 or 12}:{target.minute:02d} "
                                   f"{'PM' if target.hour >= 12 else 'AM'}.")
        self.assertEqual(epoch, target.timestamp())

    def test_in_days(self):
        epoch, _ = parse_try_again("Try again in 1 day 2 hours.")
        self.assertAlmostEqual(epoch, time.time() + 93600, delta=5)


class ResetAfterTest(unittest.TestCase):
    def test_hms(self):
        epoch, _ = parse_reset_after("Your quota will reset after 22h54m12s.")
        self.assertAlmostEqual(epoch, time.time() + 22 * 3600 + 54 * 60 + 12, delta=5)

    def test_none(self):
        self.assertIsNone(parse_reset_after("Retrying after 500 ms")[0])


class CommandLineTest(unittest.TestCase):
    def test_split_keeps_backslashes(self):
        if IS_WINDOWS:
            self.assertEqual(split_command(r'"C:\Program Files\x\py.exe" "a b" c'),
                             [r"C:\Program Files\x\py.exe", "a b", "c"])
        else:
            self.assertEqual(split_command("'/opt/x y/py' 'a b' c"), ["/opt/x y/py", "a b", "c"])

    def test_cmd_shim_guard(self):
        argv = [r"C:\npm\codex.cmd", "exec", "-m", "a&b", "x\ny", "fine"]
        self.assertEqual(unsafe_cmd_args(argv), ["a&b", "x\ny"] if IS_WINDOWS else [])
        self.assertEqual(unsafe_cmd_args([r"C:\bin\claude.exe", "a&b"]), [])

    def test_find_exe_misses(self):
        self.assertIsNone(find_exe("surely-not-a-real-binary-xyz"))


if __name__ == "__main__":
    unittest.main()
