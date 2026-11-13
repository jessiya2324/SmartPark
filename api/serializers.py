from django.contrib.auth import get_user_model
from rest_framework import serializers

from bookings.models import Reservation
from parking.models import ParkingLocation, ParkingSlot, Vehicle
from payments.models import Payment

User = get_user_model()


class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, min_length=8)

    class Meta:
        model = User
        fields = ("id", "username", "email", "password", "first_name", "last_name")

    def create(self, validated_data):
        return User.objects.create_user(**validated_data)


class ProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ("id", "username", "email", "first_name", "last_name", "phone", "role")
        read_only_fields = ("id", "username", "role")


class ParkingLocationSerializer(serializers.ModelSerializer):
    available_slots = serializers.SerializerMethodField()

    class Meta:
        model = ParkingLocation
        fields = ("id", "name", "address", "city", "description", "hourly_rate", "has_ev_charging", "available_slots")

    def get_available_slots(self, location):
        return ParkingSlot.objects.filter(floor__location=location, is_active=True).count()


class ParkingSlotSerializer(serializers.ModelSerializer):
    floor_name = serializers.CharField(source="floor.name", read_only=True)
    location_name = serializers.CharField(source="floor.location.name", read_only=True)

    class Meta:
        model = ParkingSlot
        fields = ("id", "floor", "floor_name", "location_name", "code", "vehicle_type", "is_ev_charging", "is_active")


class ReservationSerializer(serializers.ModelSerializer):
    location = serializers.CharField(source="slot.floor.location.name", read_only=True)
    slot_code = serializers.CharField(source="slot.code", read_only=True)

    class Meta:
        model = Reservation
        fields = ("booking_id", "slot", "vehicle", "location", "slot_code", "starts_at", "ends_at", "quoted_amount", "status")
        read_only_fields = ("booking_id", "quoted_amount", "status")


class ReservationCreateSerializer(serializers.Serializer):
    slot_id = serializers.IntegerField()
    vehicle_id = serializers.IntegerField()
    starts_at = serializers.DateTimeField()
    ends_at = serializers.DateTimeField()

    def validate_vehicle_id(self, value):
        if not Vehicle.objects.filter(pk=value, owner=self.context["request"].user).exists():
            raise serializers.ValidationError("Choose one of your registered vehicles.")
        return value


class PaymentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Payment
        fields = ("id", "reservation", "amount", "status", "created_at", "paid_at")
