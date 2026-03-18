import json
import secrets
import time

from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.management import call_command
from django.conf import settings
from django.db.models import Q, Prefetch
from django.http import JsonResponse
from django.shortcuts import render, redirect
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from rest_framework import generics, serializers, status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from fleet.models import fleet, MBTOperator, helper
from main.models import UserKeys
from mybustimes.permissions import ReadOnly
from routes.models import route, routeStop

from .forms import TrackingForm
from .models import Trip, Tracking
from .serializers import (
    trackingSerializer,
    trackingDataSerializer,
    TripSerializer,
    TrackingSerializer,
)
from .utils import get_progress


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _get_user_from_key(request):
    """
    Resolve a request's Authorization header to a user.
    Returns (user, None) on success or (None, Response) on failure.
    """
    session_key = request.headers.get("Authorization", "")
    if session_key.startswith("SessionKey "):
        session_key = session_key[len("SessionKey "):]

    if not session_key:
        return None, Response(
            {"detail": "Missing Authorization header"},
            status=status.HTTP_401_UNAUTHORIZED,
        )

    try:
        user_key = UserKeys.objects.select_related("user").get(session_key=session_key)
    except UserKeys.DoesNotExist:
        return None, Response(
            {"detail": "Invalid session key"},
            status=status.HTTP_401_UNAUTHORIZED,
        )

    return user_key.user, None


def _resolve_session_key(session_key):
    """
    Resolve a raw session key string (from request body) to a user.
    Returns (user, error_dict) — error_dict is None on success.
    """
    if not session_key:
        return None, {"error": "Missing session_key"}
    try:
        user_key = UserKeys.objects.select_related("user").get(session_key=session_key)
    except UserKeys.DoesNotExist:
        return None, {"error": "Invalid session key"}
    return user_key.user, None


# ---------------------------------------------------------------------------
# Tracking creation (API)
# ---------------------------------------------------------------------------

