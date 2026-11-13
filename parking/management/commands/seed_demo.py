from django.core.management.base import BaseCommand

from parking.models import ParkingFloor, ParkingLocation, ParkingSlot


class Command(BaseCommand):
    help = "Create an example SmartPark garage and parking spaces."

    def handle(self, *args, **options):
        location, _ = ParkingLocation.objects.get_or_create(
            name="Central Station Garage",
            city="Pune",
            defaults={
                "address": "18 Station Road",
                "description": "A convenient, secure garage beside the central station.",
                "hourly_rate": 40,
                "has_ev_charging": True,
            },
        )
        floor, _ = ParkingFloor.objects.get_or_create(location=location, name="Level 1")
        for code, vehicle_type, ev in (
            ("A01", ParkingSlot.VehicleType.CAR, False),
            ("A02", ParkingSlot.VehicleType.CAR, False),
            ("A03", ParkingSlot.VehicleType.CAR, True),
            ("B01", ParkingSlot.VehicleType.MOTORCYCLE, False),
            ("B02", ParkingSlot.VehicleType.MOTORCYCLE, False),
            ("E01", ParkingSlot.VehicleType.EV, True),
        ):
            ParkingSlot.objects.get_or_create(
                floor=floor,
                code=code,
                defaults={"vehicle_type": vehicle_type, "is_ev_charging": ev},
            )
        self.stdout.write(self.style.SUCCESS(f"Demo data ready: {location.name} ({location.pk})"))
