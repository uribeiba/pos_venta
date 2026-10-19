# booking/signals.py
from django.utils import timezone  # CORREGIDO: antes importaba de datetime
import logging
import json
from django.db.models.signals import post_save, post_delete, pre_save
from django.dispatch import receiver
from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
from django.contrib.auth.models import User
from django.db import IntegrityError
from django.core.exceptions import ObjectDoesNotExist
from .models import AuditLog, UserProfile, Trip, TripStop, RouteStop

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
# SEÑAL PARA CREACIÓN AUTOMÁTICA DE PARADAS EN VIAJES (TRIPSTOPS)
# ============================================================================

@receiver(post_save, sender=Trip)
def create_trip_stops_on_creation(sender, instance, created, **kwargs):
    """
    Cuando se crea un viaje nuevo (Trip), automáticamente le copia las paradas
    de su Ruta (RouteStop) a la tabla TripStop.
    Esto es vital para que el portal público (client_portal) pueda encontrar los viajes.
    """
    if created and not instance.trip_stops.exists():
        # Buscar si la ruta tiene paradas predefinidas
        route_stops = list(instance.route.stops.all().order_by('order'))
        trip_stops_to_create = []

        if route_stops:
            # Copiar las paradas configuradas en la Ruta
            for index, rs in enumerate(route_stops):
                trip_stops_to_create.append(
                    TripStop(
                        trip=instance,
                        city=rs.city,
                        terminal=rs.terminal,
                        order=rs.order,
                        is_origin=(index == 0),
                        is_destination=(index == len(route_stops) - 1),
                    )
                )
        else:
            # Si la ruta no tiene paradas intermedias, crear origen y destino base
            trip_stops_to_create.append(
                TripStop(
                    trip=instance,
                    city=instance.route.origin,
                    terminal=instance.route.origin_terminal,
                    order=1,
                    is_origin=True,
                    scheduled_time=instance.departure
                )
            )
            trip_stops_to_create.append(
                TripStop(
                    trip=instance,
                    city=instance.route.destination,
                    terminal=instance.route.destination_terminal,
                    order=2,
                    is_destination=True,
                    scheduled_time=instance.arrival
                )
            )
            
        if trip_stops_to_create:
            TripStop.objects.bulk_create(trip_stops_to_create)
            logger.info(f"✅ Paradas creadas automáticamente para el viaje {instance.id}")


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
            user_agent=request.META.get('HTTP_USER_AGENT', '')[:500],
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