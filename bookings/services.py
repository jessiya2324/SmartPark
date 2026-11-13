from datetime import timedelta
from decimal import Decimal
from io import BytesIO
import json
import logging
import uuid

import qrcode
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone

from .models import ParkingSession, QRCodeTicket, Reservation

logger = logging.getLogger(__name__)


def calculate_charge(rate, starts_at, ends_at):
    seconds = (ends_at - starts_at).total_seconds()
    hours = max(1, int((seconds + 3599) // 3600))
    return (Decimal(rate) * hours).quantize(Decimal("0.01"))


@transaction.atomic
def create_reservation(*, customer, slot_id, vehicle_id, starts_at, ends_at, location_id=None):
    if starts_at <= timezone.now() or ends_at <= starts_at:
        raise ValueError("Choose a future start time and an exit time after entry.")
    from parking.models import ParkingSlot, Vehicle

    slot = ParkingSlot.objects.select_for_update().select_related("floor__location").filter(
        pk=slot_id, is_active=True
    ).first()
    if slot is None:
        raise ValueError("Choose an available parking space.")
    vehicle = Vehicle.objects.filter(pk=vehicle_id, owner=customer).first()
    if vehicle is None:
        raise ValueError("Choose one of your registered vehicles.")
    if location_id is not None and slot.floor.location_id != location_id:
        raise ValueError("Choose a parking space at this location.")
    if vehicle.owner_id != customer.id or vehicle.vehicle_type != slot.vehicle_type:
        raise ValueError("Choose a vehicle you own that matches the selected slot.")
    if slot.is_ev_charging and not slot.floor.location.has_ev_charging:
        raise ValueError("This EV slot is not currently available.")
    Reservation.objects.filter(
        slot=slot,
        status=Reservation.Status.PENDING,
        starts_at__lte=timezone.now(),
    ).update(status=Reservation.Status.EXPIRED)
    conflict = Reservation.objects.filter(
        slot=slot,
        status__in=(Reservation.Status.PENDING, Reservation.Status.CONFIRMED, Reservation.Status.ACTIVE),
        starts_at__lt=ends_at,
        ends_at__gt=starts_at,
    ).exists()
    if conflict:
        raise ValueError("That parking slot has just been reserved. Please choose another.")
    amount = calculate_charge(slot.floor.location.hourly_rate, starts_at, ends_at)
    reservation = Reservation.objects.create(
        customer=customer, slot=slot, vehicle=vehicle, starts_at=starts_at, ends_at=ends_at, quoted_amount=amount
    )
    ParkingSession.objects.create(reservation=reservation)
    transaction.on_commit(lambda: broadcast_availability(slot.id), robust=True)
    return reservation


def ensure_qr_ticket(reservation):
    ticket, _ = QRCodeTicket.objects.get_or_create(reservation=reservation)
    if not ticket.image or ticket.payload_version < 2:
        payload = {
            "token": str(ticket.token),
            "booking_reference": str(reservation.booking_id),
            "customer": reservation.customer.get_full_name() or reservation.customer.username,
            "vehicle": reservation.vehicle.plate_number,
            "location": reservation.slot.floor.location.name,
            "slot": reservation.slot.code,
            "starts_at": timezone.localtime(reservation.starts_at).isoformat(),
            "ends_at": timezone.localtime(reservation.ends_at).isoformat(),
        }
        qr_image = qrcode.make(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        buffer = BytesIO()
        qr_image.save(buffer, format="PNG")
        ticket.image.save(
            f"{reservation.booking_id}.png",
            ContentFile(buffer.getvalue()),
            save=True,
        )
        ticket.payload_version = 2
        ticket.save(update_fields=("payload_version",))
    return ticket


def ticket_token_from_qr(value):
    if not isinstance(value, str):
        return ""
    value = value.strip()
    try:
        token = uuid.UUID(value)
    except (ValueError, TypeError, AttributeError):
        try:
            payload = json.loads(value)
            token = uuid.UUID(payload.get("token", ""))
        except (json.JSONDecodeError, AttributeError, TypeError, ValueError):
            return ""
    return str(token)


def broadcast_availability(slot_id):
    layer = get_channel_layer()
    try:
        if layer is None:
            raise RuntimeError("No channel layer is configured for availability broadcasts.")
        async_to_sync(layer.group_send)(
            "parking_availability",
            {"type": "availability.update", "payload": {"slot_id": slot_id, "updated": True}},
        )
    except Exception:
        logger.exception("Failed to broadcast parking availability for slot_id=%s", slot_id)
        raise
    logger.info("Broadcast parking availability for slot_id=%s", slot_id)


@transaction.atomic
def cancel_reservation(reservation, customer):
    locked = Reservation.objects.select_for_update().get(pk=reservation.pk)
    if locked.customer_id != customer.id:
        raise PermissionError("You cannot cancel this reservation.")
    if locked.status != Reservation.Status.CONFIRMED or locked.starts_at <= timezone.now():
        raise ValueError("Only future confirmed reservations can be cancelled.")
    locked.status = Reservation.Status.CANCELLED
    locked.save(update_fields=("status",))
    transaction.on_commit(lambda: broadcast_availability(locked.slot_id), robust=True)
    return locked


def finish_parking_session(session):
    session.exited_at = timezone.now()
    hours = max(1, int(((session.exited_at - session.entered_at).total_seconds() + 3599) // 3600))
    session.final_amount = calculate_charge(
        session.reservation.slot.floor.location.hourly_rate,
        session.entered_at,
        session.entered_at + timedelta(hours=hours),
    )
    session.reservation.status = Reservation.Status.COMPLETED
    session.reservation.save(update_fields=("status",))
    session.save(update_fields=("exited_at", "final_amount"))
    return session
