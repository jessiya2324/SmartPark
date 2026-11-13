from datetime import datetime, timedelta, timezone
from decimal import Decimal
from tempfile import TemporaryDirectory
from unittest import TestCase

from django.test import TestCase as DjangoTestCase, override_settings
from django.urls import reverse
from django.utils import timezone as django_timezone

from accounts.models import User
from .models import Reservation
from parking.models import ParkingFloor, ParkingLocation, ParkingSlot, Vehicle

from .services import calculate_charge, create_reservation


class ParkingChargeTests(TestCase):
    def test_charge_rounds_partial_hour_up(self):
        starts_at = datetime(2026, 11, 6, 9, 0, tzinfo=timezone.utc)
        ends_at = starts_at + timedelta(hours=1, minutes=1)

        self.assertEqual(calculate_charge(Decimal("25.50"), starts_at, ends_at), Decimal("51.00"))

    def test_charge_has_one_hour_minimum(self):
        starts_at = datetime(2026, 11, 6, 9, 0, tzinfo=timezone.utc)
        ends_at = starts_at + timedelta(minutes=10)

        self.assertEqual(calculate_charge(Decimal("25.50"), starts_at, ends_at), Decimal("25.50"))


class ReservationServiceTests(DjangoTestCase):
    def test_conflicting_reservation_cannot_reuse_a_slot(self):
        user = User.objects.create_user(username="driver", email="driver@example.com", password="test-pass-123")
        location = ParkingLocation.objects.create(
            name="Test Garage",
            address="1 Main Street",
            city="Pune",
            hourly_rate=Decimal("40.00"),
        )
        floor = ParkingFloor.objects.create(location=location, name="Ground")
        slot = ParkingSlot.objects.create(floor=floor, code="A1")
        vehicle = Vehicle.objects.create(owner=user, plate_number="TEST-001", vehicle_type="car")
        starts_at = django_timezone.now() + timedelta(days=1)
        ends_at = starts_at + timedelta(hours=2)

        with TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                create_reservation(
                    customer=user,
                    slot_id=slot.pk,
                    vehicle_id=vehicle.pk,
                    starts_at=starts_at,
                    ends_at=ends_at,
                    location_id=location.pk,
                )
                with self.assertRaisesMessage(ValueError, "just been reserved"):
                    create_reservation(
                        customer=user,
                        slot_id=slot.pk,
                        vehicle_id=vehicle.pk,
                        starts_at=starts_at + timedelta(minutes=30),
                        ends_at=ends_at + timedelta(minutes=30),
                        location_id=location.pk,
                    )

    def test_expired_pending_reservation_no_longer_blocks_a_slot(self):
        user = User.objects.create_user(username="driver", email="driver@example.com")
        location = ParkingLocation.objects.create(
            name="Test Garage",
            address="1 Main Street",
            city="Pune",
            hourly_rate=Decimal("40.00"),
        )
        floor = ParkingFloor.objects.create(location=location, name="Ground")
        slot = ParkingSlot.objects.create(floor=floor, code="A1")
        vehicle = Vehicle.objects.create(owner=user, plate_number="TEST-001", vehicle_type="car")
        old_start = django_timezone.now() + timedelta(days=1)
        expired_reservation = create_reservation(
            customer=user,
            slot_id=slot.pk,
            vehicle_id=vehicle.pk,
            starts_at=old_start,
            ends_at=old_start + timedelta(hours=1),
            location_id=location.pk,
        )
        expired_reservation.starts_at = django_timezone.now() - timedelta(hours=2)
        expired_reservation.ends_at = django_timezone.now() - timedelta(hours=1)
        expired_reservation.save(update_fields=("starts_at", "ends_at"))

        replacement_start = django_timezone.now() + timedelta(hours=1)
        replacement = create_reservation(
            customer=user,
            slot_id=slot.pk,
            vehicle_id=vehicle.pk,
            starts_at=replacement_start,
            ends_at=replacement_start + timedelta(hours=1),
            location_id=location.pk,
        )

        expired_reservation.refresh_from_db()
        self.assertEqual(expired_reservation.status, Reservation.Status.EXPIRED)
        self.assertEqual(replacement.status, Reservation.Status.PENDING)


