"""
Management command: detect_dutytrip_directions

Infers the inbound/outbound direction for every dutyTrip record that has a
route_link, and saves the result into dutyTrip.direction.

Three detection strategies are tried in order:
  1. Match start/end text against the route's inbound/outbound destination fields.
  2. Match against the first/last stops of each routeStop direction.
  3. Count fuzzy token matches across all stops for each direction.

If none of the strategies can decide, the direction is left as None (ambiguous).

Usage:
    python manage.py detect_dutytrip_directions
    python manage.py detect_dutytrip_directions --limit 100
    python manage.py detect_dutytrip_directions --batch-size 500 --force
"""

import json
import re
from math import ceil

from django.core.management.base import BaseCommand
from django.db import transaction

from routes.models import dutyTrip, routeStop


# ---------------------------------------------------------------------------
# Text normalisation
# ---------------------------------------------------------------------------

_NOISE_WORDS = re.compile(
    r"\b(stand|bay|platform|stop|adjacent|opposite|near|outside|"
    r"outside of|next to|by|at|opp|adj)\b"
)
_PUNCTUATION = re.compile(r"[^\w\s]")
_STANDALONE_DIGITS = re.compile(r"\b\d+\b")
_PARENTHETICAL = re.compile(r"\(.*?\)")


def normalize_location(text):
    """
    Lowercase, strip parentheticals, punctuation, noise words, and lone digits.
    Returns "" for falsy input.
    """
    if not text:
        return ""
    text = str(text)
    text = _PARENTHETICAL.sub(" ", text)
    text = text.lower()
    text = _PUNCTUATION.sub(" ", text)
    text = _NOISE_WORDS.sub(" ", text)
    text = _STANDALONE_DIGITS.sub(" ", text)
    return " ".join(text.split())


def token_overlap_match(a, b, min_common_tokens=1):
    """
    Return True if the two location strings share enough tokens to be
    considered a match.  Substring containment is treated as an automatic hit.
    """
    if not a or not b:
        return False

    a_n = normalize_location(a)
    b_n = normalize_location(b)

    if not a_n or not b_n:
        return False

    if a_n in b_n or b_n in a_n:
        return True

    a_toks = set(a_n.split())
    b_toks = set(b_n.split())

    if not a_toks or not b_toks:
        return False

    common = a_toks & b_toks
    threshold = max(min_common_tokens, ceil(min(len(a_toks), len(b_toks)) / 2))
    return len(common) >= threshold


def fuzzy_match(a, b):
    return token_overlap_match(a, b, min_common_tokens=1)


# ---------------------------------------------------------------------------
# Stop name extraction
# ---------------------------------------------------------------------------

def extract_stop_names(stops_json):
    """
    Return a list of stop name strings from a routeStop.stops value (dict list
    or JSON string).  Falls back to a regex scan on malformed input.
    """
    if not stops_json:
        return []

    try:
        data = json.loads(stops_json) if isinstance(stops_json, str) else stops_json
    except (json.JSONDecodeError, TypeError):
        # Crude regex fallback for mangled JSON
        try:
            return re.findall(r'"stop"\s*:\s*"([^"]+)"', json.dumps(stops_json))
        except Exception:
            return []

    if not isinstance(data, list):
        return []

    names = []
    for item in data:
        if isinstance(item, dict):
            name = item.get("stop") or item.get("name") or item.get("stop_name")
            if name:
                names.append(str(name))
        elif item:
            names.append(str(item))

    return names


# ---------------------------------------------------------------------------
# Direction detection
# ---------------------------------------------------------------------------

def _first_last_stops(rstop_qs, inbound_flag):
    """
    Return (first_stop_name, last_stop_name) for the given direction, or
    (None, None) if no usable routeStop is found.
    """
    rs = (
        rstop_qs.filter(inbound=inbound_flag).first()
        or rstop_qs.first()
    )
    if not rs:
        return None, None

    stops = extract_stop_names(rs.stops)
    if not stops:
        return None, None

    return stops[0], stops[-1]


def _matches_endpoints(start, end, first, last):
    """
    True if (start ≈ first AND end ≈ last) OR (start ≈ last AND end ≈ first).
    Returns False if either first or last is missing.
    """
    if not first or not last:
        return False
    return (
        (fuzzy_match(start, first) and fuzzy_match(end, last))
        or (fuzzy_match(start, last) and fuzzy_match(end, first))
    )


def _count_matches(text, stop_names):
    """Count how many names in stop_names fuzzy-match text."""
    if not text or not stop_names:
        return 0
    return sum(1 for s in stop_names if fuzzy_match(text, s))


