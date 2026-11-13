from django.urls import path

from .views import location_detail, location_list

app_name = "parking"
urlpatterns = [
    path("", location_list, name="locations"),
    path("<int:pk>/", location_detail, name="location_detail"),
]
