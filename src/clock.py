"""The one clock every module reads.

Until 2026-09-14 the product read `date.today()` and `datetime.now()` at 218
sites in 14 modules, and a test that pinned the planner's clock left the ride
store, the database and the ICU client on the real one: the EWMA of the
rider's load read the real date while the planner read the pinned one
(commit 03237915), and 130 tests flipped when the two clocks disagreed (the
audit's test lens). A pin is one call here, `freeze()`, and every module sees it.
"""
from __future__ import annotations

import datetime as _dt
from typing import Optional

_frozen: Optional[_dt.datetime] = None


def today() -> _dt.date:
    return _frozen.date() if _frozen is not None else _dt.date.today()


def now(tz: Optional[_dt.tzinfo] = None) -> _dt.datetime:
    if _frozen is None:
        return _dt.datetime.now(tz)
    if tz is None:
        return _frozen.replace(tzinfo=None)
    base = _frozen if _frozen.tzinfo is not None else _frozen.replace(tzinfo=_dt.timezone.utc)
    return base.astimezone(tz)


def utcnow() -> _dt.datetime:
    if _frozen is None:
        return _dt.datetime.utcnow()
    base = _frozen if _frozen.tzinfo is not None else _frozen.replace(tzinfo=_dt.timezone.utc)
    return base.astimezone(_dt.timezone.utc).replace(tzinfo=None)


def freeze(when) -> None:
    """Pin the clock for tests. A date freezes at noon local; a datetime as given."""
    global _frozen
    if when is None:
        _frozen = None
    elif isinstance(when, _dt.datetime):
        _frozen = when
    else:
        _frozen = _dt.datetime(when.year, when.month, when.day, 12, 0, 0)


def unfreeze() -> None:
    freeze(None)
