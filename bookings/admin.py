from django.contrib import admin

from .models import ParkingSession, QRCodeTicket, Reservation

admin.site.register((Reservation, QRCodeTicket, ParkingSession))
