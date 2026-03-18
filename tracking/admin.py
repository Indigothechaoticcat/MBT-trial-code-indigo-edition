from datetime import datetime, timedelta

from django import forms
from django.contrib import admin, messages
from django.utils import timezone

from admin_auto_filters.filters import AutocompleteFilter
from simple_history.admin import SimpleHistoryAdmin

from routes.models import timetableEntry

from .forms import make_aware_dst, _build_start_end
from .models import Trip, Tracking


# ---------------------------------------------------------------------------
# TripAdmin form
# ---------------------------------------------------------------------------

class TripForm(forms.ModelForm):
    timetable = forms.ModelChoiceField(
        queryset=timetableEntry.objects.none(),
        required=False,
        label="Timetable Entry",
    )
    start_time_choice = forms.ChoiceField(required=False, label="Select Trip Time")

    class Meta:
        model = Trip
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._configure_timetable_queryset()
        self._configure_time_choices()

    def _configure_timetable_queryset(self):
        """Filter timetable choices to those belonging to the selected route."""
        route_id = None

        if "trip_route" in self.data:
            try:
                route_id = int(self.data["trip_route"])
            except (ValueError, TypeError):
                pass
        elif self.instance.pk and self.instance.trip_route_id:
            route_id = self.instance.trip_route_id

        if route_id:
            self.fields["timetable"].queryset = (
                timetableEntry.objects.filter(route_id=route_id)
                .only("id", "route_id")[:500]
            )

    def _configure_time_choices(self):
        """Build start time choices from the selected timetable entry."""
        timetable_id = None

        if "timetable" in self.data:
            try:
                timetable_id = int(self.data["timetable"])
            except (ValueError, TypeError):
                pass
        elif self.instance.pk and getattr(self.instance, "timetable_id", None):
            timetable_id = self.instance.timetable_id

        if not timetable_id:
            self.fields["start_time_choice"].choices = []
            return

        try:
            tt = timetableEntry.objects.only("stop_times").get(id=timetable_id)
        except timetableEntry.DoesNotExist:
            self.fields["start_time_choice"].choices = []
            return

        stop_order = list(tt.stop_times)
        start_stop, end_stop = stop_order[0], stop_order[-1]
        trip_times = tt.stop_times[start_stop]["times"]

        self.fields["start_time_choice"].choices = [
            (t, f"{t} — {start_stop} ➝ {end_stop}") for t in trip_times
        ]

        if self.instance.pk and self.instance.trip_start_at:
            self.initial["start_time_choice"] = self.instance.trip_start_at.strftime("%H:%M")

    def clean(self):
        cleaned_data = super().clean()
        timetable = cleaned_data.get("timetable")
        start_time = cleaned_data.get("start_time_choice")

        if not (timetable and start_time):
            return cleaned_data

        # Delegate to the shared helper so the logic stays in one place
        start_stop, end_stop, dt_start, dt_end = _build_start_end(timetable, start_time)

        cleaned_data.update(
            {
                "trip_start_location": start_stop,
                "trip_end_location": end_stop,
                "trip_start_at": dt_start,
                "trip_end_at": dt_end,
            }
        )
        return cleaned_data


# ---------------------------------------------------------------------------
# Autocomplete filters
# ---------------------------------------------------------------------------

class TripVehicleFilter(AutocompleteFilter):
    title = "Vehicle"
    field_name = "trip_vehicle"


class TripRouteFilter(AutocompleteFilter):
    title = "Route"
    field_name = "trip_route"


class TrackingVehicleFilter(AutocompleteFilter):
    title = "Vehicle"
    field_name = "tracking_vehicle"


class TrackingRouteFilter(AutocompleteFilter):
    title = "Route"
    field_name = "tracking_route"


# ---------------------------------------------------------------------------
# TripAdmin
# ---------------------------------------------------------------------------

@admin.register(Trip)
class TripAdmin(SimpleHistoryAdmin):
    form = TripForm
    list_display = (
        "trip_id", "trip_inbound", "trip_start_at",
        "trip_end_at", "trip_ended", "trip_route", "trip_vehicle",
    )
    search_fields = (
        "trip_id",
        "trip_vehicle__fleet_number",
        "trip_route__route_name",
    )
    list_filter = ("trip_ended", TripVehicleFilter, TripRouteFilter)
    autocomplete_fields = ["trip_vehicle", "trip_route"]
    date_hierarchy = "trip_start_at"
    list_per_page = 50

    class Media:
        js = ("admin/js/jquery.init.js", "js/trip_form.js")


# ---------------------------------------------------------------------------
# TrackingAdmin
# ---------------------------------------------------------------------------

@admin.register(Tracking)
class TrackingAdmin(SimpleHistoryAdmin):
    list_display = (
        "tracking_id", "tracking_start_at", "tracking_end_at",
        "tracking_route", "tracking_vehicle",
    )
    search_fields = (
        "tracking_id",
        "tracking_vehicle__fleet_number",
        "tracking_route__route_name",
    )
    # trip_ended removed from list_filter — it is now a property, not a DB field
    list_filter = (TrackingVehicleFilter, TrackingRouteFilter)
    autocomplete_fields = ["tracking_vehicle", "tracking_route", "tracking_trip"]
    date_hierarchy = "tracking_start_at"
    list_per_page = 50

    def get_queryset(self, request):
        # Defer large JSON fields so list views stay fast
        return super().get_queryset(request).defer(
            "tracking_data", "tracking_history_data"
        )

    @admin.action(description="End selected trips")
    def end_trip(self, request, queryset):
        # Update the canonical Trip records, not the (removed) tracking field
        trip_ids = queryset.values_list("tracking_trip_id", flat=True)
        updated = Trip.objects.filter(pk__in=trip_ids).update(trip_ended=True)
        self.message_user(request, f"{updated} trip(s) marked as ended.", messages.SUCCESS)

    @admin.action(description="Un-end selected trips")
    def unend_trip(self, request, queryset):
        trip_ids = queryset.values_list("tracking_trip_id", flat=True)
        updated = Trip.objects.filter(pk__in=trip_ids).update(trip_ended=False)
        self.message_user(request, f"{updated} trip(s) marked as not ended.", messages.SUCCESS)
