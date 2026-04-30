# bus_tickets/urls.py
from django.contrib import admin
from django.urls import path, include

urlpatterns = [
    path("admin/", admin.site.urls),
    # Incluimos las rutas de 'booking' sin namespace para usar
    # nombres simples en templates: 'pos_home', 'pos_trip', etc.
    path("", include("booking.urls")),
]