class BookingViewTests(DjangoTestCase):
    def setUp(self):
        self.customer = User.objects.create_user(
            username="booking-driver",
            email="booking-driver@example.com",
            password="test-password",
        )
        self.location = ParkingLocation.objects.create(
            name="Booking Test Garage",
            address="1 Main Street",
            city="Pune",
            hourly_rate=Decimal("40.00"),
        )
        self.floor = ParkingFloor.objects.create(location=self.location, name="Ground")
        self.slot = ParkingSlot.objects.create(floor=self.floor, code="A1")
        self.vehicle = Vehicle.objects.create(
            owner=self.customer,
            plate_number="BOOK-001",
            vehicle_type="car",
        )
        self.client.force_login(self.customer)
        self.url = reverse("bookings:create", kwargs={"location_id": self.location.pk})

    def test_reservation_form_posts_required_fields_with_csrf(self):
        response = self.client.get(
            reverse("parking:location_detail", kwargs={"pk": self.location.pk})
        )

        self.assertContains(response, f'<form method="post" action="{self.url}"')
        self.assertContains(response, 'name="csrfmiddlewaretoken"')
        for field in ("vehicle_id", "starts_at", "ends_at", "slot_id"):
            self.assertContains(response, f'name="{field}"')

    def booking_data(self, starts_at=None, ends_at=None):
        starts_at = starts_at or django_timezone.localtime(
            django_timezone.now() + timedelta(days=2)
        ).strftime("%Y-%m-%dT%H:%M")
        ends_at = ends_at or django_timezone.localtime(
            django_timezone.now() + timedelta(days=2, hours=2)
        ).strftime("%Y-%m-%dT%H:%M")
        return {
            "vehicle_id": str(self.vehicle.pk),
            "slot_id": str(self.slot.pk),
            "starts_at": starts_at,
            "ends_at": ends_at,
        }

    def test_valid_submission_redirects_to_checkout_for_the_created_booking(self):
        data = self.booking_data()

        response = self.client.post(self.url, data)

        reservation = Reservation.objects.get(customer=self.customer)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response["Location"],
            f"{reverse('dashboard:payment', kwargs={'booking_id': reservation.booking_id})}?checkout=1",
        )
        self.assertEqual(reservation.status, Reservation.Status.PENDING)
        self.assertEqual(reservation.slot_id, self.slot.pk)
        self.assertEqual(reservation.vehicle_id, self.vehicle.pk)

    def test_missing_slot_shows_the_validation_message(self):
        data = self.booking_data()
        data.pop("slot_id")

        response = self.client.post(self.url, data, follow=True)

        self.assertContains(response, "Choose an available parking space.")
        self.assertFalse(Reservation.objects.exists())

    def test_invalid_entry_datetime_shows_the_validation_message(self):
        data = self.booking_data(starts_at="not-a-date")

        response = self.client.post(self.url, data, follow=True)

        self.assertContains(response, "Enter a valid entry date and time.")
        self.assertFalse(Reservation.objects.exists())

    def test_invalid_exit_datetime_shows_the_validation_message(self):
        data = self.booking_data(ends_at="not-a-date")

        response = self.client.post(self.url, data, follow=True)

        self.assertContains(response, "Enter a valid exit date and time.")
        self.assertFalse(Reservation.objects.exists())

    def test_exit_before_entry_shows_the_validation_message(self):
        starts_at = django_timezone.localtime(
            django_timezone.now() + timedelta(days=2)
        ).strftime("%Y-%m-%dT%H:%M")
        ends_at = django_timezone.localtime(
            django_timezone.now() + timedelta(days=2, hours=1)
        ).strftime("%Y-%m-%dT%H:%M")

        response = self.client.post(
            self.url,
            self.booking_data(starts_at=ends_at, ends_at=starts_at),
            follow=True,
        )

        self.assertContains(response, "Choose a future start time and an exit time after entry.")
        self.assertFalse(Reservation.objects.exists())

    def test_missing_vehicle_shows_the_validation_message(self):
        data = self.booking_data()
        data["vehicle_id"] = ""

        response = self.client.post(self.url, data, follow=True)

        self.assertContains(response, "Choose one of your registered vehicles.")
        self.assertFalse(Reservation.objects.exists())

    def test_already_reserved_slot_shows_the_conflict_message(self):
        starts_at = django_timezone.now() + timedelta(days=2)
        create_reservation(
            customer=self.customer,
            slot_id=self.slot.pk,
            vehicle_id=self.vehicle.pk,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(hours=2),
            location_id=self.location.pk,
        )
        data = self.booking_data(
            starts_at=django_timezone.localtime(starts_at + timedelta(minutes=30)).strftime(
                "%Y-%m-%dT%H:%M"
            ),
            ends_at=django_timezone.localtime(starts_at + timedelta(hours=3)).strftime(
                "%Y-%m-%dT%H:%M"
            ),
        )

        response = self.client.post(self.url, data, follow=True)

        self.assertContains(response, "That parking slot has just been reserved.")
        self.assertEqual(Reservation.objects.count(), 1)
