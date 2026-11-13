from django.contrib import admin

from .models import ParkingFloor, ParkingLocation, ParkingSlot, Vehicle

admin.site.register((ParkingLocation, ParkingFloor, ParkingSlot, Vehicle))
