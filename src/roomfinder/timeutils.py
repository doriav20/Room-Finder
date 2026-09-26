from datetime import datetime, time
from zoneinfo import ZoneInfo


def local_time(moment: datetime, timezone: str) -> time:
    return moment.astimezone(ZoneInfo(timezone)).time()


def in_daily_window(moment: time, start: time, end: time) -> bool:
    """Whether a time of day falls in [start, end), where the window may cross midnight."""
    if start == end:
        return False
    if start < end:
        return start <= moment < end
    return moment >= start or moment < end
