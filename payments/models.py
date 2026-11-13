from django.conf import settings
from django.db import models
from django.db.models import Q

from bookings.models import Reservation


class Payment(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        CREATED = "created", "Created"
        AUTHORIZED = "authorized", "Authorized"
        CAPTURED = "captured", "Captured"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"
        PAID = "paid", "Captured"
        REFUNDED = "refunded", "Refunded"

    reservation = models.ForeignKey(Reservation, on_delete=models.PROTECT, related_name="payments")
    customer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="payments")
    provider_order_id = models.CharField(max_length=100, blank=True, db_index=True)
    provider_payment_id = models.CharField(max_length=100, blank=True, unique=True, null=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.CREATED)
    failure_reason = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("reservation",),
                condition=Q(status="created"),
                name="unique_created_payment_per_reservation",
            ),
        ]

    def __str__(self):
        return f"{self.reservation_id}: {self.status}"

    @property
    def is_captured(self):
        return self.status in (self.Status.CAPTURED, self.Status.PAID)


class RazorpayWebhookEvent(models.Model):
    event_id = models.CharField(max_length=100, unique=True)
    event_type = models.CharField(max_length=80)
    received_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.event_type}: {self.event_id}"
