from datetime import timedelta
from decimal import Decimal
from io import BytesIO
import logging
import uuid

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Sum
from django.db.models.functions import TruncMonth
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from bookings.models import ParkingSession, Reservation
from bookings.services import ensure_qr_ticket, finish_parking_session, ticket_token_from_qr
from parking.models import ParkingLocation, ParkingSlot
from payments.models import Payment
from payments.services import (
    PaymentConfigurationError,
    PaymentProviderError,
    create_order,
    reconcile_order_payment,
    razorpay_configuration_error,
    record_payment_failure,
    verify_payment,
)
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas


logger = logging.getLogger(__name__)
CAPTURED_PAYMENT_STATUSES = (Payment.Status.CAPTURED, Payment.Status.PAID)


@login_required
def home(request):
    if request.user.is_staff or request.user.role == "admin":
        return admin_dashboard(request)
    if request.user.role == "operator":
        return operator_dashboard(request)
    reservations = Reservation.objects.filter(customer=request.user).select_related(
        "slot__floor__location", "vehicle"
    ).prefetch_related("payments")
    now = timezone.now()
    paid = Payment.objects.filter(
        customer=request.user,
        status__in=CAPTURED_PAYMENT_STATUSES,
    ).aggregate(total=Sum("amount"))
    return render(
        request,
        "dashboard/customer.html",
        {
            "reservations": reservations[:8],
            "total_bookings": reservations.count(),
            "upcoming_count": reservations.filter(
                status=Reservation.Status.CONFIRMED, starts_at__gt=now
            ).count(),
            "completed_count": reservations.filter(status=Reservation.Status.COMPLETED).count(),
            "cancelled_count": reservations.filter(status=Reservation.Status.CANCELLED).count(),
            "spent": paid["total"] or Decimal("0.00"),
            "vehicles": request.user.vehicles.all(),
            "payments": Payment.objects.filter(customer=request.user).select_related(
                "reservation"
            ).order_by("-created_at")[:8],
            "now": now,
        },
    )


def admin_dashboard(request):
    from accounts.models import User

    now = timezone.now()
    paid = Payment.objects.filter(status__in=CAPTURED_PAYMENT_STATUSES)
    reservations = Reservation.objects.all()
    active_slots = ParkingSlot.objects.filter(is_active=True)
    total_slots = active_slots.count()
    occupied_slot_ids = Reservation.objects.filter(
        slot__is_active=True,
        status__in=(Reservation.Status.CONFIRMED, Reservation.Status.ACTIVE),
        starts_at__lte=now,
        ends_at__gt=now,
    ).values("slot_id").distinct()
    occupied_slots = active_slots.filter(pk__in=occupied_slot_ids).count()
    available_slots = total_slots - occupied_slots
    monthly = (
        paid.filter(created_at__year=now.year, created_at__month=now.month).aggregate(total=Sum("amount"))["total"]
        or 0
    )
    monthly_rows = (
        paid.filter(created_at__gte=now - timedelta(days=180))
        .annotate(month=TruncMonth("created_at"))
        .values("month")
        .annotate(total=Sum("amount"))
        .order_by("month")
    )
    return render(
        request,
        "dashboard/admin.html",
        {
            "users_count": User.objects.count(),
            "locations_count": ParkingLocation.objects.count(),
            "bookings_count": reservations.count(),
            "total_slots": total_slots,
            "available_slots": available_slots,
            "occupied_slots": occupied_slots,
            "revenue": paid.aggregate(total=Sum("amount"))["total"] or 0,
            "monthly_revenue": monthly,
            "revenue_labels": [row["month"].strftime("%b %Y") for row in monthly_rows],
            "revenue_values": [float(row["total"]) for row in monthly_rows],
            "occupancy": round(occupied_slots * 100 / total_slots) if total_slots else 0,
        },
    )