def detect_direction(duty_trip):
    """
    Infer trip direction for a dutyTrip instance.
    Returns True (inbound), False (outbound), or None (ambiguous).
    """
    if not getattr(duty_trip, "route_link", None):
        return None

    route_obj = duty_trip.route_link
    start = duty_trip.start_at or ""
    end = duty_trip.end_at or ""

    rstop_qs = routeStop.objects.filter(route=route_obj)

    # --- Strategy 1: route destination fields ---
    inbound_dest = route_obj.inbound_destination or ""
    outbound_dest = route_obj.outbound_destination or ""

    if start and end and inbound_dest and outbound_dest:
        is_inbound = fuzzy_match(start, outbound_dest) and fuzzy_match(end, inbound_dest)
        is_outbound = fuzzy_match(start, inbound_dest) and fuzzy_match(end, outbound_dest)
        if is_inbound and not is_outbound:
            return True
        if is_outbound and not is_inbound:
            return False

    # --- Strategy 2: first/last stop of each direction ---
    in_first, in_last = _first_last_stops(rstop_qs, True)
    out_first, out_last = _first_last_stops(rstop_qs, False)

    in_match = _matches_endpoints(start, end, in_first, in_last)
    out_match = _matches_endpoints(start, end, out_first, out_last)

    if in_match and not out_match:
        return True
    if out_match and not in_match:
        return False

    # --- Strategy 3: bulk token match across all stops ---
    inbound_stops, outbound_stops = [], []
    for rs in rstop_qs:
        names = extract_stop_names(rs.stops)
        (inbound_stops if rs.inbound else outbound_stops).extend(names)

    in_score = _count_matches(start, inbound_stops) + _count_matches(end, inbound_stops)
    out_score = _count_matches(start, outbound_stops) + _count_matches(end, outbound_stops)

    if in_score > out_score:
        return True
    if out_score > in_score:
        return False

    return None  # ambiguous


# ---------------------------------------------------------------------------
# Management command
# ---------------------------------------------------------------------------

class Command(BaseCommand):
    help = "Detect inbound/outbound direction for dutyTrip rows"

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit", type=int, default=None,
            help="Cap the number of dutyTrip rows processed (useful for testing).",
        )
        parser.add_argument(
            "--batch-size", type=int, default=200,
            help="Number of rows saved per DB transaction (default: 200).",
        )
        parser.add_argument(
            "--force", action="store_true",
            help="Re-detect even when a direction is already set.",
        )

    def handle(self, *args, **options):
        limit = options["limit"]
        batch_size = options["batch_size"]
        force = options["force"]

        qs = dutyTrip.objects.select_related("route_link").order_by("id")
        total = qs.count()
        self.stdout.write(f"Found {total} dutyTrip rows.")

        if limit:
            qs = qs[:limit]
            self.stdout.write(f"Processing first {limit} rows.")

        processed = updated = ambiguous = skipped = failed = 0
        buffer = []

        for dt in qs:
            processed += 1

            if not getattr(dt, "route_link", None):
                skipped += 1
                continue

            if getattr(dt, "direction", None) is not None and not force:
                continue

            try:
                result = detect_direction(dt)
            except Exception as exc:
                self.stderr.write(f"Error on dutyTrip id={dt.id}: {exc}")
                failed += 1
                continue

            # Assign to the correct field (direction or inbound — use whichever your model has)
            if result is None:
                ambiguous += 1
                dt.direction = None
            else:
                dt.direction = result
                updated += 1

            buffer.append(dt)

            if len(buffer) >= batch_size:
                failed += self._flush(buffer)
                buffer = []

        if buffer:
            failed += self._flush(buffer)

        self.stdout.write("---- Summary ----")
        self.stdout.write(f"Processed : {processed}")
        self.stdout.write(f"Updated   : {updated}")
        self.stdout.write(f"Ambiguous : {ambiguous}")
        self.stdout.write(f"Skipped   : {skipped}  (no route_link)")
        self.stdout.write(f"Failed    : {failed}")

    def _flush(self, buffer):
        """
        Bulk-save a buffer of dutyTrip instances.
        Returns the number of records that failed to save.
        """
        try:
            with transaction.atomic():
                dutyTrip.objects.bulk_update(buffer, ["direction"])
            buffer.clear()
            return 0
        except Exception as exc:
            self.stderr.write(f"Batch save failed: {exc}")
            count = len(buffer)
            buffer.clear()
            return count
