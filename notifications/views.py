from django.conf import settings
from django.contrib import messages
from django.core.mail import send_mail
from django.shortcuts import redirect, render

from .forms import ContactForm


def contact(request):
    form = ContactForm(request.POST or None)
    if request.method == "POST":
        if form.is_valid():
            data = form.cleaned_data
            send_mail(
                f"SmartPark contact: {data['subject']}",
                f"From: {data['name']} <{data['email']}>\n\n{data['message']}",
                settings.DEFAULT_FROM_EMAIL,
                [settings.DEFAULT_FROM_EMAIL],
                reply_to=[data["email"]],
            )
            messages.success(request, "Thanks for getting in touch. We'll be in contact soon.")
        else:
            messages.error(request, "Please check your contact details and try again.")
    return redirect(f"{redirect('home').url}#contact")