def operator_dashboard(request):
    locations = ParkingLocation.objects.filter(operator=request.user)
    reservations = Reservation.objects.filter(
        slot__floor__location__in=locations, starts_at__date=timezone.localdate()
    ).select_related(
        "slot", "vehicle", "customer"
    )
    slots = list(
        ParkingSlot.objects.filter(floor__location__in=locations).select_related("floor", "floor__location")
    )
    active_slots = [slot for slot in slots if slot.is_active]
    now = timezone.now()
    occupied_slot_ids = set(
        Reservation.objects.filter(
            slot__in=active_slots,
            status__in=(Reservation.Status.CONFIRMED, Reservation.Status.ACTIVE),
            starts_at__lte=now,
            ends_at__gt=now,
        ).values_list("slot_id", flat=True)
    )
    return render(
        request,
        "dashboard/operator.html",
        {
            "reservations": reservations[:30],
            "locations": locations,
            "slots": slots,
            "total_slots": len(active_slots),
            "occupied_slots": len(occupied_slot_ids),
            "available_slots": sum(slot.id not in occupied_slot_ids for slot in active_slots),
        },
    )


@login_required
def payment_page(request, booking_id):
    reservation = get_object_or_404(Reservation, booking_id=booking_id, customer=request.user)
    if reservation.status in (
        Reservation.Status.CONFIRMED,
        Reservation.Status.ACTIVE,
        Reservation.Status.COMPLETED,
    ):
        return redirect("dashboard:ticket", booking_id=booking_id)
    payment = Payment.objects.filter(reservation=reservation).order_by("-created_at").first()
    configuration_error = razorpay_configuration_error()
    order = None
    payment_error = ""
    if reservation.status != Reservation.Status.PENDING:
        payment_error = "This booking is not available for payment."
    elif reservation.starts_at <= timezone.now() or reservation.ends_at <= timezone.now():
        Reservation.objects.filter(
            pk=reservation.pk,
            status=Reservation.Status.PENDING,
        ).update(status=Reservation.Status.EXPIRED)
        reservation.refresh_from_db()
        if reservation.status in (
            Reservation.Status.CONFIRMED,
            Reservation.Status.ACTIVE,
            Reservation.Status.COMPLETED,
        ):
            return redirect("dashboard:ticket", booking_id=booking_id)
        payment_error = "This reservation has expired. Please create a new booking."
    elif configuration_error:
        payment_error = configuration_error
    elif payment and payment.status in (
        Payment.Status.CREATED,
        Payment.Status.AUTHORIZED,
    ) and payment.provider_order_id:
        order = {
            "id": payment.provider_order_id,
            "amount": int(payment.amount * 100),
            "currency": "INR",
            "key_id": settings.RAZORPAY_KEY_ID,
        }
    payment_allowed = (
        reservation.status == Reservation.Status.PENDING
        and reservation.starts_at > timezone.now()
        and reservation.ends_at > timezone.now()
        and not configuration_error
    )

    if request.method == "POST":
        logger.info("[PAYMENT] Continue payment request received for booking %s", booking_id)
        if payment_error:
            if request.headers.get("x-requested-with") == "XMLHttpRequest":
                return JsonResponse({"error": payment_error}, status=503 if configuration_error else 400)
            return render(
                request,
                "dashboard/payment.html",
                {
                    "reservation": reservation,
                    "payment": payment,
                    "payment_error": payment_error,
                    "razorpay_configured": not configuration_error,
                    "razorpay_test_mode": settings.RAZORPAY_TEST_MODE,
                    "payment_allowed": payment_allowed,
                },
            )
        try:
            payment, order = create_order(reservation, request.user)
        except PaymentConfigurationError as error:
            logger.exception("[PAYMENT] Razorpay configuration failed for booking %s", booking_id)
            if request.headers.get("x-requested-with") == "XMLHttpRequest":
                return JsonResponse({"error": str(error)}, status=503)
            payment_error = str(error)
        except PaymentProviderError as error:
            logger.exception("[PAYMENT] Razorpay order creation failed for booking %s", booking_id)
            if request.headers.get("x-requested-with") == "XMLHttpRequest":
                return JsonResponse({"error": str(error)}, status=502)
            payment_error = str(error)
        except ValueError as error:
            logger.exception("[PAYMENT] Payment validation failed for booking %s", booking_id)
            if request.headers.get("x-requested-with") == "XMLHttpRequest":
                return JsonResponse({"error": str(error)}, status=400)
            payment_error = str(error)
        else:
            if request.headers.get("x-requested-with") == "XMLHttpRequest":
                logger.info("[PAYMENT] Redirecting to payment checkout for booking %s", booking_id)
                return JsonResponse({"order": order, "payment_id": payment.pk})
        if payment_error:
            return render(
                request,
                "dashboard/payment.html",
                {
                    "reservation": reservation,
                    "payment": payment,
                    "payment_error": payment_error,
                    "razorpay_configured": not configuration_error,
                    "razorpay_test_mode": settings.RAZORPAY_TEST_MODE,
                    "payment_allowed": payment_allowed,
                },
            )
        return render(
            request,
            "dashboard/payment.html",
            {
                "reservation": reservation,
                "payment": payment,
                "order": order,
                "razorpay_configured": True,
                "razorpay_test_mode": settings.RAZORPAY_TEST_MODE,
                "payment_allowed": payment_allowed,
            },
        )
    return render(
        request,
        "dashboard/payment.html",
        {
            "reservation": reservation,
            "payment": payment,
            "order": order,
            "payment_error": payment_error or (payment.failure_reason if payment else ""),
            "razorpay_configured": not configuration_error,
            "razorpay_test_mode": settings.RAZORPAY_TEST_MODE,
            "payment_allowed": payment_allowed,
        },
    )


