from django.urls import path

from .views import cancel_booking, create_booking

app_name = "bookings"
urlpatterns = [
    path("create/<int:location_id>/", create_booking, name="create"),
    path("<uuid:booking_id>/cancel/", cancel_booking, name="cancel"),
]
