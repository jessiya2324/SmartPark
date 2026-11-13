import hashlib
import hmac
import json
import logging
from decimal import Decimal

import razorpay
from django.conf import settings
from django.core.mail import send_mail
from django.db import IntegrityError, transaction
from django.utils import timezone
from requests.exceptions import RequestException

from bookings.models import Reservation
from bookings.services import ensure_qr_ticket
from .models import Payment, RazorpayWebhookEvent


logger = logging.getLogger(__name__)
_CAPTURED_STATUSES = (Payment.Status.CAPTURED, Payment.Status.PAID)


class PaymentConfigurationError(ValueError):
    pass


class PaymentProviderError(ValueError):
    pass


def razorpay_configuration_error():
    key_id = settings.RAZORPAY_KEY_ID
    key_secret = settings.RAZORPAY_KEY_SECRET
    if not key_id or not key_secret:
        return "Razorpay credentials are missing. Add your Test Mode key ID and secret to .env, then restart Django."
    if settings.RAZORPAY_TEST_MODE and not key_id.startswith("rzp_test_"):
        return "Test Mode is enabled, but the configured Razorpay key ID is not a Test Mode key."
    return ""


def _client():
    configuration_error = razorpay_configuration_error()
    if configuration_error:
        raise PaymentConfigurationError(configuration_error)
    key_id = settings.RAZORPAY_KEY_ID
    key_secret = settings.RAZORPAY_KEY_SECRET
    return razorpay.Client(auth=(key_id, key_secret))


def _checkout_order(payment):
    return {
        "id": payment.provider_order_id,
        "amount": int(Decimal(payment.amount) * 100),
        "currency": "INR",
        "key_id": settings.RAZORPAY_KEY_ID,
    }


def _validate_provider_payment(provider_payment, payment, order_id, payment_id):
    expected_amount = int(Decimal(payment.amount) * 100)
    if (
        not isinstance(provider_payment, dict)
        or provider_payment.get("id") != payment_id
        or provider_payment.get("order_id") != order_id
        or provider_payment.get("amount") != expected_amount
        or provider_payment.get("currency") != "INR"
    ):
        raise ValueError("Payment details do not match this booking. The booking is not confirmed.")
    return expected_amount


@transaction.atomic
def _mark_payment_failed(payment_id, provider_payment_id, reason):
    payment = Payment.objects.select_for_update().get(pk=payment_id)
    if payment.status in _CAPTURED_STATUSES:
        return payment
    duplicate = Payment.objects.filter(
        provider_payment_id=provider_payment_id
    ).exclude(pk=payment.pk).exists()
    if duplicate:
        raise ValueError("This Razorpay payment is already linked to another booking.")
    payment.provider_payment_id = provider_payment_id
    payment.status = Payment.Status.FAILED
    payment.failure_reason = str(reason or "Payment failed. You can retry checkout.")[:255]
    payment.save(
        update_fields=("provider_payment_id", "status", "failure_reason", "updated_at")
    )
    return payment


@transaction.atomic
def _mark_payment_authorized(payment_id, provider_payment_id):
    payment = Payment.objects.select_for_update().get(pk=payment_id)
    if payment.status in _CAPTURED_STATUSES or payment.status in (
        Payment.Status.FAILED,
        Payment.Status.CANCELLED,
    ):
        return payment
    if payment.provider_payment_id and payment.provider_payment_id != provider_payment_id:
        raise ValueError("A different payment is already linked to this order.")
    payment.provider_payment_id = provider_payment_id
    payment.status = Payment.Status.AUTHORIZED
    payment.failure_reason = ""
    payment.save(
        update_fields=("provider_payment_id", "status", "failure_reason", "updated_at")
    )
    return payment