def _payment_order_redirect(request):
    payment = Payment.objects.filter(
        provider_order_id=request.POST.get("razorpay_order_id", ""),
        customer=request.user,
    ).first()
    if payment:
        return redirect("dashboard:payment", booking_id=payment.reservation.booking_id)
    try:
        booking_id = uuid.UUID(request.POST.get("booking_id", ""))
    except (AttributeError, TypeError, ValueError):
        return redirect("dashboard:home")
    reservation = Reservation.objects.filter(
        booking_id=booking_id,
        customer=request.user,
    ).only("booking_id").first()
    if reservation:
        return redirect("dashboard:payment", booking_id=reservation.booking_id)
    return redirect("dashboard:home")


@login_required
def payment_verify(request):
    if request.method != "POST":
        return redirect("dashboard:home")
    try:
        payment = verify_payment(request.user, request.POST)
    except PaymentConfigurationError as error:
        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JsonResponse({"error": str(error)}, status=503)
        messages.error(request, str(error))
        return _payment_order_redirect(request)
    except PaymentProviderError as error:
        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JsonResponse({"error": str(error)}, status=502)
        messages.error(request, str(error))
        return _payment_order_redirect(request)
    except ValueError as error:
        logger.warning(
            "Payment verification failed for user %s: %s",
            request.user.pk,
            error,
        )
        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JsonResponse({"error": str(error)}, status=400)
        messages.error(request, str(error))
        return _payment_order_redirect(request)
    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JsonResponse(
            {
                "message": "Payment received. Your parking reservation is confirmed.",
                "redirect_url": reverse(
                    "dashboard:ticket",
                    kwargs={"booking_id": payment.reservation.booking_id},
                ),
            }
        )
    messages.success(request, "Payment received. Your parking reservation is confirmed.")
    return redirect("dashboard:ticket", booking_id=payment.reservation.booking_id)


@login_required
def payment_failure(request):
    if request.method != "POST":
        return redirect("dashboard:home")
    try:
        payment = record_payment_failure(request.user, request.POST)
    except ValueError as error:
        logger.warning(
            "Payment failure callback could not be recorded for user %s: %s",
            request.user.pk,
            error,
        )
        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JsonResponse({"error": str(error)}, status=400)
        messages.error(request, str(error))
        return _payment_order_redirect(request)
    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JsonResponse(
            {
                "message": payment.failure_reason,
                "retryable": True,
                "attempt_failed": payment.status == Payment.Status.FAILED,
            }
        )
    messages.error(request, payment.failure_reason)
    return redirect("dashboard:payment", booking_id=payment.reservation.booking_id)


