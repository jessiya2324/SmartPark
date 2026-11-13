from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .services import PaymentConfigurationError, process_razorpay_webhook


@csrf_exempt
@require_POST
def razorpay_webhook(request):
    try:
        result = process_razorpay_webhook(
            request.body,
            request.headers.get("X-Razorpay-Signature", ""),
            request.headers.get("X-Razorpay-Event-Id", ""),
        )
    except PaymentConfigurationError as error:
        return JsonResponse({"detail": str(error)}, status=503)
    except ValueError as error:
        return JsonResponse({"detail": str(error)}, status=400)
    return JsonResponse({"status": result})
