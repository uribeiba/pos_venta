# ============================================================================
# URLS CONFIGURATION - SEGURO
# ============================================================================
import os
import secrets
import string
import warnings
from django.contrib import admin
from django.urls import path, include
from django.contrib.auth.views import LoginView, LogoutView
from django.conf import settings
from django.conf.urls.static import static
from django.http import Http404
from booking.views import dashboard_redirect

# Configuración de URL del admin desde variable de entorno
def get_admin_url() -> str:
    """Obtiene la URL del admin desde variables de entorno o genera una aleatoria."""
    admin_url = os.getenv("DJANGO_ADMIN_URL", "").strip()
    
    if admin_url:
        # Asegurar que la URL termine con '/'
        if not admin_url.endswith('/'):
            admin_url += '/'
        return admin_url
    
    # En desarrollo, usar una URL por defecto
    if settings.DEBUG:
        return 'admin-secreto/'
    
    # En producción, generar una URL aleatoria si no está configurada
    random_suffix = ''.join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(16))
    admin_path = f'admin-{random_suffix}/'
    
    warnings.warn(
        f"⚠️ DJANGO_ADMIN_URL no está configurada. Usando URL generada: {admin_path}. "
        "Se recomienda establecer esta URL en variables de entorno.",
        RuntimeWarning
    )
    
    return admin_path

# Obtener URL del admin
ADMIN_URL = get_admin_url()

# ============================================================================
# MIDDLEWARE ADICIONAL PARA SEGURIDAD DEL ADMIN
# ============================================================================
class AdminSecurityMiddleware:
    """Middleware que añade capa adicional de seguridad al admin."""
    
    def __init__(self, get_response):
        self.get_response = get_response
    
    def __call__(self, request):
        response = self.get_response(request)
        
        # Verificar si la solicitud es para el admin
        if request.path.startswith(f'/{ADMIN_URL}'):
            # Añadir headers de seguridad adicionales
            response['X-Robots-Tag'] = 'noindex, nofollow'
            response['X-Content-Type-Options'] = 'nosniff'
            response['X-Frame-Options'] = 'DENY'
            
            # Verificar IP en producción
            if not settings.DEBUG:
                client_ip = self.get_client_ip(request)
                allowed_ips = os.getenv('ADMIN_ALLOWED_IPS', '').split(',')
                if allowed_ips and client_ip not in allowed_ips:
                    raise Http404("Página no encontrada")
        
        return response
    
    def get_client_ip(self, request):
        """Obtiene la IP del cliente considerando proxies."""
        x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
        if x_forwarded_for:
            return x_forwarded_for.split(',')[0].strip()
        return request.META.get('REMOTE_ADDR')

# ============================================================================
# URL PATTERNS
# ============================================================================
urlpatterns = [
    # Login/Logout
    path('', LoginView.as_view(
        template_name='registration/login.html',
        next_page='dashboard_redirect'
    ), name='login'),
    path('logout/', LogoutView.as_view(next_page='login'), name='logout'),
    path('dashboard/', dashboard_redirect, name='dashboard_redirect'),
    
    # Admin con URL configurable
    path(ADMIN_URL, admin.site.urls),
    
    # Apps
    path('pos/', include('booking.urls')),
    path('coordinator/', include('coordinator.urls')),
    path('client/', include('client_portal.urls')),
]

# URLs adicionales en desarrollo
if settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

# ============================================================================
# ERROR HANDLING PERSONALIZADO
# ============================================================================
# Manejadores de error personalizados (descomentar si existen las vistas)
# handler404 = 'booking.views.error_404'
# handler500 = 'booking.views.error_500'
# handler403 = 'booking.views.error_403'