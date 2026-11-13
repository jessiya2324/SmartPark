from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from datetime import time


class ParkingLocation(models.Model):
    name = models.CharField(max_length=140)
    address = models.CharField(max_length=255)
    city = models.CharField(max_length=100, db_index=True)
    description = models.TextField(blank=True)
    hourly_rate = models.DecimalField(max_digits=8, decimal_places=2, validators=[MinValueValidator(0)])
    opening_time = models.TimeField(default=time.min)
    closing_time = models.TimeField(default=time(23, 59))
    has_ev_charging = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    operator = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="managed_locations"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.name}, {self.city}"


class ParkingFloor(models.Model):
    location = models.ForeignKey(ParkingLocation, on_delete=models.CASCADE, related_name="floors")
    name = models.CharField(max_length=60)

    class Meta:
        unique_together = ("location", "name")
        ordering = ("name",)

    def __str__(self):
        return f"{self.location.name} — {self.name}"


class ParkingSlot(models.Model):
    class VehicleType(models.TextChoices):
        CAR = "car", "Car"
        MOTORCYCLE = "motorcycle", "Motorcycle"
        EV = "ev", "Electric vehicle"

    floor = models.ForeignKey(ParkingFloor, on_delete=models.CASCADE, related_name="slots")
    code = models.CharField(max_length=20)
    vehicle_type = models.CharField(max_length=12, choices=VehicleType.choices, default=VehicleType.CAR)
    is_ev_charging = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("floor", "code"), name="unique_slot_code_per_floor"),
        ]
        ordering = ("code",)

    @property
    def location(self):
        return self.floor.location

    def __str__(self):
        return f"{self.floor.name} / {self.code}"


class Vehicle(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="vehicles")
    plate_number = models.CharField(max_length=20, unique=True)
    vehicle_type = models.CharField(max_length=12, choices=ParkingSlot.VehicleType.choices)
    nickname = models.CharField(max_length=40, blank=True)

    def __str__(self):
        return f"{self.plate_number} ({self.get_vehicle_type_display()})"
