"""One school recognizer for the route loader and the chooser.

A stop is a school when the place name says so, not when a street happens to
contain a school word. Both schools on a day stay schools. "Academy Avenue"
and "Charter Park Drive" are not schools. A swim academy is not a school day.
"""
from __future__ import annotations

import re

_PLACE = re.compile(
    r"\b("
    r"schools?|"
    r"elementary|elem|"
    r"middle\s+school|high\s+school|junior\s+high|jr\.?\s+high|"
    r"preparatory|montessori"
    r")\b",
    re.IGNORECASE,
)
# Campus named "Encinal High" with no "school" word, before the street clause.
_CAMPUS_HEAD = re.compile(
    r"\b(high|middle)\b(?!\s+(st|street|rd|road|ave|avenue|blvd|boulevard|way|dr|drive|ln|lane|ct|court|freeway|fwy)\b)",
    re.IGNORECASE,
)


def is_school_address(address: object) -> bool:
    """True when this address is a school campus."""
    if not isinstance(address, str) or not address.strip():
        return False
    if _PLACE.search(address):
        return True
    head = address.split(",")[0]
    return bool(_CAMPUS_HEAD.search(head))
