"""flight_monitor.py 只是 `python -m fare_watch` 的同義入口；真正的行為測試在其他 test_*.py。"""

import unittest

import flight_monitor
from fare_watch import cli


class WrapperTests(unittest.TestCase):
    def test_delegates_to_fare_watch_cli(self):
        self.assertIs(flight_monitor.main, cli.main)


if __name__ == "__main__":
    unittest.main()