@csrf_exempt
class create_tracking(generics.CreateAPIView):
    serializer_class = trackingSerializer

    def post(self, request, *args, **kwargs):
        user, error = _get_user_from_key(request)
        if error:
            return error

        serializer = self.serializer_class(data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response(
                {"success": True, "data": serializer.data},
                status=status.HTTP_201_CREATED,
            )
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


# ---------------------------------------------------------------------------
# Trip CRUD views
# ---------------------------------------------------------------------------

class TripListView(generics.ListAPIView):
    queryset = Trip.objects.all().order_by("-trip_start_at")
    serializer_class = TripSerializer


class TripDetailView(generics.RetrieveAPIView):
    queryset = Trip.objects.all()
    serializer_class = TripSerializer
    lookup_field = "trip_id"


# ---------------------------------------------------------------------------
# Tracking CRUD views
# ---------------------------------------------------------------------------

class TrackingListView(generics.ListAPIView):
    queryset = Tracking.objects.select_related("tracking_route").order_by(
        "-tracking_updated_at"
    )
    serializer_class = TrackingSerializer


class TrackingDetailView(generics.RetrieveAPIView):
    queryset = Tracking.objects.select_related("tracking_route")
    serializer_class = TrackingSerializer
    lookup_field = "tracking_id"


class TrackingByVehicleView(generics.ListAPIView):
    serializer_class = TrackingSerializer

    def get_queryset(self):
        return (
            Tracking.objects.select_related("tracking_route")
            .filter(tracking_vehicle_id=self.kwargs["vehicle_id"])
            .order_by("-tracking_updated_at")
        )


# ---------------------------------------------------------------------------
# Start a new trip (mobile API)
# ---------------------------------------------------------------------------

# Shared select_related chain reused across several querysets
_TRACKING_SELECT_RELATED = [
    "tracking_vehicle",
    "tracking_vehicle__vehicleType",
    "tracking_vehicle__operator",
    "tracking_vehicle__livery",
    "tracking_route",
]


@csrf_exempt
def StartNewTripView(request):
    if request.method != "POST":
        return JsonResponse({"error": "POST only API"}, status=405)

    # --- Parse body ---
    try:
        data = json.loads(request.body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return JsonResponse({"error": "Invalid JSON", "details": str(exc)}, status=400)

    session_key = data.get("session_key")
    vehicle_id = data.get("vehicle_id")
    route_id = data.get("route_id")
    route_number = data.get("route_number")
    trip_end_location = data.get("outbound_destination")
    trip_start_at_raw = data.get("trip_date_time")

    # --- Validate required fields ---
    if not session_key:
        return JsonResponse({"error": "Missing session_key"}, status=400)
    if not vehicle_id:
        return JsonResponse({"error": "Missing vehicle_id"}, status=400)

    # --- Parse optional datetime ---
    trip_start_at = None
    if trip_start_at_raw:
        parsed = parse_datetime(trip_start_at_raw)
        if parsed is None:
            return JsonResponse({"error": "trip_date_time is invalid ISO8601"}, status=400)
        if timezone.is_naive(parsed):
            parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
        trip_start_at = parsed

    # --- Auth ---
    user, err = _resolve_session_key(session_key)
    if err:
        return JsonResponse(err, status=400)

    # --- Vehicle + permission check ---
    try:
        vehicle = fleet.objects.select_related("operator").get(id=vehicle_id)
    except fleet.DoesNotExist:
        return JsonResponse({"error": "Vehicle not found"}, status=404)

    operator_inst = vehicle.operator
    if operator_inst.owner != user:
        is_helper = helper.objects.filter(operator=operator_inst, helper=user).exists()
        if not is_helper:
            return JsonResponse({"error": "Permission denied"}, status=403)

    # --- Optional route lookup ---
    route_obj = None
    if route_id:
        route_obj = route.objects.filter(id=route_id).first()

    now = timezone.now()
    start_at = trip_start_at or now

    # --- Create Trip + Tracking atomically ---
    trip = Trip.objects.create(
        trip_vehicle=vehicle,
        trip_route=route_obj,
        trip_route_num=route_number,
        trip_end_location=trip_end_location,
        trip_start_at=start_at,
        trip_driver=user,
    )
    tracking = Tracking.objects.create(
        tracking_vehicle=vehicle,
        tracking_route=route_obj,
        tracking_trip=trip,
        tracking_data={"X": 0, "Y": 0, "delay": 0, "heading": 0, "current_stop_idx": "0"},
        tracking_start_location="Depot",
        tracking_end_location=trip_end_location,
        tracking_start_at=start_at,
    )

    return JsonResponse(
        {
            "message": "Trip started",
            "trip_id": trip.trip_id,
            "tracking_id": tracking.tracking_id,
        },
        status=201,
    )


# ---------------------------------------------------------------------------
# Active trips
# ---------------------------------------------------------------------------

def active_trips(request):
    # NOTE: Tracking.trip_ended is now a @property — filter via related Trip instead.
    qs = Tracking.objects.filter(tracking_trip__trip_ended=False).values(
        "tracking_id", "tracking_vehicle_id", "tracking_route_id"
    )
    return JsonResponse({"active_trips": list(qs)}, status=200)


# ---------------------------------------------------------------------------
# Update tracking position
# ---------------------------------------------------------------------------

def update_tracking(request, tracking_id):
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "Invalid method"}, status=400)

    new_tracking_data = request.POST.get("tracking_data")
    if not new_tracking_data:
        return JsonResponse({"success": False, "error": "Missing tracking_data"}, status=400)

    try:
        tracking = Tracking.objects.get(tracking_id=tracking_id)
    except Tracking.DoesNotExist:
        return JsonResponse({"success": False, "error": "Tracking not found"}, status=404)

    tracking.tracking_data = new_tracking_data
    tracking.save()

    return JsonResponse(
        {
            "success": True,
            "data": {
                "tracking_id": tracking.tracking_id,
                "tracking_data": tracking.tracking_data,
            },
        },
        status=200,
    )


def update_tracking_template(request, tracking_id):
    try:
        tracking = Tracking.objects.get(tracking_id=tracking_id)
    except Tracking.DoesNotExist:
        return JsonResponse({"success": False, "error": "Tracking not found"}, status=404)
    return render(request, "update.html", {"tracking": tracking})


# ---------------------------------------------------------------------------
# Create tracking (web form)
# ---------------------------------------------------------------------------

