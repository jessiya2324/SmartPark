import hashlib
import hmac
import json
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from requests.exceptions import ConnectionError

import razorpay
from accounts.models import User
from bookings.models import QRCodeTicket, Reservation
from bookings.services import create_reservation, ensure_qr_ticket
from parking.models import ParkingFloor, ParkingLocation, ParkingSlot, Vehicle

from .models import Payment, RazorpayWebhookEvent
from .services import (
    PaymentConfigurationError,
    PaymentProviderError,
    create_order,
    reconcile_order_payment,
    record_payment_failure,
    verify_payment,
)


@override_settings(
    RAZORPAY_KEY_ID="rzp_test_example",
    RAZORPAY_KEY_SECRET="test-secret",
    RAZORPAY_TEST_MODE=True,
    RAZORPAY_WEBHOOK_SECRET="webhook-secret",
)
class PaymentWorkflowTests(TestCase):
    def setUp(self):
        self.temporary_media = TemporaryDirectory()
        self.media_settings = override_settings(MEDIA_ROOT=self.temporary_media.name)
        self.media_settings.enable()

        self.customer = User.objects.create_user(
            username="payment-driver",
            email="payment-driver@example.com",
            password="test-password",
        )
        self.location = ParkingLocation.objects.create(
            name="Payment Test Garage",
            address="1 Main Street",
            city="Pune",
            hourly_rate=40,
        )
        self.floor = ParkingFloor.objects.create(location=self.location, name="Ground")
        self.slot = ParkingSlot.objects.create(floor=self.floor, code="A1", vehicle_type="car")
        self.vehicle = Vehicle.objects.create(
            owner=self.customer,
            plate_number="PAY-001",
            vehicle_type="car",
        )
        starts_at = timezone.now() + timedelta(days=2)
        self.reservation = create_reservation(
            customer=self.customer,
            slot_id=self.slot.pk,
            vehicle_id=self.vehicle.pk,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(hours=2),
            location_id=self.location.pk,
        )
        self.amount_in_paise = int(self.reservation.quoted_amount * 100)

        self.gateway = Mock()
        self.gateway.order.create.return_value = {
            "id": "order_test_123",
            "amount": self.amount_in_paise,
            "currency": "INR",
        }
        self.gateway.payment.fetch.return_value = {
            "id": "pay_test_123",
            "order_id": "order_test_123",
            "amount": self.amount_in_paise,
            "currency": "INR",
            "status": "captured",
        }
        self.gateway_patch = patch("payments.services._client", return_value=self.gateway)
        self.gateway_patch.start()

    def tearDown(self):
        self.gateway_patch.stop()
        self.media_settings.disable()
        self.temporary_media.cleanup()

    def create_order(self):
        return create_order(self.reservation, self.customer)

    def verification_data(self):
        return {
            "razorpay_order_id": "order_test_123",
            "razorpay_payment_id": "pay_test_123",
            "razorpay_signature": "valid-test-signature",
        }

    def webhook_request(self, event_type, event_id, entity, order_entity=None):
        payload = {"payment": {"entity": entity}}
        if order_entity is not None:
            payload["order"] = {"entity": order_entity}
        body = json.dumps({"event": event_type, "payload": payload}).encode()
        signature = hmac.new(
            b"webhook-secret",
            body,
            hashlib.sha256,
        ).hexdigest()
        return self.client.post(
            reverse("payments:razorpay_webhook"),
            data=body,
            content_type="application/json",
            HTTP_X_RAZORPAY_SIGNATURE=signature,
            HTTP_X_RAZORPAY_EVENT_ID=event_id,
        )

    def test_booking_does_not_get_a_qr_ticket_before_payment(self):
        self.assertFalse(QRCodeTicket.objects.filter(reservation=self.reservation).exists())

    def test_duplicate_order_requests_reuse_the_same_order(self):
        payment, first_order = self.create_order()
        second_payment, second_order = self.create_order()

        self.assertEqual(payment.pk, second_payment.pk)
        self.assertEqual(first_order, second_order)
        self.gateway.order.create.assert_called_once()
        self.assertEqual(Payment.objects.filter(reservation=self.reservation).count(), 1)

    def test_reconciliation_confirms_captured_payment_after_checkout_callback_was_lost(self):
        payment, _ = self.create_order()
        self.gateway.order.payments.return_value = {
            "items": [
                {
                    "id": "pay_test_123",
                    "order_id": payment.provider_order_id,
                    "amount": self.amount_in_paise,
                    "currency": "INR",
                    "status": "captured",
                }
            ]
        }

        reconciled, status = reconcile_order_payment(self.customer, payment.provider_order_id)

        self.assertEqual(status, "paid")
        self.assertEqual(reconciled.status, Payment.Status.CAPTURED)
        self.assertEqual(reconciled.provider_payment_id, "pay_test_123")
        self.reservation.refresh_from_db()
        self.assertEqual(self.reservation.status, Reservation.Status.CONFIRMED)
        self.assertTrue(QRCodeTicket.objects.filter(reservation=self.reservation).exists())
        self.gateway.order.payments.assert_called_once_with(payment.provider_order_id)

    def test_customer_can_reconcile_captured_payment_from_payment_page(self):
        payment, _ = self.create_order()
        self.gateway.order.payments.return_value = {
            "items": [
                {
                    "id": "pay_test_123",
                    "order_id": payment.provider_order_id,
                    "amount": self.amount_in_paise,
                    "currency": "INR",
                    "status": "captured",
                }
            ]
        }
        self.client.force_login(self.customer)

        response = self.client.post(
            reverse("dashboard:payment_reconcile"),
            {"razorpay_order_id": payment.provider_order_id},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "paid")
        self.assertEqual(
            response.json()["redirect_url"],
            reverse("dashboard:ticket", kwargs={"booking_id": self.reservation.booking_id}),
        )

    def test_payment_page_refresh_reuses_the_existing_order(self):
        payment, _ = self.create_order()
        self.client.force_login(self.customer)

        response = self.client.get(
            reverse("dashboard:payment", kwargs={"booking_id": self.reservation.booking_id})
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, payment.provider_order_id)
        self.assertContains(response, "Pay securely")
        self.gateway.order.create.assert_called_once()

    def test_continue_payment_keeps_the_existing_booking_and_opens_its_payment_page(self):
        self.create_order()
        self.client.force_login(self.customer)
        payment_url = reverse(
            "dashboard:payment",
            kwargs={"booking_id": self.reservation.booking_id},
        )

        dashboard_response = self.client.get(reverse("dashboard:home"))
        payment_response = self.client.get(payment_url)

        self.assertContains(dashboard_response, f'href="{payment_url}"')
        self.assertEqual(payment_response.status_code, 200)
        self.assertContains(payment_response, "order_test_123")
        self.assertEqual(Reservation.objects.count(), 1)

    def test_ajax_continue_creates_one_order_and_prevents_duplicate_request(self):
        self.client.force_login(self.customer)
        url = reverse("dashboard:payment", kwargs={"booking_id": self.reservation.booking_id})

        first = self.client.post(url, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        second = self.client.post(url, HTTP_X_REQUESTED_WITH="XMLHttpRequest")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["order"]["id"], second.json()["order"]["id"])
        self.gateway.order.create.assert_called_once()

    def test_expired_pending_booking_is_marked_expired_and_cannot_create_an_order(self):
        self.reservation.starts_at = timezone.now() - timedelta(hours=2)
        self.reservation.ends_at = timezone.now() - timedelta(hours=1)
        self.reservation.save(update_fields=("starts_at", "ends_at"))
        self.client.force_login(self.customer)
        url = reverse("dashboard:payment", kwargs={"booking_id": self.reservation.booking_id})

        response = self.client.get(url)
        self.reservation.refresh_from_db()
        post_response = self.client.post(url, HTTP_X_REQUESTED_WITH="XMLHttpRequest")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.reservation.status, Reservation.Status.EXPIRED)
        self.assertFalse(response.context["payment_allowed"])
        self.assertEqual(post_response.status_code, 400)
        self.gateway.order.create.assert_not_called()
        self.assertFalse(Payment.objects.filter(reservation=self.reservation).exists())

    def test_payment_failure_error_returns_to_the_booking_payment_page(self):
        self.client.force_login(self.customer)
        url = reverse("dashboard:payment", kwargs={"booking_id": self.reservation.booking_id})

        response = self.client.post(
            reverse("dashboard:payment_failure"),
            {
                "booking_id": str(self.reservation.booking_id),
                "razorpay_order_id": "unknown_order",
            },
        )

        self.assertRedirects(response, url)

    def test_customer_cannot_open_another_customers_continue_payment_page(self):
        another_customer = User.objects.create_user(
            username="another-driver",
            email="another-driver@example.com",
        )
        self.client.force_login(another_customer)

        response = self.client.get(
            reverse(
                "dashboard:payment",
                kwargs={"booking_id": self.reservation.booking_id},
            )
        )

        self.assertEqual(response.status_code, 404)

    def test_cancelled_booking_cannot_create_a_payment_order(self):
        self.reservation.status = Reservation.Status.CANCELLED
        self.reservation.save(update_fields=("status",))
        self.client.force_login(self.customer)
        url = reverse("dashboard:payment", kwargs={"booking_id": self.reservation.booking_id})

        response = self.client.post(url, HTTP_X_REQUESTED_WITH="XMLHttpRequest")

        self.assertEqual(response.status_code, 400)
        self.gateway.order.create.assert_not_called()
        self.assertFalse(Payment.objects.filter(reservation=self.reservation).exists())

    def test_successful_payment_confirms_booking_and_creates_detailed_qr_only_once(self):
        payment, _ = self.create_order()
        with patch("bookings.services.qrcode.make", wraps=__import__("qrcode").make) as make_qr:
            paid = verify_payment(self.customer, self.verification_data())
            ticket = QRCodeTicket.objects.get(reservation=self.reservation)
            self.assertEqual(ticket.payload_version, 2)
            self.assertTrue(Path(ticket.image.path).is_file())

            payload = json.loads(make_qr.call_args.args[0])
            self.assertEqual(payload["booking_reference"], str(self.reservation.booking_id))
            self.assertEqual(payload["customer"], self.customer.username)
            self.assertEqual(payload["vehicle"], self.vehicle.plate_number)
            self.assertEqual(payload["location"], self.location.name)
            self.assertEqual(payload["slot"], self.slot.code)

            ensure_qr_ticket(self.reservation)

        self.reservation.refresh_from_db()
        payment.refresh_from_db()
        self.assertEqual(paid.pk, payment.pk)
        self.assertEqual(payment.status, Payment.Status.CAPTURED)
        self.assertEqual(self.reservation.status, Reservation.Status.CONFIRMED)
        make_qr.assert_called_once()
        self.client.force_login(self.customer)
        ticket_response = self.client.get(
            reverse("dashboard:ticket", kwargs={"booking_id": self.reservation.booking_id})
        )
        self.assertContains(ticket_response, "Download payment receipt")

    def test_duplicate_success_callback_is_idempotent(self):
        self.create_order()

        first = verify_payment(self.customer, self.verification_data())
        second = verify_payment(self.customer, self.verification_data())

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(Payment.objects.filter(status=Payment.Status.CAPTURED).count(), 1)
        self.assertEqual(self.gateway.payment.fetch.call_count, 2)

    def test_invalid_signature_does_not_confirm_booking_or_create_qr(self):
        self.create_order()
        self.gateway.utility.verify_payment_signature.side_effect = (
            razorpay.errors.SignatureVerificationError("invalid signature")
        )

        with self.assertRaisesMessage(ValueError, "signature could not be verified"):
            verify_payment(self.customer, self.verification_data())

        self.reservation.refresh_from_db()
        self.assertEqual(self.reservation.status, Reservation.Status.PENDING)
        self.assertFalse(QRCodeTicket.objects.filter(reservation=self.reservation).exists())

    def test_amount_mismatch_does_not_confirm_booking(self):
        self.create_order()
        self.gateway.payment.fetch.return_value["amount"] += 1

        with self.assertRaisesMessage(ValueError, "do not match"):
            verify_payment(self.customer, self.verification_data())

        self.reservation.refresh_from_db()
        self.assertEqual(self.reservation.status, Reservation.Status.PENDING)

    @override_settings(RAZORPAY_CAPTURE_AUTHORIZED_PAYMENTS=False)
    def test_authorized_but_uncaptured_payment_stays_pending_for_verification_retry(self):
        self.create_order()
        self.gateway.payment.fetch.return_value["status"] = "authorized"

        with self.assertRaisesMessage(ValueError, "not captured this payment yet"):
            verify_payment(self.customer, self.verification_data())

        payment = Payment.objects.get(reservation=self.reservation)
        retried_payment, retry_order = self.create_order()
        self.reservation.refresh_from_db()
        self.assertEqual(self.reservation.status, Reservation.Status.PENDING)
        self.assertEqual(payment.status, Payment.Status.AUTHORIZED)
        self.assertEqual(retried_payment.pk, payment.pk)
        self.assertEqual(retry_order["id"], "order_test_123")
        self.gateway.order.create.assert_called_once()

    @override_settings(RAZORPAY_CAPTURE_AUTHORIZED_PAYMENTS=True)
    def test_authorized_payment_is_captured_and_refetched_before_confirmation(self):
        self.create_order()
        authorized = {
            "id": "pay_test_123",
            "order_id": "order_test_123",
            "amount": self.amount_in_paise,
            "currency": "INR",
            "status": "authorized",
        }
        captured = {**authorized, "status": "captured"}
        self.gateway.payment.fetch.side_effect = [authorized, captured]

        payment = verify_payment(self.customer, self.verification_data())

        self.gateway.payment.capture.assert_called_once_with("pay_test_123", self.amount_in_paise)
        self.assertEqual(self.gateway.payment.fetch.call_count, 2)
        self.assertEqual(payment.status, Payment.Status.CAPTURED)
        self.reservation.refresh_from_db()
        self.assertEqual(self.reservation.status, Reservation.Status.CONFIRMED)

    def test_booking_update_failure_rolls_back_payment_and_qr(self):
        self.create_order()
        with patch("payments.services.ensure_qr_ticket", side_effect=OSError("storage unavailable")):
            with self.assertRaisesMessage(OSError, "storage unavailable"):
                verify_payment(self.customer, self.verification_data())

        self.reservation.refresh_from_db()
        payment = Payment.objects.get(reservation=self.reservation)
        self.assertEqual(self.reservation.status, Reservation.Status.PENDING)
        self.assertEqual(payment.status, Payment.Status.CREATED)
        self.assertFalse(QRCodeTicket.objects.filter(reservation=self.reservation).exists())

    def test_failure_is_recorded_and_retry_reuses_the_same_order(self):
        payment, _ = self.create_order()
        record_payment_failure(
            self.customer,
            {
                "razorpay_order_id": payment.provider_order_id,
                "error_description": "Test card declined",
            },
        )

        retry_payment, retry_order = self.create_order()

        self.assertEqual(payment.pk, retry_payment.pk)
        self.assertEqual(payment.provider_order_id, retry_order["id"])
        self.assertEqual(retry_payment.failure_reason, "Test card declined")
        self.assertEqual(self.reservation.status, Reservation.Status.PENDING)
        self.gateway.order.create.assert_called_once()

    def test_provider_confirmed_failure_creates_a_new_order_for_retry(self):
        payment, _ = self.create_order()
        self.gateway.order.create.side_effect = [
            {"id": "order_test_retry", "amount": self.amount_in_paise, "currency": "INR"},
        ]
        self.gateway.payment.fetch.return_value = {
            "id": "pay_test_failed",
            "order_id": "order_test_123",
            "amount": self.amount_in_paise,
            "currency": "INR",
            "status": "failed",
            "error_description": "Payment declined",
        }

        failed = record_payment_failure(
            self.customer,
            {
                "razorpay_order_id": payment.provider_order_id,
                "razorpay_payment_id": "pay_test_failed",
                "error_description": "Payment declined",
            },
        )
        retry_payment, retry_order = self.create_order()

        self.assertEqual(failed.status, Payment.Status.FAILED)
        self.assertEqual(failed.provider_payment_id, "pay_test_failed")
        self.assertNotEqual(retry_payment.pk, failed.pk)
        self.assertEqual(retry_order["id"], "order_test_retry")
        self.assertEqual(Payment.objects.filter(reservation=self.reservation).count(), 2)

    def test_missing_and_non_test_credentials_are_rejected(self):
        self.gateway_patch.stop()
        with override_settings(RAZORPAY_KEY_ID="", RAZORPAY_KEY_SECRET=""):
            with self.assertRaises(PaymentConfigurationError):
                create_order(self.reservation, self.customer)
        with override_settings(RAZORPAY_KEY_ID="rzp_live_example"):
            with self.assertRaises(PaymentConfigurationError):
                create_order(self.reservation, self.customer)

    def test_payment_page_explains_missing_credentials_without_exposing_a_secret(self):
        self.client.force_login(self.customer)
        with override_settings(RAZORPAY_KEY_ID="", RAZORPAY_KEY_SECRET=""):
            response = self.client.get(
                reverse("dashboard:payment", kwargs={"booking_id": self.reservation.booking_id})
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Razorpay credentials are missing")
        self.assertNotContains(response, "test-secret")

    def test_provider_network_failure_returns_a_retryable_error(self):
        self.gateway.order.create.side_effect = ConnectionError("offline")

        with self.assertRaises(PaymentProviderError):
            self.create_order()

        self.reservation.refresh_from_db()
        self.assertEqual(self.reservation.status, Reservation.Status.PENDING)
        self.assertFalse(Payment.objects.filter(reservation=self.reservation).exists())

    def test_ajax_order_creation_reports_network_failure_without_confirming_booking(self):
        self.gateway.order.create.side_effect = ConnectionError("offline")
        self.client.force_login(self.customer)

        response = self.client.post(
            reverse("dashboard:payment", kwargs={"booking_id": self.reservation.booking_id}),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 502)
        self.assertIn("internet connection", response.json()["error"])
        self.reservation.refresh_from_db()
        self.assertEqual(self.reservation.status, Reservation.Status.PENDING)

    def test_checkout_cancellation_and_page_refresh_do_not_confirm_booking(self):
        self.create_order()
        self.client.force_login(self.customer)

        response = self.client.get(
            reverse("dashboard:payment", kwargs={"booking_id": self.reservation.booking_id})
        )

        self.assertContains(response, "Checkout was cancelled")
        self.reservation.refresh_from_db()
        self.assertEqual(self.reservation.status, Reservation.Status.PENDING)
        self.assertFalse(QRCodeTicket.objects.filter(reservation=self.reservation).exists())

    def test_captured_webhook_confirms_and_duplicate_webhook_is_idempotent(self):
        self.create_order()
        entity = {
            "id": "pay_test_123",
            "order_id": "order_test_123",
            "amount": self.amount_in_paise,
            "currency": "INR",
            "status": "captured",
        }

        first = self.webhook_request("payment.captured", "evt_capture_1", entity)
        second = self.webhook_request("payment.captured", "evt_capture_1", entity)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["status"], "duplicate")
        self.assertEqual(Payment.objects.get(reservation=self.reservation).status, Payment.Status.CAPTURED)
        self.assertTrue(QRCodeTicket.objects.filter(reservation=self.reservation).exists())
        self.assertEqual(RazorpayWebhookEvent.objects.count(), 1)

    def test_failed_webhook_is_recorded_without_confirming_booking(self):
        payment, _ = self.create_order()
        self.gateway.order.create.side_effect = [
            {"id": "order_test_retry", "amount": self.amount_in_paise, "currency": "INR"},
        ]
        response = self.webhook_request(
            "payment.failed",
            "evt_failed_1",
            {
                "id": "pay_test_failed",
                "order_id": payment.provider_order_id,
                "amount": self.amount_in_paise,
                "currency": "INR",
                "error_description": "Payment declined",
                "status": "failed",
            },
        )

        self.assertEqual(response.status_code, 200)
        payment.refresh_from_db()
        self.reservation.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.FAILED)
        self.assertEqual(payment.provider_payment_id, "pay_test_failed")
        self.assertEqual(payment.failure_reason, "Payment declined")
        self.assertEqual(self.reservation.status, Reservation.Status.PENDING)
        self.assertFalse(QRCodeTicket.objects.filter(reservation=self.reservation).exists())
        retry_payment, retry_order = self.create_order()
        self.assertNotEqual(retry_payment.pk, payment.pk)
        self.assertEqual(retry_order["id"], "order_test_retry")

    def test_authorized_webhook_never_confirms_or_creates_a_ticket(self):
        self.create_order()
        response = self.webhook_request(
            "payment.authorized",
            "evt_authorized_1",
            {
                "id": "pay_test_123",
                "order_id": "order_test_123",
                "amount": self.amount_in_paise,
                "currency": "INR",
                "status": "authorized",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "processed")
        self.assertEqual(Payment.objects.get(reservation=self.reservation).status, Payment.Status.AUTHORIZED)
        self.assertEqual(self.reservation.status, Reservation.Status.PENDING)
        self.assertFalse(QRCodeTicket.objects.filter(reservation=self.reservation).exists())

    def test_order_paid_webhook_uses_payment_and_order_entities(self):
        self.create_order()
        response = self.webhook_request(
            "order.paid",
            "evt_order_paid_1",
            {
                "id": "pay_test_123",
                "order_id": "order_test_123",
                "amount": self.amount_in_paise,
                "currency": "INR",
                "status": "captured",
            },
            {
                "id": "order_test_123",
                "amount": self.amount_in_paise,
                "currency": "INR",
                "status": "paid",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Payment.objects.get(reservation=self.reservation).status, Payment.Status.CAPTURED)
        self.reservation.refresh_from_db()
        self.assertEqual(self.reservation.status, Reservation.Status.CONFIRMED)
        self.assertTrue(QRCodeTicket.objects.filter(reservation=self.reservation).exists())

    def test_invalid_webhook_signature_is_rejected(self):
        self.create_order()
        response = self.client.post(
            reverse("payments:razorpay_webhook"),
            data=b'{"event":"payment.captured"}',
            content_type="application/json",
            HTTP_X_RAZORPAY_SIGNATURE="invalid",
            HTTP_X_RAZORPAY_EVENT_ID="evt_invalid",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(RazorpayWebhookEvent.objects.count(), 0)

    def test_ticket_pdf_is_downloadable_only_after_success(self):
        self.create_order()
        verify_payment(self.customer, self.verification_data())
        self.client.force_login(self.customer)

        response = self.client.get(
            reverse("dashboard:ticket_pdf", kwargs={"booking_id": self.reservation.booking_id})
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertTrue(response.content.startswith(b"%PDF"))

    def test_dashboard_exposes_retry_and_ticket_actions_for_customer(self):
        payment, _ = self.create_order()
        record_payment_failure(
            self.customer,
            {"razorpay_order_id": payment.provider_order_id, "reason": "declined"},
        )
        self.client.force_login(self.customer)

        response = self.client.get(reverse("dashboard:home"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Total bookings")
        self.assertContains(response, "Upcoming")
        self.assertContains(response, str(self.reservation.booking_id))
        self.assertContains(response, "Payment failed")
        self.assertContains(response, "Retry payment")
