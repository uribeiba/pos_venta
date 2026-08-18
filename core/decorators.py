# core/decorators.py
from functools import wraps
from django.shortcuts import redirect
from django.contrib import messages
from django.contrib.auth import REDIRECT_FIELD_NAME
from django.contrib.auth.views import redirect_to_login

def role_required(allowed_roles, login_url=None, redirect_field_name=REDIRECT_FIELD_NAME,
                  message=None, raise_exception=False):
    """
    Decorador que verifica si el usuario autenticado tiene uno de los roles permitidos.
    
    :param allowed_roles: lista de roles (strings) que pueden acceder.
    :param login_url: URL de login (por defecto usa settings.LOGIN_URL).
    :param redirect_field_name: nombre del parámetro GET para redirigir después del login.
    :param message: mensaje de error personalizado (opcional).
    :param raise_exception: si es True, lanza PermissionDenied en lugar de redirigir.
    
    Uso:
        @role_required(['admin', 'supervisor'])
        def my_view(request): ...
    """
    def decorator(view_func):
        @wraps(view_func)
        def wrapper(request, *args, **kwargs):
            # 1. Verificar autenticación
            if not request.user.is_authenticated:
                return redirect_to_login(request.get_full_path(), login_url, redirect_field_name)

            # 2. Superusuario siempre tiene acceso
            if request.user.is_superuser:
                return view_func(request, *args, **kwargs)

            # 3. Obtener el perfil del usuario
            try:
                profile = request.user.profile
            except AttributeError:
                # Si no tiene perfil, intentamos crearlo (fallback)
                from booking.models import UserProfile
                profile, created = UserProfile.objects.get_or_create(
                    user=request.user,
                    defaults={'role': 'vendedor'}
                )
                if created:
                    # Recargar usuario para tener el perfil
                    request.user.refresh_from_db()
                    profile = request.user.profile

            user_role = getattr(profile, 'role', 'vendedor')

            # 4. Verificar si el rol está en la lista permitida
            if user_role in allowed_roles:
                return view_func(request, *args, **kwargs)

            # 5. Acceso denegado: mostrar mensaje y redirigir
            # Obtener nombres de roles para mostrar
            role_display = dict(profile.ROLE_CHOICES).get(user_role, user_role)
            allowed_display = [dict(profile.ROLE_CHOICES).get(r, r) for r in allowed_roles]

            msg = message or (
                f"Acceso denegado. Su rol ({role_display}) no tiene permisos. "
                f"Requiere: {', '.join(allowed_display)}"
            )
            messages.error(request, msg)

            # Redirigir a una vista por defecto (puedes personalizarla)
            return redirect('pos_home')

        return wrapper
    return decorator


# Alias para facilitar el uso (compatibilidad con los decoradores existentes)
def admin_required(view_func):
    return role_required(['admin'])(view_func)

def supervisor_required(view_func):
    return role_required(['admin', 'supervisor'])(view_func)

def vendedor_required(view_func):
    return role_required(['admin', 'supervisor', 'vendedor', 'convenio'])(view_func)

def cajero_required(view_func):
    return role_required(['admin', 'supervisor', 'vendedor', 'cajero'])(view_func)

def coordinator_required(view_func):
    return role_required(['admin', 'coordinator'])(view_func)

def owner_or_manager_required(view_func):
    return role_required(['admin', 'supervisor'])(view_func)