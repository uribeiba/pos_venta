from django.contrib import admin
from django.urls import path, include
from django.views.generic.base import RedirectView
from django.conf import settings
from django.conf.urls.static import static
urlpatterns = [
    path("admin/", admin.site.urls),
    # Incluimos las rutas de 'booking' sin namespace para usar
    # nombres simples en templates: 'pos_home', 'pos_trip', etc.
    path("", include("booking.urls")),
    path('coordinador/', include('coordinator.urls')),   # ✅ BIEN (el nombre de la app sin acento)
    path('favicon.ico', RedirectView.as_view(url='/static/img/favicon.ico', permanent=True)),
    
    
]
# ✅ Sirve archivos estáticos y multimedia en desarrollo
if settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