@login_required
@require_POST
def payment_reconcile(request):
    try:
        payment, status = reconcile_order_payment(
            request.user,
            request.POST.get("razorpay_order_id", ""),
        )
    except PaymentConfigurationError as error:
        return JsonResponse({"error": str(error)}, status=503)
    except PaymentProviderError as error:
        return JsonResponse({"error": str(error)}, status=502)
    except ValueError as error:
        return JsonResponse({"error": str(error)}, status=400)

    if status == "paid":
        return JsonResponse(
            {
                "status": status,
                "redirect_url": reverse(
                    "dashboard:ticket",
                    kwargs={"booking_id": payment.reservation.booking_id},
                ),
            }
        )
    messages = {
        "failed": "Razorpay confirms the payment failed. You can retry with a new payment attempt.",
        "authorized": "Your payment is authorized but not captured yet. The booking is not confirmed.",
        "pending": "No captured payment was found yet. If you have just paid, check again shortly.",
    }
    return JsonResponse(
        {"status": status, "message": messages[status], "retryable": True}
    )


@login_required
def ticket(request, booking_id):
    reservation = get_object_or_404(
        Reservation.objects.select_related("slot__floor__location", "vehicle").prefetch_related("payments"),
        booking_id=booking_id,
        customer=request.user,
    )
    if reservation.status not in (Reservation.Status.CONFIRMED, Reservation.Status.ACTIVE, Reservation.Status.COMPLETED):
        if reservation.status == Reservation.Status.PENDING:
            messages.info(request, "Complete payment to activate this ticket.")
            return redirect("dashboard:payment", booking_id=booking_id)
        messages.info(request, "This reservation does not have an active ticket.")
        return redirect("dashboard:home")
    ticket = ensure_qr_ticket(reservation)
    return render(request, "dashboard/ticket.html", {"reservation": reservation, "ticket": ticket})


