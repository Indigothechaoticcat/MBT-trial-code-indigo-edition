"""
Management command: simulate_positions
Computes interpolated positions for all active trips and writes them
to the corresponding fleet.sim_lat / sim_lon / sim_heading fields.

Run periodically (e.g. every 30 s via cron or Celery beat).
"""

from django.core.management.base import BaseCommand
from django.utils import timezone

from fleet.models import fleet
from tracking.models import Trip
from tracking.utils import (
    calculate_heading,
    get_progress,
    get_route_coordinates,
    interpolate,
)

# How long after a trip ends before we clear its sim data (minutes)
_CLEAR_AFTER_MINUTES = 15


class Command(BaseCommand):
    help = "Simulate vehicle positions for all active trips"

    def handle(self, *args, **kwargs):
        now = timezone.now()
        self._clear_stale_positions(now)
        self._update_active_trips(now)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _clear_stale_positions(self, now):
        """Wipe sim data for vehicles whose trip ended more than _CLEAR_AFTER_MINUTES ago."""
        cutoff = now - timezone.timedelta(minutes=_CLEAR_AFTER_MINUTES)
        cleared = fleet.objects.filter(
            current_trip__trip_end_at__lt=cutoff
        ).update(
            sim_lat=None,
            sim_lon=None,
            sim_heading=None,
            current_trip=None,
            updated_at=now,
        )
        if cleared:
            self.stdout.write(f"Cleared stale positions for {cleared} vehicle(s).")

    def _update_active_trips(self, now):
        """Compute and save interpolated positions for every currently active trip."""
        active_trips = (
            Trip.objects
            .filter(
                trip_start_at__lte=now,
                trip_end_at__gte=now,
                trip_missed=False,
                trip_ended=False,
            )
            .select_related("trip_vehicle", "trip_vehicle__operator")
        )

        if not active_trips.exists():
            self.stdout.write("No active trips found.")
            return

        for trip in active_trips:
            self._process_trip(trip, now)

    def _process_trip(self, trip, now):
        vehicle = trip.trip_vehicle
        if not vehicle:
            return

        coords = get_route_coordinates(trip.trip_route_id, trip)
        if not coords:
            self.stdout.write(
                self.style.WARNING(f"Trip {trip.pk}: no route coordinates, skipping.")
            )
            return

        progress = get_progress(trip)

        if progress >= 1.0:
            lat, lng = coords[-1]
            heading = vehicle.sim_heading or 0
        else:
            lat, lng, seg_index = interpolate(coords, progress)

            # Choose the next point for heading calculation
            if seg_index >= len(coords) - 1:
                # At or past the last segment — look backward for heading
                prev_lat, prev_lng = coords[seg_index - 1] if seg_index > 0 else (lat, lng)
                heading = calculate_heading(prev_lat, prev_lng, lat, lng)
            else:
                next_lat, next_lng = coords[seg_index + 1]
                heading = calculate_heading(lat, lng, next_lat, next_lng)

        vehicle.sim_lat = lat
        vehicle.sim_lon = lng
        vehicle.sim_heading = heading
        vehicle.current_trip = trip
        vehicle.updated_at = now
        vehicle.save(update_fields=["sim_lat", "sim_lon", "sim_heading", "current_trip", "updated_at"])

        self.stdout.write(
            self.style.SUCCESS(
                f"Vehicle {vehicle.pk} → lat={lat:.6f}, lon={lng:.6f}, heading={heading:.1f}°"
            )
        )
