from django.contrib.auth import views as auth_views
from django.urls import path

from .views import ProfileView, add_vehicle, register

app_name = "accounts"
urlpatterns = [
    path("register/", register, name="register"),
    path("login/", auth_views.LoginView.as_view(template_name="accounts/login.html"), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("profile/", ProfileView.as_view(), name="profile"),
    path("vehicles/add/", add_vehicle, name="vehicle_add"),
]
