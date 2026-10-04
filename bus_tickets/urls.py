# bus_tickets/urls.py

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.auth.views import LoginView, LogoutView
from django.shortcuts import redirect
from django.urls import include, path

from booking.views import dashboard_redirect


# ============================================================================
# CONFIGURACIÓN ADMIN
# ============================================================================

ADMIN_URL = getattr(settings, "ADMIN_URL", "admin/")


# ============================================================================
# ENTRADA PRINCIPAL SEGÚN DOMINIO
# ============================================================================

def root_entry(request):
    """
    Entrada principal del sistema.

    - portena.online / www.portena.online:
      redirige al portal público de Buses La Porteña.

    - buspasss.online y demás dominios internos:
      conserva el login del sistema.
    """

    host = request.get_host().split(":")[0].lower()

    public_domains = {
        "portena.online",
        "www.portena.online",
	"cejer.buspasss.online",
    }

    if host in public_domains:
        return redirect("client_portal:home")

    return LoginView.as_view(
        template_name="registration/login.html",
        next_page="dashboard_redirect",
    )(request)


# ============================================================================
# URL PATTERNS
# ============================================================================

urlpatterns = [
    # ------------------------------------------------------------------------
    # LOGIN / LOGOUT
    # ------------------------------------------------------------------------

    path(
        "",
        root_entry,
        name="login",
    ),

    path(
        "logout/",
        LogoutView.as_view(
            next_page="login",
        ),
        name="logout",
    ),

    path(
        "dashboard/",
        dashboard_redirect,
        name="dashboard_redirect",
    ),

    # ------------------------------------------------------------------------
    # DJANGO ADMIN
    # ------------------------------------------------------------------------

    path(
        ADMIN_URL,
        admin.site.urls,
    ),

    # ------------------------------------------------------------------------
    # APPS
    # ------------------------------------------------------------------------

    path(
        "pos/",
        include("booking.urls"),
    ),

    path(
        "coordinator/",
        include("coordinator.urls"),
    ),

    path(
        "client/",
        include("client_portal.urls"),
    ),
]


# ============================================================================
# STATIC / MEDIA EN DESARROLLO
# ============================================================================

if settings.DEBUG:
    urlpatterns += static(
        settings.STATIC_URL,
        document_root=settings.STATIC_ROOT,
    )

    urlpatterns += static(
        settings.MEDIA_URL,
        document_root=settings.MEDIA_ROOT,
    )


# ============================================================================
# ERROR HANDLING PERSONALIZADO
# ============================================================================

handler404 = "bus_tickets.views.custom_404"

# Descomentar solamente si existen estas vistas:
# handler500 = "booking.views.error_500"
# handler403 = "booking.views.error_403"
