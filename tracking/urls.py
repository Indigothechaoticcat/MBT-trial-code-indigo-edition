from django.conf import settings
from django.conf.urls.static import static
from django.urls import path

from .views import (
    create_tracking,
    create_tracking_template,
    current_vehicle_trips,
    end_trip,
    map_view,
    map_view_history,
    StartNewTripView,
    TrackingByVehicleView,
    TrackingDetailView,
    TrackingListView,
    trackingAPIView,
    TripDetailView,
    TripListView,
    update_tracking,
    update_tracking_template,
)

urlpatterns = [
    # --- Trip REST ---
    path("trips/", TripListView.as_view(), name="trip-list"),
    path("trips/<int:trip_id>/", TripDetailView.as_view(), name="trip-detail"),
    path("trips/current/", current_vehicle_trips.as_view(), name="trip-current"),

    # --- Tracking REST ---
    path("trackings/", TrackingListView.as_view(), name="tracking-list"),
    path("trackings/<int:tracking_id>/", TrackingDetailView.as_view(), name="tracking-detail"),
    path("trackings/vehicle/<int:vehicle_id>/", TrackingByVehicleView.as_view(), name="tracking-by-vehicle"),

    # --- Start / update / end (mobile API) ---
    path("tracking/start/", StartNewTripView, name="start-trip"),
    path("tracking/update/<int:tracking_id>/", update_tracking, name="update_tracking"),
    path("end/<tracking_id>/", end_trip, name="end_trip"),

    # --- Web templates ---
    path("<str:operator_slug>/start/", create_tracking_template, name="create-tracking-template"),
    path("update/<tracking_id>/", update_tracking_template, name="update-tracking-template"),

    # --- Live map ---
    path("game-tracking/data/", map_view.as_view(), name="map-view"),
    path("game-tracking/data/<tracking_id>/", map_view.as_view(), name="map-view-single"),
    path("game-tracking/data/history/", map_view_history.as_view(), name="map-view-history"),
    path("game-tracking/data/history/<tracking_id>/", map_view_history.as_view(), name="map-view-history-single"),
    path("game-tracking/live/", trackingAPIView.as_view(), name="tracking-live"),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
