from django.urls import path

from .views import razorpay_webhook

app_name = "payments"
urlpatterns = [
    path("razorpay/webhook/", razorpay_webhook, name="razorpay_webhook"),
]
