"""Date helpers. All leave dates are calendar dates in ISO format (YYYY-MM-DD)."""
import os
from datetime import date, datetime, timedelta, timezone


def local_tz():
    # Fixed offset (default IST, +05:30). Avoids depending on a tz database.
    minutes = int(os.environ.get("TZ_OFFSET_MINUTES", "330"))
    return timezone(timedelta(minutes=minutes))


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def today_local():
    return datetime.now(local_tz()).date()


def parse_date(value, field="date"):
    if not isinstance(value, str):
        raise ValueError(f"{field} is required (YYYY-MM-DD)")
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise ValueError(f"{field} must be a valid date in YYYY-MM-DD format") from None


def working_days(start, end, holidays=()):
    """List the dates between start and end (inclusive) that count as leave.

    Saturdays, Sundays and company holidays are not counted.
    """
    holiday_set = {h if isinstance(h, date) else date.fromisoformat(h) for h in holidays}
    days = []
    current = start
    while current <= end:
        if current.weekday() < 5 and current not in holiday_set:
            days.append(current)
        current += timedelta(days=1)
    return days


def ranges_overlap(start_a, end_a, start_b, end_b):
    """True when [start_a, end_a] and [start_b, end_b] share at least one day."""
    return start_a <= end_b and end_a >= start_b
