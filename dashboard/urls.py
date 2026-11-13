from django.urls import path

from . import views

app_name = "dashboard"
urlpatterns = [
    path("", views.home, name="home"),
    path("payment/<uuid:booking_id>/", views.payment_page, name="payment"),
    path("payment/verify/", views.payment_verify, name="payment_verify"),
    path("payment/failure/", views.payment_failure, name="payment_failure"),
    path("payment/reconcile/", views.payment_reconcile, name="payment_reconcile"),
    path("ticket/<uuid:booking_id>/", views.ticket, name="ticket"),
    path("ticket/<uuid:booking_id>/pdf/", views.ticket_pdf, name="ticket_pdf"),
    path("receipt/<int:payment_id>/", views.payment_receipt, name="payment_receipt"),
    path("scan/", views.operator_scan, name="operator_scan"),
    path("slots/<int:slot_id>/availability/", views.operator_slot_update, name="operator_slot_update"),
]
