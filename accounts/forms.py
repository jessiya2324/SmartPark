from django import forms
from django.contrib.auth.forms import UserCreationForm

from .models import User
from parking.models import Vehicle


class RegistrationForm(UserCreationForm):
    class Meta:
        model = User
        fields = ("first_name", "last_name", "username", "email", "phone")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"


class VehicleForm(forms.ModelForm):
    class Meta:
        model = Vehicle
        fields = ("plate_number", "vehicle_type", "nickname")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"
