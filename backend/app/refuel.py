"""Driver Coach §12 evening-refuel policy.

The policy deliberately returns application commands instead of performing I/O:
callers deliver each ``Reminder`` to the driver's existing banner/event channel and
save each ``MissEmailDraft`` in Felix's draft mailbox.  There is no SMTP, Gmail, or
mail-send code here, so evaluating this policy can never send a message.

A production adapter must persist the three idempotency keys (reminded, refueled,
drafted) before doing its own external work.  Keeping this domain policy pure makes
that adapter testable and prevents an automatic miss-email send by construction.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from zoneinfo import ZoneInfo


PACIFIC = ZoneInfo("America/Los_Angeles")
REFUEL_WEEKDAYS = frozenset({0, 2, 4, 5})  # Monday, Wednesday, Friday, Saturday
EVENING_START = time(18, 0)
MISS_DRAFT_AT = time(23, 59)


@dataclass(frozen=True)
class Reminder:
    """An unsent command for the driver's established app-event/banner seam."""

    truck_number: int
    service_date: date
    event: str = "refuel"


@dataclass(frozen=True)
class MissEmailDraft:
    """An email to save as a draft only; it is never an outbound message."""

    truck_number: int
    service_date: date
    to: str
    subject: str
    body: str
    sent: bool = False


def _pacific(now: datetime) -> datetime:
    """Normalize an explicit instant to Pacific time; reject ambiguous naive input."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("refuel policy requires a timezone-aware timestamp")
    return now.astimezone(PACIFIC)


def is_refuel_night(now: datetime) -> bool:
    """Whether the local calendar date is one of Felix's four refuel nights."""
    return _pacific(now).weekday() in REFUEL_WEEKDAYS


def is_refuel_evening(now: datetime) -> bool:
    """Whether a local instant is in the 6 PM--midnight reminder window."""
    local = _pacific(now)
    return local.weekday() in REFUEL_WEEKDAYS and local.timetz().replace(tzinfo=None) >= EVENING_START


@dataclass
class RefuelNightBook:
    """Idempotently decide which §12 commands are due for a single process/day.

    ``RefuelNightBook`` is intentionally side-effect free.  Its in-memory key sets are
    suitable for the scheduler's unit-tested decision core; the service adapter owns
    durable storage and must hydrate/save the same keys around each run.
    """

    felix_email: str = "felix@mistersofteenorcal.com"
    _reminded: set[tuple[date, int]] = field(default_factory=set, init=False, repr=False)
    _refueled: set[tuple[date, int]] = field(default_factory=set, init=False, repr=False)
    _drafted: set[tuple[date, int]] = field(default_factory=set, init=False, repr=False)

    @staticmethod
    def _key(local: datetime, truck_number: int) -> tuple[date, int]:
        if isinstance(truck_number, bool) or not isinstance(truck_number, int) or truck_number <= 0:
            raise ValueError("truck_number must be a positive integer")
        return (local.date(), truck_number)

    def due_reminders(self, truck_numbers: list[int], now: datetime) -> list[Reminder]:
        """Return one evening refuel command per eligible truck/date, once only."""
        if not is_refuel_evening(now):
            return []
        local = _pacific(now)
        due: list[Reminder] = []
        for truck_number in sorted(set(truck_numbers)):
            key = self._key(local, truck_number)
            if key in self._reminded:
                continue
            self._reminded.add(key)
            due.append(Reminder(truck_number=truck_number, service_date=local.date()))
        return due

    def mark_refueled(self, truck_number: int, now: datetime) -> bool:
        """Record a same-night refuel confirmation without producing external output.

        ``True`` means this is the first confirmation for that truck/date.  A
        confirmation on a non-refuel date is ignored so it cannot suppress a later
        required prompt or draft.
        """
        if not is_refuel_night(now):
            return False
        local = _pacific(now)
        key = self._key(local, truck_number)
        if key in self._refueled:
            return False
        self._refueled.add(key)
        return True

    def due_miss_drafts(self, now: datetime) -> list[MissEmailDraft]:
        """Return one unsent same-night draft for each asked-but-unconfirmed truck.

        Drafting begins at 11:59 PM Pacific.  The returned object carries no send
        operation, and ``sent`` is permanently ``False`` in this policy layer.
        """
        if not is_refuel_night(now):
            return []
        local = _pacific(now)
        if local.timetz().replace(tzinfo=None) < MISS_DRAFT_AT:
            return []

        service_date = local.date()
        drafts: list[MissEmailDraft] = []
        for key in sorted(self._reminded):
            day, truck_number = key
            if day != service_date or key in self._refueled or key in self._drafted:
                continue
            self._drafted.add(key)
            weekday = service_date.strftime("%a")
            display_date = service_date.strftime("%b %-d")
            subject = f"Driver Coach: truck {truck_number} did not confirm refuel — {weekday} {display_date}"
            body = (
                f"Truck {truck_number} was asked to refuel this evening but did not confirm by end of night.\n\n"
                "Please review with the driver.\n\n"
                "No email was sent automatically."
            )
            drafts.append(
                MissEmailDraft(
                    truck_number=truck_number,
                    service_date=service_date,
                    to=self.felix_email,
                    subject=subject,
                    body=body,
                )
            )
        return drafts
