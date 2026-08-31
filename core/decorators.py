from functools import wraps

from django.contrib import messages
from django.contrib.auth import REDIRECT_FIELD_NAME
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied


def role_required(
    allowed_roles,
    login_url=None,
    redirect_field_name=REDIRECT_FIELD_NAME,
    message=None,
    raise_exception=False,
):
    """
    Verifica que el usuario autenticado tenga uno de los roles permitidos.

    IMPORTANTE:
    Si el usuario está autenticado pero no tiene permiso, NO se redirige
    automáticamente al POS. Eso evita ciclos de redirección entre módulos.
    """

    def decorator(view_func):
        @wraps(view_func)
        def wrapper(request, *args, **kwargs):

            # 1. Usuario no autenticado
            if not request.user.is_authenticated:
                return redirect_to_login(
                    request.get_full_path(),
                    login_url,
                    redirect_field_name,
                )

            # 2. Superusuario siempre tiene acceso
            if request.user.is_superuser:
                return view_func(request, *args, **kwargs)

            # 3. Obtener o crear perfil
            try:
                profile = request.user.profile
            except AttributeError:
                from booking.models import UserProfile

                profile, _ = UserProfile.objects.get_or_create(
                    user=request.user,
                    defaults={"role": "vendedor"},
                )

            user_role = getattr(profile, "role", "vendedor")

            # 4. Rol autorizado
            if user_role in allowed_roles:
                return view_func(request, *args, **kwargs)

            # 5. Acceso denegado
            role_display = dict(profile.ROLE_CHOICES).get(
                user_role,
                user_role,
            )

            allowed_display = [
                dict(profile.ROLE_CHOICES).get(role, role)
                for role in allowed_roles
            ]

            msg = message or (
                f"Acceso denegado. Su rol ({role_display}) "
                f"no tiene permisos para esta sección. "
                f"Requiere: {', '.join(allowed_display)}."
            )

            messages.error(request, msg)

            # Nunca generar una cadena de redirecciones.
            raise PermissionDenied(msg)

        return wrapper

    return decorator


# ============================================================
# Alias de roles
# ============================================================

def admin_required(view_func):
    return role_required([
        "admin",
    ])(view_func)


def supervisor_required(view_func):
    return role_required([
        "admin",
        "supervisor",
    ])(view_func)


def vendedor_required(view_func):
    return role_required([
        "admin",
        "supervisor",
        "vendedor",
        "convenio",
    ])(view_func)


def cajero_required(view_func):
    return role_required([
        "admin",
        "supervisor",
        "vendedor",
        "cajero",
    ])(view_func)


def coordinator_required(view_func):
    return role_required([
        "admin",
        "coordinator",
    ])(view_func)


def owner_required(view_func):
    return role_required([
        "admin",
        "owner",
    ])(view_func)


def executive_required(view_func):
    return role_required([
        "admin",
        "executive",
    ])(view_func)


def secretary_required(view_func):
    return role_required([
        "admin",
        "secretary",
    ])(view_func)


def owner_or_manager_required(view_func):
    return role_required([
        "admin",
        "supervisor",
        "executive",
        "owner",
    ])(view_func)