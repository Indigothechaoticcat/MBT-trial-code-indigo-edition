"""
Management command: import_trips
Usage: python manage.py import_trips <path/to/trips.csv>

Expected CSV columns:
    TripID, TripDateTime, Vehicle_ID, RouteID (optional),
    RouteNumber (optional), EndDestination (optional), Missed (optional)
"""

import csv
from datetime import datetime

from django.core.management.base import BaseCommand, CommandError
from django.utils.timezone import make_aware

from fleet.models import fleet
from routes.models import route
from tracking.models import Trip

# Sentinel values treated as "no data"
_NULL_VALUES = {"", "NULL", "N/A", "NONE"}


def _is_null(value):
    return str(value).strip().upper() in _NULL_VALUES


def _parse_row(row, stderr_write):
    """
    Validate and parse a single CSV row into a dict of Trip field values.
    Returns the dict on success, or None to skip the row.
    """
    # --- TripID ---
    trip_id = row.get("TripID", "").strip().upper()
    if _is_null(trip_id):
        stderr_write("Skipped row: empty or null TripID")
        return None

    # --- Datetime ---
    raw_dt = row.get("TripDateTime", "").strip()
    try:
        start_time = make_aware(datetime.strptime(raw_dt, "%Y-%m-%d %H:%M:%S"))
    except ValueError:
        stderr_write(f"Skipped TripID={trip_id}: invalid TripDateTime '{raw_dt}'")
        return None

    # --- Vehicle ---
    raw_vehicle = row.get("Vehicle_ID", "").strip()
    if _is_null(raw_vehicle):
        stderr_write(f"Skipped TripID={trip_id}: missing Vehicle_ID")
        return None
    try:
        vehicle = fleet.objects.get(id=int(raw_vehicle))
    except (fleet.DoesNotExist, ValueError):
        stderr_write(f"Skipped TripID={trip_id}: vehicle not found (Vehicle_ID={raw_vehicle})")
        return None

    # --- Optional route ---
    route_obj = None
    raw_route = row.get("RouteID", "").strip().upper()
    if not _is_null(raw_route):
        try:
            route_obj = route.objects.get(id=int(raw_route))
        except (route.DoesNotExist, ValueError):
            stderr_write(f"Warning TripID={trip_id}: route not found (RouteID={raw_route}), continuing without route")

    # --- Missed flag ---
    # A non-null Missed value means the trip was missed; treat it as ended.
    missed_raw = row.get("Missed", "")
    trip_ended = not _is_null(missed_raw)

    return {
        "trip_id": trip_id,
        "vehicle": vehicle,
        "route_obj": route_obj,
        "route_num": row.get("RouteNumber") or None,
        "end_location": row.get("EndDestination") or None,
        "start_time": start_time,
        "trip_ended": trip_ended,
    }


class Command(BaseCommand):
    help = "Import Trip records from a CSV file"

    def add_arguments(self, parser):
        parser.add_argument("csv_file", type=str, help="Path to the trips CSV file")

    def handle(self, *args, **options):
        file_path = options["csv_file"]

        try:
            csv_file = open(file_path, newline="", encoding="utf-8")
        except FileNotFoundError:
            raise CommandError(f"File not found: {file_path}")

        created = skipped_existing = skipped_invalid = 0

        with csv_file:
            reader = csv.DictReader(csv_file)

            for row in reader:
                parsed = _parse_row(row, self.stderr.write)
                if parsed is None:
                    skipped_invalid += 1
                    continue

                trip_id = parsed["trip_id"]

                if Trip.objects.filter(trip_display_id=trip_id).exists():
                    self.stdout.write(self.style.WARNING(f"Skipped existing TripID: {trip_id}"))
                    skipped_existing += 1
                    continue

                Trip.objects.create(
                    trip_display_id=trip_id,
                    trip_vehicle=parsed["vehicle"],
                    trip_route=parsed["route_obj"],
                    trip_route_num=parsed["route_num"],
                    trip_end_location=parsed["end_location"],
                    trip_start_at=parsed["start_time"],
                    trip_ended=parsed["trip_ended"],
                )
                self.stdout.write(self.style.SUCCESS(f"Created Trip: {trip_id}"))
                created += 1

        self.stdout.write("---- Summary ----")
        self.stdout.write(f"Created : {created}")
        self.stdout.write(f"Skipped (already existed) : {skipped_existing}")
        self.stdout.write(f"Skipped (invalid/missing data): {skipped_invalid}")
