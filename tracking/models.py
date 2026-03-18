import json

from django.db import models
from django.utils import timezone
from django.core.exceptions import ValidationError
from datetime import timedelta
from simple_history.models import HistoricalRecords

from fleet.models import fleet
from routes.models import route, duty
from gameData.models import game
from main.models import CustomUser

MAX_HISTORY_ENTRIES = 500  # Cap inline history to prevent unbounded growth


def default_tracking_data():
    return {"X": 0, "Y": 0, "heading": 0}


def default_tracking_history():
    return []


class Trip(models.Model):
    trip_id = models.AutoField(primary_key=True)
    trip_display_id = models.CharField(max_length=255, null=True, blank=True)
    trip_vehicle = models.ForeignKey(fleet, on_delete=models.CASCADE)
    trip_route = models.ForeignKey(route, on_delete=models.CASCADE, null=True, blank=True)
    trip_route_num = models.CharField(max_length=255, null=True, blank=True)
    trip_driver = models.ForeignKey(CustomUser, on_delete=models.CASCADE, null=True, blank=True)
    trip_start_location = models.CharField(max_length=255, null=True, blank=True)
    trip_end_location = models.CharField(max_length=255, null=True, blank=True)
    trip_start_at = models.DateTimeField(null=True, blank=True)
    trip_end_at = models.DateTimeField(null=True, blank=True)
    trip_updated_at = models.DateTimeField(auto_now=True)
    trip_ended = models.BooleanField(default=False)
    trip_missed = models.BooleanField(default=False)
    trip_inbound = models.BooleanField(null=True, blank=True)
    trip_board = models.ForeignKey(duty, on_delete=models.SET_NULL, null=True, blank=True)

    history = HistoricalRecords()

    class Meta:
        indexes = [
            models.Index(fields=["trip_display_id"]),
            models.Index(fields=["trip_vehicle"]),
            models.Index(fields=["trip_route"]),
            models.Index(fields=["trip_route_num"]),
            models.Index(fields=["trip_driver"]),
            models.Index(fields=["trip_start_location"]),
            models.Index(fields=["trip_end_location"]),
            models.Index(fields=["trip_start_at"]),
            models.Index(fields=["trip_end_at"]),
            models.Index(fields=["trip_updated_at"]),
            models.Index(fields=["trip_ended"]),
            models.Index(fields=["trip_missed"]),
            models.Index(fields=["trip_inbound"]),
            models.Index(fields=["trip_board"]),
        ]

    def __str__(self):
        return f"Trip {self.trip_display_id or self.trip_id} ({self.trip_vehicle})"

    def _validate_dates(self):
        now = timezone.now()
        min_date = now - timedelta(days=365 * 10)
        max_date = now + timedelta(days=365 * 10)

        errors = {}
        if self.trip_start_at and not (min_date <= self.trip_start_at <= max_date):
            errors["trip_start_at"] = "Start date must be within 10 years of today."
        if self.trip_end_at and not (min_date <= self.trip_end_at <= max_date):
            errors["trip_end_at"] = "End date must be within 10 years of today."
        if errors:
            raise ValidationError(errors)

    def clean(self):
        super().clean()
        self._validate_dates()

    def save(self, *args, **kwargs):
        # Ensure validation runs on every save, not just via forms
        self._validate_dates()
        super().save(*args, **kwargs)


class Tracking(models.Model):
    tracking_id = models.AutoField(primary_key=True)
    tracking_vehicle = models.ForeignKey(fleet, on_delete=models.CASCADE)
    tracking_route = models.ForeignKey(route, on_delete=models.CASCADE, null=True, blank=True)
    tracking_trip = models.ForeignKey(Trip, on_delete=models.CASCADE, null=True, blank=True)
    tracking_game = models.ForeignKey(game, on_delete=models.CASCADE, null=True, blank=True)
    tracking_data = models.JSONField(default=default_tracking_data)
    # NOTE: tracking_history_data is capped at MAX_HISTORY_ENTRIES.
    # For long-running vehicles, consider migrating history to a separate
    # TrackingEvent model with one row per point (much better for querying).
    tracking_history_data = models.JSONField(default=default_tracking_history)
    tracking_start_location = models.CharField(max_length=255, null=True, blank=True)
    tracking_end_location = models.CharField(max_length=255, null=True, blank=True)
    tracking_start_at = models.DateTimeField(null=True, blank=True)
    tracking_end_at = models.DateTimeField(null=True, blank=True)
    tracking_updated_at = models.DateTimeField(auto_now=True)
    # Removed: trip_ended — use tracking_trip.trip_ended instead to avoid sync issues

    history = HistoricalRecords()

    class Meta:
        indexes = [
            models.Index(fields=["tracking_vehicle"]),
            models.Index(fields=["tracking_route"]),
            models.Index(fields=["tracking_trip"]),
            models.Index(fields=["tracking_game"]),
            models.Index(fields=["tracking_start_at"]),
            models.Index(fields=["tracking_end_at"]),
            models.Index(fields=["tracking_updated_at"]),
        ]

    def __str__(self):
        return f"Tracking {self.tracking_id} — vehicle {self.tracking_vehicle_id}"

    @property
    def trip_ended(self):
        """Derived from the related Trip to avoid data drift."""
        if self.tracking_trip_id is None:
            return False
        return self.tracking_trip.trip_ended

    def save(self, *args, **kwargs):
        # Normalise tracking_data: accept either a JSON string or a dict
        if isinstance(self.tracking_data, str):
            try:
                tracking_data_dict = json.loads(self.tracking_data)
            except json.JSONDecodeError:
                tracking_data_dict = {}
        else:
            tracking_data_dict = self.tracking_data

        # Append current position snapshot to history
        history = self.tracking_history_data if self.tracking_history_data is not None else []
        record = {**tracking_data_dict, "timestamp": timezone.now().isoformat()}
        history.append(record)

        # Cap history to avoid unbounded JSON growth
        if len(history) > MAX_HISTORY_ENTRIES:
            history = history[-MAX_HISTORY_ENTRIES:]

        self.tracking_history_data = history
        self.tracking_data = tracking_data_dict

        super().save(*args, **kwargs)