@transaction.atomic
def create_order(reservation, user):
    reservation = Reservation.objects.select_for_update().select_related(
        "slot__floor__location"
    ).get(pk=reservation.pk)
    if reservation.customer_id != user.id:
        raise ValueError("You cannot pay for another customer's reservation.")
    if reservation.status != Reservation.Status.PENDING:
        raise ValueError("This reservation is no longer awaiting payment.")
    if reservation.starts_at <= timezone.now() or reservation.ends_at <= timezone.now():
        raise ValueError("This reservation has expired. Please create a new booking.")
    if Payment.objects.filter(
        reservation=reservation,
        status__in=_CAPTURED_STATUSES,
    ).exists():
        raise ValueError("This reservation has already been paid.")

    payment = Payment.objects.select_for_update().filter(
        reservation=reservation,
        status__in=(Payment.Status.CREATED, Payment.Status.AUTHORIZED),
    ).order_by("-created_at").first()
    if payment and payment.customer_id != user.id:
        raise ValueError("This payment is not associated with the booking customer.")
    if payment and payment.amount != reservation.quoted_amount:
        raise ValueError("The payment amount no longer matches this booking.")
    if payment and payment.provider_order_id:
        logger.info("[PAYMENT] Reusing Razorpay order for booking %s", reservation.booking_id)
        return payment, _checkout_order(payment)

    logger.info("[PAYMENT] Razorpay order creation started for booking %s", reservation.booking_id)
    client = _client()
    amount = int(Decimal(reservation.quoted_amount) * 100)
    try:
        order = client.order.create(
            {
                "amount": amount,
                "currency": "INR",
                "receipt": str(reservation.booking_id),
                "notes": {"booking_id": str(reservation.booking_id)},
            }
        )
    except (razorpay.errors.BadRequestError, razorpay.errors.GatewayError,
            razorpay.errors.ServerError, RequestException) as error:
        logger.exception("[PAYMENT] Razorpay order creation failed for booking %s", reservation.booking_id)
        raise PaymentProviderError(
            "Razorpay could not create the payment order. Check your Test Mode credentials and internet connection."
        ) from error

    if not isinstance(order, dict):
        raise ValueError("Razorpay returned an invalid order. Please retry.")
    order_id = order.get("id")
    if not order_id or order.get("amount") != amount or order.get("currency") != "INR":
        raise ValueError("Razorpay returned an invalid order. Please retry.")

    if payment:
        payment.provider_order_id = order_id
        payment.save(update_fields=("provider_order_id", "updated_at"))
    else:
        try:
            with transaction.atomic():
                payment = Payment.objects.create(
                    reservation=reservation,
                    customer=user,
                    amount=reservation.quoted_amount,
                    provider_order_id=order_id,
                )
        except IntegrityError:
            payment = Payment.objects.filter(
                reservation=reservation,
                status=Payment.Status.CREATED,
                provider_order_id__gt="",
            ).first()
            if payment is None:
                raise
    logger.info(
        "[PAYMENT] Razorpay order created for booking %s",
        reservation.booking_id,
    )
    return payment, _checkout_order(payment)


def _confirm_paid(payment_id, provider_payment_id):
    with transaction.atomic():
        payment = Payment.objects.select_for_update().select_related(
            "reservation__customer",
            "reservation__slot__floor__location",
            "reservation__vehicle",
        ).get(pk=payment_id)
        if payment.status in _CAPTURED_STATUSES:
            if payment.provider_payment_id == provider_payment_id:
                reservation = payment.reservation
                if reservation.status == Reservation.Status.PENDING:
                    reservation.status = Reservation.Status.CONFIRMED
                    reservation.save(update_fields=("status",))
                ensure_qr_ticket(reservation)
                return payment
            raise ValueError("A different payment has already completed for this order.")

        reservation = Reservation.objects.select_for_update().get(pk=payment.reservation_id)
        if reservation.status != Reservation.Status.PENDING:
            raise ValueError("This reservation is no longer awaiting payment.")
        if Payment.objects.filter(
            reservation=reservation, status__in=_CAPTURED_STATUSES
        ).exclude(pk=payment.pk).exists():
            raise ValueError("This reservation has already been paid.")
        if Payment.objects.filter(provider_payment_id=provider_payment_id).exclude(pk=payment.pk).exists():
            raise ValueError("This Razorpay payment is already linked to another booking.")

        payment.provider_payment_id = provider_payment_id
        payment.status = Payment.Status.CAPTURED
        payment.failure_reason = ""
        payment.paid_at = timezone.now()
        payment.save(
            update_fields=("provider_payment_id", "status", "failure_reason", "paid_at", "updated_at")
        )
        reservation.status = Reservation.Status.CONFIRMED
        reservation.save(update_fields=("status",))
        ensure_qr_ticket(reservation)

        transaction.on_commit(
            lambda: send_mail(
                "Your SmartPark booking is confirmed",
                (
                    f"Booking {reservation.booking_id} is confirmed.\n"
                    f"Location: {reservation.slot.floor.location.name}\n"
                    f"Space: {reservation.slot.code}\n"
                    "Your QR ticket is available in your SmartPark dashboard."
                ),
                settings.DEFAULT_FROM_EMAIL,
                [reservation.customer.email],
            ),
            robust=True,
        )
    return payment


