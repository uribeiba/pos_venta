# booking/tasks.py
from celery import shared_task, Task
from celery.exceptions import MaxRetriesExceededError
from django.utils import timezone
from django.db import IntegrityError, OperationalError, transaction

from .models import (
    BookingOrder,
    PaymentTransaction,
    SeatHold,
)
import logging

logger = logging.getLogger(__name__)

class BaseTaskWithRetry(Task):
    """Tarea base con retry automático."""
    autoretry_for = (OperationalError, IntegrityError)
    retry_kwargs = {'max_retries': 3}
    retry_backoff = True
    retry_backoff_max = 60

@shared_task(base=BaseTaskWithRetry, bind=True)
def release_expired_holds(self):
    """
    Libera SeatHold expirados y cierra reservas web abandonadas.

    Reglas:

    1. SeatHold vencido:
       active -> False

    2. BookingOrder:
       pending / payment_started + expires_at vencido
       -> expired

    3. PaymentTransaction:
       created de una orden expirada
       -> aborted

    SEGURIDAD:
    - Nunca modifica pagos AUTHORIZED.
    - Nunca expira una BookingOrder que tenga un pago AUTHORIZED.
    - Nunca modifica órdenes PAID.
    - Nunca crea ni elimina Tickets.

    Se puede ejecutar periódicamente con Celery Beat.
    """

    now = timezone.now()

    try:
        # ================================================================
        # 1. LOCALIZAR HOLDS EXPIRADOS
        # ================================================================
        expired_holds = SeatHold.objects.filter(
            active=True,
            expires_at__lte=now,
        )

        expired_count = expired_holds.count()

        # ================================================================
        # 2. CERRAR BOOKINGORDERS EXPIRADAS / ABANDONADAS
        # ================================================================
        #
        # Importante:
        # Se procesa ANTES de desactivar los holds.
        #
        # No dependemos únicamente del SeatHold:
        # BookingOrder ya posee su propio expires_at.
        # ================================================================

        candidate_orders = (
            BookingOrder.objects
            .filter(
                status__in=[
                    BookingOrder.STATUS_PENDING,
                    BookingOrder.STATUS_PAYMENT_STARTED,
                ],
                expires_at__lte=now,
            )
            .order_by("id")
        )

        expired_orders_count = 0
        aborted_payments_count = 0
        protected_authorized_count = 0

        for candidate in candidate_orders:

            with transaction.atomic():

                # --------------------------------------------------------
                # Bloquear orden
                # --------------------------------------------------------
                order = (
                    BookingOrder.objects
                    .select_for_update()
                    .get(pk=candidate.pk)
                )

                # Otro proceso pudo modificarla mientras esperábamos lock.
                if order.status not in [
                    BookingOrder.STATUS_PENDING,
                    BookingOrder.STATUS_PAYMENT_STARTED,
                ]:
                    continue

                if order.expires_at > now:
                    continue

                # --------------------------------------------------------
                # SEGURIDAD CRÍTICA:
                # Si Transbank ya autorizó dinero, NO expirar.
                # --------------------------------------------------------
                has_authorized_payment = (
                    PaymentTransaction.objects
                    .filter(
                        order=order,
                        status=PaymentTransaction.STATUS_AUTHORIZED,
                    )
                    .exists()
                )

                if has_authorized_payment:
                    protected_authorized_count += 1

                    logger.warning(
                        (
                            "⚠️ Orden vencida pero con pago AUTHORIZED. "
                            "No será expirada automáticamente. "
                            "order=%s"
                        ),
                        order.code,
                    )

                    continue

                # --------------------------------------------------------
                # Marcar pagos CREATED como ABORTED
                # --------------------------------------------------------
                aborted = (
                    PaymentTransaction.objects
                    .filter(
                        order=order,
                        status=PaymentTransaction.STATUS_CREATED,
                    )
                    .update(
                        status=PaymentTransaction.STATUS_ABORTED,
                        updated_at=now,
                    )
                )

                aborted_payments_count += aborted

                # --------------------------------------------------------
                # BookingOrder -> EXPIRED
                # --------------------------------------------------------
                order.status = BookingOrder.STATUS_EXPIRED

                order.save(
                    update_fields=[
                        "status",
                        "updated_at",
                    ]
                )

                expired_orders_count += 1

        # ================================================================
        # 3. DESACTIVAR SEATHOLD VENCIDOS
        # ================================================================

        updated_holds = (
            SeatHold.objects
            .filter(
                active=True,
                expires_at__lte=now,
            )
            .update(
                active=False
            )
        )

        # ================================================================
        # 4. LOG
        # ================================================================

        if (
            updated_holds > 0
            or expired_orders_count > 0
            or aborted_payments_count > 0
        ):
            logger.info(
                (
                    "✅ Limpieza expirados: "
                    "holds=%s, "
                    "orders_expired=%s, "
                    "payments_aborted=%s, "
                    "authorized_protected=%s"
                ),
                updated_holds,
                expired_orders_count,
                aborted_payments_count,
                protected_authorized_count,
            )

        else:
            logger.debug(
                "No hay reservas web expiradas para limpiar."
            )

        return {
            "status": "success",
            "holds_detected": expired_count,
            "holds_released": updated_holds,
            "orders_expired": expired_orders_count,
            "payments_aborted": aborted_payments_count,
            "authorized_protected": protected_authorized_count,
            "timestamp": now.isoformat(),
        }

    except OperationalError as e:

        logger.error(
            "⚠️ Error de base de datos en release_expired_holds: %s",
            e,
        )

        raise self.retry(
            exc=e,
            countdown=30,
        )

    except IntegrityError as e:

        logger.error(
            "⚠️ Error de integridad en release_expired_holds: %s",
            e,
        )

        raise self.retry(
            exc=e,
            countdown=10,
        )

    except Exception as e:

        logger.error(
            "❌ Error inesperado en release_expired_holds: %s",
            e,
            exc_info=True,
        )

        return {
            "status": "error",
            "error": str(e),
            "timestamp": now.isoformat(),
        }


# ✅ NUEVA TAREA: Cleanup de sessiones expiradas
@shared_task
def clean_expired_sessions():
    """
    Limpia sesiones expiradas de la base de datos.
    """
    from django.contrib.sessions.models import Session
    try:
        expired = Session.objects.filter(expire_date__lt=timezone.now())
        count = expired.count()
        expired.delete()
        logger.info(f"✅ {count} sesiones expiradas eliminadas.")
        return {'deleted': count}
    except Exception as e:
        logger.error(f"❌ Error limpiando sesiones: {e}")
        return {'error': str(e)}