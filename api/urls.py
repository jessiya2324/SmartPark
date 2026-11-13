from django.urls import path
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from .views import (
    AnalyticsAPI, LocationAPI, ParkingEntryAPI, ParkingExitAPI, PaymentOrderAPI, PaymentVerifyAPI,
    ProfileAPI, QRVerifyAPI, RegisterAPI, ReservationCancelAPI, ReservationCreateAPI, ReservationListAPI, SlotAPI,
)

urlpatterns = [
    path("auth/register/", RegisterAPI.as_view(), name="api-register"),
    path("auth/login/", TokenObtainPairView.as_view(), name="api-login"),
    path("auth/token/refresh/", TokenRefreshView.as_view(), name="api-token-refresh"),
    path("auth/profile/", ProfileAPI.as_view(), name="api-profile"),
    path("parking/locations/", LocationAPI.as_view(), name="api-locations"),
    path("parking/slots/", SlotAPI.as_view(), name="api-slots"),
    path("bookings/", ReservationListAPI.as_view(), name="api-bookings"),
    path("bookings/create/", ReservationCreateAPI.as_view(), name="api-booking-create"),
    path("bookings/cancel/", ReservationCancelAPI.as_view(), name="api-booking-cancel"),
    path("payments/create-order/", PaymentOrderAPI.as_view(), name="api-payment-order"),
    path("payments/verify/", PaymentVerifyAPI.as_view(), name="api-payment-verify"),
    path("qr/verify/", QRVerifyAPI.as_view(), name="api-qr-verify"),
    path("parking/entry/", ParkingEntryAPI.as_view(), name="api-parking-entry"),
    path("parking/exit/", ParkingExitAPI.as_view(), name="api-parking-exit"),
    path("admin/analytics/", AnalyticsAPI.as_view(), name="api-analytics"),
]
