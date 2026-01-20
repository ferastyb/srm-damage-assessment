# -----------------------------
# ATA inference rules (tightened)
# - Structure-first (wing/fuselage/doors/etc)
# - Scored keyword matching
# - Hard guardrails: if "wing" => ATA 57, if "fuselage" => ATA 53, etc.
# -----------------------------
ATA_KEYWORDS: List[Tuple[int, List[str]]] = [
    # Structures (highest priority)
    (57, ["wing", "wings", "wing skin", "wingbox", "wing box", "aileron", "flap", "flaps", "slat", "slats", "spoiler", "spoilers", "winglet", "wingtip", "wing tip", "leading edge", "trailing edge"]),
    (55, ["stabilizer", "stabilizers", "horizontal stabilizer", "vertical stabilizer", "fin", "tailplane", "tail plane"]),
    (53, ["fuselage", "cabin", "crown", "belly", "pressure bulkhead", "bulkhead", "frame", "frames", "longeron"]),
    (52, ["door", "doors", "entry door", "cargo door", "service door"]),
    (56, ["window", "windows", "windshield", "windscreen"]),
    (32, ["landing gear", "gear", "mlg", "nlg", "strut", "shock strut", "wheel", "brake", "bogie"]),
    (54, ["nacelle", "pylon", "engine pylon", "thrust reverser"]),
    # Systems / weaker hints (lower priority)
    (27, ["flight control", "elevator", "rudder", "trim"]),
    (28, ["fuel", "tank", "fuel tank"]),
    (29, ["hydraulic", "hydraulics"]),
    (30, ["ice", "anti-ice", "anti ice", "deice", "de-ice"]),
    (24, ["electrical", "power", "generator", "battery"]),
]


def infer_ata_from_text(desc: str, structure_hint: Optional[str] = None) -> Optional[int]:
    """
    Tight ATA inference:
      1) If structure_hint is present (from structured fields), use it as primary.
      2) Otherwise score keyword hits (phrase hits > single word hits).
      3) Apply hard guardrails (wing => 57, fuselage => 53, door => 52).
    """
    s = (desc or "").lower()

    # 1) Structure hint overrides (when user selected Structure field)
    if structure_hint:
        sh = structure_hint.strip().upper()
        if sh == "WING":
            return 57
        if sh == "FUSELAGE":
            return 53
        if sh == "DOOR":
            return 52
        if sh in {"EMPENNAGE", "TAIL"}:
            return 55

    # 2) Hard guardrails directly from text (before scoring)
    if re.search(r"\bwing(s)?\b", s) or any(k in s for k in ["aileron", "flap", "slat", "spoiler", "winglet", "wingtip", "leading edge", "trailing edge"]):
        return 57
    if re.search(r"\bfuselage\b", s) or any(k in s for k in ["crown", "belly", "pressure bulkhead", "bulkhead"]):
        return 53
    if re.search(r"\bdoor(s)?\b", s) or any(k in s for k in ["entry door", "cargo door", "service door"]):
        return 52

    # 3) Scored keyword matching
    # Phrase hits count more than single-token hits
    scores: Dict[int, int] = {}
    for ata, kws in ATA_KEYWORDS:
        score = 0
        for kw in kws:
            if " " in kw:
                if kw in s:
                    score += 5
            else:
                if re.search(rf"\b{re.escape(kw)}\b", s):
                    score += 2
        if score > 0:
            scores[ata] = scores.get(ata, 0) + score

    if not scores:
        return None

    # Pick max score; if tie, pick lower chapter number (more "structural" chapters are usually lower among 5x)
    best_score = max(scores.values())
    best = [ata for ata, sc in scores.items() if sc == best_score]
    return sorted(best)[0]