def verify_payment(user, data):
    order_id = data.get("razorpay_order_id", "")
    payment_id = data.get("razorpay_payment_id", "")
    signature = data.get("razorpay_signature", "")
    if not all(isinstance(value, str) and value.strip() for value in (order_id, payment_id, signature)):
        raise ValueError("Payment details are incomplete. Please retry checkout.")

    payment = Payment.objects.select_related("reservation").filter(
        provider_order_id=order_id,
        customer=user,
    ).first()
    if payment is None:
        raise ValueError("Payment order was not found for this customer.")
    if payment.status not in (
        Payment.Status.CREATED,
        Payment.Status.AUTHORIZED,
        Payment.Status.FAILED,
        *_CAPTURED_STATUSES,
    ):
        raise ValueError("This payment can no longer be verified. Please retry from your dashboard.")

    client = _client()
    try:
        client.utility.verify_payment_signature(
            {
                "razorpay_order_id": order_id,
                "razorpay_payment_id": payment_id,
                "razorpay_signature": signature,
            }
        )
    except razorpay.errors.SignatureVerificationError as error:
        logger.warning(
            "Razorpay Checkout signature verification failed for payment record %s",
            payment.pk,
        )
        raise ValueError("Payment signature could not be verified. The booking is not confirmed.") from error

    expected_amount = int(Decimal(payment.amount) * 100)
    try:
        provider_payment = client.payment.fetch(payment_id)
    except (razorpay.errors.BadRequestError, razorpay.errors.GatewayError,
            razorpay.errors.ServerError, RequestException) as error:
        logger.warning(
            "Razorpay payment lookup failed for payment record %s: %s",
            payment.pk,
            error,
        )
        raise PaymentProviderError(
            "Razorpay could not confirm the payment status. Please retry verification; your booking is not confirmed yet."
        ) from error

    _validate_provider_payment(provider_payment, payment, order_id, payment_id)
    if provider_payment.get("status") == "failed":
        _mark_payment_failed(
            payment.pk,
            payment_id,
            provider_payment.get("error_description") or "Razorpay reports this payment failed.",
        )
        raise ValueError("Razorpay reports this payment failed. You can retry checkout.")
    if provider_payment.get("status") == "authorized":
        _mark_payment_authorized(payment.pk, payment_id)
        if settings.RAZORPAY_CAPTURE_AUTHORIZED_PAYMENTS:
            try:
                client.payment.capture(payment_id, expected_amount)
                provider_payment = client.payment.fetch(payment_id)
            except (razorpay.errors.BadRequestError, razorpay.errors.GatewayError,
                    razorpay.errors.ServerError, RequestException) as error:
                raise PaymentProviderError(
                    "Razorpay authorized the payment but could not confirm capture. Retry verification; the booking is not confirmed yet."
                ) from error
            _validate_provider_payment(provider_payment, payment, order_id, payment_id)
    if provider_payment.get("status") != "captured":
        raise ValueError("Razorpay has not captured this payment yet. Please retry verification shortly.")

    return _confirm_paid(payment.pk, payment_id)


