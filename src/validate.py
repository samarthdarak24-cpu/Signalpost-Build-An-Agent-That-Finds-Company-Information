"""Modulus-11 validation for Norwegian organisation numbers.

An organisation number is nine digits. The first eight are the payload, the
ninth is a check digit computed with the fixed weight vector 3, 2, 7, 6, 5, 4,
3, 2. The check digit is ``11 - (sum mod 11)``; a result of 11 maps to 0 and a
result of 10 is invalid (it cannot be represented in a single digit).

This module is deliberately dependency-free so the validator can be unit tested
and reused by the ownership gate without dragging in the network stack.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "CHECK_WEIGHTS",
    "OrgNumber",
    "clean",
    "check_digit",
    "is_valid",
    "validate",
    "format_display",
    "harvest_from_text",
]

#: Weights applied to the first eight digits.
CHECK_WEIGHTS: tuple[int, ...] = (3, 2, 7, 6, 5, 4, 3, 2)

_DIGITS = re.compile(r"^\d{9}$")
#: Anything that is not a digit is a separator: spaces, dots, hyphens, slashes.
_SEPARATORS = re.compile(r"[\s.\-/\u00a0\u2013\u2014]+")
#: Markers that are not part of the number itself, in the languages they turn up.
#: ``NO 986 587 098 MVA`` is the VAT-registration form used on Norwegian invoices.
_SUFFIX = re.compile(
    r"(?i)\s*(mva|asa|ans|da|sa|as|org\.?\s*nr\.?|organisasjonsnummer|vat|no)\s*$"
)
_LEADING = re.compile(r"(?i)^\s*(no|org\.?\s*nr\.?|organisasjonsnummer|vat)\s*[:\-]?\s*")


@dataclass(frozen=True, slots=True)
class OrgNumber:
    """A validated organisation number.

    Instances of this class are proof that :func:`validate` succeeded, so the
    rest of the pipeline can accept ``OrgNumber`` instead of re-checking strings.
    """

    value: str  #: zero-padded nine digit string, e.g. ``"986587098"``

    @property
    def display(self) -> str:
        """``986 587 098`` — the form used by the register and on invoices."""
        return format_display(self.value)

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


def clean(raw: str | int | None) -> str | None:
    """Strip noise, returning nine digits or ``None``.

    Handles the forms these numbers actually arrive in: ``"986 587 098"``,
    ``"986.587.098"``, ``"NO 986 587 098 MVA"``, ``"Org.nr: 986587098"`` and the
    bare integer. Returns ``None`` when the result is not exactly nine digits, so
    callers can branch on truthiness.
    """
    if raw is None:
        return None
    if isinstance(raw, int):
        text = f"{raw:09d}"
    else:
        text = str(raw).strip()
        text = _LEADING.sub("", text)
        text = _SUFFIX.sub("", text)
        text = _SEPARATORS.sub("", text)
    if not _DIGITS.match(text):
        return None
    return text


def check_digit(first_eight: str) -> int | None:
    """Return the expected check digit for eight payload digits.

    ``None`` means the payload is not a valid organisation number at all: the
    weighted sum produced a remainder of 1, which would require a check digit of
    10 — a value no single digit can carry.
    """
    if len(first_eight) != 8 or not first_eight.isdigit():
        return None
    total = sum(int(d) * w for d, w in zip(first_eight, CHECK_WEIGHTS))
    remainder = total % 11
    if remainder == 0:
        return 0
    candidate = 11 - remainder
    if candidate == 10:
        return None
    return candidate


def is_valid(raw: str | int | None) -> bool:
    """True when ``raw`` is a syntactically valid Norwegian organisation number."""
    return validate(raw) is not None


def validate(raw: str | int | None) -> OrgNumber | None:
    """Validate ``raw`` and return an :class:`OrgNumber`, or ``None``.

    Usage::

        >>> validate("986 587 098")
        OrgNumber(value='986587098')
        >>> validate("986587099") is None
        True
    """
    digits = clean(raw)
    if digits is None:
        return None
    expected = check_digit(digits[:8])
    if expected is None or expected != int(digits[8]):
        return None
    return OrgNumber(digits)


def format_display(digits: str) -> str:
    """Space-group the number in the register's own style."""
    return f"{digits[0:3]} {digits[3:6]} {digits[6:9]}"


def harvest_from_text(text: str) -> set[str]:
    """Find every *checksum-valid* organisation number inside a block of text.

    This is the workhorse behind the ownership gate. It is intentionally strict:
    a nine digit run such as a phone number or a postal code will almost never
    satisfy modulus-11, so requiring a valid checksum collapses the false
    positive rate to nearly zero.

    Separators are removed before matching, so ``986 587 098``, ``986.587.098``
    and ``NO 986 587 098 MVA`` are all discovered.
    """
    if not text:
        return set()
    flat = _SEPARATORS.sub("", text)
    found: set[str] = set()
    # Scan every nine digit window. Number runs longer than nine are rejected by
    # requiring a non-digit (or string edge) on both sides.
    for match in re.finditer(r"(?<!\d)(\d{9})(?!\d)", flat):
        candidate = match.group(1)
        if is_valid(candidate):
            found.add(candidate)
    return found
