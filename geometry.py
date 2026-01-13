# geometry.py
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Optional, Tuple

# -------------------------
# Canonical geometry types
# -------------------------

@dataclass(frozen=True)
class StringerRef:
    """
    Normalized stringer reference.
    We map L to negative, R to positive, numeric part to int.
    Examples:
      "S-10L" -> index = -10
      "S-4R"  -> index = +4
      "24L"   -> -24 (some SRMs omit S- prefix)
      "5R"    -> +5
    """
    raw: str
    index: int  # signed

    @staticmethod
    def parse(s: str) -> Optional["StringerRef"]:
        if not s:
            return None
        t = s.strip().upper()

        # Accept: S-10L, S10L, 10L, 24R, 4R-5R (range handled elsewhere)
        # Grab numeric and side (L/R)
        m = re.search(r"(\d{1,3})\s*([LR])\b", t)
        if not m:
            return None

        n = int(m.group(1))
        side = m.group(2)
        idx = -n if side == "L" else n
        return StringerRef(raw=s, index=idx)

    def __str__(self) -> str:
        n = abs(self.index)
        side = "L" if self.index < 0 else "R"
        return f"{n}{side}"


@dataclass(frozen=True)
class Range1D:
    """
    Generic 1D numeric range. Inclusive endpoints by default.
    """
    lo: float
    hi: float
    inclusive: bool = True

    def contains(self, x: Optional[float]) -> bool:
        if x is None:
            return False
        if self.inclusive:
            return self.lo <= x <= self.hi
        return self.lo < x < self.hi


@dataclass(frozen=True)
class StringerRange:
    """
    Stringer range expressed as two stringer refs, inclusive.
    Note: L-to-R ranges cross zero, and inclusive check still works
          because we compare signed indices.
    Example: 24L–24R => [-24, +24]
             4R–5R   => [+4, +5]
    """
    lo: StringerRef
    hi: StringerRef

    def contains(self, s: Optional[StringerRef]) -> bool:
        if s is None:
            return False
        a = min(self.lo.index, self.hi.index)
        b = max(self.lo.index, self.hi.index)
        return a <= s.index <= b


@dataclass(frozen=True)
class DentContext:
    aircraft_family: str              # "B737", "B787", "A320", "E175"
    structure: str                    # "fuselage", "wing", etc.
    side: Optional[str]               # "LH"/"RH" or None
    sta: Optional[float]              # station position
    stringer: Optional[StringerRef]   # e.g. S-10L
    section: Optional[int]            # e.g. 43, 46, 48 (if known)
    zone: Optional[str]               # e.g. "Pressurized Crown"
    distance_from_cutout_in: Optional[float]
    distance_from_splice_in: Optional[float]


# -------------------------
# Parsing helpers
# -------------------------

def parse_station(text: str) -> Optional[float]:
    """
    Extract a single STA number (e.g., 'STA 1280' -> 1280).
    """
    if not text:
        return None
    m = re.search(r"\bSTA(?:TION)?\s*([0-9]+(?:\.[0-9]+)?)\b", text.upper())
    return float(m.group(1)) if m else None


def parse_stringer(text: str) -> Optional[StringerRef]:
    """
    Extract a stringer like 'S-10L' or '24R' from a blob.
    """
    if not text:
        return None
    # First try explicit S-##L/R
    m = re.search(r"\bS[-\s]*([0-9]{1,3})\s*([LR])\b", text.upper())
    if m:
        return StringerRef.parse(f"{m.group(1)}{m.group(2)}")
    # Fallback: plain "24L"
    m2 = re.search(r"\b([0-9]{1,3})\s*([LR])\b", text.upper())
    if m2:
        return StringerRef.parse(f"{m2.group(1)}{m2.group(2)}")
    return None


def parse_station_range(text: str) -> Optional[Range1D]:
    """
    Parse 'Stations 360-540' style.
    """
    if not text:
        return None
    t = text.upper().replace("–", "-")
    m = re.search(r"\bSTATIONS?\s*([0-9]+(?:\.[0-9]+)?)\s*-\s*([0-9]+(?:\.[0-9]+)?)\b", t)
    if not m:
        return None
    return Range1D(float(m.group(1)), float(m.group(2)), inclusive=True)


def parse_stringer_range(text: str) -> Optional[StringerRange]:
    """
    Parse 'Stringers 24L-24R' or '4R-5R' style.
    """
    if not text:
        return None
    t = text.upper().replace("–", "-")
    m = re.search(r"\bSTRINGERS?\s*([0-9]{1,3}\s*[LR])\s*-\s*([0-9]{1,3}\s*[LR])\b", t)
    if not m:
        return None
    lo = StringerRef.parse(m.group(1))
    hi = StringerRef.parse(m.group(2))
    if not lo or not hi:
        return None
    return StringerRange(lo=lo, hi=hi)
