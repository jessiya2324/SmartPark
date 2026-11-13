from django.conf import settings
from django.db.models import Sum
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenObtainPairView

from accounts.models import User
from bookings.models import ParkingSession, Reservation
from bookings.services import (
    cancel_reservation,
    create_reservation,
    finish_parking_session,
    ticket_token_from_qr,
)
from parking.models import ParkingLocation, ParkingSlot
from payments.models import Payment
from payments.services import (
    PaymentConfigurationError,
    PaymentProviderError,
    create_order,
    verify_payment,
)
from .permissions import IsPlatformAdmin
from .serializers import (
    ParkingLocationSerializer, ParkingSlotSerializer, PaymentSerializer, ProfileSerializer,
    RegisterSerializer, ReservationCreateSerializer, ReservationSerializer,
)


class RegisterAPI(generics.CreateAPIView):
    queryset = User.objects.all()
    serializer_class = RegisterSerializer
    permission_classes = (permissions.AllowAny,)


class ProfileAPI(generics.RetrieveUpdateAPIView):
    serializer_class = ProfileSerializer

    def get_object(self):
        return self.request.user


class LocationAPI(generics.ListAPIView):
    queryset = ParkingLocation.objects.filter(is_active=True)
    serializer_class = ParkingLocationSerializer
    permission_classes = (permissions.AllowAny,)
    filterset_fields = ("city", "has_ev_charging")


class SlotAPI(generics.ListAPIView):
    serializer_class = ParkingSlotSerializer
    permission_classes = (permissions.AllowAny,)
    filterset_fields = ("floor__location", "vehicle_type", "is_ev_charging")

    def get_queryset(self):
        return ParkingSlot.objects.filter(is_active=True)


class ReservationListAPI(generics.ListAPIView):
    serializer_class = ReservationSerializer

    def get_queryset(self):
        queryset = Reservation.objects.select_related("slot__floor__location")
        if self.request.user.is_staff or self.request.user.role == User.Role.ADMIN:
            return queryset
        return queryset.filter(customer=self.request.user)


class ReservationCreateAPI(APIView):
    def post(self, request):
        serializer = ReservationCreateSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        try:
            reservation = create_reservation(
                customer=request.user,
                slot_id=serializer.validated_data["slot_id"],
                vehicle_id=serializer.validated_data["vehicle_id"],
                starts_at=serializer.validated_data["starts_at"],
                ends_at=serializer.validated_data["ends_at"],
            )
        except ValueError as error:
            return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(ReservationSerializer(reservation).data, status=status.HTTP_201_CREATED)


class ReservationCancelAPI(APIView):
    def post(self, request):
        booking_id = request.data.get("booking_id")
        reservation = get_object_or_404(Reservation, booking_id=booking_id)
        try:
            cancel_reservation(reservation, request.user)
        except PermissionError:
            return Response({"detail": "Not permitted."}, status=status.HTTP_403_FORBIDDEN)
        except ValueError as error:
            return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(ReservationSerializer(reservation).data)


class PaymentOrderAPI(APIView):
    def post(self, request):
        reservation = get_object_or_404(Reservation, booking_id=request.data.get("booking_id"))
        try:
            payment, order = create_order(reservation, request.user)
        except PaymentConfigurationError as error:
            return Response({"detail": str(error)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        except PaymentProviderError as error:
            return Response({"detail": str(error)}, status=status.HTTP_502_BAD_GATEWAY)
        except ValueError as error:
            return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        return Response({
            "payment": PaymentSerializer(payment).data,
            "order_id": order["id"],
            "key": settings.RAZORPAY_KEY_ID,
            "amount": order["amount"],
            "currency": order["currency"],
        }, status=status.HTTP_201_CREATED)


class PaymentVerifyAPI(APIView):
    def post(self, request):
        try:
            payment = verify_payment(request.user, request.data)
        except PaymentConfigurationError as error:
            return Response({"detail": str(error)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        except PaymentProviderError as error:
            return Response({"detail": str(error)}, status=status.HTTP_502_BAD_GATEWAY)
        except ValueError as error:
            return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(PaymentSerializer(payment).data)


class QRVerifyAPI(APIView):
    def post(self, request):
        token = ticket_token_from_qr(request.data.get("token", ""))
        if not token:
            return Response({"detail": "Ticket not found."}, status=status.HTTP_404_NOT_FOUND)
        reservation = Reservation.objects.filter(ticket__token=token).select_related(
            "slot__floor__location", "vehicle", "customer"
        ).first()
        if not reservation:
            return Response({"detail": "Ticket not found."}, status=status.HTTP_404_NOT_FOUND)
        if not (request.user.is_staff or request.user.role == User.Role.ADMIN or
                reservation.slot.floor.location.operator_id == request.user.id):
            return Response({"detail": "Not permitted."}, status=status.HTTP_403_FORBIDDEN)
        if reservation.status not in (Reservation.Status.CONFIRMED, Reservation.Status.ACTIVE):
            return Response({"detail": "Ticket is not valid for entry."}, status=status.HTTP_409_CONFLICT)
        return Response({
            "booking_id": reservation.booking_id,
            "customer": reservation.customer.get_full_name() or reservation.customer.username,
            "vehicle": reservation.vehicle.plate_number,
            "slot": reservation.slot.code,
            "status": reservation.status,
        })


class ParkingActionAPI(APIView):
    action = None

    def post(self, request):
        token = ticket_token_from_qr(request.data.get("token", ""))
        if not token:
            return Response({"detail": "Ticket not found."}, status=status.HTTP_404_NOT_FOUND)
        reservation = get_object_or_404(Reservation, ticket__token=token)
        managed = request.user.is_staff or request.user.role == User.Role.ADMIN or (
            reservation.slot.floor.location.operator_id == request.user.id
        )
        if not managed:
            return Response({"detail": "Not permitted."}, status=status.HTTP_403_FORBIDDEN)
        if self.action == "entry" and reservation.status == Reservation.Status.CONFIRMED:
            session = reservation.parking_session
            session.entered_at = timezone.now()
            session.save(update_fields=("entered_at",))
            reservation.status = Reservation.Status.ACTIVE
            reservation.save(update_fields=("status",))
        elif self.action == "exit" and reservation.status == Reservation.Status.ACTIVE:
            finish_parking_session(reservation.parking_session)
        else:
            return Response({"detail": "Reservation cannot be processed for that action."},
                            status=status.HTTP_409_CONFLICT)
        return Response(ReservationSerializer(reservation).data)


class ParkingEntryAPI(ParkingActionAPI):
    action = "entry"


class ParkingExitAPI(ParkingActionAPI):
    action = "exit"


class AnalyticsAPI(APIView):
    permission_classes = (IsPlatformAdmin,)

    def get(self, request):
        return Response({
            "users": User.objects.count(),
            "locations": ParkingLocation.objects.count(),
            "reservations": Reservation.objects.count(),
            "revenue": Payment.objects.filter(
                status__in=(Payment.Status.CAPTURED, Payment.Status.PAID)
            ).aggregate(total=Sum("amount"))["total"] or 0,
            "active_sessions": ParkingSession.objects.filter(entered_at__isnull=False, exited_at__isnull=True).count(),
        })
