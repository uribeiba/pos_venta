# booking/signals.py
from datetime import timezone
import logging
import json
from django.db.models.signals import post_save, post_delete, pre_save
from django.dispatch import receiver
from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
from django.contrib.auth.models import User
from django.db import IntegrityError
from django.core.exceptions import ObjectDoesNotExist
from .models import AuditLog, UserProfile

logger = logging.getLogger(__name__)

# ============================================================================
# SEÑAL PARA CREACIÓN AUTOMÁTICA DE PERFIL DE USUARIO
# ============================================================================

@receiver(post_save, sender=User, dispatch_uid="booking.ensure_userprofile_once")
def ensure_userprofile_once(sender, instance, created: bool, **kwargs):
    """
    Garantiza EXACTAMENTE un UserProfile por User.
    Esta señal crea automáticamente un perfil cuando se crea un usuario.
    """
    if created:
        try:
            UserProfile.objects.create(
                user=instance,
                role="vendedor",
                is_active=True
            )
            logger.info(f"✅ Perfil creado automáticamente para {instance.username}")
        except IntegrityError:
            # Si ya existe (race condition), ignorar
            logger.warning(f"⚠️ Intento de crear perfil duplicado para {instance.username}")
            pass
    else:
        # Si el usuario existe pero no tiene perfil (por migración o datos antiguos)
        if not hasattr(instance, 'profile'):
            try:
                profile, created = UserProfile.objects.get_or_create(
                    user=instance,
                    defaults={
                        'role': "vendedor",
                        'is_active': True
                    }
                )
                if created:
                    logger.info(f"✅ Perfil recuperado para {instance.username}")
                else:
                    logger.debug(f"ℹ️ Perfil ya existía para {instance.username}")
            except Exception as e:
                logger.error(f"❌ Error recuperando perfil para {instance.username}: {e}")


# ============================================================================
# SEÑALES DE AUDITORÍA - LOGIN/LOGOUT
# ============================================================================

@receiver(user_logged_in)
def log_login(sender, request, user, **kwargs):
    """Registra inicio de sesión en auditoría."""
    try:
        AuditLog.objects.create(
            user=user,
            action='login',
            ip_address=request.META.get('REMOTE_ADDR', ''),
            user_agent=request.META.get('HTTP_USER_AGENT', '')[:500],  # Limitar longitud
            session_key=request.session.session_key
        )
        logger.info(f"🔐 Login: {user.username} desde {request.META.get('REMOTE_ADDR', 'desconocido')}")
    except Exception as e:
        logger.error(f"❌ Error registrando login: {e}")


@receiver(user_logged_out)
def log_logout(sender, request, user, **kwargs):
    """Registra cierre de sesión en auditoría."""
    if user:
        try:
            AuditLog.objects.create(
                user=user,
                action='logout',
                ip_address=request.META.get('REMOTE_ADDR', ''),
                user_agent=request.META.get('HTTP_USER_AGENT', '')[:500],
                session_key=request.session.session_key
            )
            logger.info(f"🔐 Logout: {user.username}")
        except Exception as e:
            logger.error(f"❌ Error registrando logout: {e}")


@receiver(user_login_failed)
def log_login_failed(sender, credentials, request, **kwargs):
    """Registra intentos de login fallidos en auditoría."""
    try:
        username = credentials.get('username', 'desconocido')
        user = User.objects.filter(username=username).first()
        
        AuditLog.objects.create(
            user=user,
            action='login_failed',
            ip_address=request.META.get('REMOTE_ADDR', ''),
            user_agent=request.META.get('HTTP_USER_AGENT', '')[:500],
            session_key=request.session.session_key,
            object_repr=f"Intento fallido para {username}"
        )
        logger.warning(f"⚠️ Intento de login fallido: {username} desde {request.META.get('REMOTE_ADDR', 'desconocido')}")
    except Exception as e:
        logger.error(f"❌ Error registrando login fallido: {e}")


# ============================================================================
# SEÑALES DE AUDITORÍA - CRUD DE USUARIOS
# ============================================================================

@receiver(pre_save, sender=User)
def log_user_change(sender, instance, **kwargs):
    """
    Registra cambios en usuarios antes de guardar.
    Solo si el usuario ya existe (update).
    """
    if instance.pk:
        try:
            old = User.objects.get(pk=instance.pk)
            changes = {}
            
            # Campos a monitorear
            fields_to_check = [
                'username', 'email', 'first_name', 'last_name',
                'is_active', 'is_staff', 'is_superuser'
            ]
            
            for field in fields_to_check:
                old_value = getattr(old, field)
                new_value = getattr(instance, field)
                if old_value != new_value:
                    changes[field] = {
                        'old': str(old_value),
                        'new': str(new_value)
                    }
            
            if changes:
                AuditLog.objects.create(
                    user=instance,
                    action='update',
                    model_name='User',
                    object_id=str(instance.pk),
                    object_repr=str(instance),
                    changes=changes
                )
                logger.info(f"📝 Usuario actualizado: {instance.username} - Campos: {', '.join(changes.keys())}")
        except User.DoesNotExist:
            pass
        except Exception as e:
            logger.error(f"❌ Error registrando cambio de usuario: {e}")


