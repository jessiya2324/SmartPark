from decimal import Decimal, InvalidOperation

from django.db.models import Count
from django.contrib import messages
from django.shortcuts import get_object_or_404, render
from django.utils import timezone

from bookings.models import Reservation
from .models import ParkingLocation, ParkingSlot


def location_list(request):
    locations = ParkingLocation.objects.filter(is_active=True).annotate(slot_count=Count("floors__slots"))
    city = request.GET.get("city", "").strip()
    vehicle_type = request.GET.get("vehicle_type", "").strip()
    max_rate = request.GET.get("max_rate", "").strip()
    if city:
        locations = locations.filter(city__icontains=city)
    if max_rate:
        try:
            max_rate_value = Decimal(max_rate)
            if not max_rate_value.is_finite() or max_rate_value < 0:
                raise InvalidOperation
        except (InvalidOperation, ValueError):
            messages.error(request, "Enter a valid maximum hourly price.")
        else:
            locations = locations.filter(hourly_rate__lte=max_rate_value)
    if vehicle_type in ParkingSlot.VehicleType.values:
        locations = locations.filter(floors__slots__vehicle_type=vehicle_type).distinct()
    return render(
        request,
        "parking/location_list.html",
        {
            "locations": locations,
            "city": city,
            "vehicle_type": vehicle_type,
            "max_rate": max_rate,
        },
    )


def location_detail(request, pk):
    location = get_object_or_404(ParkingLocation, pk=pk, is_active=True)
    slots = ParkingSlot.objects.filter(floor__location=location, is_active=True).select_related("floor")
    now = timezone.localtime()
    occupied_ids = Reservation.objects.filter(
        slot__floor__location=location,
        status__in=(Reservation.Status.CONFIRMED, Reservation.Status.ACTIVE),
        starts_at__lte=now,
        ends_at__gt=now,
    ).values_list("slot_id", flat=True)
    return render(
        request,
        "parking/location_detail.html",
        {
            "location": location,
            "slots": slots,
            "occupied_ids": list(occupied_ids),
            "vehicles": request.user.vehicles.all() if request.user.is_authenticated else [],
        },
    )
