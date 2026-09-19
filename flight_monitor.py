#!/usr/bin/env python3
"""`python -m fare_watch` 的同義入口，方便 launchd / cron 直接指向單一檔案。"""

import sys

from fare_watch.cli import main

if __name__ == "__main__":
    sys.exit(main())