@receiver(post_save, sender=User)
def log_user_create(sender, instance, created, **kwargs):
    """Registra creación de usuario en auditoría."""
    if created:
        try:
            AuditLog.objects.create(
                user=instance,
                action='create',
                model_name='User',
                object_id=str(instance.pk),
                object_repr=str(instance),
                changes={
                    'username': instance.username,
                    'email': instance.email,
                    'is_staff': instance.is_staff,
                    'is_superuser': instance.is_superuser
                }
            )
            logger.info(f"👤 Usuario creado: {instance.username}")
        except Exception as e:
            logger.error(f"❌ Error registrando creación de usuario: {e}")


@receiver(post_delete, sender=User)
def log_user_delete(sender, instance, **kwargs):
    """Registra eliminación de usuario en auditoría."""
    try:
        # Intentar obtener el usuario que eliminó (desde el middleware)
        # Si no está disponible, registrar como sistema
        requesting_user = None
        try:
            from threading import local
            request_local = local()
            if hasattr(request_local, 'user'):
                requesting_user = request_local.user
        except Exception:
            pass

        AuditLog.objects.create(
            user=requesting_user,
            action='delete',
            model_name='User',
            object_id=str(instance.pk),
            object_repr=f"{instance.username} ({instance.get_full_name()})",
            changes={
                'username': instance.username,
                'email': instance.email,
                'deleted_at': str(timezone.now())
            }
        )
        logger.warning(f"🗑️ Usuario eliminado: {instance.username} por {requesting_user}")
    except Exception as e:
        logger.error(f"❌ Error registrando eliminación de usuario: {e}")


# ============================================================================
# SEÑALES PARA AUDITORÍA DE MODELOS CRÍTICOS (Opcional)
# ============================================================================

# from .models import Ticket, Trip, CashRegister

# @receiver(post_save, sender=Ticket)
# def log_ticket_change(sender, instance, created, **kwargs):
#     """Registra cambios en tickets."""
#     if created:
#         AuditLog.objects.create(
#             user=instance.created_by,
#             action='create',
#             model_name='Ticket',
#             object_id=str(instance.pk),
#             object_repr=f"Ticket {instance.number}",
#             changes={
#                 'trip': str(instance.trip),
#                 'seat': instance.seat.number,
#                 'price': str(instance.price),
#                 'payment_method': instance.payment_method
#             }
#         )


# ============================================================================
# SEÑAL PARA LIMPIEZA DE HOLD EXPIRADOS (Alternativa a Celery)
# ============================================================================

# from .models import SeatHold

# @receiver(pre_save, sender=SeatHold)
# def check_expired_holds(sender, instance, **kwargs):
#     """Limpia holds expirados antes de guardar uno nuevo."""
#     if instance.active and instance.expires_at <= timezone.now():
#         instance.active = False
#         logger.debug(f"🔄 Hold expirado desactivado: {instance}")


# ============================================================================
# MIDDLEWARE PARA CAPTURAR USUARIO EN REQUEST (opcional)
# ============================================================================

# import threading
# _request_local = threading.local()

# class AuditMiddleware:
#     """Middleware para capturar el usuario actual en todas las señales."""
    
#     def __init__(self, get_response):
#         self.get_response = get_response
    
#     def __call__(self, request):
#         # Guardar usuario en thread local
#         if request.user.is_authenticated:
#             _request_local.user = request.user
#         else:
#             _request_local.user = None
        
#         response = self.get_response(request)
#         return response


# ============================================================================
# NOTA SOBRE LA SEÑAL ENSURE_USERPROFILE_ONCE
# ============================================================================
#
# Esta señal es CRÍTICA para el funcionamiento del sistema.
# Sin ella, los usuarios nuevos no tendrán perfil y muchas vistas fallarán.
#
# Si por alguna razón quieres desactivarla temporalmente, asegúrate de:
# 1. Crear perfiles manualmente para usuarios existentes
# 2. Tener un sistema de creación de perfiles alternativo
#
# Para desactivarla temporalmente (no recomendado), descomenta la línea:
# @receiver(post_save, sender=User, dispatch_uid="booking.ensure_userprofile_once")