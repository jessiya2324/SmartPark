from django.contrib import messages
from django.contrib.auth import login
from django.shortcuts import redirect, render
from django.views.generic import UpdateView
from django.contrib.auth.mixins import LoginRequiredMixin
from django.urls import reverse_lazy

from .forms import RegistrationForm, VehicleForm
from .models import User


def register(request):
    if request.user.is_authenticated:
        return redirect("dashboard:home")
    form = RegistrationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        login(request, user)
        messages.success(request, "Welcome to SmartPark. Your account is ready.")
        return redirect("dashboard:home")
    return render(request, "accounts/register.html", {"form": form})


class ProfileView(LoginRequiredMixin, UpdateView):
    model = User
    fields = ("first_name", "last_name", "email", "phone")
    template_name = "accounts/profile.html"
    success_url = reverse_lazy("accounts:profile")

    def get_object(self, queryset=None):
        return self.request.user

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["vehicles"] = self.request.user.vehicles.all()
        context["vehicle_form"] = VehicleForm()
        return context


def add_vehicle(request):
    if not request.user.is_authenticated:
        return redirect("accounts:login")
    form = VehicleForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        vehicle = form.save(commit=False)
        vehicle.owner = request.user
        vehicle.save()
        messages.success(request, "Vehicle added to your profile.")
    elif request.method == "POST":
        messages.error(request, "Please check the vehicle details.")
    return redirect("accounts:profile")
