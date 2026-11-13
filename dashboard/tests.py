from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from parking.models import ParkingFloor, ParkingLocation, ParkingSlot
from accounts.models import User


class PublicPageTests(TestCase):
    def test_public_pages_render(self):
        for path in ("/", "/parking/", "/accounts/login/", "/accounts/register/"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)

    def test_invalid_price_filter_is_reported_without_breaking_search(self):
        response = self.client.get("/parking/?max_rate=not-a-price")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Enter a valid maximum hourly price.")

    def test_city_search_matches_case_insensitively_and_preserves_filters(self):
        location =         location = ParkingLocation.objects.create(
            name="Pune Central Garage",
            address="1 Main Street",
            city="Pune",
            hourly_rate=40,
        )
        floor = ParkingFloor.objects.create(location=location, name="Ground")
        ParkingSlot.objects.create(floor=floor, code="A1", vehicle_type=ParkingSlot.VehicleType.CAR)

        response = self.client.get("/parking/?city=pUnE&vehicle_type=car&max_rate=50")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, location.name)
        self.assertContains(response, 'value="pUnE"')
        self.assertContains(response, 'value="50"')
        self.assertContains(response, '<option value="car" selected>')

    def test_location_detail_renders_for_anonymous_visitors(self):
        location = ParkingLocation.objects.create(
            name="Sample Garage",
            address="1 Main Street",
            city="Pune",
            hourly_rate=40,
        )
        floor = ParkingFloor.objects.create(location=location, name="Ground")
        ParkingSlot.objects.create(floor=floor, code="A1")

        response = self.client.get(f"/parking/{location.pk}/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Sample Garage")

    def test_customer_dashboard_and_profile_render(self):
        user = User.objects.create_user(username="driver", email="driver@example.com", password="test-pass-123")
        self.client.force_login(user)

        self.assertEqual(self.client.get("/dashboard/").status_code, 200)
        self.assertEqual(self.client.get("/accounts/profile/").status_code, 200)

    def test_operator_dashboard_renders_managed_spaces(self):
        operator = User.objects.create_user(
            username="operator", email="operator@example.com", password="test-pass-123", role=User.Role.OPERATOR
        )
        location = ParkingLocation.objects.create(
            name="Managed Garage",
            address="2 Main Street",
            city="Pune",
            hourly_rate=40,
            operator=operator,
        )
        floor = ParkingFloor.objects.create(location=location, name="Ground")
        ParkingSlot.objects.create(floor=floor, code="A1")
        self.client.force_login(operator)

        response = self.client.get("/dashboard/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Managed Garage")

    def test_demo_seed_is_repeatable(self):
        call_command("seed_demo", stdout=StringIO())
        call_command("seed_demo", stdout=StringIO())

        self.assertEqual(ParkingLocation.objects.filter(name="Central Station Garage").count(), 1)
        self.assertEqual(ParkingSlot.objects.filter(floor__location__name="Central Station Garage").count(), 6)
