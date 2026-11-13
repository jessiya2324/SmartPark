from django.urls import path

from .consumers import AvailabilityConsumer

websocket_urlpatterns = [path("ws/availability/", AvailabilityConsumer.as_asgi())]
