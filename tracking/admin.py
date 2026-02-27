from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin
from .models import *
from django.utils.html import format_html
from django.contrib import messages
from django import forms
from datetime import datetime, timedelta
from routes.models import timetableEntry
from django.utils import timezone
from admin_auto_filters.filters import AutocompleteFilter


def make_aware_dst(dt):
    """Convert a naive datetime to aware, handling DST edge cases."""
    tz = timezone.get_current_timezone()
    try:
        return timezone.make_aware(dt, tz)
    except timezone.AmbiguousTimeError:
        return timezone.make_aware(dt.replace(fold=1), tz)
    except timezone.NonExistentTimeError:
        return timezone.make_aware(dt + timedelta(hours=1), tz)


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
        """Filter timetable choices based on the selected route."""
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
                .only("id", "route_id")  # avoid fetching stop_times just for the dropdown
                [:500]
            )

    def _configure_time_choices(self):
        """Build start time choices from the selected timetable entry."""
        timetable_id = None

        if "timetable" in self.data:
            try:
                timetable_id = int(self.data["timetable"])
            except (ValueError, TypeError):
                pass
        elif self.instance.pk and hasattr(self.instance, "timetable") and self.instance.timetable_id:
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

        stop_order = list(timetable.stop_times)
        start_stop, end_stop = stop_order[0], stop_order[-1]
        start_times = timetable.stop_times[start_stop]["times"]
        end_times = timetable.stop_times[end_stop]["times"]

        try:
            idx = start_times.index(start_time)
            end_time = end_times[idx]
        except (ValueError, IndexError):
            raise forms.ValidationError("Invalid time selected.")

        today = timezone.localdate()
        dt_start = make_aware_dst(datetime.strptime(f"{today} {start_time}", "%Y-%m-%d %H:%M"))
        dt_end = make_aware_dst(datetime.strptime(f"{today} {end_time}", "%Y-%m-%d %H:%M"))

        if dt_end <= dt_start:
            dt_end += timedelta(days=1)

        cleaned_data.update({
            "trip_start_location": start_stop,
            "trip_end_location": end_stop,
            "trip_start_at": dt_start,
            "trip_end_at": dt_end,
        })

        return cleaned_data


class TripVehicleFilter(AutocompleteFilter):
    title = "Vehicle"
    field_name = "trip_vehicle"


class TripRouteFilter(AutocompleteFilter):
    title = "Route"
    field_name = "trip_route"


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


class TrackingVehicleFilter(AutocompleteFilter):
    title = "Vehicle"
    field_name = "tracking_vehicle"


class TrackingRouteFilter(AutocompleteFilter):
    title = "Route"
    field_name = "tracking_route"


@admin.register(Tracking)
class TrackingAdmin(SimpleHistoryAdmin):
    list_display = (
        "tracking_id", "tracking_start_at", "tracking_end_at",
        "trip_ended", "tracking_route", "tracking_vehicle",
    )
    search_fields = (
        "tracking_id",
        "tracking_vehicle__fleet_number",
        "tracking_route__route_name",
    )
    list_filter = ("trip_ended", TrackingVehicleFilter, TrackingRouteFilter)
    autocomplete_fields = ["tracking_vehicle", "tracking_route", "tracking_trip"]
    date_hierarchy = "tracking_start_at"
    list_per_page = 50

    def get_queryset(self, request):
        return super().get_queryset(request).defer(
            "tracking_data", "tracking_history_data"
        )

    @admin.action(description="End selected trips")
    def end_trip(self, request, queryset):
        updated = queryset.update(trip_ended=True)
        self.message_user(request, f"{updated} trip(s) marked as ended.", messages.SUCCESS)

    @admin.action(description="Un-end selected trips")
    def unend_trip(self, request, queryset):
        updated = queryset.update(trip_ended=False)
        self.message_user(request, f"{updated} trip(s) marked as not ended.", messages.SUCCESS)
