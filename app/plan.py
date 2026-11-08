"""Ordnet geplante Einheiten den tatsächlich absolvierten Aktivitäten zu."""
import datetime as dt
import re

SPORTS = ("swim", "bike", "run", "brick", "strength", "rest", "other")


_WORDS = {
    "swim": r"schwimm\w*|swim\w*|freiwasser|kraulen",
    "bike": r"rad|radfahren|radtour|radausfahrt|ausfahrt|rennrad|kickr|zwift|ftp|bike|rolle|sweetspot",
    "run": r"lauf|laufen|laufband|koppel|koppellauf|dauerlauf|joggen|jogging|run|intervalllauf",
    "strength": r"kraft\w*|gym|oberkörper|unterkörper|beine|krafttraining|yoga|mobility|stabi",
    "other": r"tennis|padel|paddel|paddelball|wandern|tc|training|sport",
}
# "Z2-Lauf" ist ein Lauf, "Firmen-Training" aber kein Sport: Nur die allgemeinen Wörter dürfen nicht hinter einem Bindestrich stehen.
_MATCH = {sport: re.compile(rf"(?<![\w{'-' if sport == 'other' else ''}])(?:{words})(?!\w)", re.I) for sport, words in _WORDS.items()}


def classify_title(title: str, extra: list[str] = (), ignore: list[str] = ()) -> str | None:
    """Erkennt Trainings an ihrem Titel und gibt die Sportart zurück, sonst None."""
    lower = title.lower()
    if any(term.lower() in lower for term in ignore if term.strip()):
        return None
    if _MATCH["swim"].search(title):
        return "swim"
    bike = _MATCH["bike"].search(title)
    run = _MATCH["run"].search(title)
    if bike and run:
        return "brick"
    if bike:
        return "bike"
    if run:
        return "run"
    if _MATCH["strength"].search(title):
        return "strength"
    if _MATCH["other"].search(title) or any(term.lower() in lower for term in extra if term.strip()):
        return "other"
    return None


def normalize_sport(type_key: str | None) -> str:
    key = (type_key or "").lower()
    if "swim" in key:
        return "swim"
    if any(s in key for s in ("cycling", "biking", "bike", "ride", "gravel")):
        return "bike"
    if "run" in key:
        return "run"
    if any(s in key for s in ("strength", "fitness_equipment", "yoga", "pilates")):
        return "strength"
    return "other"


def match(planned: list[dict], activities: list[dict], today: str) -> dict:
    """Gibt {plan_id: {"status": ..., "activity_ids": [...]}} zurück.

    Pro Plan wird die Aktivität mit passender Sportart am selben Tag gewählt,
    deren Dauer der geplanten am nächsten kommt. Eine Koppeleinheit (brick)
    braucht Rad und Lauf; jede Aktivität zählt nur für einen Plan.
    """
    used: set[int] = set()
    out = {}
    for p in sorted(planned, key=lambda x: (x["date"], x["id"])):
        if p["sport"] == "rest":
            out[p["id"]] = {"status": "rest", "activity_ids": []}
            continue
        wanted = ("bike", "run") if p["sport"] == "brick" else (p["sport"],)
        found = []
        for sport in wanted:
            options = [
                a for a in activities
                if a["date"] == p["date"] and a["sport"] == sport and a["id"] not in used
            ]
            if options:
                target = (p.get("duration_min") or 0) * 60 / len(wanted)
                best = min(options, key=lambda a: abs((a.get("duration_s") or 0) - target))
                used.add(best["id"])
                found.append(best["id"])
        if len(found) == len(wanted):
            status = "done"
        elif found:
            status = "partial"
        elif p["date"] < today:
            # Aus Google ohne Garmin-Daten: eingetragen heißt hier trainiert, nur ohne Messwerte.
            status = "calendar" if p.get("google_uid") else "missed"
        else:
            status = "open"
        out[p["id"]] = {"status": status, "activity_ids": found}
    return out


def monday(day: dt.date) -> dt.date:
    return day - dt.timedelta(days=day.weekday())