def reconcile_order_payment(user, order_id):
    if not isinstance(order_id, str) or not order_id.strip():
        raise ValueError("Payment order was not provided.")
    payment = Payment.objects.filter(
        provider_order_id=order_id,
        customer=user,
        status__in=(
            Payment.Status.CREATED,
            Payment.Status.AUTHORIZED,
            Payment.Status.FAILED,
            *_CAPTURED_STATUSES,
        ),
    ).first()
    if payment is None:
        raise ValueError("Payment order was not found for this customer.")
    if payment.status in _CAPTURED_STATUSES:
        return payment, "paid"

    client = _client()
    try:
        response = client.order.payments(order_id)
    except (razorpay.errors.BadRequestError, razorpay.errors.GatewayError,
            razorpay.errors.ServerError, RequestException) as error:
        raise PaymentProviderError(
            "Razorpay could not refresh this order's payment status. Please retry shortly."
        ) from error
    items = response.get("items") if isinstance(response, dict) else None
    if not isinstance(items, list):
        raise PaymentProviderError("Razorpay returned an invalid payment status response.")

    expected_amount = int(Decimal(payment.amount) * 100)
    captured = []
    authorized = []
    failed = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("order_id") != order_id:
            raise ValueError("Razorpay returned a payment for a different order.")
        if item.get("amount") != expected_amount or item.get("currency") != "INR":
            raise ValueError("Payment details do not match this booking. The booking is not confirmed.")
        status = item.get("status")
        if status == "captured":
            captured.append(item)
        elif status == "authorized":
            authorized.append(item)
        elif status == "failed":
            failed.append(item)

    if captured:
        provider_payment_id = captured[-1].get("id")
        if not isinstance(provider_payment_id, str) or not provider_payment_id:
            raise PaymentProviderError("Razorpay returned a captured payment without an ID.")
        return _confirm_paid(payment.pk, provider_payment_id), "paid"

    if authorized:
        provider_payment_id = authorized[-1].get("id")
        if not isinstance(provider_payment_id, str) or not provider_payment_id:
            raise PaymentProviderError("Razorpay returned an authorized payment without an ID.")
        payment = _mark_payment_authorized(payment.pk, provider_payment_id)
        if settings.RAZORPAY_CAPTURE_AUTHORIZED_PAYMENTS:
            try:
                client.payment.capture(provider_payment_id, expected_amount)
                provider_payment = client.payment.fetch(provider_payment_id)
            except (razorpay.errors.BadRequestError, razorpay.errors.GatewayError,
                    razorpay.errors.ServerError, RequestException) as error:
                raise PaymentProviderError(
                    "Razorpay authorized the payment but could not confirm capture. Retry status check; the booking is not confirmed yet."
                ) from error
            _validate_provider_payment(provider_payment, payment, order_id, provider_payment_id)
            if provider_payment.get("status") == "captured":
                return _confirm_paid(payment.pk, provider_payment_id), "paid"
        return payment, "authorized"

    if failed and len(failed) == len(items):
        latest = failed[-1]
        provider_payment_id = latest.get("id")
        if not isinstance(provider_payment_id, str) or not provider_payment_id:
            raise PaymentProviderError("Razorpay returned a failed payment without an ID.")
        payment = _mark_payment_failed(
            payment.pk,
            provider_payment_id,
            latest.get("error_description") or "Payment failed. You can retry checkout.",
        )
        return payment, "failed"
    return payment, "pending"


@transaction.atomic
def record_payment_failure(user, data):
    order_id = data.get("razorpay_order_id", "")
    if not isinstance(order_id, str) or not order_id.strip():
        raise ValueError("Payment order was not provided.")
    payment = Payment.objects.select_for_update().filter(
        provider_order_id=order_id,
        customer=user,
        status__in=(
            Payment.Status.CREATED,
            Payment.Status.AUTHORIZED,
            Payment.Status.FAILED,
        ),
    ).first()
    if payment is None:
        raise ValueError("Payment order was not found or is no longer retryable.")

    reason = data.get("error_description") or data.get("reason") or "Payment failed. You can retry checkout."
    payment_id = data.get("razorpay_payment_id", "")
    if payment_id:
        if not isinstance(payment_id, str):
            raise ValueError("Razorpay payment ID is invalid.")
        client = _client()
        try:
            provider_payment = client.payment.fetch(payment_id)
        except (razorpay.errors.BadRequestError, razorpay.errors.GatewayError,
                razorpay.errors.ServerError, RequestException) as error:
            raise PaymentProviderError(
                "Razorpay could not verify the failed payment attempt. Your booking remains unpaid."
            ) from error
        _validate_provider_payment(provider_payment, payment, order_id, payment_id)
        if provider_payment.get("status") != "failed":
            raise ValueError("Razorpay has not confirmed this payment failed. Please retry verification.")
        return _mark_payment_failed(
            payment.pk,
            payment_id,
            provider_payment.get("error_description") or reason,
        )

    payment.failure_reason = str(reason)[:255]
    payment.save(update_fields=("failure_reason", "updated_at"))
    return payment


