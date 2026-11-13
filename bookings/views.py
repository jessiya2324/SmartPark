from datetime import datetime
import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from parking.models import ParkingLocation
from .models import Reservation
from .services import cancel_reservation, create_reservation


logger = logging.getLogger(__name__)


def _parse_booking_datetime(value, label):
    try:
        if "T" not in value:
            raise ValueError
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"Enter a valid {label} date and time.") from error
    return parsed if timezone.is_aware(parsed) else timezone.make_aware(parsed)


@login_required
@require_POST
def create_booking(request, location_id):
    location = get_object_or_404(ParkingLocation, pk=location_id, is_active=True)
    vehicle_id = request.POST.get("vehicle_id", "").strip()
    starts_value = request.POST.get("starts_at", "").strip()
    ends_value = request.POST.get("ends_at", "").strip()
    slot_id = request.POST.get("slot_id", "").strip()

    logger.info("[BOOKING] Continue payment request received")
    logger.info("[BOOKING] Vehicle ID received: %s", vehicle_id or "(missing)")
    logger.info("[BOOKING] Entry datetime received: %s", starts_value or "(missing)")
    logger.info("[BOOKING] Exit datetime received: %s", ends_value or "(missing)")
    logger.info("[BOOKING] Parking slot ID received: %s", slot_id or "(missing)")
    logger.info("[BOOKING] Form validation started")

    try:
        if not vehicle_id:
            raise ValueError("Choose one of your registered vehicles.")
        if not slot_id:
            raise ValueError("Choose an available parking space.")
        if not starts_value:
            raise ValueError("Choose an entry date and time.")
        if not ends_value:
            raise ValueError("Choose an exit date and time.")

        starts_at = _parse_booking_datetime(starts_value, "entry")
        ends_at = _parse_booking_datetime(ends_value, "exit")
        reservation = create_reservation(
            customer=request.user,
            slot_id=slot_id,
            vehicle_id=vehicle_id,
            starts_at=starts_at,
            ends_at=ends_at,
            location_id=location.pk,
        )
    except (ValueError, TypeError) as error:
        logger.exception("[BOOKING] Validation failed: %s", error)
        messages.error(request, str(error) or "Please check your reservation details.")
        return redirect("parking:location_detail", pk=location.pk)

    logger.info("[BOOKING] Validation successful")
    logger.info("[BOOKING] Booking created successfully: %s", reservation.booking_id)
    messages.success(request, "Slot held. Complete payment to confirm your reservation.")
    payment_url = reverse("dashboard:payment", kwargs={"booking_id": reservation.booking_id})
    return redirect(f"{payment_url}?checkout=1")


@login_required
def cancel_booking(request, booking_id):
    reservation = get_object_or_404(Reservation, booking_id=booking_id)
    try:
        cancel_reservation(reservation, request.user)
    except PermissionError as error:
        raise PermissionDenied from error
    except ValueError as error:
        messages.error(request, str(error))
    else:
        messages.success(request, "Reservation cancelled.")
    return redirect("dashboard:home")
