"""One place for 'now in UTC'. datetime.utcnow() is deprecated (Python 3.12); stored
timestamps stay naive ISO strings in UTC so existing snapshots compare unchanged."""
from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)