def process_razorpay_webhook(raw_body, signature, event_id):
    webhook_secret = settings.RAZORPAY_WEBHOOK_SECRET
    if not webhook_secret:
        raise PaymentConfigurationError("Razorpay webhook secret is not configured.")
    if not signature or not event_id:
        raise ValueError("Webhook signature or event ID is missing.")
    if len(raw_body) > 1024 * 1024:
        raise ValueError("Webhook payload is too large.")
    if len(event_id) > 100:
        raise ValueError("Webhook event ID is invalid.")
    expected_signature = hmac.new(
        webhook_secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected_signature, signature):
        raise ValueError("Webhook signature could not be verified.")
    try:
        payload = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ValueError("Webhook payload is not valid JSON.") from error
    if not isinstance(payload, dict):
        raise ValueError("Webhook payload must be a JSON object.")

    event_type = payload.get("event", "")
    if not isinstance(event_type, str) or not event_type or len(event_type) > 80:
        raise ValueError("Webhook event type is missing.")

    with transaction.atomic():
        _, created = RazorpayWebhookEvent.objects.get_or_create(
            event_id=event_id[:100],
            defaults={"event_type": event_type},
        )
        if not created:
            return "duplicate"
        if event_type not in (
            "payment.authorized",
            "payment.captured",
            "order.paid",
            "payment.failed",
        ):
            return "ignored"

        try:
            webhook_payload = payload["payload"]
            if not isinstance(webhook_payload, dict):
                raise TypeError
            payment_payload = webhook_payload["payment"]
            if not isinstance(payment_payload, dict):
                raise TypeError
            entity = payment_payload["entity"]
            if not isinstance(entity, dict):
                raise TypeError
            order_id = entity["order_id"]
            provider_payment_id = entity["id"]
        except (KeyError, TypeError) as error:
            raise ValueError("Webhook payment details are incomplete.") from error
        if not isinstance(order_id, str) or not isinstance(provider_payment_id, str):
            raise ValueError("Webhook payment identifiers are invalid.")

        payment = Payment.objects.select_for_update().filter(
            provider_order_id=order_id
        ).first()
        if payment is None:
            raise ValueError("Webhook order does not match a SmartPark payment.")

        expected_amount = int(Decimal(payment.amount) * 100)
        if (
            entity.get("amount") != expected_amount
            or entity.get("currency") != "INR"
        ):
            raise ValueError("Webhook payment does not match the booking amount.")

        if event_type == "payment.authorized":
            if entity.get("status") != "authorized":
                raise ValueError("Authorized webhook does not report an authorized payment.")
            _mark_payment_authorized(payment.pk, provider_payment_id)
            return "processed"

        if event_type == "payment.failed":
            if payment.status not in _CAPTURED_STATUSES:
                if entity.get("status") != "failed":
                    raise ValueError("Failed webhook does not report a failed payment.")
                _mark_payment_failed(
                    payment.pk,
                    provider_payment_id,
                    entity.get("error_description")
                    or entity.get("error_reason")
                    or "Payment failed. You can retry checkout.",
                )
            return "processed"

        if entity.get("status") != "captured":
            raise ValueError("Captured webhook payment does not match the booking amount.")
        if event_type == "order.paid":
            order_payload = webhook_payload.get("order")
            order_entity = (
                order_payload.get("entity")
                if isinstance(order_payload, dict)
                else None
            )
            if (
                not isinstance(order_entity, dict)
                or
                order_entity.get("id") != order_id
                or order_entity.get("status") != "paid"
                or order_entity.get("amount") != expected_amount
                or order_entity.get("currency") != "INR"
            ):
                raise ValueError("Paid webhook order does not match the booking amount.")
        _confirm_paid(payment.pk, provider_payment_id)
        return "processed"
