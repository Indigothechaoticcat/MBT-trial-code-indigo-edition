import json
import math

from django.utils import timezone

from routes.models import routeStop


# ---------------------------------------------------------------------------
# Coordinate helpers
# ---------------------------------------------------------------------------

def get_snapped_coords(rs):
    """
    Parse rs.snapped_route (JSON text) and return a list of (lat, lng) tuples.
    The DB stores coordinates as [[lng, lat], ...] so the pair is flipped.
    Returns None if the field is empty or unparseable.
    """
    if not rs.snapped_route:
        return None

    try:
        data = json.loads(rs.snapped_route)
    except (json.JSONDecodeError, TypeError):
        return None

    coords = []
    for pair in data:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            continue
        try:
            lng, lat = pair
            coords.append((float(lat), float(lng)))
        except (TypeError, ValueError):
            continue

    return coords if coords else None


def calculate_heading(lat1, lng1, lat2, lng2):
    """
    Return a bearing in degrees (0–360) using the great-circle formula.
    0 = North, 90 = East, 180 = South, 270 = West.
    Returns 0.0 when the two points are effectively identical.
    """
    if abs(lat1 - lat2) < 1e-9 and abs(lng1 - lng2) < 1e-9:
        return 0.0

    lat1_r = math.radians(lat1)
    lat2_r = math.radians(lat2)
    d_lng = math.radians(lng2 - lng1)

    x = math.sin(d_lng) * math.cos(lat2_r)
    y = (
        math.cos(lat1_r) * math.sin(lat2_r)
        - math.sin(lat1_r) * math.cos(lat2_r) * math.cos(d_lng)
    )

    return (math.degrees(math.atan2(x, y)) + 360) % 360


# ---------------------------------------------------------------------------
# Route coordinate extraction
# ---------------------------------------------------------------------------

def _parse_stop_coords(stop):
    """
    Try every known coordinate key layout for a stop dict.
    Returns (lat, lng) floats or raises ValueError / TypeError.
    """
    # Combined "lat,lng" string
    cords = stop.get("cords") or stop.get("coords")
    if cords:
        lat_str, lng_str = cords.split(",")
        return float(lat_str.strip()), float(lng_str.strip())

    # Separate fields
    lat = stop.get("lat") or stop.get("latitude")
    lng = stop.get("lng") or stop.get("longitude") or stop.get("long")
    if lat is not None and lng is not None:
        return float(lat), float(lng)

    raise ValueError("No coordinate fields found")


def extract_coords_and_last_stop(rs):
    """
    Extract a list of (lat, lng) tuples and the name of the last stop
    from a routeStop's stops list.
    Falls back to snapped_route when available.
    Returns (coords, last_stop_name).
    """
    snapped = get_snapped_coords(rs)
    if snapped:
        return snapped, None

    coords = []
    last_stop_name = None

    if not rs.stops or not isinstance(rs.stops, list):
        return coords, None

    for stop in rs.stops:
        if not isinstance(stop, dict):
            continue

        sname = stop.get("stop") or stop.get("name") or stop.get("title")
        if sname:
            last_stop_name = sname

        try:
            lat, lng = _parse_stop_coords(stop)
            coords.append((lat, lng))
        except (TypeError, ValueError, AttributeError):
            continue

    return coords, last_stop_name


def extract_coords_from_routeStop(rs):
    """Return just the coordinate list for a routeStop (snapped preferred)."""
    snapped = get_snapped_coords(rs)
    if snapped:
        return snapped
    coords, _ = extract_coords_and_last_stop(rs)
    return coords or []


def get_route_coordinates(route_id, trip):
    """
    Determine which routeStop direction to use and return its coordinates.

    Priority:
    1. trip.trip_inbound is False → outbound direction (second routeStop)
    2. trip.trip_inbound is True  → inbound direction (first routeStop)
    3. trip.trip_inbound is None  → auto-detect by matching trip_end_location
       against each direction's last stop name, falling back to the first.
    """
    stops_qs = routeStop.objects.filter(route_id=route_id).order_by("id")
    count = stops_qs.count()

    if not count:
        return []

    if trip.trip_inbound is False:
        rs = stops_qs[1] if count >= 2 else stops_qs[0]
        return extract_coords_from_routeStop(rs)

    if trip.trip_inbound is True:
        return extract_coords_from_routeStop(stops_qs[0])

    # Auto-detect: collect all directions and match on end location
    candidates = []
    for rs in stops_qs:
        coords, last_stop = extract_coords_and_last_stop(rs)
        if coords:
            candidates.append({"coords": coords, "last_stop": last_stop})

    if not candidates:
        return []

    trip_end = (trip.trip_end_location or "").lower().strip()
    for candidate in candidates:
        last = (candidate["last_stop"] or "").lower().strip()
        if trip_end and last and trip_end in last:
            return candidate["coords"]

    return candidates[0]["coords"]


# ---------------------------------------------------------------------------
# Progress + interpolation
# ---------------------------------------------------------------------------

def get_progress(trip):
    """
    Return a float in [0.0, 1.0] representing how far through a trip we are.
    Returns 0.0 if the trip hasn't started, 1.0 if it has ended.
    """
    now = timezone.now()
    start = trip.trip_start_at
    end = trip.trip_end_at
    duration = (end - start).total_seconds()

    if duration <= 0:
        return 0.0

    elapsed = (now - start).total_seconds()
    return max(0.0, min(1.0, elapsed / duration))


def interpolate(coords, progress):
    """
    Given a list of (lat, lng) waypoints and a progress in [0, 1],
    return (lat, lng, segment_index) for the interpolated position.
    Returns (None, None, None) for an empty coordinate list.
    """
    if not coords:
        return None, None, None

    if len(coords) == 1:
        return coords[0][0], coords[0][1], 0

    total_segments = len(coords) - 1
    segment_float = progress * total_segments
    seg_index = int(segment_float)

    if seg_index >= total_segments:
        return coords[-1][0], coords[-1][1], total_segments - 1

    seg_progress = segment_float - seg_index
    lat1, lng1 = coords[seg_index]
    lat2, lng2 = coords[seg_index + 1]

    lat = lat1 + (lat2 - lat1) * seg_progress
    lng = lng1 + (lng2 - lng1) * seg_progress

    return lat, lng, seg_index