def create_tracking_template(request, operator_slug):
    operator_instance = MBTOperator.objects.filter(operator_slug=operator_slug).first()

    if request.method == "POST":
        form = TrackingForm(request.POST, operator=operator_instance)

        # Resolve vehicle + route up front so form errors surface cleanly
        try:
            vehicle = fleet.objects.get(id=request.POST.get("tracking_vehicle"))
            route_obj = route.objects.get(id=request.POST.get("tracking_route"))
        except (fleet.DoesNotExist, route.DoesNotExist):
            return JsonResponse(
                {"success": False, "error": "Vehicle or route not found."},
                status=404,
            )

        if form.is_valid():
            trip = Trip.objects.create(
                trip_vehicle=vehicle,
                trip_route=route_obj,
                trip_start_location=form.cleaned_data.get("tracking_start_location"),
                trip_end_location=form.cleaned_data.get("tracking_end_location"),
                trip_start_at=form.cleaned_data.get("tracking_start_at"),
            )
            form.instance.tracking_trip = trip
            form.save()
            return redirect("update-tracking-template", tracking_id=form.instance.tracking_id)

        return JsonResponse(
            {"success": False, "errors": form.errors, "data": form.data},
            status=400,
        )

    form = TrackingForm(operator=operator_instance)
    return render(request, "create.html", {"form": form})


# ---------------------------------------------------------------------------
# End a trip
# ---------------------------------------------------------------------------

def end_trip(request, tracking_id):
    try:
        tracking = Tracking.objects.select_related(
            "tracking_vehicle__operator"
        ).get(tracking_id=tracking_id)
    except Tracking.DoesNotExist:
        return JsonResponse({"success": False, "error": "Tracking ID not found"}, status=404)

    # End the canonical Trip record rather than the (now removed) tracking field
    if tracking.tracking_trip_id:
        Trip.objects.filter(pk=tracking.tracking_trip_id).update(trip_ended=True)

    vehicle = tracking.tracking_vehicle
    return redirect(
        "vehicle_detail",
        operator_slug=vehicle.operator.operator_slug,
        vehicle_id=vehicle.id,
    )


# ---------------------------------------------------------------------------
# Map views (read-only list API)
# ---------------------------------------------------------------------------

def _tracking_qs_with_relations():
    return Tracking.objects.select_related(*_TRACKING_SELECT_RELATED)


class map_view(generics.ListAPIView):
    serializer_class = trackingDataSerializer
    permission_classes = [ReadOnly]

    def get_queryset(self):
        tracking_id = self.kwargs.get("tracking_id")
        tracking_game = self.kwargs.get("game_id")
        qs = _tracking_qs_with_relations()

        if tracking_id:
            return qs.filter(tracking_id=tracking_id)
        if tracking_game:
            return qs.filter(tracking_game_id=tracking_game, tracking_trip__trip_ended=False)
        return qs.filter(tracking_trip__trip_ended=False)


class map_view_history(generics.ListAPIView):
    serializer_class = trackingDataSerializer
    permission_classes = [ReadOnly]

    def get_queryset(self):
        tracking_id = self.kwargs.get("tracking_id")
        tracking_game = self.kwargs.get("game_id")
        qs = _tracking_qs_with_relations()

        if tracking_id:
            return qs.filter(tracking_id=tracking_id)
        if tracking_game:
            return qs.filter(tracking_game_id=tracking_game)
        return qs.all()


# ---------------------------------------------------------------------------
# Current vehicle trips
# ---------------------------------------------------------------------------

class current_vehicle_trips(generics.ListAPIView):
    serializer_class = TripSerializer
    permission_classes = [ReadOnly]

    def get_queryset(self):
        now = timezone.now()
        return Trip.objects.filter(trip_start_at__lte=now, trip_end_at__gte=now)


# ---------------------------------------------------------------------------
# Estimated position serializer + live map API
# ---------------------------------------------------------------------------

