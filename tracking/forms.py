import json
import re
from datetime import datetime, timedelta

from django import forms
from django.db.models import Case, When
from django.utils import timezone

from fleet.models import fleet
from routes.models import route, timetableEntry

from .models import Tracking


def alphanum_key(fleet_number):
    """Sort key that correctly interleaves letters and numbers (e.g. V1 < V2 < V10)."""
    return [
        int(part) if part.isdigit() else part.lower()
        for part in re.split(r"([0-9]+)", fleet_number or "")
    ]


def make_aware_dst(dt):
    """Convert a naive datetime to timezone-aware, handling DST edge cases."""
    tz = timezone.get_current_timezone()
    try:
        return timezone.make_aware(dt, tz)
    except timezone.AmbiguousTimeError:
        return timezone.make_aware(dt.replace(fold=1), tz)
    except timezone.NonExistentTimeError:
        return timezone.make_aware(dt + timedelta(hours=1), tz)


def _parse_stop_times(timetable):
    """
    Return stop_times as a dict regardless of whether it is stored as a
    JSONField (already a dict) or a TextField (JSON string).
    """
    stop_times = timetable.stop_times
    if isinstance(stop_times, str):
        return json.loads(stop_times)
    return stop_times


def _build_start_end(timetable, start_time):
    """
    Given a timetable entry and a chosen start time string, return
    (start_stop, end_stop, dt_start, dt_end) or raise ValidationError.
    """
    stop_times = _parse_stop_times(timetable)
    stop_order = list(stop_times)
    start_stop, end_stop = stop_order[0], stop_order[-1]

    try:
        idx = stop_times[start_stop]["times"].index(start_time)
        end_time = stop_times[end_stop]["times"][idx]
    except (KeyError, ValueError, IndexError):
        raise forms.ValidationError("Invalid time selected.")

    today = timezone.localdate()
    fmt = "%Y-%m-%d %H:%M"
    dt_start = make_aware_dst(datetime.strptime(f"{today} {start_time}", fmt))
    dt_end = make_aware_dst(datetime.strptime(f"{today} {end_time}", fmt))

    # Handle services that run past midnight
    if dt_end <= dt_start:
        dt_end += timedelta(days=1)

    return start_stop, end_stop, dt_start, dt_end


class TrackingForm(forms.ModelForm):
    tracking_route = forms.ModelChoiceField(
        queryset=route.objects.none(),
        required=False,
        label="Route",
    )
    timetable = forms.ModelChoiceField(
        queryset=timetableEntry.objects.none(),
        required=False,
        label="Timetable Entry",
    )
    start_time_choice = forms.ChoiceField(required=False, label="Select Trip Time")

    class Meta:
        model = Tracking
        fields = [
            "tracking_vehicle",
            "tracking_route",
            "timetable",
            "start_time_choice",
            "tracking_start_location",
            "tracking_end_location",
            "tracking_start_at",
            "tracking_end_at",
            "tracking_data",
        ]
        widgets = {
            "tracking_start_at": forms.DateTimeInput(attrs={"type": "datetime-local"}),
            "tracking_end_at": forms.DateTimeInput(attrs={"type": "datetime-local"}),
            "tracking_data": forms.HiddenInput(),
        }

    def __init__(self, *args, **kwargs):
        operator = kwargs.pop("operator", None)
        super().__init__(*args, **kwargs)

        if operator:
            self._configure_vehicle_queryset(operator)
            self.fields["tracking_route"].queryset = (
                route.objects.filter(route_operators=operator).order_by("route_num")
            )

    def _configure_vehicle_queryset(self, operator):
        """
        Sort fleet alphanumerically in Python (avoids a fragile Case/When
        on large querysets), then preserve that order in one DB query.
        """
        fleet_qs = fleet.objects.filter(operator=operator).only("id", "fleet_number")
        sorted_ids = [f.id for f in sorted(fleet_qs, key=lambda f: alphanum_key(f.fleet_number))]

        preserved = Case(*[When(pk=pk, then=pos) for pos, pk in enumerate(sorted_ids)])
        self.fields["tracking_vehicle"].queryset = (
            fleet.objects.filter(pk__in=sorted_ids).order_by(preserved)
        )

    def clean(self):
        cleaned_data = super().clean()
        timetable = cleaned_data.get("timetable")
        start_time = cleaned_data.get("start_time_choice")

        if not (timetable and start_time):
            return cleaned_data

        start_stop, end_stop, dt_start, dt_end = _build_start_end(timetable, start_time)

        cleaned_data.update(
            {
                "tracking_start_location": start_stop,
                "tracking_end_location": end_stop,
                "tracking_start_at": dt_start,
                "tracking_end_at": dt_end,
            }
        )
        return cleaned_data


class UpdateTrackingForm(forms.ModelForm):
    class Meta:
        model = Tracking
        fields = ["tracking_data", "tracking_history_data"]
