from functools import wraps
from django.shortcuts import redirect
from django.contrib import messages

def role_required(roles):
    def decorator(view_func):
        @wraps(view_func)
        def _wrapped(request, *args, **kwargs):
            user = request.user
            if not user.is_authenticated:
                return redirect('login')
            profile = getattr(user, 'profile', None)
            if user.is_superuser:
                return view_func(request, *args, **kwargs)
            if profile and profile.role in roles:
                return view_func(request, *args, **kwargs)
            messages.error(request, "No tienes permisos para acceder a esta sección.")
            return redirect('pos_home')  # ajusta al nombre de tu dashboard POS
        return _wrapped
    return decorator
