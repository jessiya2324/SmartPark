from django.contrib import admin
from django.conf import settings
from django.conf.urls.static import static
from django.urls import include, path
from notifications.views import contact
from django.views.generic import TemplateView

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", TemplateView.as_view(template_name="home.html"), name="home"),
    path("accounts/", include("accounts.urls")),
    path("parking/", include("parking.urls")),
    path("bookings/", include("bookings.urls")),
    path("dashboard/", include("dashboard.urls")),
    path("api/", include("api.urls")),
    path("payments/", include("payments.urls")),
    path("contact/", contact, name="contact"),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
