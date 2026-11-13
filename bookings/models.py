import uuid

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models

from parking.models import ParkingSlot, Vehicle


class Reservation(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending payment"
        CONFIRMED = "confirmed", "Confirmed"
        ACTIVE = "active", "Vehicle parked"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"
        EXPIRED = "expired", "Expired"

    booking_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    customer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="reservations")
    slot = models.ForeignKey(ParkingSlot, on_delete=models.PROTECT, related_name="reservations")
    vehicle = models.ForeignKey(Vehicle, on_delete=models.PROTECT, related_name="reservations")
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    quoted_amount = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(0)])
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [models.Index(fields=("slot", "starts_at", "ends_at", "status"))]

    def __str__(self):
        return str(self.booking_id)


class QRCodeTicket(models.Model):
    reservation = models.OneToOneField(Reservation, on_delete=models.CASCADE, related_name="ticket")
    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    image = models.ImageField(upload_to="tickets/", blank=True)
    payload_version = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)


class ParkingSession(models.Model):
    reservation = models.OneToOneField(Reservation, on_delete=models.CASCADE, related_name="parking_session")
    entered_at = models.DateTimeField(null=True, blank=True)
    exited_at = models.DateTimeField(null=True, blank=True)
    final_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
