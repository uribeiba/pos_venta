# booking/apps.py
from django.apps import AppConfig

# booking/apps.py
class BookingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "booking"
    def ready(self):
        pass  # COMENTA ESTA LÍNEA: from . import signals