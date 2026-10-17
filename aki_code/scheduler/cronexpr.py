from __future__ import annotations

from datetime import datetime

# Five-field cron: minute hour day-of-month month day-of-week.
# Supported tokens per field: * , */N , N , N-M , N-M/S , and comma lists.
# Day-of-week uses 0=Sunday .. 6=Saturday, and 7 is accepted as Sunday too.
_FIELD_RANGES = (
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day-of-month", 1, 31),
    ("month", 1, 12),
    ("day-of-week", 0, 7),
)


def _parse_field(field: str, name: str, lo: int, hi: int) -> set[int]:
    """Expand one cron field into the set of matching integer values."""
    if not field:
        raise ValueError(f"{name}: empty field")
    values: set[int] = set()
    for part in field.split(","):
        part = part.strip()
        if not part:
            raise ValueError(f"{name}: empty list element")
        step = 1
        if "/" in part:
            part, _, step_str = part.partition("/")
            if not step_str.isdigit() or int(step_str) < 1:
                raise ValueError(f"{name}: bad step '{step_str}'")
            step = int(step_str)
        if part == "*":
            start, end = lo, hi
        elif "-" in part:
            a, _, b = part.partition("-")
            start, end = _bound(a, name, lo, hi), _bound(b, name, lo, hi)
            if start > end:
                raise ValueError(f"{name}: range {start}-{end} is reversed")
        else:
            start = end = _bound(part, name, lo, hi)
        values.update(range(start, end + 1, step))
    return values


def _bound(token: str, name: str, lo: int, hi: int) -> int:
    if not token.isdigit():
        raise ValueError(f"{name}: '{token}' is not a number")
    value = int(token)
    if not (lo <= value <= hi):
        raise ValueError(f"{name}: {value} out of range {lo}-{hi}")
    return value


def parse_cron(expr: str) -> tuple[set[int], set[int], set[int], set[int], set[int]]:
    """Parse and validate a 5-field expression; return the five value sets."""
    fields = str(expr).split()
    if len(fields) != 5:
        raise ValueError(
            f"cron expression must have 5 fields (minute hour dom month dow), "
            f"got {len(fields)}: '{expr}'"
        )
    sets = []
    for field, (name, lo, hi) in zip(fields, _FIELD_RANGES):
        values = _parse_field(field, name, lo, hi)
        if name == "day-of-week" and 7 in values:
            values.add(0)  # 7 == Sunday == 0
        sets.append(values)
    return tuple(sets)  # type: ignore[return-value]


def validate_cron(expr: str) -> None:
    """Raise ValueError if the expression is not a valid 5-field cron."""
    parse_cron(expr)


def cron_matches(expr: str, moment: datetime) -> bool:
    """True when `moment` matches the expression (all five fields, AND-ed).

    Note: unlike Vixie cron, a restricted day-of-month and day-of-week are
    AND-ed, not OR-ed; the course grammar does not require the OR special case.
    """
    minutes, hours, doms, months, dows = parse_cron(expr)
    cron_dow = (moment.weekday() + 1) % 7  # Mon=0..Sun=6 -> Sun=0..Sat=6
    return (
        moment.minute in minutes
        and moment.hour in hours
        and moment.day in doms
        and moment.month in months
        and cron_dow in dows
    )