@login_required
def ticket_pdf(request, booking_id):
    reservation = get_object_or_404(
        Reservation.objects.select_related("slot__floor__location", "vehicle").prefetch_related("payments"),
        booking_id=booking_id,
        customer=request.user,
    )
    if reservation.status not in (Reservation.Status.CONFIRMED, Reservation.Status.ACTIVE, Reservation.Status.COMPLETED):
        if reservation.status == Reservation.Status.PENDING:
            return redirect("dashboard:payment", booking_id=booking_id)
        messages.error(request, "A ticket is available only for a confirmed booking.")
        return redirect("dashboard:home")
    ticket = ensure_qr_ticket(reservation)
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    pdf.setTitle(f"SmartPark ticket {reservation.booking_id}")
    pdf.setFont("Helvetica-Bold", 20)
    pdf.drawString(55, 785, "SmartPark parking ticket")
    pdf.setFont("Helvetica", 11)
    lines = (
        f"Booking reference: {reservation.booking_id}",
        f"Customer: {reservation.customer.get_full_name() or reservation.customer.username}",
        f"Status: {reservation.get_status_display()}",
        f"Location: {reservation.slot.floor.location.name}",
        f"Address: {reservation.slot.floor.location.address}",
        f"Space: {reservation.slot.code} ({reservation.slot.floor.name})",
        f"Vehicle: {reservation.vehicle.plate_number}",
        f"Arrival: {timezone.localtime(reservation.starts_at).strftime('%d %b %Y, %I:%M %p')}",
        f"Exit: {timezone.localtime(reservation.ends_at).strftime('%d %b %Y, %I:%M %p')}",
        f"Amount: INR {reservation.quoted_amount}",
    )
    for index, line in enumerate(lines):
        pdf.drawString(55, 740 - index * 24, line)
    if ticket.image:
        from reportlab.lib.utils import ImageReader

        pdf.drawImage(ImageReader(ticket.image), 55, 470, width=145, height=145)
        pdf.drawString(55, 450, "Show this QR code to the parking operator.")
    pdf.save()
    response = HttpResponse(buffer.getvalue(), content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="smartpark-{reservation.booking_id}.pdf"'
    return response


@login_required
def payment_receipt(request, payment_id):
    payment = get_object_or_404(
        Payment,
        pk=payment_id,
        customer=request.user,
        status__in=CAPTURED_PAYMENT_STATUSES,
    )
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    pdf.setTitle(f"SmartPark receipt {payment.pk}")
    pdf.setFont("Helvetica-Bold", 20)
    pdf.drawString(55, 785, "SmartPark payment receipt")
    pdf.setFont("Helvetica", 11)
    lines = (
        f"Receipt: SP-{payment.pk:06d}",
        f"Booking: {payment.reservation.booking_id}",
        f"Location: {payment.reservation.slot.floor.location.name}",
        f"Vehicle: {payment.reservation.vehicle.plate_number}",
        f"Amount paid: INR {payment.amount}",
        f"Payment reference: {payment.provider_payment_id or 'N/A'}",
        f"Paid at: {timezone.localtime(payment.paid_at).strftime('%d %b %Y, %I:%M %p')}",
        "Status: Paid",
    )
    for index, line in enumerate(lines):
        pdf.drawString(55, 740 - index * 26, line)
    pdf.save()
    response = HttpResponse(buffer.getvalue(), content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="smartpark-receipt-{payment.pk}.pdf"'
    return response


@login_required
def operator_scan(request):
    if request.user.role not in ("operator", "admin") and not request.user.is_staff:
        from django.core.exceptions import PermissionDenied

        raise PermissionDenied
    reservation = None
    if request.method == "POST":
        token = ticket_token_from_qr(request.POST.get("token", ""))
        if token:
            reservation = Reservation.objects.filter(ticket__token=token).select_related(
                "slot__floor__location", "vehicle", "customer"
            ).first()
        if reservation and reservation.status not in (Reservation.Status.CONFIRMED, Reservation.Status.ACTIVE):
            messages.error(request, "This ticket is not valid for entry.")
            reservation = None
        if reservation and request.POST.get("action") in ("entry", "exit"):
            managed = request.user.is_staff or reservation.slot.floor.location.operator_id == request.user.id
            if not managed:
                from django.core.exceptions import PermissionDenied

                raise PermissionDenied
            session = reservation.parking_session
            if request.POST["action"] == "entry" and reservation.status == Reservation.Status.CONFIRMED:
                session.entered_at = timezone.now()
                session.save(update_fields=("entered_at",))
                reservation.status = Reservation.Status.ACTIVE
                reservation.save(update_fields=("status",))
            elif request.POST["action"] == "exit" and reservation.status == Reservation.Status.ACTIVE:
                finish_parking_session(session)
            else:
                messages.error(request, "This reservation cannot be processed for that action.")
                return redirect("dashboard:operator_scan")
            messages.success(request, "Parking status updated.")
            return redirect("dashboard:operator_scan")
        if not reservation:
            messages.error(request, "Ticket not found.")
    return render(request, "dashboard/operator_scan.html", {"reservation": reservation})


@login_required
def operator_slot_update(request, slot_id):
    if request.method != "POST":
        return redirect("dashboard:home")
    slot = get_object_or_404(ParkingSlot.objects.select_related("floor__location"), pk=slot_id)
    if not (
        request.user.is_staff
        or request.user.role == "admin"
        or (request.user.role == "operator" and slot.floor.location.operator_id == request.user.id)
    ):
        from django.core.exceptions import PermissionDenied

        raise PermissionDenied
    slot.is_active = request.POST.get("is_active") == "true"
    slot.save(update_fields=("is_active",))
    from bookings.services import broadcast_availability

    broadcast_availability(slot.pk)
    messages.success(request, f"Space {slot.code} availability updated.")
    return redirect("dashboard:home")
