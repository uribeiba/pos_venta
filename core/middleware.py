from django.utils.deprecation import MiddlewareMixin
from django.contrib.auth.models import User
from booking.models import AuditLog
import json

class AuditMiddleware(MiddlewareMixin):
    def process_request(self, request):
        # Registrar intentos de login (lo haremos en la vista de login)
        pass

    def process_response(self, request, response):
        # Si es una vista que modifica datos, podemos registrar, pero lo haremos mediante signals
        return response