class EstimatedPositionSerializer(serializers.Serializer):
    """Inline serializer — avoids nested serializer overhead for the live map."""

    def to_representation(self, obj):
        ct = obj.current_trip
        trip_route = ct.trip_route if ct else None
        progress = get_progress(ct) if ct else None

        livery = obj.livery
        has_livery = livery is not None
        livery_colour = livery.colour if has_livery else (obj.colour or "#000000")
        livery_text = livery.text_colour if has_livery else "#ffffff"
        livery_left = livery.left_css if has_livery else ""
        livery_right = livery.right_css if has_livery else ""
        livery_stroke = livery.stroke_colour if has_livery else ""

        # Vehicle's own colour overrides livery CSS when set
        left_css = obj.colour or livery_left
        right_css = obj.colour or livery_right

        vehicle_name = (
            f"{obj.fleet_number} - {obj.reg}"
            if obj.fleet_number
            else (obj.reg or "Unknown Vehicle")
        )
        features = ""
        if obj.features:
            features = (
                "<br>".join(obj.features)
                if isinstance(obj.features, list)
                else str(obj.features)
            )

        operator_slug = obj.operator.operator_slug
        operator_name = obj.operator.operator_name

        vehicle_data = {
            "url": f"/operator/{operator_slug}/vehicles/{obj.id}/",
            "name": vehicle_name,
            "operator_slug": operator_slug,
            "operator_name": operator_name,
            "features": features,
            "livery": {
                "id": livery.id if has_livery else None,
                "name": livery.name if has_livery else "Default",
                "colour": livery_colour,
                "text_colour": livery_text,
                "left_css": livery_left,
                "right_css": livery_right,
                "stroke_colour": livery_stroke,
            },
            "colour": livery_colour,
            "text_colour": livery_text,
            "white_text": livery_text.lower() in ("#fff", "#ffffff", "white"),
            "left_css": left_css,
            "right_css": right_css,
            "stroke_colour": livery_stroke,
            "custom_features": obj.advanced_details or None,
        }

        if trip_route:
            # Use prefetched operators when available to avoid N+1
            operators = getattr(trip_route, "_prefetched_operators", None)
            op = operators[0] if operators else trip_route.route_operators.first()
            service_data = {
                "url": f"/operator/{op.operator_slug}/route/{trip_route.id}/" if op else None,
                "line_name": trip_route.route_num or "Unknown Service",
            }
        else:
            service_data = {"url": None, "line_name": "Unknown Service"}

        return {
            "trip_id": ct.trip_id if ct else None,
            "vehicle": vehicle_data,
            "service_id": trip_route.id if trip_route else None,
            "service": service_data,
            "progress": progress,
            "lat": obj.sim_lat,
            "lng": obj.sim_lon,
            "destination": ct.trip_end_location if ct else "",
            "heading": obj.sim_heading,
            "updated_at": obj.updated_at,
        }


class trackingAPIView(generics.ListAPIView):
    serializer_class = EstimatedPositionSerializer
    permission_classes = [AllowAny]

    def get_queryset(self):
        params = self.request.query_params

        try:
            min_lat = float(params["ymin"])
            max_lat = float(params["ymax"])
            min_lng = float(params["xmin"])
            max_lng = float(params["xmax"])
        except (KeyError, TypeError, ValueError):
            return fleet.objects.none()

        operator_id = params.get("operator_id")
        route_id = params.get("route_id")
        vehicle_id = params.get("vehicle_id")
        hidden_ids = [
            int(x)
            for x in params.get("hide_operator_ids", "").split(",")
            if x.strip().isdigit()
        ]

        filters = Q(
            sim_lat__isnull=False,
            sim_lon__isnull=False,
            sim_lat__gte=min_lat,
            sim_lat__lte=max_lat,
            sim_lon__gte=min_lng,
            sim_lon__lte=max_lng,
            current_trip__isnull=False,
        )
        if operator_id:
            filters &= Q(operator_id=operator_id) | Q(loan_operator__id=operator_id)
        if route_id:
            filters &= Q(current_trip__trip_route_id=route_id)
        if vehicle_id:
            filters &= Q(id=vehicle_id)
        if hidden_ids:
            filters &= ~Q(operator_id__in=hidden_ids)

        return (
            fleet.objects.select_related(
                "operator",
                "livery",
                "current_trip",
                "current_trip__trip_route",
            )
            .prefetch_related(
                Prefetch(
                    "current_trip__trip_route__route_operators",
                    queryset=MBTOperator.objects.only("id", "operator_slug"),
                    to_attr="_prefetched_operators",
                )
            )
            .only(
                "id", "fleet_number", "reg", "colour", "advanced_details", "features",
                "sim_lat", "sim_lon", "sim_heading", "updated_at",
                "operator__id", "operator__operator_slug", "operator__operator_name",
                "livery__id", "livery__name", "livery__colour", "livery__text_colour",
                "livery__left_css", "livery__right_css", "livery__stroke_colour",
                "current_trip__trip_id", "current_trip__trip_end_location",
                "current_trip__trip_start_at", "current_trip__trip_end_at",
                "current_trip__trip_route__id", "current_trip__trip_route__route_num",
            )
            .filter(filters)
        )
