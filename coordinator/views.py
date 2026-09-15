# coordinator/views.py
import json
import os
import subprocess
import calendar
from datetime import datetime, timedelta
from decimal import Decimal
from venv import logger

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError, PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction, IntegrityError
from django.db.models import ProtectedError, Count, Q, Exists, OuterRef, Sum, Avg, Max
from django.http import JsonResponse, HttpResponse, FileResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST, require_GET, require_http_methods

# ===== IMPORTACIÓN DEL DECORADOR UNIFICADO =====
from core.decorators import role_required

from booking.models import (
    Assistant, Bus, BusDocument, City, Company, Driver, DriverDocument,
    Maintenance, Route, Seat, SeatHold, Terminal, Ticket, Trip, Agency, FleetOwner,
    Parcel, FuelRecord, BusLayout, AuditLog, User
)
from booking.forms import (
    TerminalForm, CityForm, RouteForm, RouteStopFormSet,
    AssistantForm, DriverForm, AgencyForm, BusFullForm, TripForm, FleetOwnerForm
)


from booking.access import (
    get_user_scope,
    buses_for_user,
    routes_for_user,
    drivers_for_user,
    assistants_for_user,
    trips_for_user,
    tickets_for_user,
    sales_for_user,
    booking_orders_for_user,
    assert_bus_access,
    assert_route_access,
    assert_driver_access,
    assert_assistant_access,
    assert_trip_access,
)
# ============================================================================
# ALIAS PARA SIMPLIFICAR DECORADORES
# ============================================================================
coordinator_required = role_required(['admin', 'coordinator'])
admin_only = role_required(['admin'])
security_auditor_only = role_required(['admin', 'coordinator'])


# ============================================================================
# HELPERS UTILITARIOS
# ============================================================================

def assign_missing_numbers(numbers, layout, prefix, start_from=1):
    """
    Asigna números correlativos a las celdas tipo 'L' que no tengan número.
    Retorna (numbers_actualizados, último_contador_usado).
    """
    counter = start_from
    for i in range(len(numbers)):
        if layout[i] == 'L' and (i >= len(numbers) or not numbers[i]):
            numbers[i] = f"{prefix}{counter}"
            counter += 1
    return numbers, counter


def make_aware_datetime(dt_str, field_name="Fecha"):
    """
    Convierte string de datetime-local a datetime timezone-aware.
    Lanza ValidationError si el formato es inválido.
    """
    if not dt_str:
        return None
    try:
        # Intentar parsear con zona horaria primero
        try:
            from dateutil import parser
            dt = parser.parse(dt_str)
            if dt.tzinfo is None:
                return timezone.make_aware(dt)
            return dt
        except:
            # Fallback a formato estándar
            naive = datetime.strptime(dt_str, '%Y-%m-%dT%H:%M')
            return timezone.make_aware(naive)
    except (ValueError, TypeError) as e:
        raise ValidationError(f"{field_name}: formato inválido. Use YYYY-MM-DDTHH:MM. Error: {e}")


def validate_trip_conflicts(route, bus, driver1_id, driver2_id, departure_dt, arrival_dt, assistant_id=None, exclude_trip=None):
    """
    Valida que no haya conflictos con otros viajes.
    Retorna lista de errores.
    """
    errors = []

    # Validar que la ruta sea válida (origen != destino)
    if route.origin == route.destination:
        errors.append("El origen y destino de la ruta no pueden ser iguales.")

    # Validar que el bus no tenga otro viaje en el mismo horario
    bus_conflicts = Trip.objects.filter(
        bus=bus,
        departure__lt=arrival_dt,
        arrival__gt=departure_dt
    )
    if exclude_trip:
        bus_conflicts = bus_conflicts.exclude(pk=exclude_trip.pk)
    if bus_conflicts.exists():
        errors.append(f"El bus {bus.plate} ya tiene un viaje programado en ese horario.")

    # Validar que el chofer principal no tenga otro viaje
    if driver1_id:
        driver1_conflicts = Trip.objects.filter(
            driver1_id=driver1_id,
            departure__lt=arrival_dt,
            arrival__gt=departure_dt
        )
        if exclude_trip:
            driver1_conflicts = driver1_conflicts.exclude(pk=exclude_trip.pk)
        if driver1_conflicts.exists():
            errors.append("El chofer principal ya tiene un viaje programado en ese horario.")

    # Validar que el chofer secundario no tenga otro viaje
    if driver2_id:
        driver2_conflicts = Trip.objects.filter(
            driver2_id=driver2_id,
            departure__lt=arrival_dt,
            arrival__gt=departure_dt
        )
        if exclude_trip:
            driver2_conflicts = driver2_conflicts.exclude(pk=exclude_trip.pk)
        if driver2_conflicts.exists():
            errors.append("El chofer secundario ya tiene un viaje programado en ese horario.")

    # FASE 2.15 — El auxiliar no puede estar en dos viajes superpuestos.
    if assistant_id:
        assistant_conflicts = Trip.objects.filter(
            assistant_id=assistant_id,
            departure__lt=arrival_dt,
            arrival__gt=departure_dt,
        )
        if exclude_trip:
            assistant_conflicts = assistant_conflicts.exclude(pk=exclude_trip.pk)
        if assistant_conflicts.exists():
            errors.append("El auxiliar ya tiene un viaje programado en ese horario.")

    return errors


def get_backup_dir():
    """Retorna la ruta del directorio de respaldos."""
    backup_dir = os.path.join(settings.BASE_DIR, 'backups')
    if not os.path.exists(backup_dir):
        os.makedirs(backup_dir)
    return backup_dir


def create_backup():
    """Crea un respaldo de la base de datos PostgreSQL."""
    backup_dir = get_backup_dir()
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    backup_file = os.path.join(backup_dir, f'backup_{timestamp}.sql')

    db_settings = settings.DATABASES['default']
    host = db_settings.get('HOST', 'localhost')
    port = db_settings.get('PORT', '5432')
    name = db_settings['NAME']
    user = db_settings['USER']
    password = db_settings['PASSWORD']

    env = os.environ.copy()
    env['PGPASSWORD'] = password

    cmd = [
        'pg_dump',
        '-h', host,
        '-p', str(port),
        '-U', user,
        '-d', name,
        '-f', backup_file,
        '--clean',
        '--if-exists'
    ]

    try:
        result = subprocess.run(cmd, env=env, capture_output=True, text=True, check=True)
        return backup_file
    except subprocess.CalledProcessError as e:
        print(f"Error en pg_dump: {e.stderr}")
        return None
    except Exception as e:
        print(f"Error inesperado: {e}")
        return None


# ============================================================================
# DASHBOARD DEL COORDINADOR
# ============================================================================

@login_required
@coordinator_required
def dashboard(request):
    """
    Panel principal del coordinador con aislamiento multiempresa.

    Alcance:
    - Superuser: información global.
    - Usuario de empresa: solo información de su empresa.
    - Owner: respeta el alcance definido en access.py.
    """

    # ============================================================
    # LIMPIAR MENSAJES ANTIGUOS
    # ============================================================

    storage = messages.get_messages(request)
    storage.used = True
    list(storage)

    now = timezone.now()
    today = now.date()

    # ============================================================
    # QUERYSETS AUTORIZADOS
    # ============================================================

    allowed_trips = trips_for_user(
        request.user,
        Trip.objects.all(),
    )

    allowed_tickets = tickets_for_user(
        request.user,
        Ticket.objects.all(),
    )

    allowed_buses = buses_for_user(
        request.user,
        Bus.objects.all(),
    )

    allowed_drivers = drivers_for_user(
        request.user,
        Driver.objects.all(),
    )

    # ============================================================
    # MÉTRICAS DE RESUMEN
    # ============================================================

    viajes_hoy_qs = allowed_trips.filter(
        departure__date=today
    )

    viajes_hoy = viajes_hoy_qs.count()

    proxima_semana = today + timedelta(days=7)

    buses_activos = (
        allowed_trips
        .filter(
            departure__date__range=[
                today,
                proxima_semana,
            ]
        )
        .values("bus")
        .distinct()
        .count()
    )

    pasajeros_hoy = (
        allowed_tickets
        .filter(
            trip__departure__date=today
        )
        .count()
    )

    trips_hoy = (
        viajes_hoy_qs
        .select_related(
            "route__origin",
            "route__destination",
            "bus",
            "driver1",
        )
        .annotate(
            sold_count=Count("tickets")
        )
    )

    ocupacion_promedio = 0

    if trips_hoy.exists():

        total_ocupacion = sum(
            (
                trip.sold_count
                / trip.seats_total
                * 100
                if trip.seats_total
                else 0
            )
            for trip in trips_hoy
        )

        ocupacion_promedio = round(
            total_ocupacion
            / trips_hoy.count(),
            1,
        )

    # ============================================================
    # VIAJES PRÓXIMOS - 24 HORAS
    # ============================================================

    proximas_24h = now + timedelta(hours=24)

    viajes_proximos_qs = (
        allowed_trips
        .filter(
            Q(
                status=Trip.STATUS_IN_PROGRESS
            )
            | Q(
                status=Trip.STATUS_SCHEDULED,
                departure__gte=now,
                departure__lte=proximas_24h,
            )
        )
        .select_related(
            "route__origin",
            "route__destination",
            "bus",
            "driver1",
            "driver2",
            "assistant",
        )
        .annotate(
            sold_count=Count("tickets")
        )
        .order_by(
            "departure"
        )
    )

    viajes_data = []

    viajes_operational_counts = {
        "operational": 0,
        "attention": 0,
        "no_operational": 0,
    }

    viajes_no_operativos = []
    viajes_pendientes_cierre = []

    for trip in viajes_proximos_qs:

        libres = max(
            (trip.seats_total or 0)
            - trip.sold_count,
            0,
        )

        if (
            trip.status
            == Trip.STATUS_IN_PROGRESS
            and trip.arrival
            and trip.arrival < now
        ):
            estado = "Pendiente de cierre"
            estado_color = "warning"
            estado_icon = "fa-flag-checkered"

        elif (
            trip.status
            == Trip.STATUS_IN_PROGRESS
        ):
            estado = "En viaje"
            estado_color = "primary"
            estado_icon = "fa-bus"

        elif (
            trip.status
            == Trip.STATUS_COMPLETED
        ):
            estado = "Finalizado"
            estado_color = "secondary"
            estado_icon = "fa-circle-check"

        elif trip.departure > now:
            estado = "Próximo"
            estado_color = "success"
            estado_icon = "fa-clock"

        else:
            estado = "Pendiente de despacho"
            estado_color = "danger"
            estado_icon = (
                "fa-triangle-exclamation"
            )

        operational = (
            _trip_operational_status(
                trip
            )
        )

        viajes_operational_counts[
            operational["key"]
        ] += 1

        if (
            trip.status
            == Trip.STATUS_SCHEDULED
            and operational["key"]
            == "no_operational"
        ):

            viajes_no_operativos.append({
                "id": trip.id,
                "route": (
                    f"{trip.route.origin.name} "
                    f"→ "
                    f"{trip.route.destination.name}"
                ),
                "time": (
                    timezone
                    .localtime(
                        trip.departure
                    )
                    .strftime("%H:%M")
                ),
                "reason": (
                    operational[
                        "primary_reason"
                    ]
                ),
            })

        if (
            trip.status
            == Trip.STATUS_IN_PROGRESS
            and trip.arrival
            and trip.arrival < now
        ):

            viajes_pendientes_cierre.append({
                "id": trip.id,
                "route": (
                    f"{trip.route.origin.name} "
                    f"→ "
                    f"{trip.route.destination.name}"
                ),
                "arrival": (
                    timezone
                    .localtime(
                        trip.arrival
                    )
                    .strftime("%H:%M")
                ),
            })

        viajes_data.append({
            "id": trip.id,

            "hora": (
                timezone
                .localtime(
                    trip.departure
                )
                .strftime("%H:%M")
            ),

            "ruta": (
                f"{trip.route.origin.name} "
                f"→ "
                f"{trip.route.destination.name}"
            ),

            "bus": trip.bus.plate,

            "chofer": (
                trip.driver1.full_name
                if trip.driver1
                else "Sin asignar"
            ),

            "asientos_totales": (
                trip.seats_total
            ),

            "asientos_libres": libres,

            "asientos_ocupados": (
                trip.sold_count
            ),

            "estado": estado,
            "estado_color": estado_color,
            "estado_icon": estado_icon,

            "trip_status": (
                trip.status
            ),

            "actual_departure": (
                timezone
                .localtime(
                    trip.actual_departure
                )
                .strftime("%H:%M:%S")
                if trip.actual_departure
                else None
            ),

            "actual_arrival": (
                timezone
                .localtime(
                    trip.actual_arrival
                )
                .strftime("%H:%M:%S")
                if trip.actual_arrival
                else None
            ),

            "operational_key": (
                operational["key"]
            ),

            "operational_label": (
                operational["label"]
            ),

            "operational_color": (
                operational["color"]
            ),

            "operational_icon": (
                operational["icon"]
            ),

            "operational_reason": (
                operational[
                    "primary_reason"
                ]
            ),

            "operational_reasons": (
                operational["reasons"]
            ),

            "operational_can_dispatch": (
                operational[
                    "can_dispatch"
                ]
            ),
        })

    # ============================================================
    # ALERTAS OPERACIONALES
    # ============================================================

    alertas_operacionales = []

    limite_30_dias = (
        today
        + timedelta(days=30)
    )

    # ============================================================
    # VIAJES NO OPERATIVOS
    # ============================================================

    if viajes_no_operativos:

        primero = (
            viajes_no_operativos[0]
        )

        alertas_operacionales.append({
            "level": "danger",
            "icon": "fa-route",
            "title": (
                "Viajes próximos "
                "no operativos"
            ),
            "message": (
                f"{len(viajes_no_operativos)} "
                "viaje(s) de las próximas "
                "24 horas no están en "
                "condiciones de despacho. "
                f'Próximo caso: '
                f'{primero["route"]} '
                f'a las {primero["time"]}. '
                f'{primero["reason"]}.'
            ),
            "url_name": (
                "coordinator:"
                "trips_dashboard"
            ),
            "action": (
                "Revisar viajes"
            ),
        })

    # ============================================================
    # VIAJES PENDIENTES DE CIERRE
    # ============================================================

    if viajes_pendientes_cierre:

        primero = (
            viajes_pendientes_cierre[0]
        )

        alertas_operacionales.append({
            "level": "warning",
            "icon": (
                "fa-flag-checkered"
            ),
            "title": (
                "Viajes pendientes "
                "de cierre"
            ),
            "message": (
                f"{len(viajes_pendientes_cierre)} "
                "viaje(s) siguen EN VIAJE "
                "aunque ya superaron su "
                "llegada programada. "
                f'Próximo caso: '
                f'{primero["route"]}, '
                "llegada programada "
                f'{primero["arrival"]}.'
            ),
            "url_name": (
                "coordinator:"
                "trips_dashboard"
            ),
            "action": (
                "Revisar cierres"
            ),
        })

    # ============================================================
    # DOCUMENTACIÓN
    # ============================================================

    limite_7_dias = (
        today
        + timedelta(days=7)
    )

    # Solo documentos de choferes autorizados.
    driver_docs_base = (
        DriverDocument.objects
        .filter(
            driver__in=allowed_drivers
        )
    )

    # Solo documentos de buses autorizados.
    bus_docs_base = (
        BusDocument.objects
        .filter(
            bus__in=allowed_buses
        )
    )

    # ============================================================
    # DOCUMENTOS VENCIDOS
    # ============================================================

    driver_docs_vencidos = (
        driver_docs_base
        .filter(
            expiry_date__isnull=False,
            expiry_date__lt=today,
        )
        .count()
    )

    bus_docs_vencidos = (
        bus_docs_base
        .filter(
            expiry_date__isnull=False,
            expiry_date__lt=today,
        )
        .count()
    )

    # ============================================================
    # DOCUMENTOS URGENTES
    # ============================================================

    driver_docs_urgentes = (
        driver_docs_base
        .filter(
            expiry_date__isnull=False,
            expiry_date__gte=today,
            expiry_date__lte=limite_7_dias,
        )
        .count()
    )

    bus_docs_urgentes = (
        bus_docs_base
        .filter(
            expiry_date__isnull=False,
            expiry_date__gte=today,
            expiry_date__lte=limite_7_dias,
        )
        .count()
    )

    # ============================================================
    # DOCUMENTOS PRÓXIMOS
    # ============================================================

    driver_docs_proximos = (
        driver_docs_base
        .filter(
            expiry_date__isnull=False,
            expiry_date__gt=limite_7_dias,
            expiry_date__lte=limite_30_dias,
        )
        .count()
    )

    bus_docs_proximos = (
        bus_docs_base
        .filter(
            expiry_date__isnull=False,
            expiry_date__gt=limite_7_dias,
            expiry_date__lte=limite_30_dias,
        )
        .count()
    )

    total_docs_vencidos = (
        driver_docs_vencidos
        + bus_docs_vencidos
    )

    total_docs_urgentes = (
        driver_docs_urgentes
        + bus_docs_urgentes
    )

    total_docs_proximos = (
        driver_docs_proximos
        + bus_docs_proximos
    )

    total_docs_choferes = (
        driver_docs_vencidos
        + driver_docs_urgentes
        + driver_docs_proximos
    )

    total_docs_buses = (
        bus_docs_vencidos
        + bus_docs_urgentes
        + bus_docs_proximos
    )

    total_docs_atencion = (
        total_docs_vencidos
        + total_docs_urgentes
        + total_docs_proximos
    )

    # ============================================================
    # ALERTAS DOCUMENTALES
    # ============================================================

    if total_docs_vencidos:

        alertas_operacionales.append({
            "level": "danger",
            "icon": (
                "fa-file-circle-xmark"
            ),
            "title": (
                "Documentación vencida"
            ),
            "message": (
                f"{total_docs_vencidos} "
                "documento(s) vencido(s): "
                f"{driver_docs_vencidos} "
                "de choferes y "
                f"{bus_docs_vencidos} "
                "de buses."
            ),
            "url_name": (
                "coordinator:"
                "expiring_documents"
            ),
            "action": (
                "Revisar documentos"
            ),
        })

    if total_docs_urgentes:

        alertas_operacionales.append({
            "level": "warning",
            "icon": (
                "fa-file-circle-exclamation"
            ),
            "title": (
                "Documentación urgente"
            ),
            "message": (
                f"{total_docs_urgentes} "
                "documento(s) vencen en "
                "7 días o menos: "
                f"{driver_docs_urgentes} "
                "de choferes y "
                f"{bus_docs_urgentes} "
                "de buses."
            ),
            "url_name": (
                "coordinator:"
                "expiring_documents"
            ),
            "action": (
                "Ver urgentes"
            ),
        })

    if total_docs_proximos:

        alertas_operacionales.append({
            "level": "info",
            "icon": "fa-file-lines",
            "title": (
                "Documentos próximos"
            ),
            "message": (
                f"{total_docs_proximos} "
                "documento(s) vencen entre "
                "8 y 30 días: "
                f"{driver_docs_proximos} "
                "de choferes y "
                f"{bus_docs_proximos} "
                "de buses."
            ),
            "url_name": (
                "coordinator:"
                "expiring_documents"
            ),
            "action": (
                "Ver próximos"
            ),
        })

    # ============================================================
    # MANTENCIÓN POR KILOMETRAJE
    # ============================================================

    margen_mantenimiento_km = 1000

    buses_mantenimiento = list(
        allowed_buses
        .filter(
            is_active=True,
            next_maintenance_mileage__gt=0,
        )
        .only(
            "id",
            "plate",
            "model",
            "current_mileage",
            "next_maintenance_mileage",
            "last_maintenance_mileage",
        )
        .order_by(
            "plate"
        )
    )

    buses_mantencion_vencida = []
    buses_mantencion_proxima = []
    buses_mantencion_ok = []

    for bus_item in buses_mantenimiento:

        km_actual = (
            bus_item.current_mileage
            or 0
        )

        km_proximo = (
            bus_item.next_maintenance_mileage
            or 0
        )

        km_restantes = (
            km_proximo
            - km_actual
        )

        item_data = {
            "id": bus_item.id,
            "plate": bus_item.plate,
            "model": (
                bus_item.model
                or ""
            ),
            "current_km": km_actual,
            "next_km": km_proximo,
            "remaining_km": (
                km_restantes
            ),
            "overdue_km": (
                abs(km_restantes)
                if km_restantes <= 0
                else 0
            ),
        }

        if km_restantes <= 0:

            buses_mantencion_vencida.append(
                item_data
            )

        elif (
            km_restantes
            <= margen_mantenimiento_km
        ):

            buses_mantencion_proxima.append(
                item_data
            )

        else:

            buses_mantencion_ok.append(
                item_data
            )

    # Los más críticos primero.

    buses_mantencion_vencida.sort(
        key=lambda item: (
            item["remaining_km"]
        )
    )

    buses_mantencion_proxima.sort(
        key=lambda item: (
            item["remaining_km"]
        )
    )

    mantenciones_vencidas = len(
        buses_mantencion_vencida
    )

    mantenciones_proximas = len(
        buses_mantencion_proxima
    )

    mantenciones_al_dia = len(
        buses_mantencion_ok
    )

    # ============================================================
    # ALERTAS MANTENCIÓN
    # ============================================================

    if mantenciones_vencidas:

        peor = (
            buses_mantencion_vencida[0]
        )

        alertas_operacionales.append({
            "level": "danger",
            "icon": (
                "fa-screwdriver-wrench"
            ),
            "title": (
                "Mantenciones vencidas"
            ),
            "message": (
                f"{mantenciones_vencidas} "
                "bus(es) superaron su "
                "kilometraje programado. "
                "El más crítico es "
                f'{peor["plate"]}, '
                "excedido por "
                f'{peor["overdue_km"]:,} km.'
            ),
            "url_name": (
                "coordinator:"
                "maintenance_list"
            ),
            "querystring": (
                "?status=overdue"
            ),
            "action": (
                "Ver vencidas"
            ),
        })

    if mantenciones_proximas:

        siguiente = (
            buses_mantencion_proxima[0]
        )

        alertas_operacionales.append({
            "level": "warning",
            "icon": "fa-gauge-high",
            "title": (
                "Mantenciones próximas"
            ),
            "message": (
                f"{mantenciones_proximas} "
                "bus(es) están a "
                f"{margen_mantenimiento_km:,} "
                "km o menos de su "
                "mantención. "
                f'{siguiente["plate"]} '
                "es el próximo: "
                "faltan "
                f'{siguiente["remaining_km"]:,} '
                "km."
            ),
            "url_name": (
                "coordinator:"
                "maintenance_list"
            ),
            "querystring": (
                "?status=soon"
            ),
            "action": (
                "Ver próximas"
            ),
        })

    # ============================================================
    # VIAJES SIN CHOFER
    # ============================================================

    viajes_sin_chofer = (
        allowed_trips
        .filter(
            departure__gte=now,
            departure__lte=(
                now
                + timedelta(days=7)
            ),
            driver1__isnull=True,
        )
        .count()
    )

    if viajes_sin_chofer:

        alertas_operacionales.append({
            "level": "danger",
            "icon": "fa-user-slash",
            "title": (
                "Viajes sin chofer"
            ),
            "message": (
                f"{viajes_sin_chofer} "
                "viaje(s) de los próximos "
                "7 días no tienen chofer "
                "principal asignado."
            ),
            "url_name": (
                "coordinator:"
                "trips_dashboard"
            ),
            "action": (
                "Asignar chofer"
            ),
        })

    # ============================================================
    # CONTEXTO
    # ============================================================

    context = {
        "title": (
            "Dashboard - Coordinador"
        ),

        "viajes_hoy": (
            viajes_hoy
        ),

        "buses_activos": (
            buses_activos
        ),

        "pasajeros_hoy": (
            pasajeros_hoy
        ),

        "ocupacion_promedio": (
            ocupacion_promedio
        ),

        "viajes_proximos": (
            viajes_data
        ),

        # Próximas 24 horas
        "viajes_operativos_24h": (
            viajes_operational_counts[
                "operational"
            ]
        ),

        "viajes_atencion_24h": (
            viajes_operational_counts[
                "attention"
            ]
        ),

        "viajes_no_operativos_24h": (
            viajes_operational_counts[
                "no_operational"
            ]
        ),

        "viajes_total_24h": sum(
            viajes_operational_counts.values()
        ),

        "viajes_pendientes_cierre_24h": (
            len(
                viajes_pendientes_cierre
            )
        ),

        "alertas_operacionales": (
            alertas_operacionales
        ),

        "total_alertas": (
            len(
                alertas_operacionales
            )
        ),

        # Documentación
        "total_docs_vencidos": (
            total_docs_vencidos
        ),

        "total_docs_urgentes": (
            total_docs_urgentes
        ),

        "total_docs_proximos": (
            total_docs_proximos
        ),

        "total_docs_atencion": (
            total_docs_atencion
        ),

        "total_docs_choferes": (
            total_docs_choferes
        ),

        "total_docs_buses": (
            total_docs_buses
        ),

        "driver_docs_vencidos": (
            driver_docs_vencidos
        ),

        "driver_docs_urgentes": (
            driver_docs_urgentes
        ),

        "driver_docs_proximos": (
            driver_docs_proximos
        ),

        "bus_docs_vencidos": (
            bus_docs_vencidos
        ),

        "bus_docs_urgentes": (
            bus_docs_urgentes
        ),

        "bus_docs_proximos": (
            bus_docs_proximos
        ),

        # Mantención
        "margen_mantenimiento_km": (
            margen_mantenimiento_km
        ),

        "mantenciones_vencidas": (
            mantenciones_vencidas
        ),

        "mantenciones_proximas": (
            mantenciones_proximas
        ),

        "mantenciones_al_dia": (
            mantenciones_al_dia
        ),

        "buses_mantencion_vencida": (
            buses_mantencion_vencida
        ),

        "buses_mantencion_proxima": (
            buses_mantencion_proxima
        ),

        "buses_mantencion_ok": (
            buses_mantencion_ok
        ),
    }

    return render(
        request,
        "coordinator/dashboard.html",
        context,
    )



# ============================================================================
# SINCRONIZACIÓN DOCUMENTAL DE BUSES
# ============================================================================

_BUS_DOC_SYNC_NOTE = "[SYNC_FICHA_BUS]"


def _sync_bus_document_expiry(bus, doc_type, expiry_date):
    """
    Mantiene un BusDocument operacional sincronizado desde la ficha del bus.

    Sólo administra registros marcados con _BUS_DOC_SYNC_NOTE, por lo que
    nunca modifica ni elimina documentos cargados manualmente.
    """
    synced_qs = BusDocument.objects.filter(
        bus=bus,
        doc_type=doc_type,
        notes=_BUS_DOC_SYNC_NOTE,
    ).order_by('-created_at', '-id')

    synced_doc = synced_qs.first()

    if not expiry_date:
        synced_qs.delete()
        return

    if synced_doc:
        if synced_doc.expiry_date != expiry_date:
            synced_doc.expiry_date = expiry_date
            synced_doc.save(update_fields=['expiry_date'])

        # Evita duplicados antiguos creados por el propio sincronizador.
        synced_qs.exclude(pk=synced_doc.pk).delete()
        return

    BusDocument.objects.create(
        bus=bus,
        doc_type=doc_type,
        expiry_date=expiry_date,
        notes=_BUS_DOC_SYNC_NOTE,
    )


def _sync_bus_documents(bus):
    """Sincroniza revisión técnica, seguro y permiso de circulación."""
    _sync_bus_document_expiry(
        bus,
        'technical',
        bus.technical_review_expiry,
    )
    _sync_bus_document_expiry(
        bus,
        'insurance',
        bus.insurance_expiry,
    )
    _sync_bus_document_expiry(
        bus,
        'permit',
        bus.permit_expiry,
    )


def _bus_operational_status(bus, today=None, seat_count=None):
    """
    FASE 2.12 — Estado operacional integral.

    NO OPERATIVO:
    - Bus inactivo.
    - Sin asientos físicos.
    - Documento obligatorio vencido.
    - Mantención vencida por kilometraje.

    ATENCIÓN:
    - Documento obligatorio sin fecha.
    - Documento por vencer dentro de 30 días.
    - Mantención sin programación.
    - Mantención dentro de 1.000 km.

    OPERATIVO:
    - Sin bloqueos ni advertencias.
    """
    today = today or timezone.localdate()
    blockers = []
    warnings = []

    if not bus.is_active:
        blockers.append("Bus inactivo")

    if seat_count is None:
        seat_count = Seat.objects.filter(bus=bus).count()

    if seat_count <= 0:
        blockers.append("Sin asientos físicos configurados")

    docs = (
        ("Revisión técnica", bus.technical_review_expiry),
        ("Seguro", bus.insurance_expiry),
        ("Permiso de circulación", bus.permit_expiry),
    )

    for label, expiry in docs:
        if not expiry:
            warnings.append(f"{label}: sin fecha de vencimiento")
            continue

        days = (expiry - today).days

        if days < 0:
            blockers.append(f"{label} vencido hace {abs(days)} día(s)")
        elif days <= 30:
            warnings.append(f"{label} vence en {days} día(s)")

    current_km = bus.current_mileage or 0
    next_km = bus.next_maintenance_mileage or 0

    if next_km:
        remaining = next_km - current_km
        if remaining <= 0:
            blockers.append(
                f"Mantención vencida por {abs(remaining):,} km"
            )
        elif remaining <= 1000:
            warnings.append(
                f"Mantención próxima: faltan {remaining:,} km"
            )
    else:
        warnings.append("Mantención sin programación")

    if blockers:
        return {
            "key": "no_operational",
            "label": "No operativo",
            "color": "danger",
            "icon": "fa-circle-xmark",
            "can_dispatch": False,
            "blockers": blockers,
            "warnings": warnings,
            "reasons": blockers + warnings,
        }

    if warnings:
        return {
            "key": "attention",
            "label": "Atención",
            "color": "warning",
            "icon": "fa-triangle-exclamation",
            "can_dispatch": True,
            "blockers": [],
            "warnings": warnings,
            "reasons": warnings,
        }

    return {
        "key": "operational",
        "label": "Operativo",
        "color": "success",
        "icon": "fa-circle-check",
        "can_dispatch": True,
        "blockers": [],
        "warnings": [],
        "reasons": [],
    }


def _validate_bus_dispatch(bus):
    status = _bus_operational_status(bus)

    if not status["can_dispatch"]:
        raise ValidationError(
            f"El bus {bus.plate} está NO OPERATIVO y no puede "
            f"ser asignado a un viaje. Motivo(s): "
            + "; ".join(status["blockers"])
        )

    return status


def _prepare_bus_dispatch_options(buses, current_bus_id=None):
    """FASE 2.13: estado operacional para selectores de viajes."""
    prepared = []
    current_bus_id = str(current_bus_id) if current_bus_id else None
    for bus in buses:
        status = _bus_operational_status(bus)
        bus.dispatch_key = status['key']
        bus.dispatch_label = status['label']
        bus.dispatch_can_assign = status['can_dispatch']
        bus.dispatch_reasons = status['reasons']
        bus.dispatch_reason = status['reasons'][0] if status['reasons'] else 'Sin restricciones operacionales'
        bus.dispatch_is_current = current_bus_id is not None and str(bus.id) == current_bus_id
        prepared.append(bus)
    return prepared


def _decorate_trip_bus_field(form):
    """Añade estado operacional a las etiquetas del ModelChoiceField Bus."""
    statuses = {}
    for bus in form.fields['bus'].queryset:
        statuses[str(bus.pk)] = _bus_operational_status(bus)

    def label_from_instance(bus):
        status = statuses.get(str(bus.pk))
        if not status:
            return str(bus)
        suffix = status['label'].upper()
        reason = status['reasons'][0] if status['reasons'] else ''
        base = f"{bus.plate} - {bus.model or 'Sin modelo'} — {suffix}"
        return f"{base} · {reason}" if reason else base

    form.fields['bus'].label_from_instance = label_from_instance
    return statuses



# ============================================================================
# FASE 2.14 — ESTADO OPERACIONAL DE CHOFERES PARA VIAJES
# ============================================================================

def _driver_operational_status(driver, reference_date=None):
    """
    Evalúa si un chofer puede ser asignado a un viaje en una fecha dada.

    NO OPERATIVO:
    - Chofer inactivo.
    - Licencia, certificado médico o antecedentes vencidos para la fecha del viaje.

    ATENCIÓN:
    - Documento sin fecha de vencimiento.
    - Documento que vence dentro de los próximos 7 días respecto de la fecha evaluada.

    OPERATIVO:
    - Sin bloqueos ni advertencias.
    """
    reference_date = reference_date or timezone.localdate()
    blockers = []
    warnings = []

    if not driver.is_active:
        blockers.append("Chofer inactivo")

    documents = (
        ("Licencia de conducir", driver.license_expiry),
        ("Certificado médico", driver.medical_cert_expiry),
        ("Certificado de antecedentes", driver.background_check_expiry),
    )

    for label, expiry in documents:
        if not expiry:
            warnings.append(f"{label}: sin fecha de vencimiento")
            continue

        days = (expiry - reference_date).days
        if days < 0:
            blockers.append(
                f"{label} vencido para la fecha del viaje "
                f"({expiry.strftime('%d/%m/%Y')})"
            )
        elif days <= 7:
            warnings.append(f"{label} vence en {days} día(s)")

    if blockers:
        return {
            'key': 'no_operational',
            'label': 'No operativo',
            'color': 'danger',
            'icon': 'fa-circle-xmark',
            'can_dispatch': False,
            'blockers': blockers,
            'warnings': warnings,
            'reasons': blockers + warnings,
        }

    if warnings:
        return {
            'key': 'attention',
            'label': 'Atención',
            'color': 'warning',
            'icon': 'fa-triangle-exclamation',
            'can_dispatch': True,
            'blockers': [],
            'warnings': warnings,
            'reasons': warnings,
        }

    return {
        'key': 'operational',
        'label': 'Operativo',
        'color': 'success',
        'icon': 'fa-circle-check',
        'can_dispatch': True,
        'blockers': [],
        'warnings': [],
        'reasons': [],
    }


def _validate_driver_dispatch(driver, departure, role_label="Chofer"):
    """Valida al chofer contra la fecha local de salida del viaje."""
    if not driver:
        return None

    if departure:
        local_departure = timezone.localtime(departure) if timezone.is_aware(departure) else departure
        reference_date = local_departure.date()
    else:
        reference_date = timezone.localdate()

    status = _driver_operational_status(driver, reference_date=reference_date)
    if not status['can_dispatch']:
        raise ValidationError(
            f"{role_label} {driver.full_name} está NO OPERATIVO para el "
            f"{reference_date.strftime('%d/%m/%Y')} y no puede ser asignado. "
            "Motivo(s): " + '; '.join(status['blockers'])
        )
    return status


def _decorate_trip_driver_fields(form, reference_date=None):
    """Añade OPERATIVO / ATENCIÓN / NO OPERATIVO a ambos selectores de chofer."""
    reference_date = reference_date or timezone.localdate()
    all_statuses = {}

    for field_name in ('driver1', 'driver2'):
        field = form.fields.get(field_name)
        if not field:
            continue

        statuses = {}
        for driver in field.queryset:
            statuses[str(driver.pk)] = _driver_operational_status(
                driver, reference_date=reference_date
            )

        def label_from_instance(driver, statuses=statuses):
            status = statuses.get(str(driver.pk))
            if not status:
                return str(driver)
            suffix = status['label'].upper()
            reason = status['reasons'][0] if status['reasons'] else ''
            base = f"{driver.full_name} — {suffix}"
            return f"{base} · {reason}" if reason else base

        field.label_from_instance = label_from_instance
        all_statuses[field_name] = statuses

    return all_statuses

def _assistant_operational_status(assistant):
    """
    FASE 2.15 — Estado operacional del auxiliar.
    El modelo actual sólo posee is_active como regla operacional.
    """
    if not assistant:
        return {
            "key": "not_assigned",
            "label": "Sin auxiliar",
            "color": "secondary",
            "can_dispatch": True,
            "blockers": [],
            "warnings": [],
            "reasons": [],
        }

    if not assistant.is_active:
        return {
            "key": "no_operational",
            "label": "No operativo",
            "color": "danger",
            "can_dispatch": False,
            "blockers": ["Auxiliar inactivo"],
            "warnings": [],
            "reasons": ["Auxiliar inactivo"],
        }

    return {
        "key": "operational",
        "label": "Operativo",
        "color": "success",
        "can_dispatch": True,
        "blockers": [],
        "warnings": [],
        "reasons": [],
    }


def _validate_assistant_dispatch(assistant):
    status = _assistant_operational_status(assistant)

    if not status["can_dispatch"]:
        raise ValidationError(
            f"El auxiliar {assistant.full_name} está NO OPERATIVO y no puede "
            f"ser asignado al viaje. Motivo(s): "
            + "; ".join(status["blockers"])
        )

    return status


def _decorate_trip_assistant_field(form):
    statuses = {}

    for assistant in form.fields['assistant'].queryset:
        status = _assistant_operational_status(assistant)
        statuses[str(assistant.pk)] = status

    def label_from_instance(assistant):
        status = statuses.get(str(assistant.pk))
        if not status:
            return str(assistant)

        base = (
            f"{assistant.full_name} ({assistant.rut}) "
            f"— {status['label'].upper()}"
        )
        reason = status['reasons'][0] if status['reasons'] else ''
        return f"{base} · {reason}" if reason else base

    form.fields['assistant'].label_from_instance = label_from_instance
    return statuses



def _trip_operational_status(trip):
    """
    FASE 2.16 — Estado operacional integral del viaje.

    Consolida:
    - Bus
    - Chofer principal
    - Segundo chofer (si existe)
    - Auxiliar (si existe)

    El estado se evalúa respecto de la FECHA DE SALIDA del viaje.
    """
    if trip.departure:
        local_departure = (
            timezone.localtime(trip.departure)
            if timezone.is_aware(trip.departure)
            else trip.departure
        )
        reference_date = local_departure.date()
    else:
        reference_date = timezone.localdate()

    blockers = []
    warnings = []
    components = []

    # --------------------------------------------------------
    # BUS
    # --------------------------------------------------------
    if not trip.bus:
        blockers.append("Sin bus asignado")
        components.append({
            "name": "Bus",
            "key": "no_operational",
            "label": "No operativo",
            "reason": "Sin bus asignado",
        })
    else:
        bus_status = _bus_operational_status(
            trip.bus,
            today=reference_date,
            seat_count=trip.seats_total or None,
        )

        components.append({
            "name": f"Bus {trip.bus.plate}",
            "key": bus_status["key"],
            "label": bus_status["label"],
            "reason": (
                bus_status["reasons"][0]
                if bus_status["reasons"]
                else "Sin restricciones"
            ),
        })

        blockers.extend(
            [f"Bus: {reason}" for reason in bus_status["blockers"]]
        )
        warnings.extend(
            [f"Bus: {reason}" for reason in bus_status["warnings"]]
        )

    # --------------------------------------------------------
    # CHOFER PRINCIPAL
    # --------------------------------------------------------
    if not trip.driver1:
        blockers.append("Sin chofer principal asignado")
        components.append({
            "name": "Chofer principal",
            "key": "no_operational",
            "label": "No operativo",
            "reason": "Sin chofer principal asignado",
        })
    else:
        driver1_status = _driver_operational_status(
            trip.driver1,
            reference_date=reference_date,
        )
        components.append({
            "name": f"Chofer: {trip.driver1.full_name}",
            "key": driver1_status["key"],
            "label": driver1_status["label"],
            "reason": (
                driver1_status["reasons"][0]
                if driver1_status["reasons"]
                else "Sin restricciones"
            ),
        })
        blockers.extend(
            [f"Chofer principal: {reason}" for reason in driver1_status["blockers"]]
        )
        warnings.extend(
            [f"Chofer principal: {reason}" for reason in driver1_status["warnings"]]
        )

    # --------------------------------------------------------
    # SEGUNDO CHOFER — OPCIONAL
    # --------------------------------------------------------
    if trip.driver2:
        driver2_status = _driver_operational_status(
            trip.driver2,
            reference_date=reference_date,
        )
        components.append({
            "name": f"Segundo chofer: {trip.driver2.full_name}",
            "key": driver2_status["key"],
            "label": driver2_status["label"],
            "reason": (
                driver2_status["reasons"][0]
                if driver2_status["reasons"]
                else "Sin restricciones"
            ),
        })
        blockers.extend(
            [f"Segundo chofer: {reason}" for reason in driver2_status["blockers"]]
        )
        warnings.extend(
            [f"Segundo chofer: {reason}" for reason in driver2_status["warnings"]]
        )

    # --------------------------------------------------------
    # AUXILIAR — OPCIONAL
    # --------------------------------------------------------
    if trip.assistant:
        assistant_status = _assistant_operational_status(trip.assistant)
        components.append({
            "name": f"Auxiliar: {trip.assistant.full_name}",
            "key": assistant_status["key"],
            "label": assistant_status["label"],
            "reason": (
                assistant_status["reasons"][0]
                if assistant_status["reasons"]
                else "Sin restricciones"
            ),
        })
        blockers.extend(
            [f"Auxiliar: {reason}" for reason in assistant_status["blockers"]]
        )
        warnings.extend(
            [f"Auxiliar: {reason}" for reason in assistant_status["warnings"]]
        )

    if blockers:
        return {
            "key": "no_operational",
            "label": "No operativo",
            "color": "danger",
            "icon": "fa-circle-xmark",
            "can_dispatch": False,
            "blockers": blockers,
            "warnings": warnings,
            "reasons": blockers + warnings,
            "primary_reason": blockers[0],
            "components": components,
            "reference_date": reference_date,
        }

    if warnings:
        return {
            "key": "attention",
            "label": "Atención",
            "color": "warning",
            "icon": "fa-triangle-exclamation",
            "can_dispatch": True,
            "blockers": [],
            "warnings": warnings,
            "reasons": warnings,
            "primary_reason": warnings[0],
            "components": components,
            "reference_date": reference_date,
        }

    return {
        "key": "operational",
        "label": "Operativo",
        "color": "success",
        "icon": "fa-circle-check",
        "can_dispatch": True,
        "blockers": [],
        "warnings": [],
        "reasons": [],
        "primary_reason": "Sin restricciones operacionales",
        "components": components,
        "reference_date": reference_date,
    }


def _decorate_trip_operational_statuses(trips):
    """
    Agrega atributos temporales a cada Trip para que el template
    pueda mostrar su estado operacional integral sin alterar la BD.
    """
    counters = {
        "operational": 0,
        "attention": 0,
        "no_operational": 0,
    }

    for trip in trips:
        status = _trip_operational_status(trip)

        trip.operational_key = status["key"]
        trip.operational_label = status["label"]
        trip.operational_color = status["color"]
        trip.operational_icon = status["icon"]
        trip.operational_can_dispatch = status["can_dispatch"]
        trip.operational_reason = status["primary_reason"]
        trip.operational_reasons = status["reasons"]
        trip.operational_components = status["components"]

        counters[status["key"]] += 1

    return counters


# ============================================================================
# VISTAS DE BUSES
# ============================================================================
@login_required
@coordinator_required
def bus_list(request):
    """
    Centro operacional de buses.

    MULTIEMPRESA:
    - Solo muestra buses accesibles para el usuario.
    - Superuser puede ver todos.
    - Coordinador/empresa solo ve buses de su empresa.
    - Owner solo ve sus buses.

    LISTADO:
    - Búsqueda por patente, modelo, marca o empresa.
    - Filtro por estado.
    - Paginación configurable: 10, 20 o 50 buses.
    - Las métricas se calculan sobre el conjunto completo filtrado,
      no solamente sobre la página visible.

    Mantiene capacidad física, actividad, historial,
    mantenimiento y accesos operacionales.
    """

    now = timezone.now()
    today = timezone.localdate()

    query = request.GET.get(
        "q",
        "",
    ).strip()

    status_filter = request.GET.get(
        "status",
        "all",
    )

    # ============================================================
    # CANTIDAD DE BUSES POR PÁGINA
    # ============================================================
    per_page = request.GET.get(
        "per_page",
        "10",
    )

    try:
        per_page = int(
            per_page
        )

    except (
        TypeError,
        ValueError,
    ):
        per_page = 10

    if per_page not in (
        10,
        20,
        50,
    ):
        per_page = 10

    # ============================================================
    # BUSES AUTORIZADOS
    # ============================================================
    buses_qs = buses_for_user(
        request.user,
        Bus.objects
        .select_related(
            "company",
            "owner",
        )
        .annotate(
            real_seat_count=Count(
                "seats",
                distinct=True,
            ),
            trip_count=Count(
                "trips",
                distinct=True,
            ),
            future_trip_count=Count(
                "trips",
                filter=Q(
                    trips__departure__gt=now
                ),
                distinct=True,
            ),
            ticket_count=Count(
                "trips__tickets",
                distinct=True,
            ),
            document_count=Count(
                "documents",
                distinct=True,
            ),
            maintenance_count=Count(
                "maintenances",
                distinct=True,
            ),
            fuel_record_count=Count(
                "fuel_records",
                distinct=True,
            ),
        ),
    ).order_by(
        "-is_active",
        "company__name",
        "plate",
    )

    # ============================================================
    # BÚSQUEDA
    # ============================================================
    if query:
        buses_qs = buses_qs.filter(
            Q(
                plate__icontains=query
            )
            |
            Q(
                model__icontains=query
            )
            |
            Q(
                brand__icontains=query
            )
            |
            Q(
                company__name__icontains=query
            )
        )

    # ============================================================
    # FILTRO ACTIVO / INACTIVO
    # ============================================================
    if status_filter == "active":

        buses_qs = buses_qs.filter(
            is_active=True
        )

    elif status_filter == "inactive":

        buses_qs = buses_qs.filter(
            is_active=False
        )

    # ============================================================
    # CONVERTIR A LISTA PARA CALCULAR ESTADO OPERACIONAL
    # ============================================================
    buses = list(
        buses_qs
    )

    # ============================================================
    # DECORAR CADA BUS
    # ============================================================
    for bus in buses:

        # --------------------------------------------------------
        # HISTORIAL
        # --------------------------------------------------------
        bus.has_history = bool(
            bus.trip_count
            or bus.ticket_count
            or bus.document_count
            or bus.maintenance_count
            or bus.fuel_record_count
        )

        # --------------------------------------------------------
        # MANTENIMIENTO
        # --------------------------------------------------------
        bus.maintenance_status = "ok"
        bus.maintenance_label = "Sin alerta"

        next_km = (
            bus.next_maintenance_mileage
            or 0
        )

        current_km = (
            bus.current_mileage
            or 0
        )

        if next_km:

            remaining = (
                next_km
                - current_km
            )

            if remaining <= 0:

                bus.maintenance_status = (
                    "danger"
                )

                bus.maintenance_label = (
                    "Mantención vencida"
                )

            elif remaining <= 1000:

                bus.maintenance_status = (
                    "warning"
                )

                bus.maintenance_label = (
                    f"{remaining:,} km restantes"
                )

            else:

                bus.maintenance_status = (
                    "success"
                )

                bus.maintenance_label = (
                    f"{remaining:,} km restantes"
                )

        # --------------------------------------------------------
        # ESTADO OPERACIONAL
        # --------------------------------------------------------
        operational = (
            _bus_operational_status(
                bus,
                today=today,
                seat_count=(
                    bus.real_seat_count
                ),
            )
        )

        bus.operational_key = (
            operational["key"]
        )

        bus.operational_label = (
            operational["label"]
        )

        bus.operational_color = (
            operational["color"]
        )

        bus.operational_icon = (
            operational["icon"]
        )

        bus.operational_reasons = (
            operational["reasons"]
        )

    # ============================================================
    # FILTRO OPERACIONAL
    # ============================================================
    if status_filter in (
        "operational",
        "attention",
        "no_operational",
    ):

        buses = [
            bus
            for bus in buses
            if bus.operational_key
            == status_filter
        ]

    # ============================================================
    # CONTADORES
    # IMPORTANTE:
    # Se calculan ANTES de paginar para representar toda
    # la flota filtrada.
    # ============================================================
    total_buses = len(
        buses
    )

    active_count = sum(
        1
        for bus in buses
        if bus.is_active
    )

    inactive_count = sum(
        1
        for bus in buses
        if not bus.is_active
    )

    operational_count = sum(
        1
        for bus in buses
        if bus.operational_key
        == "operational"
    )

    attention_count = sum(
        1
        for bus in buses
        if bus.operational_key
        == "attention"
    )

    no_operational_count = sum(
        1
        for bus in buses
        if bus.operational_key
        == "no_operational"
    )

    total_seats = sum(
        bus.real_seat_count
        for bus in buses
    )

    future_trips = sum(
        bus.future_trip_count
        for bus in buses
    )

    # ============================================================
    # PAGINACIÓN
    # ============================================================
    paginator = Paginator(
        buses,
        per_page,
    )

    buses_page = paginator.get_page(
        request.GET.get(
            "page"
        )
    )

    # ============================================================
    # CONTEXTO
    # ============================================================
    context = {
        "buses": buses_page,

        "query": query,
        "status_filter": status_filter,
        "per_page": per_page,

        "total_buses": total_buses,
        "active_count": active_count,
        "inactive_count": inactive_count,

        "operational_count": operational_count,
        "attention_count": attention_count,
        "no_operational_count": no_operational_count,

        "total_seats": total_seats,
        "future_trips": future_trips,
    }

    return render(
        request,
        "coordinator/bus_list.html",
        context,
    )

@login_required
@coordinator_required
def bus_editor(request, bus_id=None):
    """
    Editor avanzado de buses con protección de integridad entre
    el plano visual (layout/numeración/servicios) y los Seat reales.

    MULTIEMPRESA:
    - Un usuario normal solo puede crear/editar buses de su empresa.
    - La empresa del usuario se fuerza desde backend.
    - Superuser puede seleccionar empresa.
    - El propietario debe pertenecer a la misma empresa del bus.
    - Un ID de bus de otra empresa devuelve 404.
    - Se revalida el bus dentro de transaction.atomic().
    - Se protege contra manipulación de company/owner por POST.

    B3.2:
    - Permite seleccionar una plantilla BusLayout.
    - Valida que la plantilla exista, esté activa y esté autorizada
      para la empresa del bus.
    - Las plantillas con company=None son genéricas y compartidas.
    - Guarda la plantilla seleccionada en Bus.layout_template.
    - Conserva compatibilidad con buses antiguos sin plantilla.
    - Expone al template la configuración completa de las plantillas
      para aplicar dimensiones y fondos dinámicamente desde JavaScript.
    """

    # ============================================================
    # ALCANCE MULTIEMPRESA
    # ============================================================

    scope = get_user_scope(request.user)
    user_company = scope.get("company")

    if not request.user.is_superuser and not user_company:
        raise PermissionDenied(
            "El usuario no tiene una empresa asignada."
        )

    # ============================================================
    # BUS EN EDICIÓN
    # ============================================================

    if bus_id:
        bus = get_object_or_404(
            buses_for_user(
                request.user,
                Bus.objects.select_related(
                    "company",
                    "owner",
                    "owner__company",
                    "layout_template",
                ),
            ),
            pk=bus_id,
        )
    else:
        bus = Bus()

        if not request.user.is_superuser:
            bus.company = user_company

    # ============================================================
    # HELPERS
    # ============================================================

    def safe_json_loads(val):
        try:
            parsed = json.loads(val) if val else []
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []

    # ============================================================
    # POST
    # ============================================================

    if request.method == "POST":

        try:
            # =====================================================
            # EMPRESA AUTORIZADA
            # =====================================================

            posted_company_id = (
                request.POST.get("company") or ""
            ).strip()

            if request.user.is_superuser:

                if not posted_company_id:
                    raise ValidationError(
                        "Debe seleccionar una empresa."
                    )

                target_company = get_object_or_404(
                    Company,
                    pk=posted_company_id,
                )

            else:
                # Nunca confiar en company recibido por POST.
                target_company = user_company

                # Si alguien manipula el POST enviando otra empresa,
                # se ignora y se conserva la empresa autorizada.
                posted_company_id = str(
                    target_company.pk
                )

            company_id = target_company.pk

            # =====================================================
            # DATOS GENERALES
            # =====================================================

            owner_id = (
                request.POST.get("owner") or ""
            ).strip()

            layout_template_id = (
                request.POST.get("layout_template") or ""
            ).strip()

            plate = (
                request.POST.get(
                    "plate",
                    "",
                )
                .upper()
                .strip()
            )

            model = (
                request.POST.get(
                    "model",
                    "",
                )
                .strip()
            )

            year_val = request.POST.get(
                "year"
            )

            year = (
                int(year_val)
                if year_val
                and year_val.isdigit()
                else 2024
            )

            # =====================================================
            # CONFIGURACIÓN DEL PLANO
            # =====================================================

            floors = int(
                request.POST.get(
                    "floors",
                    1,
                )
            )

            rows_lower = int(
                request.POST.get(
                    "rows_lower",
                    5,
                )
            )

            rows_upper = int(
                request.POST.get(
                    "rows_upper",
                    0,
                )
            )

            cols = int(
                request.POST.get(
                    "cols",
                    4,
                )
            )

            prefix_lower = request.POST.get(
                "prefix_lower",
                "",
            )

            prefix_upper = request.POST.get(
                "prefix_upper",
                "",
            )

            # =====================================================
            # DOCUMENTOS / FECHAS
            # =====================================================

            technical_review_expiry = (
                request.POST.get(
                    "technical_review_expiry"
                )
                or None
            )

            insurance_expiry = (
                request.POST.get(
                    "insurance_expiry"
                )
                or None
            )

            permit_expiry = (
                request.POST.get(
                    "permit_expiry"
                )
                or None
            )

            last_maintenance = (
                request.POST.get(
                    "last_maintenance"
                )
                or None
            )

            # =====================================================
            # VALIDACIONES GENERALES
            # =====================================================

            if not plate:
                raise ValidationError(
                    "La patente es obligatoria."
                )

            if floors not in (1, 2):
                raise ValidationError(
                    "El bus sólo puede tener 1 o 2 pisos."
                )

            if rows_lower < 1:
                raise ValidationError(
                    "El piso inferior debe tener al menos una fila."
                )

            if (
                floors == 2
                and rows_upper < 1
            ):
                raise ValidationError(
                    "Un bus de 2 pisos debe tener filas "
                    "en el piso superior."
                )

            if cols < 1 or cols > 6:
                raise ValidationError(
                    "La cantidad de columnas debe estar "
                    "entre 1 y 6."
                )

            # =====================================================
            # B3.1 — VALIDAR PLANTILLA VISUAL
            # =====================================================

            layout_template = None

            if layout_template_id:

                layout_template_qs = (
                    BusLayout.objects
                    .filter(
                        pk=layout_template_id,
                        is_active=True,
                    )
                )

                # Usuario normal: solo puede utilizar plantillas
                # de su empresa o plantillas genéricas.
                if not request.user.is_superuser:
                    layout_template_qs = (
                        layout_template_qs
                        .filter(
                            Q(company=target_company)
                            | Q(company__isnull=True)
                        )
                    )

                layout_template = (
                    layout_template_qs
                    .select_related("company")
                    .first()
                )

                if not layout_template:
                    raise ValidationError(
                        "La plantilla de bus seleccionada no existe, "
                        "está inactiva o no pertenece a la empresa "
                        "del bus."
                    )

            # =====================================================
            # VALIDAR PROPIETARIO CONTRA EMPRESA
            # =====================================================

            owner = None

            if owner_id:

                owner = (
                    FleetOwner.objects
                    .filter(
                        pk=owner_id,
                        company=target_company,
                        is_active=True,
                    )
                    .select_related(
                        "company"
                    )
                    .first()
                )

                if not owner:
                    raise ValidationError(
                        "El propietario / socio seleccionado "
                        "no existe, está inactivo o pertenece "
                        "a otra empresa operadora."
                    )

            # =====================================================
            # TAMAÑOS DE LOS PLANOS
            # =====================================================

            lower_size = (
                rows_lower * cols
            )

            upper_size = (
                rows_upper * cols
                if floors == 2
                else 0
            )

            # =====================================================
            # RECUPERAR JSON DEL EDITOR
            # =====================================================

            layout_lower = safe_json_loads(
                request.POST.get(
                    "layout_lower"
                )
            )

            layout_upper = safe_json_loads(
                request.POST.get(
                    "layout_upper"
                )
            )

            numbers_lower = safe_json_loads(
                request.POST.get(
                    "numbers_lower"
                )
            )

            numbers_upper = safe_json_loads(
                request.POST.get(
                    "numbers_upper"
                )
            )

            services_lower = safe_json_loads(
                request.POST.get(
                    "services_lower"
                )
            )

            services_upper = safe_json_loads(
                request.POST.get(
                    "services_upper"
                )
            )

            # =====================================================
            # NORMALIZAR PISO INFERIOR
            # =====================================================

            layout_lower = (
                layout_lower
                + ["L"] * lower_size
            )[:lower_size]

            numbers_lower = (
                numbers_lower
                + [""] * lower_size
            )[:lower_size]

            services_lower = (
                services_lower
                + ["semi_cama"] * lower_size
            )[:lower_size]

            # =====================================================
            # NORMALIZAR PISO SUPERIOR
            # =====================================================

            if floors == 2:

                layout_upper = (
                    layout_upper
                    + ["L"] * upper_size
                )[:upper_size]

                numbers_upper = (
                    numbers_upper
                    + [""] * upper_size
                )[:upper_size]

                services_upper = (
                    services_upper
                    + ["semi_cama"] * upper_size
                )[:upper_size]

            else:

                layout_upper = []
                numbers_upper = []
                services_upper = []

            # =====================================================
            # NUMERACIÓN AUTOMÁTICA
            # =====================================================

            next_counter = 1

            numbers_lower, next_counter = (
                assign_missing_numbers(
                    numbers_lower,
                    layout_lower,
                    prefix_lower,
                    next_counter,
                )
            )

            if floors == 2:

                numbers_upper, _ = (
                    assign_missing_numbers(
                        numbers_upper,
                        layout_upper,
                        prefix_upper,
                        next_counter,
                    )
                )

            # =====================================================
            # DETECTAR CAMBIOS DEL PLANO
            # =====================================================

            seatmap_changed = not bus.pk

            if bus.pk:

                seatmap_changed = any([
                    bus.layout_template_id
                    != (
                        layout_template.id
                        if layout_template
                        else None
                    ),

                    bus.floors != floors,
                    bus.rows_lower != rows_lower,
                    bus.rows_upper != rows_upper,
                    bus.cols != cols,

                    (bus.prefix_lower or "")
                    != prefix_lower,

                    (bus.prefix_upper or "")
                    != prefix_upper,

                    list(
                        bus.layout_lower or []
                    )
                    != layout_lower,

                    list(
                        bus.layout_upper or []
                    )
                    != layout_upper,

                    list(
                        bus.numbers_lower or []
                    )
                    != numbers_lower,

                    list(
                        bus.numbers_upper or []
                    )
                    != numbers_upper,

                    list(
                        bus.services_lower or []
                    )
                    != services_lower,

                    list(
                        bus.services_upper or []
                    )
                    != services_upper,
                ])

            # =====================================================
            # PROTECCIÓN DE INTEGRIDAD OPERACIONAL
            # =====================================================

            if (
                bus.pk
                and seatmap_changed
            ):

                future_trips_count = (
                    Trip.objects
                    .filter(
                        bus=bus,
                        departure__gt=timezone.now(),
                    )
                    .count()
                )

                ticket_count = (
                    Ticket.objects
                    .filter(
                        seat__bus=bus,
                    )
                    .count()
                )

                booking_seat_count = (
                    Seat.objects
                    .filter(
                        bus=bus,
                        booking_order_items__isnull=False,
                    )
                    .distinct()
                    .count()
                )

                blockers = []

                if future_trips_count:
                    blockers.append(
                        f"{future_trips_count} viaje(s) futuro(s)"
                    )

                if ticket_count:
                    blockers.append(
                        f"{ticket_count} ticket(s) asociado(s)"
                    )

                if booking_seat_count:
                    blockers.append(
                        f"{booking_seat_count} asiento(s) "
                        "en reservas web"
                    )

                if blockers:
                    raise ValidationError(
                        "No se guardaron cambios en el plano "
                        "de asientos. "
                        f"El bus {bus.plate} tiene "
                        + ", ".join(blockers)
                        + ". Para proteger tickets, reservas "
                          "y viajes, la distribución actual "
                          "permanece intacta."
                    )

            # =====================================================
            # GUARDADO ATÓMICO
            # =====================================================

            with transaction.atomic():

                if bus.pk:

                    locked_bus_qs = (
                        Bus.objects
                        .select_for_update(of=("self",))
                        .select_related(
                            "company",
                            "owner",
                            "owner__company",
                        )
                    )

                    locked_bus_qs = (
                        buses_for_user(
                            request.user,
                            locked_bus_qs,
                        )
                    )

                    bus = get_object_or_404(
                        locked_bus_qs,
                        pk=bus.pk,
                    )

                # =================================================
                # REVALIDAR EMPRESA DENTRO DE LA TRANSACCIÓN
                # =================================================

                if not request.user.is_superuser:

                    if (
                        bus.pk
                        and bus.company_id
                        != user_company.id
                    ):
                        raise PermissionDenied(
                            "No tiene permisos para modificar "
                            "este bus."
                        )

                    target_company = (
                        user_company
                    )

                # =================================================
                # REVALIDAR PROPIETARIO
                # =================================================

                owner = None

                if owner_id:

                    owner = (
                        FleetOwner.objects
                        .select_for_update()
                        .filter(
                            pk=owner_id,
                            company=target_company,
                            is_active=True,
                        )
                        .first()
                    )

                    if not owner:
                        raise ValidationError(
                            "El propietario / socio seleccionado "
                            "no pertenece a la empresa del bus "
                            "o ya no está activo."
                        )

                # -------------------------------------------------
                # DATOS GENERALES
                # -------------------------------------------------

                bus.company = target_company
                bus.owner = owner

                # B3.1 — plantilla seleccionada
                bus.layout_template = (
                    layout_template
                )

                bus.plate = plate
                bus.model = model
                bus.year = year

                # -------------------------------------------------
                # CONFIGURACIÓN DEL PLANO
                # -------------------------------------------------

                bus.floors = floors
                bus.rows_lower = rows_lower
                bus.rows_upper = rows_upper
                bus.cols = cols

                bus.prefix_lower = (
                    prefix_lower
                )

                bus.prefix_upper = (
                    prefix_upper
                )

                # -------------------------------------------------
                # DOCUMENTOS
                # -------------------------------------------------

                bus.technical_review_expiry = (
                    technical_review_expiry
                )

                bus.insurance_expiry = (
                    insurance_expiry
                )

                bus.permit_expiry = (
                    permit_expiry
                )

                bus.last_maintenance = (
                    last_maintenance
                )

                # -------------------------------------------------
                # LAYOUT / NÚMEROS / SERVICIOS
                # -------------------------------------------------

                bus.layout_lower = (
                    layout_lower
                )

                bus.layout_upper = (
                    layout_upper
                )

                bus.numbers_lower = (
                    numbers_lower
                )

                bus.numbers_upper = (
                    numbers_upper
                )

                bus.services_lower = (
                    services_lower
                )

                bus.services_upper = (
                    services_upper
                )

                bus.save()

                # -------------------------------------------------
                # NORMALIZAR FECHAS Y DOCUMENTOS
                # -------------------------------------------------

                bus.refresh_from_db(
                    fields=[
                        "technical_review_expiry",
                        "insurance_expiry",
                        "permit_expiry",
                    ]
                )

                _sync_bus_documents(
                    bus
                )

                # -------------------------------------------------
                # SINCRONIZAR SEAT FÍSICOS
                # -------------------------------------------------

                if seatmap_changed:

                    created = (
                        bus.regenerate_seats()
                    )

                    messages.success(
                        request,
                        f"Plano de asientos actualizado "
                        f"para {bus.plate}. "
                        f"{created} asiento(s) físicos "
                        "sincronizados.",
                    )

                messages.success(
                    request,
                    f"Bus {bus.plate} guardado correctamente.",
                )

            return redirect(
                "coordinator:bus_list"
            )

        except PermissionDenied:
            raise

        except ValidationError as e:

            messages.error(
                request,
                str(e),
            )

        except (
            TypeError,
            ValueError,
        ) as e:

            messages.error(
                request,
                f"Datos inválidos en el formulario: {str(e)}",
            )

        except Exception as e:

            messages.error(
                request,
                f"No fue posible guardar el bus: {str(e)}",
            )

    # ============================================================
    # DATOS PARA MOSTRAR EL EDITOR
    # ============================================================

    if request.user.is_superuser:

        companies = (
            Company.objects
            .all()
            .order_by(
                "name"
            )
        )

        fleet_owners = (
            FleetOwner.objects
            .filter(
                is_active=True
            )
            .select_related(
                "company"
            )
            .order_by(
                "company__name",
                "first_name",
                "last_name",
            )
        )

    else:

        companies = (
            Company.objects
            .filter(
                pk=user_company.pk
            )
        )

        fleet_owners = (
            FleetOwner.objects
            .filter(
                company=user_company,
                is_active=True,
            )
            .select_related(
                "company"
            )
            .order_by(
                "first_name",
                "last_name",
            )
        )

    # ============================================================
    # B3.1 — PLANTILLAS ACTIVAS
    # ============================================================

    if request.user.is_superuser:
        layout_templates = (
            BusLayout.objects
            .filter(
                is_active=True,
            )
            .select_related(
                "company",
            )
            .order_by(
                "company__name",
                "name",
            )
        )
    else:
        layout_templates = (
            BusLayout.objects
            .filter(
                Q(company=user_company)
                | Q(company__isnull=True),
                is_active=True,
            )
            .select_related(
                "company",
            )
            .order_by(
                "name",
            )
        )

    # ============================================================
    # CONSERVAR PROPIETARIO SELECCIONADO
    # ============================================================

    selected_owner_id = (
        request.POST.get(
            "owner"
        )
        if request.method == "POST"
        else bus.owner_id
    )

    # Evitar conservar visualmente un propietario
    # manipulado de otra empresa.
    if (
        selected_owner_id
        and not request.user.is_superuser
        and not fleet_owners.filter(
            pk=selected_owner_id
        ).exists()
    ):
        selected_owner_id = None

    # ============================================================
    # B3.1 — CONSERVAR PLANTILLA SELECCIONADA
    # ============================================================

    selected_layout_template_id = (
        request.POST.get(
            "layout_template"
        )
        if request.method == "POST"
        else bus.layout_template_id
    )

    # ============================================================
    # ESTADO OPERACIONAL DEL PLANO
    # ============================================================

    future_trips_count = 0
    ticket_count = 0
    booking_seat_count = 0
    seatmap_locked = False

    if bus.pk:

        future_trips_count = (
            Trip.objects
            .filter(
                bus=bus,
                departure__gt=timezone.now(),
            )
            .count()
        )

        ticket_count = (
            Ticket.objects
            .filter(
                seat__bus=bus,
            )
            .count()
        )

        booking_seat_count = (
            Seat.objects
            .filter(
                bus=bus,
                booking_order_items__isnull=False,
            )
            .distinct()
            .count()
        )

        seatmap_locked = bool(
            future_trips_count
            or ticket_count
            or booking_seat_count
        )

    # ============================================================
    # B3.2 — DATOS JSON DE LAS PLANTILLAS PARA EL EDITOR
    # ============================================================

    layout_templates_payload = [
        {
            "id": template.id,
            "name": template.name,
            "floors": template.floors,
            "rows_lower": (
                template.rows_lower
            ),
            "rows_upper": (
                template.rows_upper
            ),
            "cols": template.cols,
            "background_lower": (
                template.background_lower
                or ""
            ),
            "background_upper": (
                template.background_upper
                or ""
            ),
            "editor_config": (
                template.editor_config
                or {}
            ),
            "structure_config": (
                template.structure_config
                or {}
            ),
        }
        for template in layout_templates
    ]

    # ============================================================
    # CONTEXTO
    # ============================================================

    context = {
        "bus": bus,
        "companies": companies,
        "fleet_owners": fleet_owners,

        "layout_templates": (
            layout_templates
        ),

        "layout_templates_json": (
            json.dumps(
                layout_templates_payload
            )
        ),

        "selected_owner_id": (
            selected_owner_id
        ),

        "selected_layout_template_id": (
            selected_layout_template_id
        ),

        "layout_lower_json": json.dumps(
            bus.layout_lower or []
        ),

        "layout_upper_json": json.dumps(
            bus.layout_upper or []
        ),

        "numbers_lower_json": json.dumps(
            bus.numbers_lower or []
        ),

        "numbers_upper_json": json.dumps(
            bus.numbers_upper or []
        ),

        "services_lower_json": json.dumps(
            bus.services_lower or []
        ),

        "services_upper_json": json.dumps(
            bus.services_upper or []
        ),

        "seatmap_locked": (
            seatmap_locked
        ),

        "future_trips_count": (
            future_trips_count
        ),

        "ticket_count": (
            ticket_count
        ),

        "booking_seat_count": (
            booking_seat_count
        ),

        "real_seat_count": (
            Seat.objects
            .filter(
                bus=bus
            )
            .count()
            if bus.pk
            else 0
        ),
    }

    return render(
        request,
        "coordinator/bus_editor.html",
        context,
    )


# ============================================================================
# FASE 2.18.3-A1.1 — PROPIETARIOS / SOCIOS DE FLOTA
# ============================================================================

@login_required
@coordinator_required
def fleet_owners_dashboard(request):
    """
    Gestión de propietarios / socios de flota.

    MULTIEMPRESA:
    - Superuser puede administrar todas las empresas.
    - Resto solo puede ver, crear y editar propietarios de su empresa.
    - Impide cambiar la empresa manipulando el POST.
    """

    scope = get_user_scope(request.user)
    company = scope.get("company")

    # ============================================================
    # QUERYSET AUTORIZADO DE PROPIETARIOS
    # ============================================================
    owners_allowed = FleetOwner.objects.select_related(
        "company"
    )

    if not request.user.is_superuser:
        if not company:
            owners_allowed = owners_allowed.none()
        else:
            owners_allowed = owners_allowed.filter(
                company=company
            )

    # ============================================================
    # EDICIÓN
    # ============================================================
    owner_to_edit = None
    edit_id = request.GET.get("edit")

    if edit_id:
        owner_to_edit = get_object_or_404(
            owners_allowed,
            pk=edit_id,
        )

    # ============================================================
    # FORMULARIO
    # ============================================================
    if request.method == "POST":

        form = (
            FleetOwnerForm(
                request.POST,
                instance=owner_to_edit,
            )
            if owner_to_edit
            else FleetOwnerForm(
                request.POST
            )
        )

        # --------------------------------------------------------
        # Restringir empresa antes de validar POST
        # --------------------------------------------------------
        if (
            not request.user.is_superuser
            and company
            and "company" in form.fields
        ):
            form.fields[
                "company"
            ].queryset = (
                form.fields[
                    "company"
                ].queryset.filter(
                    pk=company.pk
                )
            )

        if form.is_valid():

            owner = form.save(
                commit=False
            )

            # ----------------------------------------------------
            # Forzar empresa real del usuario
            # ----------------------------------------------------
            if not request.user.is_superuser:

                if not company:
                    raise PermissionDenied(
                        "El usuario no tiene una empresa asignada."
                    )

                owner.company = company

            owner.save()

            messages.success(
                request,
                (
                    f"Propietario "
                    f"{owner.display_name} "
                    "guardado correctamente."
                ),
            )

            return redirect(
                "coordinator:fleet_owners_dashboard"
            )

        messages.error(
            request,
            "Por favor corrige los errores del formulario.",
        )

    else:

        form = (
            FleetOwnerForm(
                instance=owner_to_edit
            )
            if owner_to_edit
            else FleetOwnerForm()
        )

        if (
            not request.user.is_superuser
            and company
            and "company" in form.fields
        ):
            form.fields[
                "company"
            ].queryset = (
                form.fields[
                    "company"
                ].queryset.filter(
                    pk=company.pk
                )
            )

            form.fields[
                "company"
            ].initial = company

    # ============================================================
    # LISTADO
    # ============================================================
    query = request.GET.get(
        "q",
        "",
    ).strip()

    owners_qs = (
        owners_allowed
        .annotate(
            bus_count=Count(
                "buses"
            )
        )
        .order_by(
            "company__name",
            "first_name",
            "last_name",
        )
    )

    if query:
        owners_qs = owners_qs.filter(
            Q(
                first_name__icontains=query
            )
            |
            Q(
                last_name__icontains=query
            )
            |
            Q(
                rut__icontains=query
            )
            |
            Q(
                company__name__icontains=query
            )
            |
            Q(
                email__icontains=query
            )
        )

    paginator = Paginator(
        owners_qs,
        12,
    )

    owners_page = paginator.get_page(
        request.GET.get("page")
    )

    return render(
        request,
        "coordinator/fleet_owners.html",
        {
            "form": form,
            "owners": owners_page,
            "query": query,
            "edit_mode": bool(
                owner_to_edit
            ),
            "owner_edit_id": (
                owner_to_edit.id
                if owner_to_edit
                else None
            ),
        },
    )


@login_required
@coordinator_required
@require_POST
def fleet_owner_delete(request, owner_id):
    """
    Elimina un propietario únicamente dentro del
    alcance empresarial del usuario.
    """

    scope = get_user_scope(
        request.user
    )

    company = scope.get(
        "company"
    )

    owners_allowed = (
        FleetOwner.objects
        .select_related(
            "company"
        )
    )

    if not request.user.is_superuser:

        if not company:
            raise PermissionDenied(
                "El usuario no tiene una empresa asignada."
            )

        owners_allowed = (
            owners_allowed.filter(
                company=company
            )
        )

    owner = get_object_or_404(
        owners_allowed,
        pk=owner_id,
    )

    try:

        owner.delete()

        messages.success(
            request,
            "Propietario eliminado correctamente.",
        )

    except ProtectedError:

        messages.error(
            request,
            (
                f"No se puede eliminar a "
                f"{owner.display_name} "
                "porque tiene buses asociados. "
                "Desactívalo en su lugar."
            ),
        )

    return redirect(
        "coordinator:fleet_owners_dashboard"
    )


@login_required
@coordinator_required
def buses_dashboard(request):
    """
    Tablero unificado para gestión rápida de buses.

    MULTIEMPRESA:
    - Superuser puede administrar todos los buses.
    - Resto solo puede administrar buses de su empresa.
    - El campo company queda restringido.
    - El campo owner solo muestra propietarios de la empresa.
    - Se valida nuevamente company/owner antes de guardar.
    """

    scope = get_user_scope(
        request.user
    )

    company = scope.get(
        "company"
    )

    # ============================================================
    # BUSES AUTORIZADOS
    # ============================================================
    allowed_buses = buses_for_user(
        request.user,
        Bus.objects.select_related(
            "company",
            "owner",
            "owner__company",
        ),
    )

    # ============================================================
    # BUS A EDITAR
    # ============================================================
    bus_to_edit = None

    edit_id = request.GET.get(
        "edit"
    )

    if edit_id:
        bus_to_edit = get_object_or_404(
            allowed_buses,
            pk=edit_id,
        )

    # ============================================================
    # CREAR FORM
    # ============================================================
    if request.method == "POST":

        form = (
            BusFullForm(
                request.POST,
                instance=bus_to_edit,
            )
            if bus_to_edit
            else BusFullForm(
                request.POST
            )
        )

    else:

        form = (
            BusFullForm(
                instance=bus_to_edit
            )
            if bus_to_edit
            else BusFullForm()
        )

    # ============================================================
    # RESTRINGIR CAMPOS MULTIEMPRESA
    # ANTES DE form.is_valid()
    # ============================================================
    if not request.user.is_superuser:

        if not company:
            raise PermissionDenied(
                "El usuario no tiene una empresa asignada."
            )

        # --------------------------------------------------------
        # Empresa
        # --------------------------------------------------------
        company_field = form.fields.get(
            "company"
        )

        if company_field:

            company_field.queryset = (
                company_field.queryset.filter(
                    pk=company.pk
                )
            )

            company_field.initial = company

        # --------------------------------------------------------
        # Propietarios de la empresa
        # --------------------------------------------------------
        owner_field = form.fields.get(
            "owner"
        )

        if owner_field:

            owner_field.queryset = (
                FleetOwner.objects
                .filter(
                    company=company,
                    is_active=True,
                )
                .select_related(
                    "company"
                )
                .order_by(
                    "first_name",
                    "last_name",
                )
            )

    # ============================================================
    # GUARDAR
    # ============================================================
    if request.method == "POST":

        if form.is_valid():

            bus = form.save(
                commit=False
            )

            # ----------------------------------------------------
            # Forzar empresa para usuario no-superuser
            # ----------------------------------------------------
            if not request.user.is_superuser:
                bus.company = company

            # ----------------------------------------------------
            # VALIDAR PROPIETARIO VS EMPRESA
            # ----------------------------------------------------
            if (
                bus.owner_id
                and bus.company_id
                and bus.owner.company_id
                != bus.company_id
            ):

                form.add_error(
                    "owner",
                    (
                        "El propietario seleccionado "
                        "no pertenece a la empresa del bus."
                    ),
                )

            else:

                with transaction.atomic():

                    bus.save()

                    form.save_m2m()

                    _sync_bus_documents(
                        bus
                    )

                if bus_to_edit:

                    messages.success(
                        request,
                        (
                            f"Datos del bus "
                            f"{bus.plate} "
                            "actualizados correctamente."
                        ),
                    )

                    return redirect(
                        "coordinator:bus_detail",
                        bus_id=bus.id,
                    )

                messages.success(
                    request,
                    (
                        f"Bus {bus.plate} "
                        "creado correctamente."
                    ),
                )

                return redirect(
                    "coordinator:bus_list"
                )

        # ========================================================
        # ERRORES
        # ========================================================
        error_messages = []

        for field, errors in (
            form.errors.items()
        ):

            label = (
                form.fields[field].label
                if field in form.fields
                else "Formulario"
            )

            for error in errors:

                error_messages.append(
                    f"{label}: {error}"
                )

        messages.error(
            request,
            (
                "Error al guardar: "
                + " | ".join(
                    error_messages
                )
            ),
        )

    # ============================================================
    # LISTADO DE BUSES
    # ============================================================
    query = request.GET.get(
        "q",
        "",
    ).strip()

    buses_list = buses_for_user(
        request.user,
        Bus.objects.select_related(
            "company",
            "owner",
            "owner__company",
        ),
    ).order_by(
        "company__name",
        "plate",
    )

    if query:

        buses_list = (
            buses_list.filter(
                Q(
                    plate__icontains=query
                )
                |
                Q(
                    model__icontains=query
                )
                |
                Q(
                    owner__first_name__icontains=query
                )
                |
                Q(
                    owner__last_name__icontains=query
                )
                |
                Q(
                    owner__rut__icontains=query
                )
                |
                Q(
                    owner_first_name__icontains=query
                )
                |
                Q(
                    owner_last_name__icontains=query
                )
                |
                Q(
                    brand__icontains=query
                )
            )
        )

    paginator = Paginator(
        buses_list,
        10,
    )

    buses_page = paginator.get_page(
        request.GET.get(
            "page"
        )
    )

    context = {
        "form": form,
        "buses": buses_page,
        "query": query,
        "edit_mode": bool(
            bus_to_edit
        ),
        "bus_edit_id": (
            bus_to_edit.id
            if bus_to_edit
            else None
        ),
    }

    return render(
        request,
        "buses/buses_full.html",
        context,
    )


@login_required
@coordinator_required
def bus_duplicate(request, bus_id):
    """
    Duplica la configuración física de un bus autorizado.

    MULTIEMPRESA:
    No permite duplicar un bus perteneciente a otra empresa.
    """

    original = get_object_or_404(
        buses_for_user(
            request.user,
            Bus.objects.select_related(
                "company",
                "owner",
            ),
        ),
        pk=bus_id,
    )

    base_plate = f"{original.plate}-COPIA"
    candidate = base_plate[:30]
    counter = 2

    # La patente es globalmente única, por eso esta consulta
    # deliberadamente revisa todos los buses.
    while Bus.objects.filter(
        plate=candidate
    ).exists():
        suffix = f"-{counter}"

        candidate = (
            f"{base_plate[:30-len(suffix)]}"
            f"{suffix}"
        )

        counter += 1

    with transaction.atomic():

        new_bus = Bus(
            company=original.company,
            owner=original.owner,
            plate=candidate,
            model=original.model,
            year=original.year,
            floors=original.floors,
            rows_lower=original.rows_lower,
            rows_upper=original.rows_upper,
            cols=original.cols,
            prefix_lower=original.prefix_lower,
            prefix_upper=original.prefix_upper,
            layout_lower=list(
                original.layout_lower or []
            ),
            layout_upper=list(
                original.layout_upper or []
            ),
            numbers_lower=list(
                original.numbers_lower or []
            ),
            numbers_upper=list(
                original.numbers_upper or []
            ),
            services_lower=list(
                original.services_lower or []
            ),
            services_upper=list(
                original.services_upper or []
            ),
            is_active=False,
        )

        new_bus.save()

        created = new_bus.regenerate_seats()

    messages.success(
        request,
        (
            f"Bus duplicado como {new_bus.plate}. "
            f"Se crearon {created} asiento(s). "
            "Cambia la patente y revisa el plano "
            "antes de activarlo."
        ),
    )

    return redirect(
        "coordinator:bus_editor",
        bus_id=new_bus.id,
    )

@login_required
@coordinator_required
@require_POST
def bus_toggle_active(request, bus_id):
    """
    Activa o desactiva un bus autorizado.

    Un bus con viajes futuros no puede desactivarse.

    MULTIEMPRESA:
    Impide modificar buses de otra empresa manipulando bus_id.
    """

    with transaction.atomic():

        bus = get_object_or_404(
            buses_for_user(
                request.user,
                Bus.objects.select_for_update(),
            ),
            pk=bus_id,
        )

        if bus.is_active:

            future_trips = Trip.objects.filter(
                bus=bus,
                departure__gt=timezone.now(),
            ).count()

            if future_trips:

                return JsonResponse(
                    {
                        "success": False,
                        "protected": True,
                        "message": (
                            f"No se puede desactivar "
                            f"el bus {bus.plate}: "
                            f"tiene {future_trips} "
                            "viaje(s) futuro(s) "
                            "programado(s)."
                        ),
                    },
                    status=409,
                )

            bus.is_active = False
            action = "desactivado"

        else:

            seat_count = Seat.objects.filter(
                bus=bus
            ).count()

            if seat_count == 0:

                return JsonResponse(
                    {
                        "success": False,
                        "message": (
                            f"El bus {bus.plate} "
                            "no tiene asientos físicos. "
                            "Revise el plano antes "
                            "de activarlo."
                        ),
                    },
                    status=409,
                )

            bus.is_active = True
            action = "activado"

        bus.save(
            update_fields=[
                "is_active"
            ]
        )

    return JsonResponse(
        {
            "success": True,
            "message": (
                f"Bus {bus.plate} "
                f"{action} correctamente."
            ),
            "is_active": bus.is_active,
        }
    )


def _bus_history_summary(bus):
    """Retorna referencias que impiden eliminar físicamente un bus."""
    references = []

    trip_count = Trip.objects.filter(bus=bus).count()
    ticket_count = Ticket.objects.filter(trip__bus=bus).count()
    maintenance_count = Maintenance.objects.filter(bus=bus).count()
    fuel_count = FuelRecord.objects.filter(bus=bus).count()
    document_count = BusDocument.objects.filter(bus=bus).count()

    if trip_count:
        references.append(f'{trip_count} viaje(s)')
    if ticket_count:
        references.append(f'{ticket_count} ticket(s)')
    if maintenance_count:
        references.append(f'{maintenance_count} mantención(es)')
    if fuel_count:
        references.append(f'{fuel_count} carga(s) de combustible')
    if document_count:
        references.append(f'{document_count} documento(s)')

    return references



@login_required
@coordinator_required
@require_POST
def bus_delete_massive(request):
    """
    Eliminación masiva segura y multiempresa.

    - Solo elimina buses accesibles para el usuario.
    - No elimina buses con historial operacional.
    - Bloquea únicamente la fila principal de Bus.
    - Los Seat se eliminan automáticamente por CASCADE.
    """

    try:
        data = json.loads(request.body)
        raw_ids = data.get("ids", [])

        if not raw_ids:
            return JsonResponse(
                {
                    "success": False,
                    "error": "No se seleccionaron buses.",
                },
                status=400,
            )

        try:
            ids = [int(bus_id) for bus_id in raw_ids]
        except (TypeError, ValueError):
            return JsonResponse(
                {
                    "success": False,
                    "error": "Uno o más IDs de buses son inválidos.",
                },
                status=400,
            )

        # ========================================================
        # BUSES AUTORIZADOS PARA EL USUARIO
        # ========================================================
        authorized = list(
            buses_for_user(
                request.user,
                Bus.objects.filter(pk__in=ids),
            ).values_list("pk", "plate")
        )

        authorized_ids = {bus_id for bus_id, _plate in authorized}

        deleted_count = 0
        errors = []

        # IDs manipulados o buses fuera de la empresa.
        for requested_id in ids:
            if requested_id not in authorized_ids:
                errors.append(
                    f"Bus ID {requested_id}: no existe o no tienes acceso."
                )

        # ========================================================
        # ELIMINAR UNO POR UNO BAJO TRANSACCIÓN
        # ========================================================
        for bus_id, original_plate in authorized:

            try:
                with transaction.atomic():

                    locked_queryset = buses_for_user(
                        request.user,
                        Bus.objects.select_for_update(of=("self",)),
                    )

                    locked_bus = (
                        locked_queryset
                        .filter(pk=bus_id)
                        .first()
                    )

                    if not locked_bus:
                        errors.append(
                            f"{original_plate}: no fue posible bloquear "
                            "el bus para eliminarlo."
                        )
                        continue

                    references = _bus_history_summary(locked_bus)

                    if references:
                        errors.append(
                            f"{locked_bus.plate}: "
                            + ", ".join(references)
                        )
                        continue

                    plate = locked_bus.plate

                    # Seat tiene CASCADE hacia Bus.
                    # Django elimina automáticamente los asientos.
                    locked_bus.delete()

                    deleted_count += 1

            except ProtectedError as e:
                errors.append(
                    f"{original_plate}: tiene referencias protegidas. {e}"
                )

            except Exception as e:
                errors.append(
                    f"{original_plate}: {type(e).__name__}: {e}"
                )

        # ========================================================
        # RESPUESTA
        # ========================================================
        if errors:
            return JsonResponse(
                {
                    "success": deleted_count > 0,
                    "partial": deleted_count > 0,
                    "deleted": deleted_count,
                    "errors": errors,
                }
            )

        return JsonResponse(
            {
                "success": True,
                "deleted": deleted_count,
                "message": (
                    f"Se eliminaron correctamente "
                    f"{deleted_count} bus(es)."
                ),
            }
        )

    except json.JSONDecodeError:
        return JsonResponse(
            {
                "success": False,
                "error": "Solicitud JSON inválida.",
            },
            status=400,
        )

    except Exception as e:
        return JsonResponse(
            {
                "success": False,
                "error": f"{type(e).__name__}: {e}",
            },
            status=400,
        )


@login_required
@coordinator_required
@require_POST
def bus_delete(request, bus_id):
    """
    Eliminación individual segura y multiempresa.

    - Solo permite eliminar buses accesibles para el usuario.
    - No elimina buses con historial operacional.
    - Bloquea solamente la fila Bus.
    - Los Seat asociados se eliminan automáticamente por CASCADE.
    """

    bus = get_object_or_404(
        buses_for_user(
            request.user,
            Bus.objects.all(),
        ),
        pk=bus_id,
    )

    references = _bus_history_summary(bus)

    if references:
        return JsonResponse(
            {
                "success": False,
                "protected": True,
                "message": (
                    f"El bus {bus.plate} tiene historial: "
                    + ", ".join(references)
                    + ". No se puede eliminar. "
                      "Desactívalo para conservar la trazabilidad operacional."
                ),
            },
            status=409,
        )

    try:
        with transaction.atomic():

            locked_queryset = buses_for_user(
                request.user,
                Bus.objects.select_for_update(of=("self",)),
            )

            locked_bus = get_object_or_404(
                locked_queryset,
                pk=bus_id,
            )

            references = _bus_history_summary(locked_bus)

            if references:
                return JsonResponse(
                    {
                        "success": False,
                        "protected": True,
                        "message": (
                            f"El bus {locked_bus.plate} tiene historial: "
                            + ", ".join(references)
                            + ". No se puede eliminar. "
                              "Desactívalo para conservar "
                              "la trazabilidad operacional."
                        ),
                    },
                    status=409,
                )

            plate = locked_bus.plate

            # Los Seat asociados se eliminan por CASCADE.
            locked_bus.delete()

        return JsonResponse(
            {
                "success": True,
                "message": f"Bus {plate} eliminado correctamente.",
            }
        )

    except ProtectedError as e:
        return JsonResponse(
            {
                "success": False,
                "protected": True,
                "message": (
                    f"El bus no puede eliminarse porque "
                    f"tiene referencias protegidas. {e}"
                ),
            },
            status=409,
        )

    except Exception as e:
        return JsonResponse(
            {
                "success": False,
                "error": f"{type(e).__name__}: {e}",
            },
            status=400,
        )


@login_required
@coordinator_required
def api_bus_data(request, bus_id):
    """
    API que devuelve el layout únicamente
    de buses accesibles para el usuario.
    """

    bus = get_object_or_404(
        buses_for_user(
            request.user,
            Bus.objects.all(),
        ),
        pk=bus_id,
    )

    bus.ensure_layouts()

    return JsonResponse({
        "id": bus.id,
        "floors": bus.floors,
        "rows_lower": bus.rows_lower,
        "rows_upper": bus.rows_upper,
        "cols": bus.cols,
        "layout_lower": bus.layout_lower,
        "layout_upper": bus.layout_upper,
        "numbers_lower": bus.numbers_lower,
        "numbers_upper": bus.numbers_upper,
        "services_lower": bus.services_lower,
        "services_upper": bus.services_upper,
        "prefix_lower": bus.prefix_lower,
        "prefix_upper": bus.prefix_upper,
    })

# ============================================================================
# GESTIÓN DE VIAJES
# ============================================================================

@login_required
@coordinator_required
def trip_list(request):
    """
    Lista los viajes visibles para el usuario.

    MULTIEMPRESA:
    - Superuser: todos.
    - Coordinador/empresa: solo su empresa.
    - Owner: solo buses de su propiedad.
    """

    trips = trips_for_user(
        request.user,
        Trip.objects.select_related(
            "route",
            "route__origin",
            "route__destination",
            "bus",
            "bus__company",
        ),
    ).order_by(
        "-departure"
    )

    return render(
        request,
        "coordinator/trip_list.html",
        {
            "trips": trips,
        },
    )


@login_required
@coordinator_required
def trip_create_edit(request, trip_id=None):
    """
    Crear o editar un viaje individual con aislamiento multiempresa.

    Seguridad:
    - Solo permite editar viajes accesibles para el usuario.
    - Ruta y bus deben estar dentro de su alcance.
    - Choferes y auxiliar deben estar dentro de su alcance.
    - Ruta, bus y personal deben pertenecer a la misma empresa.
    - Protege contra manipulación manual de IDs vía POST.
    """

    # ============================================================
    # VIAJE A EDITAR
    # ============================================================

    if trip_id:
        trip = get_object_or_404(
            trips_for_user(
                request.user,
                Trip.objects.select_related(
                    "route",
                    "route__company",
                    "bus",
                    "bus__company",
                    "driver1",
                    "driver2",
                    "assistant",
                ),
            ),
            pk=trip_id,
        )
    else:
        trip = Trip()

    # ============================================================
    # QUERYSETS AUTORIZADOS
    # ============================================================

    allowed_routes = routes_for_user(
        request.user,
        Route.objects.select_related(
            "origin",
            "destination",
            "origin_terminal",
            "destination_terminal",
            "company",
        ).filter(
            is_active=True
        ),
    )

    allowed_buses = buses_for_user(
        request.user,
        Bus.objects.select_related(
            "company",
            "owner",
        ).filter(
            is_active=True
        ),
    )

    allowed_drivers = drivers_for_user(
        request.user,
        Driver.objects.select_related(
            "company",
        ).filter(
            is_active=True
        ),
    )

    allowed_assistants = assistants_for_user(
        request.user,
        Assistant.objects.select_related(
            "company",
        ).filter(
            is_active=True
        ),
    )

    # ============================================================
    # GUARDAR
    # ============================================================

    if request.method == "POST":

        try:

            route_id = request.POST.get("route")
            bus_id = request.POST.get("bus")

            driver1_id = (
                request.POST.get("driver1")
                or None
            )

            driver2_id = (
                request.POST.get("driver2")
                or None
            )

            assistant_id = (
                request.POST.get("assistant")
                or None
            )

            # ====================================================
            # VALIDACIONES BÁSICAS
            # ====================================================

            if not route_id:
                raise ValidationError(
                    "Debe seleccionar una ruta."
                )

            if not bus_id:
                raise ValidationError(
                    "Debe seleccionar un bus."
                )

            # ====================================================
            # RUTA AUTORIZADA
            # ====================================================

            route = (
                allowed_routes
                .filter(pk=route_id)
                .first()
            )

            if not route:
                raise ValidationError(
                    "La ruta seleccionada no pertenece "
                    "a su empresa o no está activa."
                )

            # ====================================================
            # BUS AUTORIZADO
            # ====================================================

            bus = (
                allowed_buses
                .filter(pk=bus_id)
                .first()
            )

            if not bus:
                raise ValidationError(
                    "El bus seleccionado no pertenece "
                    "a su empresa o no está activo."
                )

            # ====================================================
            # EMPRESA RUTA / BUS
            # ====================================================

            if route.company_id != bus.company_id:
                raise ValidationError(
                    "La ruta y el bus deben pertenecer "
                    "a la misma empresa."
                )

            # ====================================================
            # CHOFER PRINCIPAL
            # ====================================================

            driver1 = None

            if driver1_id:

                driver1 = (
                    allowed_drivers
                    .filter(pk=driver1_id)
                    .first()
                )

                if not driver1:
                    raise ValidationError(
                        "El chofer principal seleccionado "
                        "no está autorizado."
                    )

                if driver1.company_id != bus.company_id:
                    raise ValidationError(
                        "El chofer principal debe pertenecer "
                        "a la misma empresa del viaje."
                    )

            # ====================================================
            # SEGUNDO CHOFER
            # ====================================================

            driver2 = None

            if driver2_id:

                driver2 = (
                    allowed_drivers
                    .filter(pk=driver2_id)
                    .first()
                )

                if not driver2:
                    raise ValidationError(
                        "El segundo chofer seleccionado "
                        "no está autorizado."
                    )

                if driver2.company_id != bus.company_id:
                    raise ValidationError(
                        "El segundo chofer debe pertenecer "
                        "a la misma empresa del viaje."
                    )

            # ====================================================
            # AUXILIAR
            # ====================================================

            assistant = None

            if assistant_id:

                assistant = (
                    allowed_assistants
                    .filter(pk=assistant_id)
                    .first()
                )

                if not assistant:
                    raise ValidationError(
                        "El auxiliar seleccionado "
                        "no está autorizado."
                    )

                if assistant.company_id != bus.company_id:
                    raise ValidationError(
                        "El auxiliar debe pertenecer "
                        "a la misma empresa del viaje."
                    )

            # ====================================================
            # SALIDA
            # ====================================================

            departure = make_aware_datetime(
                request.POST.get("departure"),
                "Salida",
            )

            # ====================================================
            # LLEGADA
            # ====================================================

            if request.POST.get("arrival"):

                arrival = make_aware_datetime(
                    request.POST.get("arrival"),
                    "Llegada",
                )

            else:

                arrival = (
                    departure
                    + timedelta(
                        minutes=route.duration_minutes
                    )
                )

            # ====================================================
            # ASIGNAR DATOS
            # ====================================================

            trip.route = route
            trip.bus = bus

            trip.departure = departure
            trip.arrival = arrival

            trip.driver1 = driver1
            trip.driver2 = driver2
            trip.assistant = assistant

            trip.seats_total = (
                Seat.objects
                .filter(bus=bus)
                .count()
            )

            # ====================================================
            # VALIDACIÓN AUXILIAR
            # ====================================================

            if trip.assistant:
                _validate_assistant_dispatch(
                    trip.assistant
                )

            # ====================================================
            # CONFLICTOS OPERACIONALES
            # ====================================================

            conflicts = validate_trip_conflicts(
                route,
                bus,
                trip.driver1_id,
                trip.driver2_id,
                trip.departure,
                trip.arrival,
                assistant_id=trip.assistant_id,
                exclude_trip=(
                    trip
                    if trip.pk
                    else None
                ),
            )

            if conflicts:

                for conflict in conflicts:
                    messages.error(
                        request,
                        conflict,
                    )

            else:

                # =================================================
                # GUARDAR
                # =================================================

                trip.save()

                messages.success(
                    request,
                    "Viaje guardado correctamente.",
                )

                return redirect(
                    "coordinator:trip_list"
                )

        except ValidationError as e:

            messages.error(
                request,
                str(e),
            )

        except Exception as e:

            messages.error(
                request,
                f"Error: {str(e)}",
            )

    # ============================================================
    # DATOS DEL FORMULARIO
    # ============================================================

    routes = allowed_routes

    buses = allowed_buses

    drivers = (
        allowed_drivers
        .order_by("full_name")
    )

    assistants = (
        allowed_assistants
        .order_by("full_name")
    )

    return render(
        request,
        "coordinator/trip_form.html",
        {
            "trip": trip,
            "routes": routes,
            "buses": buses,
            "drivers": drivers,
            "assistants": assistants,
        },
    )



@login_required
@coordinator_required
def trips_dashboard(request):
    """
    Tablero de gestión de viajes con filtros y paginación.

    MULTIEMPRESA:
    - Solo permite editar viajes accesibles para el usuario.
    - Solo lista viajes accesibles para el usuario.
    - Restringe rutas, buses, choferes y auxiliares del formulario.
    - Revalida los recursos recibidos antes de guardar.
    """

    # ============================================================
    # LIMPIAR MENSAJES ANTIGUOS
    # ============================================================

    storage = messages.get_messages(request)
    storage.used = True
    list(storage)

    # ============================================================
    # VIAJE EN EDICIÓN
    # ============================================================

    trip_to_edit = None
    edit_id = request.GET.get("edit")

    if edit_id:
        trip_to_edit = get_object_or_404(
            trips_for_user(
                request.user,
                Trip.objects.select_related(
                    "route",
                    "route__company",
                    "bus",
                    "bus__company",
                    "driver1",
                    "driver2",
                    "assistant",
                ),
            ),
            pk=edit_id,
        )

    original_bus_id = (
        trip_to_edit.bus_id
        if trip_to_edit
        else None
    )

    original_assistant_id = (
        trip_to_edit.assistant_id
        if trip_to_edit
        else None
    )

    original_driver1_id = (
        trip_to_edit.driver1_id
        if trip_to_edit
        else None
    )

    original_driver2_id = (
        trip_to_edit.driver2_id
        if trip_to_edit
        else None
    )

    original_departure = (
        trip_to_edit.departure
        if trip_to_edit
        else None
    )

    # ============================================================
    # FORMULARIO
    # ============================================================

    if request.method == "POST":
        form = (
            TripForm(
                request.POST,
                instance=trip_to_edit,
            )
            if trip_to_edit
            else TripForm(request.POST)
        )
    else:
        form = (
            TripForm(instance=trip_to_edit)
            if trip_to_edit
            else TripForm()
        )

    # ============================================================
    # QUERYSETS AUTORIZADOS DEL FORMULARIO
    # ============================================================

    allowed_routes = routes_for_user(
        request.user,
        Route.objects.filter(
            is_active=True
        ),
    )

    allowed_buses = buses_for_user(
        request.user,
        Bus.objects.filter(
            is_active=True
        ),
    )

    allowed_drivers = drivers_for_user(
        request.user,
        Driver.objects.filter(
            is_active=True
        ),
    )

    allowed_assistants = assistants_for_user(
        request.user,
        Assistant.objects.filter(
            is_active=True
        ),
    )

    if "route" in form.fields:
        form.fields["route"].queryset = allowed_routes

    if "bus" in form.fields:
        form.fields["bus"].queryset = allowed_buses

    if "driver1" in form.fields:
        form.fields["driver1"].queryset = allowed_drivers

    if "driver2" in form.fields:
        form.fields["driver2"].queryset = allowed_drivers

    if "assistant" in form.fields:
        form.fields["assistant"].queryset = allowed_assistants

    # ============================================================
    # GUARDAR
    # ============================================================

    if request.method == "POST":

        if form.is_valid():

            trip = form.save(
                commit=False
            )

            # ====================================================
            # REVALIDACIÓN MULTIEMPRESA
            # ====================================================

            if trip.route_id:
                trip.route = get_object_or_404(
                    allowed_routes,
                    pk=trip.route_id,
                )

            if trip.bus_id:
                trip.bus = get_object_or_404(
                    allowed_buses,
                    pk=trip.bus_id,
                )

            if trip.driver1_id:
                trip.driver1 = get_object_or_404(
                    allowed_drivers,
                    pk=trip.driver1_id,
                )

            if trip.driver2_id:
                trip.driver2 = get_object_or_404(
                    allowed_drivers,
                    pk=trip.driver2_id,
                )

            if trip.assistant_id:
                trip.assistant = get_object_or_404(
                    allowed_assistants,
                    pk=trip.assistant_id,
                )

            # ====================================================
            # EMPRESA CONSISTENTE
            # ====================================================

            route_company_id = getattr(
                trip.route,
                "company_id",
                None,
            )

            bus_company_id = getattr(
                trip.bus,
                "company_id",
                None,
            )

            if (
                route_company_id
                and bus_company_id
                and route_company_id != bus_company_id
            ):
                messages.error(
                    request,
                    (
                        "La ruta y el bus deben pertenecer "
                        "a la misma empresa."
                    ),
                )

                return redirect(
                    "coordinator:trips_dashboard"
                )

            # ====================================================
            # ARRIBO / CAPACIDAD
            # ====================================================

            if not trip.arrival and trip.route:
                trip.arrival = (
                    trip.departure
                    + timedelta(
                        minutes=trip.route.duration_minutes
                    )
                )

            if trip.bus:
                trip.seats_total = (
                    Seat.objects
                    .filter(bus=trip.bus)
                    .count()
                )

            # ====================================================
            # FASE 2.12
            # VALIDACIÓN BUS OPERATIVO
            # ====================================================

            if trip.bus and (
                original_bus_id is None
                or str(original_bus_id)
                != str(trip.bus_id)
            ):
                try:
                    _validate_bus_dispatch(
                        trip.bus
                    )
                except ValidationError as e:
                    messages.error(
                        request,
                        str(e),
                    )

                    return redirect(
                        "coordinator:trips_dashboard"
                    )

            # ====================================================
            # FASE 2.14
            # VALIDACIÓN DE CHOFERES
            # ====================================================

            departure_changed = (
                original_departure is None
                or trip.departure
                != original_departure
            )

            driver_checks = (
                (
                    trip.driver1,
                    original_driver1_id,
                    "Chofer principal",
                ),
                (
                    trip.driver2,
                    original_driver2_id,
                    "Segundo chofer",
                ),
            )

            for (
                driver,
                original_driver_id,
                role_label,
            ) in driver_checks:

                if not driver:
                    continue

                driver_changed = (
                    original_driver_id is None
                    or str(original_driver_id)
                    != str(driver.pk)
                )

                if (
                    driver_changed
                    or departure_changed
                ):
                    try:
                        _validate_driver_dispatch(
                            driver,
                            trip.departure,
                            role_label=role_label,
                        )
                    except ValidationError as e:
                        messages.error(
                            request,
                            str(e),
                        )

                        return redirect(
                            "coordinator:trips_dashboard"
                        )

            # ====================================================
            # CHOFER PRINCIPAL != SEGUNDO CHOFER
            # ====================================================

            if (
                trip.driver1_id
                and trip.driver2_id
                and trip.driver1_id
                == trip.driver2_id
            ):
                messages.error(
                    request,
                    (
                        "El chofer principal y el segundo "
                        "chofer deben ser personas diferentes."
                    ),
                )

                return redirect(
                    "coordinator:trips_dashboard"
                )

            # ====================================================
            # FASE 2.15
            # VALIDACIÓN AUXILIAR
            # ====================================================

            if trip.assistant and (
                original_assistant_id is None
                or str(original_assistant_id)
                != str(trip.assistant_id)
            ):
                try:
                    _validate_assistant_dispatch(
                        trip.assistant
                    )
                except ValidationError as e:
                    messages.error(
                        request,
                        str(e),
                    )

                    return redirect(
                        "coordinator:trips_dashboard"
                    )

            # ====================================================
            # CONFLICTOS
            # ====================================================

            conflicts = validate_trip_conflicts(
                trip.route,
                trip.bus,
                trip.driver1_id,
                trip.driver2_id,
                trip.departure,
                trip.arrival,
                assistant_id=trip.assistant_id,
                exclude_trip=(
                    trip
                    if trip.pk
                    else None
                ),
            )

            if conflicts:

                for conflict in conflicts:
                    messages.error(
                        request,
                        conflict,
                    )

                return redirect(
                    "coordinator:trips_dashboard"
                )

            # ====================================================
            # GUARDAR
            # ====================================================

            trip.save()

            messages.success(
                request,
                f"Viaje {trip} procesado con éxito.",
            )

            return redirect(
                "coordinator:trips_dashboard"
            )

        else:
            messages.error(
                request,
                f"Error al guardar: {form.errors}",
            )

    # ============================================================
    # DECORADORES OPERACIONALES DEL FORMULARIO
    # ============================================================

    bus_dispatch_statuses = (
        _decorate_trip_bus_field(
            form
        )
    )

    assistant_dispatch_statuses = (
        _decorate_trip_assistant_field(
            form
        )
    )

    # ============================================================
    # FASE 2.14
    # ESTADO DE CHOFERES
    # ============================================================

    driver_reference_date = (
        timezone.localdate()
    )

    if (
        trip_to_edit
        and trip_to_edit.departure
    ):
        driver_reference_date = (
            timezone.localtime(
                trip_to_edit.departure
            ).date()
        )

    driver_dispatch_statuses = (
        _decorate_trip_driver_fields(
            form,
            reference_date=(
                driver_reference_date
            ),
        )
    )

    # ============================================================
    # FILTROS
    # ============================================================

    query = request.GET.get(
        "q",
        "",
    ).strip()

    status_filter = request.GET.get(
        "estado",
        "proximos",
    ).strip().lower()

    if status_filter not in {
        "proximos",
        "finalizados",
        "todos",
    }:
        status_filter = "proximos"

    now = timezone.now()

    # ============================================================
    # VIAJES AUTORIZADOS
    # ============================================================

    trips_list = trips_for_user(
        request.user,
        (
            Trip.objects
            .select_related(
                "route",
                "route__company",
                "route__origin",
                "route__destination",
                "bus",
                "bus__company",
                "driver1",
                "driver2",
                "assistant",
            )
            .annotate(
                tickets_count=Count(
                    "tickets",
                    distinct=True,
                )
            )
        ),
    )

    # ============================================================
    # FASE 2.18.1
    # ESTADO REAL DEL VIAJE
    # ============================================================

    if status_filter == "proximos":

        trips_list = (
            trips_list
            .filter(
                Q(
                    status=(
                        Trip.STATUS_IN_PROGRESS
                    )
                )
                |
                Q(
                    status=(
                        Trip.STATUS_SCHEDULED
                    ),
                    departure__gte=now,
                )
            )
            .order_by(
                "departure"
            )
        )

    elif status_filter == "finalizados":

        trips_list = (
            trips_list
            .filter(
                Q(
                    status=(
                        Trip.STATUS_COMPLETED
                    )
                )
                |
                Q(
                    status=(
                        Trip.STATUS_SCHEDULED
                    ),
                    departure__lt=now,
                )
            )
            .order_by(
                "-departure"
            )
        )

    else:

        trips_list = (
            trips_list
            .order_by(
                "-departure"
            )
        )

    # ============================================================
    # BÚSQUEDA
    # ============================================================

    if query:

        trips_list = trips_list.filter(
            Q(
                route__origin__name__icontains=query
            )
            |
            Q(
                route__destination__name__icontains=query
            )
            |
            Q(
                bus__plate__icontains=query
            )
            |
            Q(
                driver1__full_name__icontains=query
            )
        )

    # ============================================================
    # PAGINACIÓN
    # ============================================================

    paginator = Paginator(
        trips_list,
        10,
    )

    trips_page = paginator.get_page(
        request.GET.get(
            "page"
        )
    )

    # ============================================================
    # FASE 2.16
    # ESTADO OPERACIONAL
    # ============================================================

    trip_operational_counts = (
        _decorate_trip_operational_statuses(
            trips_page.object_list
        )
    )

    # ============================================================
    # FASE 2.18.2
    # CIERRE ASISTIDO
    # ============================================================

    for trip in trips_page.object_list:

        trip.pending_close = bool(
            trip.status
            == Trip.STATUS_IN_PROGRESS
            and trip.arrival
            and trip.arrival < now
        )

    # ============================================================
    # CONTEXTO
    # ============================================================

    context = {
        "form": form,
        "trips": trips_page,
        "query": query,
        "status_filter": status_filter,
        "edit_mode": bool(
            trip_to_edit
        ),
        "trip_edit_id": (
            trip_to_edit.id
            if trip_to_edit
            else None
        ),
        "bus_dispatch_statuses": (
            bus_dispatch_statuses
        ),
        "assistant_dispatch_statuses": (
            assistant_dispatch_statuses
        ),
        "driver_dispatch_statuses": (
            driver_dispatch_statuses
        ),
        "trip_operational_counts": (
            trip_operational_counts
        ),
    }

    return render(
        request,
        "viajes/viajes.html",
        context,
    )


@login_required
@coordinator_required
def trip_change_bus(request, trip_id):
    """
    Reasigna un viaje a un bus diferente.

    MULTIEMPRESA:
    - Solo permite acceder a viajes autorizados.
    - Solo permite seleccionar buses autorizados.
    - Impide enviar manualmente un bus de otra empresa.
    - Mantiene la reasignación de pasajeros/asientos existente.
    """

    from booking.views import _build_trip_grid

    # ============================================================
    # VIAJE AUTORIZADO
    # ============================================================
    trip = get_object_or_404(
        trips_for_user(
            request.user,
            Trip.objects.select_related(
                "bus",
                "route",
                "route__company",
            ),
        ),
        pk=trip_id,
    )

    # ============================================================
    # POST
    # ============================================================
    if request.method == "POST":
        new_bus_id = request.POST.get(
            "new_bus"
        )

        if not new_bus_id:
            messages.error(
                request,
                "Debe seleccionar un bus nuevo.",
            )

            return redirect(
                "coordinator:trip_change_bus",
                trip_id=trip.id,
            )

        try:
            with transaction.atomic():

                # =================================================
                # VOLVER A BLOQUEAR Y VALIDAR EL VIAJE
                # =================================================
                locked_trip = get_object_or_404(
                    trips_for_user(
                        request.user,
                        Trip.objects.select_for_update()
                        .select_related(
                            "bus",
                            "route",
                            "route__company",
                        ),
                    ),
                    pk=trip.id,
                )

                # =================================================
                # BUS NUEVO AUTORIZADO
                # =================================================
                new_bus = get_object_or_404(
                    buses_for_user(
                        request.user,
                        Bus.objects.select_for_update()
                        .filter(
                            is_active=True
                        ),
                    ),
                    pk=new_bus_id,
                )

                # No tiene sentido reasignar al mismo bus.
                if new_bus.pk == locked_trip.bus_id:
                    raise ValidationError(
                        "Debe seleccionar un bus diferente al actual."
                    )

                # =================================================
                # VALIDAR EMPRESA
                # =================================================
                if (
                    locked_trip.route.company_id
                    != new_bus.company_id
                ):
                    raise ValidationError(
                        "El nuevo bus y la ruta del viaje "
                        "deben pertenecer a la misma empresa."
                    )

                # =================================================
                # VALIDACIÓN OPERACIONAL DEL BUS
                # =================================================
                _validate_bus_dispatch(
                    new_bus
                )

                # =================================================
                # MAPEO DE ASIENTOS
                # =================================================
                reassign_map = {
                    k.split("_")[1]: v
                    for k, v in request.POST.items()
                    if k.startswith("seat_")
                }

                # =================================================
                # BLOQUEAR TICKETS
                # =================================================
                tickets = (
                    Ticket.objects
                    .filter(
                        trip=locked_trip
                    )
                    .select_related(
                        "seat"
                    )
                    .select_for_update()
                )

                # =================================================
                # VIAJE SIN PASAJEROS
                # =================================================
                if not tickets.exists():

                    locked_trip.bus = new_bus

                    locked_trip.seats_total = (
                        Seat.objects.filter(
                            bus=new_bus
                        ).count()
                    )

                    locked_trip.save(
                        update_fields=[
                            "bus",
                            "seats_total",
                        ]
                    )

                    messages.success(
                        request,
                        (
                            f"Bus cambiado a {new_bus.plate} "
                            "(sin pasajeros)."
                        ),
                    )

                    return redirect(
                        "coordinator:trip_list"
                    )

                # =================================================
                # VALIDAR QUE TODOS LOS ASIENTOS FUERON REASIGNADOS
                # =================================================
                missing = [
                    ticket.seat.number
                    for ticket in tickets
                    if str(ticket.seat.id)
                    not in reassign_map
                ]

                if missing:
                    raise ValidationError(
                        "Asientos sin reasignar: "
                        + ", ".join(
                            str(number)
                            for number in missing
                        )
                    )

                # =================================================
                # BLOQUEAR ASIENTOS DEL NUEVO BUS
                # =================================================
                new_seat_ids = list(
                    reassign_map.values()
                )

                new_seats = (
                    Seat.objects
                    .filter(
                        pk__in=new_seat_ids,
                        bus=new_bus,
                    )
                    .select_for_update()
                )

                new_seats_dict = {
                    str(seat.id): seat
                    for seat in new_seats
                }

                # =================================================
                # VALIDAR ASIENTOS DESTINO
                # =================================================
                for ticket in tickets:

                    new_seat_id = (
                        reassign_map.get(
                            str(ticket.seat.id)
                        )
                    )

                    if (
                        not new_seat_id
                        or new_seat_id
                        not in new_seats_dict
                    ):
                        raise ValidationError(
                            (
                                "Asiento destino no encontrado "
                                f"para {ticket.seat.number}"
                            )
                        )

                    new_seat = (
                        new_seats_dict[
                            new_seat_id
                        ]
                    )

                    # Verificar que otro ticket del mismo viaje
                    # no esté ya asociado al asiento destino.
                    if Ticket.objects.filter(
                        trip=locked_trip,
                        seat=new_seat,
                    ).exclude(
                        pk=ticket.pk
                    ).exists():
                        raise ValidationError(
                            (
                                f"El asiento {new_seat.number} "
                                "ya está ocupado."
                            )
                        )

                # =================================================
                # EJECUTAR REASIGNACIÓN
                # =================================================
                for ticket in tickets:

                    new_seat = new_seats_dict[
                        reassign_map[
                            str(ticket.seat.id)
                        ]
                    ]

                    old_seat = ticket.seat

                    old_seat.is_occupied = False

                    old_seat.save(
                        update_fields=[
                            "is_occupied"
                        ]
                    )

                    ticket.seat = new_seat
                    ticket.save(
                        update_fields=[
                            "seat"
                        ]
                    )

                    new_seat.is_occupied = True

                    new_seat.save(
                        update_fields=[
                            "is_occupied"
                        ]
                    )

                # =================================================
                # ACTUALIZAR VIAJE
                # =================================================
                locked_trip.bus = new_bus

                locked_trip.seats_total = (
                    Seat.objects.filter(
                        bus=new_bus
                    ).count()
                )

                locked_trip.save(
                    update_fields=[
                        "bus",
                        "seats_total",
                    ]
                )

                messages.success(
                    request,
                    (
                        f"Viaje reasignado a bus "
                        f"{new_bus.plate}."
                    ),
                )

                return redirect(
                    "coordinator:trip_list"
                )

        except ValidationError as e:
            messages.error(
                request,
                str(e),
            )

        except Exception as e:
            messages.error(
                request,
                f"Error inesperado: {str(e)}",
            )

        return redirect(
            "coordinator:trip_change_bus",
            trip_id=trip.id,
        )

    # ============================================================
    # BUSES DISPONIBLES PARA LA EMPRESA
    # ============================================================
    buses = _prepare_bus_dispatch_options(
        buses_for_user(
            request.user,
            Bus.objects.filter(
                is_active=True
            ).exclude(
                pk=trip.bus.pk
            ),
        ).order_by(
            "plate"
        ),
        current_bus_id=trip.bus_id,
    )

    current_lower, current_upper, cols = (
        _build_trip_grid(
            trip
        )
    )

    context = {
        "trip": trip,
        "buses": buses,
        "current_lower": current_lower,
        "current_upper": current_upper,
        "cols": cols,
    }

    return render(
        request,
        "coordinator/trip_change_bus.html",
        context,
    )


@login_required
@coordinator_required
def trip_delete(request, trip_id):
    """
    Elimina un viaje solo si pertenece al alcance del usuario
    y no tiene tickets vendidos.
    """

    trip = trips_for_user(
        request.user,
        Trip.objects.all(),
    ).filter(
        pk=trip_id
    ).first()

    if not trip:
        messages.warning(
            request,
            "El viaje que intenta eliminar no existe "
            "o no pertenece a su empresa.",
        )
        return redirect(
            "coordinator:trips_dashboard"
        )

    if Ticket.objects.filter(
        trip=trip
    ).exists():
        messages.error(
            request,
            "No se puede eliminar un viaje con tickets vendidos.",
        )
        return redirect(
            "coordinator:trips_dashboard"
        )

    trip.delete()

    messages.success(
        request,
        "Viaje eliminado correctamente.",
    )

    return redirect(
        "coordinator:trips_dashboard"
    )


# ============================================================================
# GENERACIÓN MASIVA DE VIAJES Y CALENDARIO
# ============================================================================
@login_required
@coordinator_required
def generate_trips(request):
    """
    Genera múltiples viajes recurrentes en un rango de fechas.

    MULTIEMPRESA:
    - Solo muestra rutas, buses, choferes y auxiliares accesibles.
    - Valida nuevamente todos los IDs recibidos por POST.
    - Impide mezclar ruta/bus/personal de distintas empresas.
    - Revalida ruta, bus y personal dentro de transaction.atomic().
    - Mantiene validaciones de conflictos de horario.
    - Mantiene vista previa y generación recurrente.
    """

    from django.utils.dateparse import parse_date

    # ============================================================
    # FECHA / CALENDARIO
    # ============================================================

    today = timezone.now().date()

    try:
        year = int(
            request.GET.get(
                "year",
                today.year,
            )
        )
    except (TypeError, ValueError):
        year = today.year

    try:
        month = int(
            request.GET.get(
                "month",
                today.month,
            )
        )
    except (TypeError, ValueError):
        month = today.month

    if month < 1 or month > 12:
        month = today.month

    if year < 2000 or year > 2100:
        year = today.year

    cal = calendar.monthcalendar(
        year,
        month,
    )

    first_day = datetime(
        year,
        month,
        1,
    ).date()

    last_day = (
        (
            datetime(
                year,
                month + 1,
                1,
            )
            - timedelta(days=1)
        ).date()
        if month < 12
        else datetime(
            year,
            12,
            31,
        ).date()
    )

    # ============================================================
    # QUERYSETS AUTORIZADOS
    # ============================================================

    allowed_routes = routes_for_user(
        request.user,
        Route.objects.select_related(
            "origin",
            "destination",
            "origin_terminal",
            "destination_terminal",
            "company",
        ).filter(
            is_active=True
        ),
    )

    allowed_buses = buses_for_user(
        request.user,
        Bus.objects.filter(
            is_active=True
        ),
    )

    allowed_drivers = drivers_for_user(
        request.user,
        Driver.objects.filter(
            is_active=True
        ),
    )

    allowed_assistants = assistants_for_user(
        request.user,
        Assistant.objects.filter(
            is_active=True
        ),
    )

    # ============================================================
    # VIAJES EXISTENTES DEL CALENDARIO
    # ============================================================

    existing_trips = trips_for_user(
        request.user,
        Trip.objects.filter(
            departure__date__range=(
                first_day,
                last_day,
            )
        ),
    ).values_list(
        "departure__date",
        flat=True,
    ).distinct()

    existing_dates = set(
        existing_trips
    )

    month_days = []

    for week in cal:
        week_days = []

        for day in week:

            if day == 0:
                week_days.append(
                    None
                )

            else:
                date_obj = datetime(
                    year,
                    month,
                    day,
                ).date()

                has_trip = (
                    date_obj
                    in existing_dates
                )

                week_days.append(
                    {
                        "day": day,
                        "date": date_obj,
                        "has_trip": has_trip,
                    }
                )

        month_days.append(
            week_days
        )

    # ============================================================
    # POST
    # ============================================================

    if request.method == "POST":

        is_ajax = (
            request.headers.get(
                "X-Requested-With"
            )
            == "XMLHttpRequest"
        )

        route_id = request.POST.get(
            "route"
        )

        bus_id = request.POST.get(
            "bus"
        )

        driver1_id = (
            request.POST.get(
                "driver1"
            )
            or None
        )

        driver2_id = (
            request.POST.get(
                "driver2"
            )
            or None
        )

        assistant_id = (
            request.POST.get(
                "assistant"
            )
            or None
        )

        departure_hour = (
            request.POST.get(
                "departure_hour"
            )
        )

        start_date_str = (
            request.POST.get(
                "start_date"
            )
        )

        end_date_str = (
            request.POST.get(
                "end_date"
            )
        )

        weekdays = request.POST.getlist(
            "weekdays"
        )

        action = request.POST.get(
            "action",
            "create",
        )

        errors = []

        # Objetos que serán validados.
        route = None
        bus = None
        driver1 = None
        driver2 = None
        assistant = None

        start_date = None
        end_date = None
        selected_weekdays = []
        hour = None
        minute = None

        # ========================================================
        # VALIDACIONES BÁSICAS
        # ========================================================

        if not route_id:
            errors.append(
                "Debe seleccionar una ruta."
            )

        if not bus_id:
            errors.append(
                "Debe seleccionar un bus."
            )

        if not departure_hour:
            errors.append(
                "Debe ingresar la hora de salida."
            )

        if (
            not start_date_str
            or not end_date_str
        ):
            errors.append(
                "Debe ingresar fecha inicio y fecha fin."
            )

        if not weekdays:
            errors.append(
                "Debe seleccionar al menos un día de la semana."
            )

        # ========================================================
        # VALIDACIONES DE FECHAS Y HORARIO
        # ========================================================

        if not errors:

            try:
                start_date = parse_date(
                    start_date_str
                )

                end_date = parse_date(
                    end_date_str
                )

                hour, minute = map(
                    int,
                    departure_hour.split(":"),
                )

                selected_weekdays = [
                    int(d)
                    for d in weekdays
                ]

                if (
                    not start_date
                    or not end_date
                ):
                    errors.append(
                        "Formato de fecha inválido."
                    )

                elif start_date > end_date:
                    errors.append(
                        "La fecha de inicio no puede ser "
                        "posterior a la fecha fin."
                    )

                if (
                    hour < 0
                    or hour > 23
                    or minute < 0
                    or minute > 59
                ):
                    errors.append(
                        "La hora de salida debe ser válida "
                        "(00:00 - 23:59)."
                    )

                if any(
                    day < 1 or day > 7
                    for day in selected_weekdays
                ):
                    errors.append(
                        "Los días de la semana seleccionados "
                        "no son válidos."
                    )

            except ValueError:
                errors.append(
                    "Formato de fecha u hora inválido."
                )

            except Exception as e:
                errors.append(
                    f"Error en datos: {str(e)}"
                )

        # ========================================================
        # VALIDACIONES DE NEGOCIO / MULTIEMPRESA
        # ========================================================

        if not errors:

            try:
                # ------------------------------------------------
                # RUTA
                # ------------------------------------------------

                route = allowed_routes.filter(
                    pk=route_id
                ).first()

                if not route:
                    errors.append(
                        "La ruta seleccionada no pertenece "
                        "a su empresa o no está activa."
                    )

                # ------------------------------------------------
                # BUS
                # ------------------------------------------------

                bus = allowed_buses.filter(
                    pk=bus_id
                ).first()

                if not bus:
                    errors.append(
                        "El bus seleccionado no pertenece "
                        "a su empresa o no está activo."
                    )

                # ------------------------------------------------
                # CHOFER PRINCIPAL
                # ------------------------------------------------

                if driver1_id:

                    driver1 = (
                        allowed_drivers.filter(
                            pk=driver1_id
                        ).first()
                    )

                    if not driver1:
                        errors.append(
                            "El chofer principal seleccionado "
                            "no pertenece a su empresa "
                            "o no está activo."
                        )

                # ------------------------------------------------
                # CHOFER SECUNDARIO
                # ------------------------------------------------

                if driver2_id:

                    driver2 = (
                        allowed_drivers.filter(
                            pk=driver2_id
                        ).first()
                    )

                    if not driver2:
                        errors.append(
                            "El chofer secundario seleccionado "
                            "no pertenece a su empresa "
                            "o no está activo."
                        )

                # ------------------------------------------------
                # AUXILIAR
                # ------------------------------------------------

                if assistant_id:

                    assistant = (
                        allowed_assistants.filter(
                            pk=assistant_id
                        ).first()
                    )

                    if not assistant:
                        errors.append(
                            "El auxiliar seleccionado "
                            "no pertenece a su empresa "
                            "o no está activo."
                        )

                # ------------------------------------------------
                # EMPRESA RUTA / BUS
                # ------------------------------------------------

                if route and bus:

                    if (
                        route.company_id
                        != bus.company_id
                    ):
                        errors.append(
                            "La ruta y el bus deben pertenecer "
                            "a la misma empresa."
                        )

                    if (
                        route.origin_id
                        == route.destination_id
                    ):
                        errors.append(
                            "El origen y destino de la ruta "
                            "no pueden ser iguales."
                        )

                    operational = (
                        _bus_operational_status(
                            bus
                        )
                    )

                    if not operational[
                        "can_dispatch"
                    ]:
                        errors.append(
                            f"El bus {bus.plate} está "
                            "NO OPERATIVO: "
                            + "; ".join(
                                operational[
                                    "blockers"
                                ]
                            )
                        )

                # ------------------------------------------------
                # CHOFERES DIFERENTES
                # ------------------------------------------------

                if (
                    driver1_id
                    and driver2_id
                    and str(driver1_id)
                    == str(driver2_id)
                ):
                    errors.append(
                        "El chofer principal y el chofer "
                        "secundario no pueden ser "
                        "la misma persona."
                    )

            except Exception as e:
                errors.append(
                    f"Error validando datos: {str(e)}"
                )

        # ========================================================
        # ERRORES
        # ========================================================

        if errors:

            error_msg = " | ".join(
                errors
            )

            if is_ajax:
                return JsonResponse(
                    {
                        "success": False,
                        "error": error_msg,
                    },
                    status=400,
                )

            for err in errors:
                messages.error(
                    request,
                    err,
                )

            return redirect(
                "coordinator:generate_trips"
            )

        # ========================================================
        # VISTA PREVIA
        # ========================================================

        if action == "preview":

            preview_rows = []

            preview_stats = {
                "available": 0,
                "existing": 0,
                "bus_conflict": 0,
                "driver_conflict": 0,
                "total": 0,
            }

            current_date = start_date
            delta = timedelta(days=1)

            total_seats = Seat.objects.filter(
                bus=bus
            ).count()

            while current_date <= end_date:

                if (
                    current_date.isoweekday()
                    not in selected_weekdays
                ):
                    current_date += delta
                    continue

                departure_dt = (
                    timezone.make_aware(
                        datetime.combine(
                            current_date,
                            datetime.strptime(
                                departure_hour,
                                "%H:%M",
                            ).time(),
                        ),
                        timezone.get_current_timezone(),
                    )
                )

                arrival_dt = (
                    departure_dt
                    + timedelta(
                        minutes=(
                            route.duration_minutes
                        )
                    )
                )

                status = "available"
                status_label = "Disponible"
                detail = "Se puede crear"

                # ------------------------------------------------
                # DUPLICADO EXACTO
                # ------------------------------------------------

                exact_trip = trips_for_user(
                    request.user,
                    Trip.objects.filter(
                        route=route,
                        bus=bus,
                        departure__date=current_date,
                        departure__hour=(
                            departure_dt.hour
                        ),
                        departure__minute=(
                            departure_dt.minute
                        ),
                    ),
                ).first()

                if exact_trip:

                    status = "existing"
                    status_label = "Ya existe"
                    detail = (
                        "Misma ruta, bus y hora"
                    )

                    preview_stats[
                        "existing"
                    ] += 1

                else:
                    # --------------------------------------------
                    # CONFLICTO BUS
                    # --------------------------------------------

                    bus_conflict = (
                        trips_for_user(
                            request.user,
                            Trip.objects.filter(
                                bus=bus,
                                departure__lt=arrival_dt,
                                arrival__gt=departure_dt,
                            ),
                        ).first()
                    )

                    if bus_conflict:

                        status = "bus_conflict"
                        status_label = (
                            "Bus ocupado"
                        )

                        detail = (
                            f"Conflicto con viaje "
                            f"#{bus_conflict.id} "
                            f"a las "
                            f"{timezone.localtime(bus_conflict.departure).strftime('%H:%M')}"
                        )

                        preview_stats[
                            "bus_conflict"
                        ] += 1

                    else:
                        # ----------------------------------------
                        # CONFLICTOS DE CHOFERES
                        # ----------------------------------------

                        driver_conflict = None
                        driver_label = ""

                        if driver1:

                            driver_conflict = (
                                trips_for_user(
                                    request.user,
                                    Trip.objects.filter(
                                        driver1=driver1,
                                        departure__lt=arrival_dt,
                                        arrival__gt=departure_dt,
                                    ),
                                ).first()
                            )

                            if driver_conflict:
                                driver_label = (
                                    "Chofer principal ocupado"
                                )

                        if (
                            not driver_conflict
                            and driver2
                        ):

                            driver_conflict = (
                                trips_for_user(
                                    request.user,
                                    Trip.objects.filter(
                                        driver2=driver2,
                                        departure__lt=arrival_dt,
                                        arrival__gt=departure_dt,
                                    ),
                                ).first()
                            )

                            if driver_conflict:
                                driver_label = (
                                    "Chofer secundario ocupado"
                                )

                        if driver_conflict:

                            status = (
                                "driver_conflict"
                            )

                            status_label = (
                                driver_label
                            )

                            detail = (
                                f"Conflicto con viaje "
                                f"#{driver_conflict.id} "
                                f"a las "
                                f"{timezone.localtime(driver_conflict.departure).strftime('%H:%M')}"
                            )

                            preview_stats[
                                "driver_conflict"
                            ] += 1

                        else:
                            preview_stats[
                                "available"
                            ] += 1

                preview_stats[
                    "total"
                ] += 1

                preview_rows.append(
                    {
                        "date": (
                            current_date.strftime(
                                "%d/%m/%Y"
                            )
                        ),
                        "date_iso": (
                            current_date.isoformat()
                        ),
                        "departure": (
                            departure_dt.strftime(
                                "%H:%M"
                            )
                        ),
                        "arrival": (
                            arrival_dt.strftime(
                                "%H:%M"
                            )
                        ),
                        "status": status,
                        "status_label": (
                            status_label
                        ),
                        "detail": detail,
                    }
                )

                current_date += delta

            return JsonResponse(
                {
                    "success": True,
                    "preview": True,
                    "rows": preview_rows,
                    "stats": preview_stats,
                    "summary": {
                        "route": str(route),
                        "bus": (
                            f"{bus.plate} - "
                            f"{bus.model or 'Sin modelo'}"
                        ),
                        "departure_hour": (
                            departure_hour
                        ),
                        "start_date": (
                            start_date.strftime(
                                "%d/%m/%Y"
                            )
                        ),
                        "end_date": (
                            end_date.strftime(
                                "%d/%m/%Y"
                            )
                        ),
                        "total_seats": (
                            total_seats
                        ),
                    },
                }
            )

        # ========================================================
        # CREACIÓN REAL
        # ========================================================

        created_count = 0
        skipped_count = 0
        conflict_count = 0

        current_date = start_date
        delta = timedelta(days=1)

        try:
            with transaction.atomic():

                # =================================================
                # REVALIDAR Y BLOQUEAR RUTA
                # =================================================

                locked_routes = routes_for_user(
                    request.user,
                    Route.objects.select_for_update()
                    .select_related(
                        "origin",
                        "destination",
                        "company",
                    )
                    .filter(
                        is_active=True
                    ),
                )

                route = locked_routes.filter(
                    pk=route_id
                ).first()

                if not route:
                    raise ValidationError(
                        "La ruta seleccionada ya no está "
                        "disponible para su empresa."
                    )

                # =================================================
                # REVALIDAR Y BLOQUEAR BUS
                # =================================================

                locked_buses = buses_for_user(
                    request.user,
                    Bus.objects.select_for_update()
                    .filter(
                        is_active=True
                    ),
                )

                bus = locked_buses.filter(
                    pk=bus_id
                ).first()

                if not bus:
                    raise ValidationError(
                        "El bus seleccionado ya no está "
                        "disponible para su empresa."
                    )

                if (
                    route.company_id
                    != bus.company_id
                ):
                    raise ValidationError(
                        "La ruta y el bus deben pertenecer "
                        "a la misma empresa."
                    )

                # =================================================
                # REVALIDAR PERSONAL
                # =================================================

                driver1 = None
                driver2 = None
                assistant = None

                if driver1_id:

                    driver1 = drivers_for_user(
                        request.user,
                        Driver.objects.filter(
                            is_active=True
                        ),
                    ).filter(
                        pk=driver1_id
                    ).first()

                    if not driver1:
                        raise ValidationError(
                            "El chofer principal ya no está "
                            "disponible para su empresa."
                        )

                if driver2_id:

                    driver2 = drivers_for_user(
                        request.user,
                        Driver.objects.filter(
                            is_active=True
                        ),
                    ).filter(
                        pk=driver2_id
                    ).first()

                    if not driver2:
                        raise ValidationError(
                            "El chofer secundario ya no está "
                            "disponible para su empresa."
                        )

                if assistant_id:

                    assistant = (
                        assistants_for_user(
                            request.user,
                            Assistant.objects.filter(
                                is_active=True
                            ),
                        ).filter(
                            pk=assistant_id
                        ).first()
                    )

                    if not assistant:
                        raise ValidationError(
                            "El auxiliar ya no está "
                            "disponible para su empresa."
                        )

                if (
                    driver1
                    and driver2
                    and driver1.pk
                    == driver2.pk
                ):
                    raise ValidationError(
                        "El chofer principal y el chofer "
                        "secundario no pueden ser "
                        "la misma persona."
                    )

                # =================================================
                # REVALIDAR OPERACIÓN DEL BUS
                # =================================================

                operational = (
                    _bus_operational_status(
                        bus
                    )
                )

                if not operational[
                    "can_dispatch"
                ]:
                    raise ValidationError(
                        f"El bus {bus.plate} está "
                        "NO OPERATIVO: "
                        + "; ".join(
                            operational[
                                "blockers"
                            ]
                        )
                    )

                # =================================================
                # TOTAL DE ASIENTOS
                # =================================================

                total_seats = (
                    Seat.objects.filter(
                        bus=bus
                    ).count()
                )

                # =================================================
                # PRECARGAR VIAJES DEL BUS
                # =================================================

                existing_trips_dict = {}

                trips_in_range = (
                    trips_for_user(
                        request.user,
                        Trip.objects.filter(
                            bus=bus,
                            departure__date__range=(
                                start_date,
                                end_date,
                            ),
                        ),
                    )
                    .select_related(
                        "route",
                        "bus",
                        "driver1",
                        "driver2",
                    )
                )

                for trip in trips_in_range:

                    date_key = (
                        timezone.localtime(
                            trip.departure
                        ).date()
                        if timezone.is_aware(
                            trip.departure
                        )
                        else trip.departure.date()
                    )

                    existing_trips_dict.setdefault(
                        date_key,
                        [],
                    ).append(
                        trip
                    )

                # =================================================
                # RECORRER FECHAS
                # =================================================

                while current_date <= end_date:

                    day_of_week = (
                        current_date.isoweekday()
                    )

                    if (
                        day_of_week
                        not in selected_weekdays
                    ):
                        current_date += delta
                        continue

                    departure_dt = (
                        timezone.make_aware(
                            datetime.combine(
                                current_date,
                                datetime.strptime(
                                    departure_hour,
                                    "%H:%M",
                                ).time(),
                            ),
                            timezone.get_current_timezone(),
                        )
                    )

                    arrival_dt = (
                        departure_dt
                        + timedelta(
                            minutes=(
                                route.duration_minutes
                            )
                        )
                    )

                    # =============================================
                    # 1. DUPLICADO EXACTO
                    # =============================================

                    existing_on_date = (
                        existing_trips_dict.get(
                            current_date,
                            [],
                        )
                    )

                    existing_trip = None

                    for trip in existing_on_date:

                        same_route = (
                            trip.route_id
                            == route.id
                        )

                        same_bus = (
                            trip.bus_id
                            == bus.id
                        )

                        trip_departure = (
                            timezone.localtime(
                                trip.departure
                            )
                            if timezone.is_aware(
                                trip.departure
                            )
                            else trip.departure
                        )

                        same_departure_time = (
                            trip_departure.hour
                            == departure_dt.hour
                            and
                            trip_departure.minute
                            == departure_dt.minute
                        )

                        if (
                            same_route
                            and same_bus
                            and same_departure_time
                        ):
                            existing_trip = trip
                            break

                    if existing_trip:

                        skipped_count += 1
                        current_date += delta
                        continue

                    # =============================================
                    # 2. CONFLICTO DE BUS
                    # =============================================

                    bus_conflict = (
                        trips_for_user(
                            request.user,
                            Trip.objects.filter(
                                bus=bus,
                                departure__lt=arrival_dt,
                                arrival__gt=departure_dt,
                            ),
                        ).exists()
                    )

                    if bus_conflict:

                        conflict_count += 1
                        current_date += delta
                        continue

                    # =============================================
                    # 3. CONFLICTO CHOFER PRINCIPAL
                    # =============================================

                    driver1_conflict = False

                    if driver1:

                        driver1_conflict = (
                            trips_for_user(
                                request.user,
                                Trip.objects.filter(
                                    driver1=driver1,
                                    departure__lt=arrival_dt,
                                    arrival__gt=departure_dt,
                                ),
                            ).exists()
                        )

                    if driver1_conflict:

                        conflict_count += 1
                        current_date += delta
                        continue

                    # =============================================
                    # 4. CONFLICTO CHOFER SECUNDARIO
                    # =============================================

                    driver2_conflict = False

                    if driver2:

                        driver2_conflict = (
                            trips_for_user(
                                request.user,
                                Trip.objects.filter(
                                    driver2=driver2,
                                    departure__lt=arrival_dt,
                                    arrival__gt=departure_dt,
                                ),
                            ).exists()
                        )

                    if driver2_conflict:

                        conflict_count += 1
                        current_date += delta
                        continue

                    # =============================================
                    # 5. CREAR VIAJE
                    # =============================================

                    new_trip = Trip.objects.create(
                        route=route,
                        bus=bus,
                        driver1=driver1,
                        driver2=driver2,
                        assistant=assistant,
                        departure=departure_dt,
                        arrival=arrival_dt,
                        seats_total=total_seats,
                    )

                    created_count += 1

                    existing_trips_dict.setdefault(
                        current_date,
                        [],
                    ).append(
                        new_trip
                    )

                    current_date += delta

            # ====================================================
            # MENSAJE DE RESULTADO
            # ====================================================

            message_parts = []

            if created_count > 0:
                message_parts.append(
                    f"✅ {created_count} viajes creados"
                )

            if skipped_count > 0:
                message_parts.append(
                    f"⏭️ {skipped_count} omitidos "
                    "(ya existían)"
                )

            if conflict_count > 0:
                message_parts.append(
                    f"⚠️ {conflict_count} con conflictos "
                    "de horario (no creados)"
                )

            if not message_parts:
                message_parts.append(
                    "No se crearon viajes. "
                    "Verifica los parámetros."
                )

            message = " | ".join(
                message_parts
            )

            if is_ajax:
                return JsonResponse(
                    {
                        "success": True,
                        "message": message,
                        "stats": {
                            "created": (
                                created_count
                            ),
                            "skipped": (
                                skipped_count
                            ),
                            "conflicts": (
                                conflict_count
                            ),
                        },
                    }
                )

            if created_count > 0:
                messages.success(
                    request,
                    message,
                )
            else:
                messages.warning(
                    request,
                    message,
                )

            return redirect(
                "coordinator:trips_dashboard"
            )

        except ValidationError as e:

            if is_ajax:
                return JsonResponse(
                    {
                        "success": False,
                        "error": str(e),
                    },
                    status=400,
                )

            messages.error(
                request,
                str(e),
            )

            return redirect(
                "coordinator:generate_trips"
            )

        except Exception as e:

            if is_ajax:
                return JsonResponse(
                    {
                        "success": False,
                        "error": str(e),
                    },
                    status=500,
                )

            messages.error(
                request,
                f"Error al generar viajes: {str(e)}",
            )

            return redirect(
                "coordinator:generate_trips"
            )

    # ============================================================
    # DATOS DEL FORMULARIO
    # ============================================================

    routes = allowed_routes.order_by(
        "origin__name",
        "destination__name",
    )

    buses = _prepare_bus_dispatch_options(
        allowed_buses.order_by(
            "plate"
        )
    )

    drivers = allowed_drivers.order_by(
        "full_name"
    )

    assistants = (
        allowed_assistants.order_by(
            "full_name"
        )
    )

    context = {
        "routes": routes,
        "buses": buses,
        "drivers": drivers,
        "assistants": assistants,
        "year": year,
        "month": month,
        "month_days": month_days,
        "first_day": first_day,
        "last_day": last_day,
        "today": today,
        "existing_dates": existing_dates,
        "weekday_choices": [
            (1, "Lunes"),
            (2, "Martes"),
            (3, "Miércoles"),
            (4, "Jueves"),
            (5, "Viernes"),
            (6, "Sábado"),
            (7, "Domingo"),
        ],
    }

    return render(
        request,
        "coordinator/generate_trips.html",
        context,
    )
    


@login_required
@coordinator_required
def api_trips_calendar(request):
    """
    Endpoint para FullCalendar.

    MULTIEMPRESA:
    - Devuelve únicamente viajes accesibles para la empresa del usuario.
    - Un coordinador CEJER no verá viajes de La Porteña.
    - Un coordinador de otra empresa futura verá solo los suyos.
    """

    start_str = request.GET.get("start")
    end_str = request.GET.get("end")

    if not start_str or not end_str:
        return JsonResponse([], safe=False)

    try:
        start = timezone.make_aware(
            datetime.fromisoformat(
                start_str.replace("Z", "+00:00")
            )
        )

        end = timezone.make_aware(
            datetime.fromisoformat(
                end_str.replace("Z", "+00:00")
            )
        )

    except Exception:
        return JsonResponse([], safe=False)

    # ============================================================
    # VIAJES FILTRADOS POR EMPRESA / ALCANCE
    # ============================================================
    trips = trips_for_user(
        request.user,
        Trip.objects.filter(
            departure__range=(start, end)
        ).select_related(
            "route__origin",
            "route__destination",
            "bus",
        ),
    ).annotate(
        has_tickets=Exists(
            Ticket.objects.filter(
                trip=OuterRef("pk")
            )
        )
    )

    events = []

    for trip in trips:
        events.append(
            {
                "id": trip.id,
                "title": (
                    f"{trip.route.origin.name} "
                    f"→ {trip.route.destination.name}"
                ),
                "start": trip.departure.isoformat(),
                "end": (
                    trip.arrival.isoformat()
                    if trip.arrival
                    else None
                ),
                "extendedProps": {
                    "id": trip.id,
                    "route": str(trip.route),
                    "bus": trip.bus.plate,
                    "departure_date": (
                        trip.departure.strftime(
                            "%Y-%m-%d"
                        )
                    ),
                    "departure_time": (
                        trip.departure.strftime(
                            "%H:%M"
                        )
                    ),
                    "has_tickets": (
                        trip.has_tickets
                    ),
                },
            }
        )

    return JsonResponse(
        events,
        safe=False,
    )


@login_required
@coordinator_required
@require_POST
def delete_trip_by_date(request):
    """
    Elimina un viaje específico desde el calendario.

    MULTIEMPRESA:
    - Solo permite eliminar viajes accesibles para la empresa del usuario.
    - Impide borrar un viaje de otra empresa manipulando trip_id.
    - Mantiene la protección existente de tickets vendidos.
    """

    trip_id = request.POST.get("trip_id")

    if not trip_id:
        return JsonResponse(
            {
                "success": False,
                "error": (
                    "ID de viaje no proporcionado"
                ),
            },
            status=400,
        )

    # ============================================================
    # BUSCAR SOLO ENTRE VIAJES PERMITIDOS
    # ============================================================
    trip = get_object_or_404(
        trips_for_user(
            request.user,
            Trip.objects.select_related(
                "route",
                "bus",
            ),
        ),
        pk=trip_id,
    )

    # ============================================================
    # NO ELIMINAR SI YA TIENE TICKETS
    # ============================================================
    if Ticket.objects.filter(
        trip=trip
    ).exists():

        return JsonResponse(
            {
                "success": False,
                "error": (
                    "El viaje tiene tickets vendidos, "
                    "no se puede eliminar."
                ),
            },
            status=400,
        )

    trip.delete()

    return JsonResponse(
        {
            "success": True,
            "message": (
                "Viaje eliminado correctamente."
            ),
        }
    )


# ============================================================================
# GESTIÓN DE CIUDADES
# ============================================================================

@login_required
@coordinator_required
def city_list(request):
    cities = City.objects.all().order_by("name")
    return render(request, "coordinator/city_list.html", {"cities": cities})


@login_required
@coordinator_required
def city_create_edit(request, city_id=None):
    city = get_object_or_404(City, pk=city_id) if city_id else City()
    if request.method == "POST":
        city.name = request.POST.get("name")
        city.save()
        messages.success(request, f"Ciudad '{city.name}' guardada.")
        return redirect("coordinator:city_list")
    return render(request, "coordinator/city_form.html", {"city": city})


@login_required
@coordinator_required
def city_delete(request, city_id):
    city = get_object_or_404(City, pk=city_id)
    city.delete()
    messages.success(request, "Ciudad eliminada.")
    return redirect("coordinator:city_list")


@login_required
@coordinator_required
def cities_dashboard(request):
    city_to_edit = None
    edit_id = request.GET.get('edit')
    if edit_id:
        city_to_edit = get_object_or_404(City, pk=edit_id)

    if request.method == 'POST':
        form = CityForm(request.POST, instance=city_to_edit) if city_to_edit else CityForm(request.POST)
        if form.is_valid():
            city = form.save()
            messages.success(request, f'Ciudad "{city.name}" guardada.')
            return redirect('coordinator:cities_dashboard')
        else:
            messages.error(request, 'Por favor corrige los errores.')
    else:
        form = CityForm(instance=city_to_edit) if city_to_edit else CityForm()

    query = request.GET.get('q', '').strip()
    cities_list = City.objects.all().order_by('name')
    if query:
        cities_list = cities_list.filter(name__icontains=query)

    paginator = Paginator(cities_list, 10)
    cities_page = paginator.get_page(request.GET.get('page'))

    context = {
        'form': form,
        'cities': cities_page,
        'query': query,
        'edit_mode': bool(city_to_edit),
        'city_edit_id': city_to_edit.id if city_to_edit else None,
    }
    return render(request, 'ciudades/ciudades.html', context)


# ============================================================================
# GESTIÓN DE TERMINALES
# ============================================================================

@login_required
@coordinator_required
def terminal_list(request):
    terminals = Terminal.objects.select_related("city").all().order_by("city__name", "name")
    return render(request, "coordinator/terminal_list.html", {"terminals": terminals})


@login_required
@coordinator_required
def terminal_create_edit(request, terminal_id=None):
    terminal = get_object_or_404(Terminal, pk=terminal_id) if terminal_id else Terminal()
    if request.method == "POST":
        terminal.name = request.POST.get("name")
        terminal.city_id = request.POST.get("city")
        terminal.address = request.POST.get("address", "")
        terminal.is_active = "is_active" in request.POST
        terminal.save()
        messages.success(request, f"Terminal '{terminal.name}' guardada.")
        return redirect("coordinator:terminal_list")
    cities = City.objects.all()
    return render(request, "coordinator/terminal_form.html", {"terminal": terminal, "cities": cities})


@login_required
@coordinator_required
def terminal_delete(request, terminal_id):
    terminal = get_object_or_404(Terminal, pk=terminal_id)
    terminal.delete()
    messages.success(request, "Terminal eliminada.")
    return redirect("coordinator:terminal_list")


@login_required
@coordinator_required
def terminals_dashboard(request):
    terminal_to_edit = None
    edit_id = request.GET.get('edit')
    if edit_id:
        terminal_to_edit = get_object_or_404(Terminal, pk=edit_id)

    if request.method == 'POST':
        form = TerminalForm(request.POST, instance=terminal_to_edit) if terminal_to_edit else TerminalForm(request.POST)
        if form.is_valid():
            terminal = form.save()
            messages.success(request, f'Terminal {terminal.name} guardada.')
            return redirect('coordinator:terminals_dashboard')
        else:
            messages.error(request, 'Por favor corrige los errores del formulario.')
    else:
        form = TerminalForm(instance=terminal_to_edit) if terminal_to_edit else TerminalForm()

    query = request.GET.get('q', '').strip()
    terminals_list = Terminal.objects.select_related('city').all().order_by('city__name', 'name')
    if query:
        terminals_list = terminals_list.filter(
            Q(name__icontains=query) | Q(city__name__icontains=query) | Q(address__icontains=query)
        )

    paginator = Paginator(terminals_list, 10)
    terminals_page = paginator.get_page(request.GET.get('page'))

    context = {
        'form': form,
        'terminals': terminals_page,
        'query': query,
        'edit_mode': bool(terminal_to_edit),
        'terminal_edit_id': terminal_to_edit.id if terminal_to_edit else None,
    }
    return render(request, 'terminales/terminales.html', context)


# ============================================================================
# GESTIÓN DE RUTAS
# ============================================================================

@coordinator_required
def route_list(request):
    """
    Lista de rutas visible para el usuario según su empresa.
    """

    routes = routes_for_user(
        request.user,
        Route.objects.select_related(
            "origin",
            "destination",
            "origin_terminal",
            "destination_terminal",
            "company",
        ),
    )

    return render(
        request,
        "coordinator/route_list.html",
        {
            "routes": routes,
        },
    )


@login_required
@coordinator_required
def route_create_edit(request, route_id=None):
    """
    Crear o editar una ruta.

    MULTIEMPRESA:
    - Al crear, asigna automáticamente la empresa del usuario.
    - Al editar, solo permite acceder a rutas de su empresa.
    - Impide editar rutas de otra empresa manipulando la URL.
    """

    scope = get_user_scope(request.user)

    # ============================================================
    # RUTA A EDITAR
    # ============================================================
    if route_id:
        route = get_object_or_404(
            routes_for_user(
                request.user,
                Route.objects.select_related(
                    "origin",
                    "destination",
                    "origin_terminal",
                    "destination_terminal",
                    "company",
                ),
            ),
            pk=route_id,
        )
    else:
        route = Route()

        # Solo usuarios con empresa pueden crear rutas normales.
        if scope["type"] != "superuser":
            if not scope["company"]:
                raise PermissionDenied(
                    "Su usuario no tiene una empresa asociada."
                )

            route.company = scope["company"]

    # ============================================================
    # POST
    # ============================================================
    if request.method == "POST":
        try:
            route.origin_id = request.POST.get("origin")
            route.destination_id = request.POST.get("destination")

            route.origin_terminal_id = (
                request.POST.get("origin_terminal")
                or None
            )

            route.destination_terminal_id = (
                request.POST.get("destination_terminal")
                or None
            )

            route.duration_minutes = int(
                request.POST.get(
                    "duration_minutes",
                    120,
                )
            )

            route.base_price = Decimal(
                request.POST.get(
                    "base_price",
                    0,
                )
            )

            # ====================================================
            # EMPRESA
            # ====================================================
            if scope["type"] != "superuser":
                route.company = scope["company"]

            elif not route.company_id:
                raise ValidationError(
                    "Debe asignar una empresa a la ruta."
                )

            # ====================================================
            # VALIDACIONES BÁSICAS
            # ====================================================
            if not route.origin_id:
                raise ValidationError(
                    "Debe seleccionar una ciudad de origen."
                )

            if not route.destination_id:
                raise ValidationError(
                    "Debe seleccionar una ciudad de destino."
                )

            if route.origin_id == route.destination_id:
                raise ValidationError(
                    "El origen y el destino no pueden ser iguales."
                )

            route.save()

            messages.success(
                request,
                f"Ruta {route} guardada.",
            )

            return redirect(
                "coordinator:route_list"
            )

        except (ValueError, TypeError):
            messages.error(
                request,
                "Los datos ingresados no son válidos.",
            )

        except ValidationError as e:
            messages.error(
                request,
                str(e),
            )

        except Exception as e:
            messages.error(
                request,
                f"Error al guardar la ruta: {str(e)}",
            )

    cities = City.objects.all().order_by(
        "name"
    )

    terminals = Terminal.objects.select_related(
        "city"
    ).filter(
        is_active=True
    ).order_by(
        "city__name",
        "name",
    )

    return render(
        request,
        "coordinator/route_form.html",
        {
            "route": route,
            "cities": cities,
            "terminals": terminals,
        },
    )
    


@login_required
@coordinator_required
def route_delete(request, route_id):
    """
    Elimina una ruta únicamente si pertenece al alcance del usuario.
    """

    route = get_object_or_404(
        routes_for_user(
            request.user,
            Route.objects.all(),
        ),
        pk=route_id,
    )

    try:
        route.delete()

        messages.success(
            request,
            "Ruta eliminada.",
        )

    except Exception as e:
        messages.error(
            request,
            f"No se pudo eliminar la ruta: {str(e)}",
        )

    return redirect(
        "coordinator:route_list"
    )



@login_required
@coordinator_required
def routes_dashboard(request):
    """
    Dashboard de rutas.

    MULTIEMPRESA:
    - Lista únicamente rutas de la empresa del usuario.
    - Solo permite editar rutas de la empresa.
    - Las rutas nuevas quedan asociadas automáticamente a la empresa.
    - Superuser puede seleccionar empresa desde RouteForm.
    - Usuarios normales quedan restringidos a su empresa.
    """

    scope = get_user_scope(request.user)

    company = scope.get("company")

    # ============================================================
    # VALIDAR EMPRESA PARA USUARIO NORMAL
    # ============================================================
    if scope["type"] != "superuser" and not company:
        raise PermissionDenied(
            "Su usuario no tiene una empresa asociada."
        )

    # ============================================================
    # RUTA EN EDICIÓN
    # ============================================================
    route_to_edit = None

    edit_id = request.GET.get("edit")

    if edit_id:
        route_to_edit = get_object_or_404(
            routes_for_user(
                request.user,
                Route.objects.select_related(
                    "origin",
                    "destination",
                    "origin_terminal",
                    "destination_terminal",
                    "company",
                ),
            ),
            pk=edit_id,
        )

    # ============================================================
    # CONSTRUIR FORMULARIO Y FORMSET
    # ============================================================
    if request.method == "POST":

        if route_to_edit:

            form = RouteForm(
                request.POST,
                instance=route_to_edit,
            )

            formset = RouteStopFormSet(
                request.POST,
                instance=route_to_edit,
            )

        else:

            form = RouteForm(
                request.POST
            )

            formset = RouteStopFormSet(
                request.POST
            )

    else:

        if route_to_edit:

            form = RouteForm(
                instance=route_to_edit
            )

            formset = RouteStopFormSet(
                instance=route_to_edit
            )

        else:

            form = RouteForm()

            formset = RouteStopFormSet()

    # ============================================================
    # RESTRINGIR EMPRESA ANTES DE form.is_valid()
    # ============================================================
    if scope["type"] != "superuser":

        company_field = form.fields.get(
            "company"
        )

        if company_field:

            company_field.queryset = (
                company_field
                .queryset
                .filter(
                    pk=company.pk
                )
            )

            company_field.initial = company

            # El coordinador normal no envía company desde HTML.
            # La empresa se fuerza posteriormente desde backend.
            company_field.required = False

    # ============================================================
    # PROCESAR POST
    # ============================================================
    if request.method == "POST":

        if form.is_valid() and formset.is_valid():

            route = form.save(
                commit=False
            )

            # ====================================================
            # EMPRESA
            # ====================================================
            if scope["type"] != "superuser":

                # No confiar en el POST.
                # Siempre usar la empresa real del usuario.
                route.company = company

            elif not route.company_id:

                form.add_error(
                    "company",
                    "Debe seleccionar una empresa para la ruta.",
                )

                messages.error(
                    request,
                    "Debe seleccionar una empresa para la ruta.",
                )

            # ====================================================
            # GUARDAR
            # ====================================================
            if not form.errors:

                with transaction.atomic():

                    route.save()

                    form.save_m2m()

                    formset.instance = route

                    formset.save()

                messages.success(
                    request,
                    f"Ruta {route} guardada correctamente.",
                )

                return redirect(
                    "coordinator:routes_dashboard"
                )

        else:

            messages.error(
                request,
                "Por favor corrige los errores del formulario.",
            )

    # ============================================================
    # LISTADO
    # ============================================================
    query = request.GET.get(
        "q",
        "",
    ).strip()

    routes_list = routes_for_user(
        request.user,
        Route.objects.select_related(
            "origin",
            "destination",
            "origin_terminal",
            "destination_terminal",
            "company",
        ),
    ).order_by(
        "origin__name",
        "destination__name",
    )

    if query:

        routes_list = routes_list.filter(
            Q(
                origin__name__icontains=query
            )
            |
            Q(
                destination__name__icontains=query
            )
        )

    paginator = Paginator(
        routes_list,
        10,
    )

    routes_page = paginator.get_page(
        request.GET.get(
            "page"
        )
    )

    # ============================================================
    # CONTEXTO
    # ============================================================
    context = {
        "form": form,
        "formset": formset,
        "routes": routes_page,
        "query": query,
        "edit_mode": bool(
            route_to_edit
        ),
        "route_edit_id": (
            route_to_edit.id
            if route_to_edit
            else None
        ),

        # Geografía global del sistema.
        "cities": (
            City.objects
            .all()
            .order_by(
                "name"
            )
        ),

        "terminals": (
            Terminal.objects
            .select_related(
                "city"
            )
            .filter(
                is_active=True
            )
            .order_by(
                "city__name",
                "name",
            )
        ),

        "current_company": company,
        "is_superuser": request.user.is_superuser,
    }

    return render(
        request,
        "rutas/rutas.html",
        context,
    )


# ============================================================================
# GESTIÓN DE CHOFERES
# ============================================================================

_DRIVER_DOC_SYNC_NOTE = "[SYNC_FICHA_CHOFER]"


def _sync_driver_document_expiry(driver, doc_type, expiry_date, document_number=""):
    """
    Mantiene un documento operacional sincronizado desde la ficha del chofer.

    Se usa un registro marcado internamente para no modificar ni eliminar
    documentos históricos que hayan sido cargados manualmente.
    """
    synced_qs = DriverDocument.objects.filter(
        driver=driver,
        doc_type=doc_type,
        notes=_DRIVER_DOC_SYNC_NOTE,
    ).order_by('-created_at', '-id')

    synced_doc = synced_qs.first()

    # Si el usuario limpia la fecha, eliminamos únicamente el documento
    # autogenerado por esta ficha; no tocamos archivos/documentos manuales.
    if not expiry_date:
        synced_qs.delete()
        return

    if synced_doc:
        changed_fields = []

        if synced_doc.expiry_date != expiry_date:
            synced_doc.expiry_date = expiry_date
            changed_fields.append('expiry_date')

        if doc_type == 'license':
            number = document_number or ''
            if synced_doc.document_number != number:
                synced_doc.document_number = number
                changed_fields.append('document_number')

        if changed_fields:
            synced_doc.save(update_fields=changed_fields)

        # Evita duplicados antiguos del propio sincronizador.
        synced_qs.exclude(pk=synced_doc.pk).delete()
        return

    DriverDocument.objects.create(
        driver=driver,
        doc_type=doc_type,
        document_number=(document_number or '') if doc_type == 'license' else '',
        expiry_date=expiry_date,
        notes=_DRIVER_DOC_SYNC_NOTE,
    )


def _sync_driver_documents(driver):
    """Sincroniza licencia, certificado médico y antecedentes."""
    _sync_driver_document_expiry(
        driver,
        'license',
        driver.license_expiry,
        driver.license_number,
    )
    _sync_driver_document_expiry(
        driver,
        'medical',
        driver.medical_cert_expiry,
    )
    _sync_driver_document_expiry(
        driver,
        'background',
        driver.background_check_expiry,
    )


@login_required
@coordinator_required
def driver_list(request):
    """
    Lista de choferes según la empresa del usuario.
    """

    drivers = drivers_for_user(
        request.user,
        Driver.objects.all(),
    ).order_by("full_name")

    return render(
        request,
        "coordinator/driver_list.html",
        {
            "drivers": drivers,
        },
    )


@login_required
@coordinator_required
def driver_create_edit(request, driver_id=None):
    """
    Crear o editar chofer.

    MULTIEMPRESA:
    - Solo permite editar choferes accesibles para el usuario.
    - Los choferes nuevos quedan asociados automáticamente
      a la empresa del coordinador.
    """

    scope = get_user_scope(request.user)

    # ============================================================
    # CHOFER A EDITAR
    # ============================================================
    if driver_id:
        driver = get_object_or_404(
            drivers_for_user(
                request.user,
                Driver.objects.all(),
            ),
            pk=driver_id,
        )
    else:
        driver = Driver()

        if scope["type"] != "superuser":
            if not scope["company"]:
                raise PermissionDenied(
                    "Su usuario no tiene una empresa asociada."
                )

            driver.company = scope["company"]

    # ============================================================
    # POST
    # ============================================================
    if request.method == "POST":

        driver.license_expiry = (
            request.POST.get("license_expiry")
            or None
        )

        driver.medical_cert_expiry = (
            request.POST.get("medical_cert_expiry")
            or None
        )

        driver.background_check_expiry = (
            request.POST.get("background_check_expiry")
            or None
        )

        driver.notes = request.POST.get(
            "notes",
            "",
        )

        driver.full_name = request.POST.get(
            "full_name"
        )

        driver.rut = request.POST.get(
            "rut"
        )

        driver.email = request.POST.get(
            "email",
            "",
        )

        driver.phone = request.POST.get(
            "phone",
            "",
        )

        driver.license_number = request.POST.get(
            "license_number",
            "",
        )

        driver.is_active = (
            "is_active" in request.POST
        )

        if "photo" in request.FILES:
            driver.photo = request.FILES[
                "photo"
            ]

        # ========================================================
        # EMPRESA
        # ========================================================
        if scope["type"] != "superuser":

            if not scope["company"]:
                raise PermissionDenied(
                    "Su usuario no tiene una empresa asociada."
                )

            driver.company = scope["company"]

        elif not driver.company_id:
            messages.error(
                request,
                "Debe asignar una empresa al chofer.",
            )

            return render(
                request,
                "coordinator/driver_form.html",
                {
                    "driver": driver,
                },
            )

        try:
            with transaction.atomic():
                driver.save()
                _sync_driver_documents(driver)

            messages.success(
                request,
                f"Chofer {driver.full_name} guardado.",
            )

            return redirect(
                "coordinator:driver_list"
            )

        except Exception as e:
            messages.error(
                request,
                f"No fue posible guardar el chofer: {str(e)}",
            )

    return render(
        request,
        "coordinator/driver_form.html",
        {
            "driver": driver,
        },
    )


@login_required
@coordinator_required
def driver_delete(request, driver_id):
    """
    Elimina únicamente choferes accesibles para la empresa.
    """

    driver = get_object_or_404(
        drivers_for_user(
            request.user,
            Driver.objects.all(),
        ),
        pk=driver_id,
    )

    try:
        driver.delete()

        messages.success(
            request,
            "Chofer eliminado.",
        )

    except Exception as e:
        messages.error(
            request,
            f"No se pudo eliminar el chofer: {str(e)}",
        )

    return redirect(
        "coordinator:driver_list"
    )

@login_required
@coordinator_required
def drivers_dashboard(request):
    """
    Alta/edición de choferes.

    MULTIEMPRESA:
    - Lista únicamente choferes de la empresa.
    - Solo permite editar choferes de la empresa.
    - Los nuevos choferes quedan asociados a la empresa.
    - Superuser puede seleccionar empresa desde DriverForm.
    - Usuarios normales quedan restringidos a su empresa.
    - Las fechas operacionales se sincronizan con DriverDocument.
    """

    scope = get_user_scope(request.user)

    company = scope.get("company")

    # ============================================================
    # VALIDAR EMPRESA PARA USUARIO NORMAL
    # ============================================================
    if scope["type"] != "superuser" and not company:
        raise PermissionDenied(
            "Su usuario no tiene una empresa asociada."
        )

    # ============================================================
    # CHOFER EN EDICIÓN
    # ============================================================
    driver_to_edit = None

    edit_id = request.GET.get(
        "edit"
    )

    if edit_id:
        driver_to_edit = get_object_or_404(
            drivers_for_user(
                request.user,
                Driver.objects.select_related(
                    "company"
                ),
            ),
            pk=edit_id,
        )

    # ============================================================
    # CONSTRUIR FORMULARIO
    # ============================================================
    if request.method == "POST":

        form = (
            DriverForm(
                request.POST,
                request.FILES,
                instance=driver_to_edit,
            )
            if driver_to_edit
            else DriverForm(
                request.POST,
                request.FILES,
            )
        )

    else:

        form = (
            DriverForm(
                instance=driver_to_edit
            )
            if driver_to_edit
            else DriverForm()
        )

    # ============================================================
    # RESTRINGIR EMPRESA ANTES DE form.is_valid()
    # ============================================================
    if scope["type"] != "superuser":

        company_field = form.fields.get(
            "company"
        )

        if company_field:

            company_field.queryset = (
                company_field
                .queryset
                .filter(
                    pk=company.pk
                )
            )

            company_field.initial = company

            # El coordinador normal no envía company desde HTML.
            # La empresa se fuerza posteriormente desde backend.
            company_field.required = False

    # ============================================================
    # PROCESAR POST
    # ============================================================
    if request.method == "POST":

        if form.is_valid():

            try:

                with transaction.atomic():

                    # ------------------------------------------------
                    # No guardamos todavía.
                    # Primero asignamos/validamos empresa.
                    # ------------------------------------------------
                    driver = form.save(
                        commit=False
                    )

                    # ------------------------------------------------
                    # USUARIO NORMAL
                    # ------------------------------------------------
                    if scope["type"] != "superuser":

                        # Nunca confiar en company enviado por POST.
                        driver.company = company

                    # ------------------------------------------------
                    # SUPERUSER
                    # ------------------------------------------------
                    elif not driver.company_id:

                        form.add_error(
                            "company",
                            "Debe seleccionar una empresa para el chofer.",
                        )

                        raise ValidationError(
                            "Debe seleccionar una empresa para el chofer."
                        )

                    # ------------------------------------------------
                    # GUARDAR
                    # ------------------------------------------------
                    driver.save()

                    form.save_m2m()

                    _sync_driver_documents(
                        driver
                    )

                messages.success(
                    request,
                    (
                        f"Chofer {driver.full_name} guardado "
                        "y documentos sincronizados."
                    ),
                )

                return redirect(
                    "coordinator:drivers_dashboard"
                )

            except ValidationError as e:

                messages.error(
                    request,
                    str(e),
                )

            except Exception as e:

                messages.error(
                    request,
                    (
                        "No fue posible guardar el chofer: "
                        f"{str(e)}"
                    ),
                )

        else:

            messages.error(
                request,
                "Por favor corrige los errores del formulario.",
            )

    # ============================================================
    # LISTADO MULTIEMPRESA
    # ============================================================
    query = request.GET.get(
        "q",
        "",
    ).strip()

    drivers_list = drivers_for_user(
        request.user,
        Driver.objects.select_related(
            "company"
        ),
    ).order_by(
        "-is_active",
        "full_name",
    )

    if query:

        drivers_list = drivers_list.filter(
            Q(
                full_name__icontains=query
            )
            |
            Q(
                rut__icontains=query
            )
        )

    paginator = Paginator(
        drivers_list,
        10,
    )

    drivers_page = paginator.get_page(
        request.GET.get(
            "page"
        )
    )

    # ============================================================
    # CONTEXTO
    # ============================================================
    context = {
        "form": form,
        "drivers": drivers_page,
        "query": query,
        "edit_mode": bool(
            driver_to_edit
        ),
        "driver_edit_id": (
            driver_to_edit.id
            if driver_to_edit
            else None
        ),
        "current_company": company,
        "is_superuser": request.user.is_superuser,
    }

    return render(
        request,
        "choferes/choferes.html",
        context,
    )

# ============================================================================
# GESTIÓN DE AUXILIARES
# ============================================================================

@login_required
@coordinator_required
def assistant_list(request):
    """
    Lista de auxiliares según empresa.
    """

    assistants = assistants_for_user(
        request.user,
        Assistant.objects.all(),
    ).order_by(
        "full_name"
    )

    return render(
        request,
        "coordinator/assistant_list.html",
        {
            "assistants": assistants,
        },
    )


@login_required
@coordinator_required
def assistant_create_edit(request, assistant_id=None):
    """
    Crear o editar auxiliar.

    MULTIEMPRESA:
    - Solo permite editar auxiliares de la empresa.
    - Los nuevos auxiliares quedan asociados automáticamente
      a la empresa del usuario.
    """

    scope = get_user_scope(request.user)

    # ============================================================
    # AUXILIAR A EDITAR
    # ============================================================
    if assistant_id:

        assistant = get_object_or_404(
            assistants_for_user(
                request.user,
                Assistant.objects.all(),
            ),
            pk=assistant_id,
        )

    else:
        assistant = Assistant()

        if scope["type"] != "superuser":

            if not scope["company"]:
                raise PermissionDenied(
                    "Su usuario no tiene una empresa asociada."
                )

            assistant.company = scope[
                "company"
            ]

    # ============================================================
    # POST
    # ============================================================
    if request.method == "POST":

        assistant.full_name = request.POST.get(
            "full_name"
        )

        assistant.rut = request.POST.get(
            "rut"
        )

        assistant.email = request.POST.get(
            "email",
            "",
        )

        assistant.phone = request.POST.get(
            "phone",
            "",
        )

        assistant.notes = request.POST.get(
            "notes",
            "",
        )

        assistant.is_active = (
            "is_active" in request.POST
        )

        if "photo" in request.FILES:
            assistant.photo = request.FILES[
                "photo"
            ]

        # ========================================================
        # EMPRESA
        # ========================================================
        if scope["type"] != "superuser":

            if not scope["company"]:
                raise PermissionDenied(
                    "Su usuario no tiene una empresa asociada."
                )

            assistant.company = scope[
                "company"
            ]

        elif not assistant.company_id:

            messages.error(
                request,
                "Debe asignar una empresa al auxiliar.",
            )

            return render(
                request,
                "coordinator/assistant_form.html",
                {
                    "assistant": assistant,
                },
            )

        try:
            assistant.save()

            messages.success(
                request,
                f"Auxiliar {assistant.full_name} guardado.",
            )

            return redirect(
                "coordinator:assistant_list"
            )

        except Exception as e:
            messages.error(
                request,
                f"No fue posible guardar el auxiliar: {str(e)}",
            )

    return render(
        request,
        "coordinator/assistant_form.html",
        {
            "assistant": assistant,
        },
    )


@login_required
@coordinator_required
def assistant_delete(request, assistant_id):
    """
    Elimina únicamente auxiliares accesibles para la empresa.
    """

    assistant = get_object_or_404(
        assistants_for_user(
            request.user,
            Assistant.objects.all(),
        ),
        pk=assistant_id,
    )

    try:
        assistant.delete()

        messages.success(
            request,
            "Auxiliar eliminado.",
        )

    except Exception as e:
        messages.error(
            request,
            f"No se pudo eliminar el auxiliar: {str(e)}",
        )

    return redirect(
        "coordinator:assistant_list"
    )

@login_required
@coordinator_required
def assistants_dashboard(request):
    """
    Tablero multiempresa de auxiliares.

    MULTIEMPRESA:
    - Lista únicamente auxiliares de la empresa.
    - Solo permite editar auxiliares de la empresa.
    - Los nuevos auxiliares quedan asociados a la empresa.
    - Superuser puede seleccionar empresa desde AssistantForm.
    - Usuarios normales quedan restringidos a su empresa.
    """

    scope = get_user_scope(
        request.user
    )

    company = scope.get(
        "company"
    )

    # ============================================================
    # VALIDAR EMPRESA PARA USUARIO NORMAL
    # ============================================================
    if scope["type"] != "superuser" and not company:
        raise PermissionDenied(
            "Su usuario no tiene una empresa asociada."
        )

    # ============================================================
    # AUXILIAR EN EDICIÓN
    # ============================================================
    assistant_to_edit = None

    edit_id = request.GET.get(
        "edit"
    )

    if edit_id:

        assistant_to_edit = get_object_or_404(
            assistants_for_user(
                request.user,
                Assistant.objects.select_related(
                    "company"
                ),
            ),
            pk=edit_id,
        )

    # ============================================================
    # CONSTRUIR FORMULARIO
    # ============================================================
    if request.method == "POST":

        form = (
            AssistantForm(
                request.POST,
                request.FILES,
                instance=assistant_to_edit,
            )
            if assistant_to_edit
            else AssistantForm(
                request.POST,
                request.FILES,
            )
        )

    else:

        form = (
            AssistantForm(
                instance=assistant_to_edit
            )
            if assistant_to_edit
            else AssistantForm()
        )

    # ============================================================
    # RESTRINGIR EMPRESA ANTES DE form.is_valid()
    # ============================================================
    if scope["type"] != "superuser":

        company_field = form.fields.get(
            "company"
        )

        if company_field:

            company_field.queryset = (
                company_field
                .queryset
                .filter(
                    pk=company.pk
                )
            )

            company_field.initial = company

            # El coordinador normal no envía company desde HTML.
            # La empresa se fuerza posteriormente desde backend.
            company_field.required = False

    # ============================================================
    # PROCESAR POST
    # ============================================================
    if request.method == "POST":

        if form.is_valid():

            try:

                with transaction.atomic():

                    assistant = form.save(
                        commit=False
                    )

                    # ------------------------------------------------
                    # USUARIO NORMAL
                    # ------------------------------------------------
                    if scope["type"] != "superuser":

                        # No confiar en company enviado por POST.
                        assistant.company = company

                    # ------------------------------------------------
                    # SUPERUSER
                    # ------------------------------------------------
                    elif not assistant.company_id:

                        form.add_error(
                            "company",
                            "Debe seleccionar una empresa para el auxiliar.",
                        )

                        raise ValidationError(
                            "Debe seleccionar una empresa para el auxiliar."
                        )

                    # ------------------------------------------------
                    # GUARDAR
                    # ------------------------------------------------
                    assistant.save()

                    form.save_m2m()

                messages.success(
                    request,
                    f"Auxiliar {assistant.full_name} guardado.",
                )

                return redirect(
                    "coordinator:assistants_dashboard"
                )

            except ValidationError as e:

                messages.error(
                    request,
                    str(e),
                )

            except Exception as e:

                messages.error(
                    request,
                    (
                        "No fue posible guardar el auxiliar: "
                        f"{str(e)}"
                    ),
                )

        else:

            messages.error(
                request,
                "Por favor corrige los errores del formulario.",
            )

    # ============================================================
    # LISTADO MULTIEMPRESA
    # ============================================================
    query = request.GET.get(
        "q",
        "",
    ).strip()

    assistants_list = assistants_for_user(
        request.user,
        Assistant.objects.select_related(
            "company"
        ),
    ).order_by(
        "-is_active",
        "full_name",
    )

    if query:

        assistants_list = assistants_list.filter(
            Q(
                full_name__icontains=query
            )
            |
            Q(
                rut__icontains=query
            )
        )

    paginator = Paginator(
        assistants_list,
        10,
    )

    assistants_page = paginator.get_page(
        request.GET.get(
            "page"
        )
    )

    # ============================================================
    # CONTEXTO
    # ============================================================
    context = {
        "form": form,
        "assistants": assistants_page,
        "query": query,
        "edit_mode": bool(
            assistant_to_edit
        ),
        "assistant_edit_id": (
            assistant_to_edit.id
            if assistant_to_edit
            else None
        ),
        "current_company": company,
        "is_superuser": request.user.is_superuser,
    }

    return render(
        request,
        "auxiliares/assistants.html",
        context,
    )


# ============================================================================
# GESTIÓN DE AGENCIAS
# ============================================================================
@login_required
@coordinator_required
def agencies_dashboard(request):
    """
    Gestión de agencias.

    MULTIEMPRESA:
    - Superuser puede ver todas las agencias.
    - Resto solo ve, crea y edita agencias de su empresa.
    - La empresa se fuerza desde backend para usuarios normales.
    - Superuser puede seleccionar empresa desde AgencyForm.
    """

    scope = get_user_scope(
        request.user
    )

    company = scope.get(
        "company"
    )

    # ============================================================
    # AGENCIAS AUTORIZADAS
    # ============================================================

    agencies_allowed = (
        Agency.objects
        .select_related(
            "city",
            "company",
        )
    )

    if not request.user.is_superuser:

        if not company:
            raise PermissionDenied(
                "El usuario no tiene una empresa asignada."
            )

        agencies_allowed = (
            agencies_allowed.filter(
                company=company
            )
        )

    # ============================================================
    # EDICIÓN
    # ============================================================

    agency_to_edit = None

    edit_id = request.GET.get(
        "edit"
    )

    if edit_id:

        agency_to_edit = get_object_or_404(
            agencies_allowed,
            pk=edit_id,
        )

    # ============================================================
    # FORMULARIO
    # ============================================================

    if request.method == "POST":

        form = (
            AgencyForm(
                request.POST,
                instance=agency_to_edit,
            )
            if agency_to_edit
            else AgencyForm(
                request.POST
            )
        )

        # ========================================================
        # RESTRINGIR EMPRESA ANTES DE form.is_valid()
        # ========================================================

        if not request.user.is_superuser:

            if not company:
                raise PermissionDenied(
                    "El usuario no tiene una empresa asignada."
                )

            company_field = form.fields.get(
                "company"
            )

            if company_field:

                company_field.queryset = (
                    company_field
                    .queryset
                    .filter(
                        pk=company.pk
                    )
                )

                company_field.initial = company

        # ========================================================
        # VALIDAR Y GUARDAR
        # ========================================================

        if form.is_valid():

            agency = form.save(
                commit=False
            )

            # ----------------------------------------------------
            # USUARIO NORMAL:
            # ignorar cualquier manipulación del POST y forzar
            # la empresa real de su perfil.
            # ----------------------------------------------------

            if not request.user.is_superuser:

                agency.company = company

            # ----------------------------------------------------
            # SUPERUSER:
            # debe seleccionar empresa en el formulario.
            # ----------------------------------------------------

            elif not agency.company_id:

                form.add_error(
                    "company",
                    "Debe definir la empresa de la agencia."
                )

                messages.error(
                    request,
                    "Debe seleccionar una empresa para la agencia.",
                )

                # No guardar.
                agency = None

            if agency is not None:

                agency.save()

                messages.success(
                    request,
                    (
                        f'Agencia "{agency.name}" '
                        "guardada correctamente."
                    ),
                )

                return redirect(
                    "coordinator:agencies_dashboard"
                )

        else:

            messages.error(
                request,
                "Por favor corrige los errores del formulario.",
            )

    else:

        form = (
            AgencyForm(
                instance=agency_to_edit
            )
            if agency_to_edit
            else AgencyForm()
        )

        # ========================================================
        # RESTRINGIR EMPRESA EN GET
        # ========================================================

        if not request.user.is_superuser:

            if not company:
                raise PermissionDenied(
                    "El usuario no tiene una empresa asignada."
                )

            company_field = form.fields.get(
                "company"
            )

            if company_field:

                company_field.queryset = (
                    company_field
                    .queryset
                    .filter(
                        pk=company.pk
                    )
                )

                company_field.initial = company

    # ============================================================
    # LISTADO
    # ============================================================

    query = request.GET.get(
        "q",
        "",
    ).strip()

    agencies_list = (
        agencies_allowed
        .order_by(
            "name"
        )
    )

    if query:

        agencies_list = (
            agencies_list.filter(
                Q(
                    name__icontains=query
                )
                |
                Q(
                    city__name__icontains=query
                )
                |
                Q(
                    address__icontains=query
                )
                |
                Q(
                    company__name__icontains=query
                )
            )
        )

    paginator = Paginator(
        agencies_list,
        10,
    )

    agencies_page = paginator.get_page(
        request.GET.get(
            "page"
        )
    )

    # ============================================================
    # CONTEXTO
    # ============================================================

    context = {
        "form": form,
        "agencies": agencies_page,
        "query": query,
        "edit_mode": bool(
            agency_to_edit
        ),
        "agency_edit_id": (
            agency_to_edit.id
            if agency_to_edit
            else None
        ),
        "current_company": company,
        "is_superuser": request.user.is_superuser,
    }

    return render(
        request,
        "agencias/agencias.html",
        context,
    )


@login_required
@coordinator_required
@require_POST
def agency_delete(request, agency_id):
    """
    Elimina una agencia únicamente dentro
    del alcance empresarial del usuario.
    """

    scope = get_user_scope(
        request.user
    )

    company = scope.get(
        "company"
    )

    agencies_allowed = (
        Agency.objects
        .select_related(
            "company"
        )
    )

    if not request.user.is_superuser:

        if not company:
            raise PermissionDenied(
                "El usuario no tiene una empresa asignada."
            )

        agencies_allowed = (
            agencies_allowed.filter(
                company=company
            )
        )

    agency = get_object_or_404(
        agencies_allowed,
        pk=agency_id,
    )

    agency.delete()

    messages.success(
        request,
        "Agencia eliminada correctamente.",
    )

    return redirect(
        "coordinator:agencies_dashboard"
    )


# ============================================================================
# DOCUMENTOS DE PERSONAL Y FLOTA
# ============================================================================

@login_required
@coordinator_required
def driver_documents(request, driver_id):
    """
    Documentos de un chofer accesible para la empresa.
    """

    driver = get_object_or_404(
        drivers_for_user(
            request.user,
            Driver.objects.all(),
        ),
        pk=driver_id,
    )

    documents = DriverDocument.objects.filter(
        driver=driver
    ).order_by(
        "expiry_date"
    )

    return render(
        request,
        "coordinator/driver_documents.html",
        {
            "driver": driver,
            "documents": documents,
        },
    )


@login_required
@coordinator_required
def driver_document_create(request, driver_id):
    """
    Agregar documento únicamente a un chofer accesible.
    """

    driver = get_object_or_404(
        drivers_for_user(
            request.user,
            Driver.objects.all(),
        ),
        pk=driver_id,
    )

    if request.method == "POST":

        DriverDocument.objects.create(
            driver=driver,
            doc_type=request.POST.get(
                "doc_type"
            ),
            document_number=request.POST.get(
                "doc_number",
                "",
            ),
            issue_date=(
                request.POST.get(
                    "issue_date"
                )
                or None
            ),
            expiry_date=request.POST.get(
                "expiry_date"
            ),
            notes=request.POST.get(
                "notes",
                "",
            ),
        )

        messages.success(
            request,
            f"Documento agregado a {driver.full_name}",
        )

        return redirect(
            "coordinator:driver_documents",
            driver_id=driver.id,
        )

    return render(
        request,
        "coordinator/driver_document_form.html",
        {
            "driver": driver,
        },
    )


@login_required
@coordinator_required
def driver_document_edit(request, doc_id):
    """
    Edita únicamente documentos pertenecientes
    a choferes accesibles para el usuario.
    """

    allowed_drivers = drivers_for_user(
        request.user,
        Driver.objects.all(),
    )

    doc = get_object_or_404(
        DriverDocument.objects.select_related(
            "driver"
        ).filter(
            driver__in=allowed_drivers
        ),
        pk=doc_id,
    )

    if request.method == "POST":

        doc.doc_type = request.POST.get(
            "doc_type"
        )

        doc.document_number = request.POST.get(
            "doc_number",
            "",
        )

        doc.issue_date = (
            request.POST.get(
                "issue_date"
            )
            or None
        )

        doc.expiry_date = request.POST.get(
            "expiry_date"
        )

        doc.notes = request.POST.get(
            "notes",
            "",
        )

        doc.save()

        messages.success(
            request,
            "Documento actualizado",
        )

        return redirect(
            "coordinator:driver_documents",
            driver_id=doc.driver.id,
        )

    return render(
        request,
        "coordinator/driver_document_form.html",
        {
            "doc": doc,
            "driver": doc.driver,
        },
    )


@login_required
@coordinator_required
def driver_document_delete(request, doc_id):
    """
    Elimina únicamente documentos de choferes
    accesibles para la empresa.
    """

    allowed_drivers = drivers_for_user(
        request.user,
        Driver.objects.all(),
    )

    doc = get_object_or_404(
        DriverDocument.objects.select_related(
            "driver"
        ).filter(
            driver__in=allowed_drivers
        ),
        pk=doc_id,
    )

    driver_id = doc.driver.id

    doc.delete()

    messages.success(
        request,
        "Documento eliminado",
    )

    return redirect(
        "coordinator:driver_documents",
        driver_id=driver_id,
    )


@login_required
@coordinator_required
def bus_documents(request, bus_id):
    """
    Documentos de un bus accesible para la empresa del usuario.
    """

    bus = get_object_or_404(
        buses_for_user(
            request.user,
            Bus.objects.all(),
        ),
        pk=bus_id,
    )

    documents = BusDocument.objects.filter(
        bus=bus
    ).order_by("expiry_date")

    return render(
        request,
        "coordinator/bus_documents.html",
        {
            "bus": bus,
            "documents": documents,
        },
    )



@login_required
@coordinator_required
def bus_document_create(request, bus_id):
    """
    Agrega un documento únicamente a un bus accesible.
    """

    bus = get_object_or_404(
        buses_for_user(
            request.user,
            Bus.objects.all(),
        ),
        pk=bus_id,
    )

    if request.method == "POST":
        BusDocument.objects.create(
            bus=bus,
            doc_type=request.POST.get("doc_type"),
            document_number=request.POST.get(
                "doc_number",
                "",
            ),
            issue_date=(
                request.POST.get("issue_date")
                or None
            ),
            expiry_date=request.POST.get(
                "expiry_date"
            ),
            notes=request.POST.get(
                "notes",
                "",
            ),
        )

        messages.success(
            request,
            f"Documento agregado a bus {bus.plate}",
        )

        return redirect(
            "coordinator:bus_documents",
            bus_id=bus.id,
        )

    return render(
        request,
        "coordinator/bus_document_form.html",
        {
            "bus": bus,
        },
    )


@login_required
@coordinator_required
def bus_document_edit(request, doc_id):
    """
    Edita únicamente documentos pertenecientes
    a buses accesibles para el usuario.
    """

    allowed_buses = buses_for_user(
        request.user,
        Bus.objects.all(),
    )

    doc = get_object_or_404(
        BusDocument.objects.select_related(
            "bus"
        ).filter(
            bus__in=allowed_buses
        ),
        pk=doc_id,
    )

    if request.method == "POST":
        doc.doc_type = request.POST.get(
            "doc_type"
        )

        doc.document_number = request.POST.get(
            "doc_number",
            "",
        )

        doc.issue_date = (
            request.POST.get(
                "issue_date"
            )
            or None
        )

        doc.expiry_date = request.POST.get(
            "expiry_date"
        )

        doc.notes = request.POST.get(
            "notes",
            "",
        )

        doc.save()

        messages.success(
            request,
            "Documento actualizado",
        )

        return redirect(
            "coordinator:bus_documents",
            bus_id=doc.bus.id,
        )

    return render(
        request,
        "coordinator/bus_document_form.html",
        {
            "doc": doc,
            "bus": doc.bus,
        },
    )


@login_required
@coordinator_required
def bus_document_delete(request, doc_id):
    """
    Elimina únicamente documentos pertenecientes
    a buses accesibles para el usuario.
    """

    allowed_buses = buses_for_user(
        request.user,
        Bus.objects.all(),
    )

    doc = get_object_or_404(
        BusDocument.objects.select_related(
            "bus"
        ).filter(
            bus__in=allowed_buses
        ),
        pk=doc_id,
    )

    bus_id = doc.bus.id

    doc.delete()

    messages.success(
        request,
        "Documento eliminado",
    )

    return redirect(
        "coordinator:bus_documents",
        bus_id=bus_id,
    )


@login_required
@coordinator_required
def expiring_documents(request):
    """
    Centro de alertas documentales.

    MULTIEMPRESA:
    - Solo muestra documentos de choferes accesibles para el usuario.
    - Solo muestra documentos de buses accesibles para el usuario.
    - Mantiene la clasificación:
      vencidos, urgentes y próximos.
    """

    today = timezone.localdate()
    warning_days = 30
    urgent_days = 7
    expiry_limit = today + timedelta(days=warning_days)

    # ============================================================
    # CHOFERES PERMITIDOS
    # ============================================================
    allowed_drivers = drivers_for_user(
        request.user,
        Driver.objects.all(),
    )

    # ============================================================
    # BUSES PERMITIDOS
    # ============================================================
    allowed_buses = buses_for_user(
        request.user,
        Bus.objects.all(),
    )

    # ============================================================
    # DOCUMENTOS DE CHOFERES
    # ============================================================
    driver_docs = list(
        DriverDocument.objects.filter(
            driver__in=allowed_drivers,
            expiry_date__isnull=False,
            expiry_date__lte=expiry_limit,
        )
        .select_related(
            "driver",
            "driver__company",
        )
        .order_by(
            "expiry_date",
            "driver__full_name",
        )
    )

    # ============================================================
    # DOCUMENTOS DE BUSES
    # ============================================================
    bus_docs = list(
        BusDocument.objects.filter(
            bus__in=allowed_buses,
            expiry_date__isnull=False,
            expiry_date__lte=expiry_limit,
        )
        .select_related(
            "bus",
            "bus__company",
        )
        .order_by(
            "expiry_date",
            "bus__plate",
        )
    )

    # ============================================================
    # DECORAR DOCUMENTOS PARA EL TEMPLATE
    # ============================================================
    def decorate_document(doc, owner_type):
        days = (
            doc.expiry_date - today
        ).days

        doc.days_remaining = days
        doc.owner_type = owner_type

        if owner_type == "driver":
            doc.owner_name = (
                doc.driver.full_name
            )

            doc.owner_detail = (
                doc.driver.rut
            )

        else:
            doc.owner_name = (
                doc.bus.plate
            )

            doc.owner_detail = (
                doc.bus.model or ""
            )

        if days < 0:
            doc.alert_status = "expired"
            doc.alert_label = "Vencido"

            doc.remaining_label = (
                f'Venció hace {abs(days)} día'
                f'{"s" if abs(days) != 1 else ""}'
            )

            doc.alert_order = 0

        elif days <= urgent_days:
            doc.alert_status = "urgent"
            doc.alert_label = "Urgente"

            if days == 0:
                doc.remaining_label = (
                    "Vence hoy"
                )
            else:
                doc.remaining_label = (
                    f'{days} día'
                    f'{"s" if days != 1 else ""}'
                )

            doc.alert_order = 1

        else:
            doc.alert_status = "warning"
            doc.alert_label = "Por vencer"

            doc.remaining_label = (
                f"{days} días"
            )

            doc.alert_order = 2

        return doc

    driver_docs = [
        decorate_document(
            doc,
            "driver",
        )
        for doc in driver_docs
    ]

    bus_docs = [
        decorate_document(
            doc,
            "bus",
        )
        for doc in bus_docs
    ]

    # ============================================================
    # UNIR Y ORDENAR
    # ============================================================
    all_docs = sorted(
        driver_docs + bus_docs,
        key=lambda doc: (
            doc.alert_order,
            doc.expiry_date,
            doc.owner_name.lower(),
        ),
    )

    expired_count = sum(
        1
        for doc in all_docs
        if doc.alert_status == "expired"
    )

    urgent_count = sum(
        1
        for doc in all_docs
        if doc.alert_status == "urgent"
    )

    warning_count = sum(
        1
        for doc in all_docs
        if doc.alert_status == "warning"
    )

    context = {
        "all_docs": all_docs,
        "driver_docs": driver_docs,
        "bus_docs": bus_docs,
        "expired_count": expired_count,
        "urgent_count": urgent_count,
        "warning_count": warning_count,
        "attention_count": len(
            all_docs
        ),
        "warning_days": warning_days,
        "urgent_days": urgent_days,
        "today": today,
    }

    return render(
        request,
        "coordinator/expiring_documents.html",
        context,
    )


# ============================================================================
# DETALLE DE VIAJE Y BUS
# ============================================================================

@login_required
@coordinator_required
def trip_detail(request, trip_id):
    """
    FASE 2.18 — Ficha operacional del viaje.

    Además de ocupación/pasajeros, muestra:
    - estado real del viaje,
    - salida/llegada real,
    - estado operacional integral,
    - disponibilidad del despacho.
    """

    trip = get_object_or_404(
        trips_for_user(
            request.user,
            Trip.objects.select_related(
                "route__origin",
                "route__destination",
                "bus",
                "driver1",
                "driver2",
                "assistant",
                "dispatched_by",
                "completed_by",
            ).annotate(
                total_tickets=Count("tickets"),
                lower_tickets=Count(
                    "tickets",
                    filter=Q(
                        tickets__seat__deck=1
                    ),
                ),
                upper_tickets=Count(
                    "tickets",
                    filter=Q(
                        tickets__seat__deck=2
                    ),
                ),
            ),
        ),
        pk=trip_id,
    )

    tickets = (
        Ticket.objects
        .filter(trip=trip)
        .select_related(
            "seat",
            "customer",
        )
        .order_by(
            "seat__deck",
            "seat__row",
            "seat__number",
        )
    )

    total_tickets = trip.total_tickets

    lower_occupied = trip.lower_tickets

    upper_occupied = (
        trip.upper_tickets
        if trip.bus.floors == 2
        else 0
    )

    lower_seats = Seat.objects.filter(
        bus=trip.bus,
        deck=1,
    ).count()

    upper_seats = (
        Seat.objects.filter(
            bus=trip.bus,
            deck=2,
        ).count()
        if trip.bus.floors == 2
        else 0
    )

    occupancy_rate = (
        total_tickets / trip.seats_total * 100
        if trip.seats_total
        else 0
    )

    lower_occupancy = (
        lower_occupied / lower_seats * 100
        if lower_seats
        else 0
    )

    upper_occupancy = (
        upper_occupied / upper_seats * 100
        if upper_seats
        else 0
    )

    no_show_count = tickets.filter(
        checked_in=False
    ).count()

    operational = _trip_operational_status(
        trip
    )

    pending_close = bool(
        trip.status == Trip.STATUS_IN_PROGRESS
        and trip.arrival
        and trip.arrival < timezone.now()
    )

    overdue_close_minutes = 0

    if pending_close:
        overdue_close_minutes = max(
            int(
                (
                    timezone.now()
                    - trip.arrival
                ).total_seconds()
                // 60
            ),
            0,
        )

    context = {
        "trip": trip,
        "tickets": tickets,
        "total_tickets": total_tickets,
        "lower_occupied": lower_occupied,
        "upper_occupied": upper_occupied,
        "lower_seats": lower_seats,
        "upper_seats": upper_seats,
        "occupancy_rate": occupancy_rate,
        "lower_occupancy": lower_occupancy,
        "upper_occupancy": upper_occupancy,
        "no_show_count": no_show_count,
        "operational": operational,
        "pending_close": pending_close,
        "overdue_close_minutes": overdue_close_minutes,
    }

    return render(
        request,
        "coordinator/trip_detail.html",
        context,
    )


@login_required
@coordinator_required
@require_POST
def trip_dispatch_start(request, trip_id):
    """
    Inicia/despacha un viaje autorizado.

    Seguridad:
    - Solo POST.
    - Solo viajes accesibles mediante trips_for_user().
    - SELECT FOR UPDATE evita despachos simultáneos.
    - Revalida el alcance dentro de transaction.atomic().
    - Recalcula estado operacional en backend.
    """

    with transaction.atomic():

        trip = get_object_or_404(
            trips_for_user(
                request.user,
                Trip.objects
                .select_for_update()
                .select_related(
                    "route",
                    "route__company",
                    "bus",
                    "bus__company",
                    "driver1",
                    "driver2",
                    "assistant",
                ),
            ),
            pk=trip_id,
        )

        if trip.status == Trip.STATUS_COMPLETED:

            messages.error(
                request,
                (
                    "Este viaje ya está finalizado "
                    "y no puede volver a despacharse."
                ),
            )

            return redirect(
                "coordinator:trip_detail",
                trip_id=trip.id,
            )

        if trip.status == Trip.STATUS_IN_PROGRESS:

            messages.warning(
                request,
                (
                    "Este viaje ya fue despachado "
                    "y se encuentra en curso."
                ),
            )

            return redirect(
                "coordinator:trip_detail",
                trip_id=trip.id,
            )

        operational = _trip_operational_status(
            trip
        )

        if not operational["can_dispatch"]:

            reasons = "; ".join(
                operational["blockers"]
            ) or operational["primary_reason"]

            messages.error(
                request,
                (
                    "DESPACHO BLOQUEADO. "
                    "El viaje está NO OPERATIVO. "
                    f"Motivo(s): {reasons}"
                ),
            )

            return redirect(
                "coordinator:trip_detail",
                trip_id=trip.id,
            )

        now = timezone.now()

        trip.status = (
            Trip.STATUS_IN_PROGRESS
        )

        trip.actual_departure = now

        trip.dispatched_by = request.user

        trip.save(
            update_fields=[
                "status",
                "actual_departure",
                "dispatched_by",
            ]
        )

        if operational["key"] == "attention":

            messages.warning(
                request,
                (
                    "Viaje despachado con "
                    "ADVERTENCIA operacional. "
                    f'{operational["primary_reason"]}'
                ),
            )

        else:

            messages.success(
                request,
                "Viaje despachado correctamente.",
            )

    return redirect(
        "coordinator:trip_detail",
        trip_id=trip_id,
    )


@login_required
@coordinator_required
@require_POST
def trip_dispatch_finish(request, trip_id):
    """
    Finaliza un viaje autorizado.

    Seguridad:
    - Solo POST.
    - Solo viajes accesibles mediante trips_for_user().
    - SELECT FOR UPDATE evita cierres simultáneos.
    - No permite finalizar un viaje ajeno manipulando trip_id.
    """

    with transaction.atomic():

        trip = get_object_or_404(
            trips_for_user(
                request.user,
                Trip.objects
                .select_for_update()
                .select_related(
                    "route",
                    "route__company",
                    "bus",
                    "bus__company",
                ),
            ),
            pk=trip_id,
        )

        if trip.status == Trip.STATUS_SCHEDULED:

            messages.error(
                request,
                (
                    "No se puede finalizar este viaje "
                    "porque aún no ha sido despachado."
                ),
            )

            return redirect(
                "coordinator:trip_detail",
                trip_id=trip.id,
            )

        if trip.status == Trip.STATUS_COMPLETED:

            messages.warning(
                request,
                "Este viaje ya se encuentra finalizado.",
            )

            return redirect(
                "coordinator:trip_detail",
                trip_id=trip.id,
            )

        now = timezone.now()

        trip.status = (
            Trip.STATUS_COMPLETED
        )

        trip.actual_arrival = now

        trip.completed_by = request.user

        trip.save(
            update_fields=[
                "status",
                "actual_arrival",
                "completed_by",
            ]
        )

        messages.success(
            request,
            "Viaje finalizado correctamente.",
        )

    return redirect(
        "coordinator:trip_detail",
        trip_id=trip_id,
    )


@login_required
@coordinator_required
def bus_detail(request, bus_id):
    """
    Centro operacional / ficha técnica de un bus.

    FASE 2.18.3-A2.3.2
    - Carga explícitamente la relación Bus.owner -> FleetOwner.
    - Expone el propietario tanto con la relación nueva como mediante
      alias temporales compatibles con templates históricos.
    - No modifica datos históricos del Bus en la base de datos.
    - Mantiene todos los KPIs, documentos, mantenimiento, combustible,
      viajes y línea de tiempo que ya utilizaba esta vista.
    """
    bus = get_object_or_404(
    buses_for_user(
        request.user,
        Bus.objects.select_related(
            'company',
            'owner',
            'owner__company',
        ),
    ),
    pk=bus_id,
    )

    now = timezone.now()
    today = timezone.localdate()

    # ============================================================
    # PROPIETARIO / SOCIO
    # ============================================================
    # Fuente oficial:
    #
    #     Bus.owner -> FleetOwner
    #
    # Los campos owner_first_name / owner_last_name del Bus son históricos.
    # No deben utilizarse como fuente principal, pero algunos templates
    # antiguos todavía pueden leerlos. Por compatibilidad, se reemplazan
    # SOLO EN MEMORIA durante esta respuesta.
    owner = getattr(bus, 'owner', None)

    owner_display_name = ''
    owner_first_name = ''
    owner_last_name = ''
    owner_rut = ''
    owner_company_name = ''

    if owner is not None:
        owner_first_name = (getattr(owner, 'first_name', '') or '').strip()
        owner_last_name = (getattr(owner, 'last_name', '') or '').strip()
        owner_rut = (getattr(owner, 'rut', '') or '').strip()

        owner_company = getattr(owner, 'company', None)
        if owner_company is not None:
            owner_company_name = (
                getattr(owner_company, 'name', '') or ''
            ).strip()

        # FleetOwner.display_name puede ser property o atributo según
        # la versión del modelo; si está vacío construimos un fallback.
        try:
            owner_display_name = (owner.display_name or '').strip()
        except Exception:
            owner_display_name = ''

        if not owner_display_name:
            owner_display_name = (
                f"{owner_first_name} {owner_last_name}".strip()
                or str(owner)
            )

    # ------------------------------------------------------------
    # Compatibilidad visual con templates antiguos.
    # ------------------------------------------------------------
    # IMPORTANTE:
    # Esto NO hace bus.save() y NO altera la BD.
    # Son solamente atributos temporales del objeto Python enviado
    # al template.
    bus.owner_display_name = owner_display_name
    bus.owner_name = owner_display_name
    bus.owner_full_name = owner_display_name
    bus.display_owner = owner_display_name
    bus.owner_label = owner_display_name
    bus.owner_first_name = owner_first_name
    bus.owner_last_name = owner_last_name
    bus.owner_rut = owner_rut
    bus.owner_company_name = owner_company_name

    # ============================================================
    # CAPACIDAD / VIAJES / TICKETS / ENCOMIENDAS
    # ============================================================
    real_seat_count = Seat.objects.filter(bus=bus).count()

    trips_qs = (
        Trip.objects
        .filter(bus=bus)
        .select_related(
            'route__origin',
            'route__destination',
        )
        .annotate(
            ticket_count=Count('tickets'),
        )
        .order_by('-departure')
    )

    total_trips = trips_qs.count()
    future_trips_count = trips_qs.filter(
        departure__gt=now,
    ).count()
    past_trips_count = trips_qs.filter(
        departure__lte=now,
    ).count()

    total_tickets = Ticket.objects.filter(
        trip__bus=bus,
    ).count()

    total_parcels = Parcel.objects.filter(
        trip__bus=bus,
    ).count()

    upcoming_trips = (
        trips_qs
        .filter(departure__gte=now)
        .order_by('departure')[:5]
    )

    recent_trips = trips_qs[:8]

    # ============================================================
    # DOCUMENTOS
    # ============================================================
    documents_qs = (
        BusDocument.objects
        .filter(bus=bus)
        .order_by('expiry_date', 'doc_type')
    )
    documents = list(documents_qs)

    documents_count = len(documents)
    expired_documents_count = 0
    urgent_documents_count = 0
    upcoming_documents_count = 0
    manual_documents_count = 0
    synced_documents_count = 0

    for doc in documents:
        doc.is_synced = (
            (doc.notes or '').strip() == _BUS_DOC_SYNC_NOTE
        )

        if doc.expiry_date:
            doc.days_remaining = (
                doc.expiry_date - today
            ).days
        else:
            doc.days_remaining = None

        if doc.is_synced:
            synced_documents_count += 1
            doc.origin_label = 'Ficha del bus'
            doc.origin_icon = 'fa-rotate'
        else:
            manual_documents_count += 1
            doc.origin_label = 'Manual'
            doc.origin_icon = 'fa-file-arrow-up'

        if doc.days_remaining is None:
            doc.status_key = 'unknown'
            doc.status_label = 'Sin fecha'
            doc.status_color = 'secondary'

        elif doc.days_remaining < 0:
            expired_documents_count += 1
            doc.status_key = 'expired'
            doc.status_label = 'Vencido'
            doc.status_color = 'danger'

        elif doc.days_remaining <= 7:
            urgent_documents_count += 1
            doc.status_key = 'urgent'
            doc.status_label = 'Urgente'
            doc.status_color = 'warning'

        elif doc.days_remaining <= 30:
            upcoming_documents_count += 1
            doc.status_key = 'upcoming'
            doc.status_label = 'Próximo'
            doc.status_color = 'primary'

        else:
            doc.status_key = 'valid'
            doc.status_label = 'Vigente'
            doc.status_color = 'success'

    expiring_documents_count = (
        urgent_documents_count
        + upcoming_documents_count
    )

    if expired_documents_count:
        documents_status = 'danger'
        documents_label = (
            f'{expired_documents_count} vencido(s)'
        )
    elif urgent_documents_count:
        documents_status = 'warning'
        documents_label = (
            f'{urgent_documents_count} urgente(s)'
        )
    elif upcoming_documents_count:
        documents_status = 'primary'
        documents_label = (
            f'{upcoming_documents_count} próximo(s)'
        )
    else:
        documents_status = 'success'
        documents_label = 'Al día'

    # ============================================================
    # MANTENIMIENTO
    # ============================================================
    maintenances = (
        Maintenance.objects
        .filter(bus=bus)
        .order_by('-date', '-created_at')
    )

    latest_maintenance = maintenances.first()
    recent_maintenances = maintenances[:5]

    current_km = bus.current_mileage or 0
    next_maintenance_km = (
        bus.next_maintenance_mileage or 0
    )

    maintenance_remaining_km = None
    maintenance_status = 'secondary'
    maintenance_label = 'Sin programación'

    if next_maintenance_km:
        maintenance_remaining_km = (
            next_maintenance_km - current_km
        )

        if maintenance_remaining_km <= 0:
            maintenance_status = 'danger'
            maintenance_label = 'Vencida'

        elif maintenance_remaining_km <= 1000:
            maintenance_status = 'warning'
            maintenance_label = (
                f'{maintenance_remaining_km:,} km restantes'
            )

        else:
            maintenance_status = 'success'
            maintenance_label = (
                f'{maintenance_remaining_km:,} km restantes'
            )

    # ============================================================
    # COMBUSTIBLE
    # ============================================================
    fuel_records_asc = list(
        FuelRecord.objects
        .filter(bus=bus)
        .order_by(
            'date',
            'created_at',
            'id',
        )
    )

    previous_fuel = None
    fuel_efficiencies = []
    fuel_cost_per_km_values = []

    total_liters = Decimal('0')
    total_fuel_cost = Decimal('0')
    fuel_total_distance = 0

    for fuel_item in fuel_records_asc:
        liters = (
            fuel_item.liters
            or Decimal('0')
        )
        cost = (
            fuel_item.cost
            or Decimal('0')
        )

        total_liters += liters
        total_fuel_cost += cost

        fuel_item.distance_since_previous = None
        fuel_item.efficiency_kml = None
        fuel_item.cost_per_km = None

        fuel_item.cost_per_liter = (
            round(cost / liters, 2)
            if liters > 0
            else None
        )

        if previous_fuel:
            distance = (
                fuel_item.mileage
                - previous_fuel.mileage
            )

            if distance > 0 and liters > 0:
                fuel_item.distance_since_previous = distance

                fuel_item.efficiency_kml = round(
                    Decimal(distance) / liters,
                    2,
                )

                fuel_item.cost_per_km = round(
                    cost / Decimal(distance),
                    2,
                )

                fuel_efficiencies.append(
                    fuel_item.efficiency_kml
                )

                fuel_cost_per_km_values.append(
                    fuel_item.cost_per_km
                )

                fuel_total_distance += distance

        previous_fuel = fuel_item

    recent_fuel = list(
        reversed(
            fuel_records_asc[-5:]
        )
    )

    latest_fuel = (
        fuel_records_asc[-1]
        if fuel_records_asc
        else None
    )

    avg_fuel_efficiency = (
        round(
            sum(
                fuel_efficiencies,
                Decimal('0'),
            )
            / Decimal(
                len(fuel_efficiencies)
            ),
            2,
        )
        if fuel_efficiencies
        else None
    )

    avg_fuel_cost_per_km = (
        round(
            sum(
                fuel_cost_per_km_values,
                Decimal('0'),
            )
            / Decimal(
                len(fuel_cost_per_km_values)
            ),
            2,
        )
        if fuel_cost_per_km_values
        else None
    )

    avg_fuel_cost_per_liter = (
        round(
            total_fuel_cost / total_liters,
            2,
        )
        if total_liters > 0
        else None
    )

    # Compatibilidad con el KPI actual del template.
    consumption_per_100km = (
        round(
            Decimal('100')
            / avg_fuel_efficiency,
            2,
        )
        if (
            avg_fuel_efficiency
            and avg_fuel_efficiency > 0
        )
        else 0
    )

    # ============================================================
    # INTEGRIDAD DEL PLANO
    # ============================================================
    booking_seat_count = (
        Seat.objects
        .filter(
            bus=bus,
            booking_order_items__isnull=False,
        )
        .distinct()
        .count()
    )

    seatmap_locked = bool(
        future_trips_count
        or total_tickets
        or booking_seat_count
    )

    # ============================================================
    # ESTADO OPERACIONAL
    # ============================================================
    operational_status = _bus_operational_status(
        bus,
        today=today,
        seat_count=real_seat_count,
    )

    # ============================================================
    # LÍNEA DE TIEMPO
    # ============================================================
    timeline = []

    for item in recent_maintenances:
        timeline.append({
            'date': item.date,
            'icon': 'fa-screwdriver-wrench',
            'color': 'warning',
            'title': 'Mantención',
            'text': (
                item.description
                or item.get_maintenance_type_display()
            ),
            'meta': f'{item.mileage:,} km',
        })

    for item in recent_fuel:
        timeline.append({
            'date': item.date,
            'icon': 'fa-gas-pump',
            'color': 'success',
            'title': 'Carga de combustible',
            'text': f'{item.liters} L',
            'meta': f'${item.cost:,.0f}',
        })

    for item in documents[:5]:
        if not item.expiry_date:
            continue

        timeline.append({
            'date': item.expiry_date,
            'icon': 'fa-file-lines',
            'color': (
                'danger'
                if item.expiry_date < today
                else 'warning'
                if item.expiry_date
                <= today + timedelta(days=30)
                else 'primary'
            ),
            'title': item.get_doc_type_display(),
            'text': 'Vencimiento documental',
            'meta': item.expiry_date.strftime(
                '%d/%m/%Y'
            ),
        })

    for trip in recent_trips[:5]:
        local_departure = (
            timezone.localtime(
                trip.departure
            )
            if timezone.is_aware(
                trip.departure
            )
            else trip.departure
        )

        timeline.append({
            'date': local_departure.date(),
            'icon': 'fa-route',
            'color': 'primary',
            'title': 'Viaje',
            'text': (
                f'{trip.route.origin.name} '
                f'→ {trip.route.destination.name}'
            ),
            'meta': local_departure.strftime(
                '%d/%m/%Y %H:%M'
            ),
        })

    timeline.sort(
        key=lambda x: x['date'] or today,
        reverse=True,
    )
    timeline = timeline[:12]

    # ============================================================
    # CONTEXTO
    # ============================================================
    context = {
        'bus': bus,

        # Nueva relación oficial.
        'owner': owner,
        'fleet_owner': owner,

        # Variables explícitas para el template.
        'owner_display_name': owner_display_name,
        'owner_name': owner_display_name,
        'owner_full_name': owner_display_name,
        'owner_first_name': owner_first_name,
        'owner_last_name': owner_last_name,
        'owner_rut': owner_rut,
        'owner_company_name': owner_company_name,

        'real_seat_count': real_seat_count,
        'total_trips': total_trips,
        'future_trips_count': future_trips_count,
        'past_trips_count': past_trips_count,
        'total_tickets': total_tickets,
        'total_parcels': total_parcels,
        'upcoming_trips': upcoming_trips,
        'recent_trips': recent_trips,

        'documents': documents,
        'documents_count': documents_count,
        'documents_status': documents_status,
        'documents_label': documents_label,
        'expired_documents_count': expired_documents_count,
        'urgent_documents_count': urgent_documents_count,
        'upcoming_documents_count': upcoming_documents_count,
        'expiring_documents_count': expiring_documents_count,
        'manual_documents_count': manual_documents_count,
        'synced_documents_count': synced_documents_count,

        'latest_maintenance': latest_maintenance,
        'recent_maintenances': recent_maintenances,
        'maintenance_remaining_km': maintenance_remaining_km,
        'maintenance_status': maintenance_status,
        'maintenance_label': maintenance_label,

        'recent_fuel': recent_fuel,
        'latest_fuel': latest_fuel,
        'total_liters': total_liters,
        'total_fuel_cost': total_fuel_cost,
        'avg_fuel_efficiency': avg_fuel_efficiency,
        'avg_fuel_cost_per_km': avg_fuel_cost_per_km,
        'avg_fuel_cost_per_liter': avg_fuel_cost_per_liter,
        'fuel_total_distance': fuel_total_distance,
        'consumption_per_100km': consumption_per_100km,

        'booking_seat_count': booking_seat_count,
        'seatmap_locked': seatmap_locked,
        'operational_status': operational_status,

        'today': today,
        'limit_30_days': (
            today + timedelta(days=30)
        ),
        'timeline': timeline,
    }


    return render(
        request,
        'coordinator/bus_detail.html',
        context,
    )
# ============================================================================
# REPORTE DE OCUPACIÓN
# ============================================================================

@login_required
@coordinator_required
def occupancy_report(request):
    import calendar
    from datetime import datetime

    month = request.GET.get('month', datetime.now().strftime('%Y-%m'))
    year, month = map(int, month.split('-'))
    start_date = datetime(year, month, 1).date()
    end_date = (datetime(year, month + 1, 1) - timedelta(days=1)).date() if month < 12 else datetime(year, 12, 31).date()

    trips = Trip.objects.filter(
        departure__date__range=(start_date, end_date)
    ).select_related('route', 'bus').annotate(
        sold_count=Count('tickets')
    )

    data = []
    for trip in trips:
        sold = trip.sold_count
        data.append({
            'date': trip.departure.strftime('%Y-%m-%d'),
            'route': str(trip.route),
            'bus': trip.bus.plate,
            'total': trip.seats_total,
            'sold': sold,
            'occupancy': round(sold / trip.seats_total * 100, 1) if trip.seats_total else 0,
        })

    context = {'data': data, 'month': f"{year}-{month:02d}"}
    return render(request, 'coordinator/occupancy_report.html', context)


# ============================================================================
# CHECK‑IN DE PASAJEROS
# ============================================================================

@login_required
@coordinator_required
@require_http_methods(["GET", "POST"])
def checkin_ticket(request, ticket_number):
    """
    Check-in seguro y multiempresa.

    GET:
    - Solo permite consultar tickets accesibles para el usuario.

    POST:
    - Revalida y bloquea el ticket autorizado.
    - Evita doble check-in concurrente.
    - No permite operar tickets de otra empresa.
    """

    # ============================================================
    # TICKET AUTORIZADO
    # ============================================================

    ticket = get_object_or_404(
        tickets_for_user(
            request.user,
            Ticket.objects.select_related(
                "trip",
                "trip__bus",
                "trip__bus__company",
                "trip__route",
                "trip__route__origin",
                "trip__route__destination",
                "seat",
                "customer",
            ),
        ),
        number=ticket_number,
    )

    now = timezone.now()

    trip_departure = ticket.trip.departure

    trip_departed = (
        trip_departure < now
    )

    too_early = (
        trip_departure
        > now + timedelta(minutes=30)
    )

    # ============================================================
    # GET
    # ============================================================

    if request.method == "GET":

        context = {
            "ticket": ticket,
            "trip": ticket.trip,
            "trip_departed": trip_departed,
            "too_early": too_early,
            "already_checked_in": ticket.checked_in,
        }

        return render(
            request,
            "coordinator/checkin_result.html",
            context,
        )

    # ============================================================
    # POST
    # ============================================================

    try:

        with transaction.atomic():

            locked_ticket = get_object_or_404(
                tickets_for_user(
                    request.user,
                    Ticket.objects
                    .select_for_update()
                    .select_related(
                        "trip",
                        "trip__bus",
                        "trip__bus__company",
                        "trip__route",
                        "trip__route__origin",
                        "trip__route__destination",
                        "seat",
                        "customer",
                    ),
                ),
                pk=ticket.pk,
            )

            now = timezone.now()

            # ====================================================
            # VIAJE YA PARTIÓ
            # ====================================================

            if locked_ticket.trip.departure < now:

                messages.error(
                    request,
                    (
                        f"El viaje del ticket "
                        f"{locked_ticket.number} ya partió."
                    ),
                )

                return redirect(
                    "coordinator:checkin",
                    ticket_number=locked_ticket.number,
                )

            # ====================================================
            # YA EMBARCADO
            # ====================================================

            if locked_ticket.checked_in:

                messages.warning(
                    request,
                    (
                        f"El pasajero "
                        f"{locked_ticket.buyer_name} "
                        "ya había sido embarcado."
                    ),
                )

                return redirect(
                    "coordinator:checkin",
                    ticket_number=locked_ticket.number,
                )

            # ====================================================
            # EMBARCAR
            # ====================================================

            locked_ticket.checked_in = True
            locked_ticket.checked_in_at = now

            locked_ticket.save(
                update_fields=[
                    "checked_in",
                    "checked_in_at",
                ]
            )

            # ====================================================
            # AUDITORÍA
            # ====================================================

            AuditLog.objects.create(
                user=request.user,
                action="checkin",
                model_name="Ticket",
                object_id=str(
                    locked_ticket.id
                ),
                object_repr=(
                    f"Check-in: "
                    f"{locked_ticket.number} - "
                    f"{locked_ticket.buyer_name}"
                ),
                ip=request.META.get(
                    "REMOTE_ADDR"
                ),
                user_agent=request.META.get(
                    "HTTP_USER_AGENT",
                    "",
                ),
            )

        messages.success(
            request,
            (
                f"Pasajero "
                f"{locked_ticket.buyer_name} "
                f"(asiento "
                f"{locked_ticket.seat.number}) "
                "embarcado correctamente."
            ),
        )

        return redirect(
            "coordinator:checkin",
            ticket_number=locked_ticket.number,
        )

    except Ticket.DoesNotExist:

        messages.error(
            request,
            "El pasaje ya no existe.",
        )

        return redirect(
            "coordinator:trips_dashboard"
        )

    except Exception:

        logger.exception(
            "Error realizando check-in ticket=%s",
            ticket_number,
        )

        messages.error(
            request,
            "No fue posible confirmar el embarque.",
        )

        return redirect(
            "coordinator:checkin",
            ticket_number=ticket_number,
        )

@login_required
@coordinator_required
@require_http_methods(["GET", "POST"])
def checkin_qr_scan(request):
    """
    Escaneo QR seguro y multiempresa.

    - Valida firma Django.
    - Comprueba ticket_id + ticket_number.
    - Solo acepta tickets accesibles para el usuario.
    - Mantiene el prefijo/salt CEJER por compatibilidad
      con QR ya emitidos.
    """

    from django.core import signing
    from django.core.signing import BadSignature

    if request.method == "GET":

        return render(
            request,
            "coordinator/checkin_qr_scan.html",
        )

    qr_value = (
        request.POST.get(
            "qr_value",
            "",
        )
        .strip()
    )

    if not qr_value:

        messages.error(
            request,
            "No se recibió ningún código QR.",
        )

        return redirect(
            "coordinator:checkin_qr_scan"
        )

    # IMPORTANTE:
    # No cambiar todavía este prefijo ni el salt porque
    # invalidaría los QR históricos ya emitidos.
    prefix = "CEJER:TICKET:"

    if not qr_value.startswith(prefix):

        messages.error(
            request,
            "El código QR no corresponde a un pasaje válido.",
        )

        return redirect(
            "coordinator:checkin_qr_scan"
        )

    signed_token = qr_value[
        len(prefix):
    ]

    try:

        payload = signing.loads(
            signed_token,
            salt="cejer-ticket-qr-v1",
        )

    except BadSignature:

        messages.error(
            request,
            "El código QR es inválido o fue alterado.",
        )

        return redirect(
            "coordinator:checkin_qr_scan"
        )

    except Exception:

        messages.error(
            request,
            "No fue posible validar el código QR.",
        )

        return redirect(
            "coordinator:checkin_qr_scan"
        )

    ticket_id = payload.get(
        "ticket_id"
    )

    ticket_number = payload.get(
        "ticket_number"
    )

    if (
        not ticket_id
        or not ticket_number
    ):

        messages.error(
            request,
            "El código QR no contiene información válida.",
        )

        return redirect(
            "coordinator:checkin_qr_scan"
        )

    # ============================================================
    # TICKET AUTORIZADO
    # ============================================================

    ticket = (
        tickets_for_user(
            request.user,
            Ticket.objects.select_related(
                "trip",
                "trip__bus",
                "trip__bus__company",
            ),
        )
        .filter(
            pk=ticket_id,
            number=ticket_number,
        )
        .first()
    )

    if not ticket:

        messages.error(
            request,
            (
                "El pasaje indicado por el QR no existe "
                "o no pertenece a su empresa."
            ),
        )

        return redirect(
            "coordinator:checkin_qr_scan"
        )

    return redirect(
        "coordinator:checkin",
        ticket_number=ticket.number,
    )



@login_required
@coordinator_required
def parcel_list(request):
    """
    Listado de encomiendas con aislamiento multiempresa.

    - Superuser: ve todas.
    - Coordinador/empresa: solo encomiendas de viajes accesibles.
    - Owner: queda limitado por trips_for_user().
    """

    status_filter = request.GET.get(
        "status",
        "",
    ).strip()

    trip_filter = request.GET.get(
        "trip",
        "",
    ).strip()

    # ============================================================
    # VIAJES AUTORIZADOS
    # ============================================================

    allowed_trips = trips_for_user(
        request.user,
        Trip.objects.select_related(
            "route",
            "route__origin",
            "route__destination",
            "bus",
            "bus__company",
        ),
    ).order_by(
        "-departure"
    )

    # ============================================================
    # ENCOMIENDAS AUTORIZADAS
    # ============================================================

    parcels = (
        Parcel.objects
        .select_related(
            "trip",
            "trip__bus",
            "trip__bus__company",
            "trip__route",
            "created_by",
        )
        .filter(
            trip__in=allowed_trips
        )
        .order_by(
            "-created_at"
        )
    )

    # ============================================================
    # FILTROS
    # ============================================================

    if status_filter:
        parcels = parcels.filter(
            status=status_filter
        )

    if trip_filter:
        parcels = parcels.filter(
            trip_id=trip_filter
        )

    context = {
        "parcels": parcels,
        "status_filter": status_filter,
        "trip_filter": trip_filter,
        "trips": allowed_trips,
    }

    return render(
        request,
        "coordinator/parcel_list.html",
        context,
    )


@require_POST
@login_required
@coordinator_required
def parcel_deliver(request, parcel_id):
    """
    Marca una encomienda como entregada únicamente
    si pertenece a un viaje accesible para el usuario.
    """

    allowed_trips = trips_for_user(
        request.user,
        Trip.objects.all(),
    )

    parcel = get_object_or_404(
        Parcel.objects
        .select_related(
            "trip",
            "trip__bus",
            "trip__bus__company",
        )
        .filter(
            trip__in=allowed_trips
        ),
        pk=parcel_id,
    )

    if parcel.status == "pending":

        parcel.deliver()

        messages.success(
            request,
            (
                f"Encomienda "
                f"{parcel.tracking_number} "
                "marcada como entregada."
            ),
        )

    else:

        messages.warning(
            request,
            "La encomienda ya no está pendiente.",
        )

    return redirect(
        "coordinator:parcel_list"
    )
# ============================================================================
# MANTENIMIENTO Y COMBUSTIBLE
# ============================================================================

def _sync_bus_maintenance_summary(bus):
    """
    Sincroniza el resumen operativo del bus desde su mantención más reciente.

    No reduce current_mileage: ese dato también puede provenir de combustible
    u otras fuentes operacionales.
    """
    latest = (
        Maintenance.objects
        .filter(bus=bus)
        .order_by('-date', '-created_at', '-id')
        .first()
    )

    if latest:
        bus.last_maintenance = latest.date
        bus.last_maintenance_mileage = latest.mileage or 0
        bus.next_maintenance_mileage = latest.next_maintenance_km or 0
    else:
        bus.last_maintenance = None
        bus.last_maintenance_mileage = 0
        bus.next_maintenance_mileage = 0

    bus.save(update_fields=[
        'last_maintenance',
        'last_maintenance_mileage',
        'next_maintenance_mileage',
    ])



def _highest_known_bus_mileage(bus):
    """
    Retorna el mayor kilometraje conocido del bus entre:
    - kilometraje operacional consolidado;
    - mantenciones;
    - cargas de combustible.

    La regla de FASE 2.10 es monotónica: el kilometraje operacional
    nunca retrocede automáticamente.
    """
    maintenance_max = (
        Maintenance.objects
        .filter(bus=bus)
        .aggregate(max_km=Max('mileage'))
        .get('max_km')
        or 0
    )

    fuel_max = (
        FuelRecord.objects
        .filter(bus=bus)
        .aggregate(max_km=Max('mileage'))
        .get('max_km')
        or 0
    )

    return max(
        bus.current_mileage or 0,
        bus.last_maintenance_mileage or 0,
        bus.last_fuel_mileage or 0,
        maintenance_max,
        fuel_max,
    )


def _sync_bus_operational_mileage(bus):
    """
    Consolida current_mileage sin permitir retrocesos.
    """
    highest_known = _highest_known_bus_mileage(bus)

    if highest_known > (bus.current_mileage or 0):
        bus.current_mileage = highest_known
        bus.save(update_fields=['current_mileage'])

    return highest_known


def _sync_bus_fuel_summary(bus):
    """
    Sincroniza en Bus la última carga cronológica de combustible.
    No modifica current_mileage hacia abajo.
    """
    latest = (
        FuelRecord.objects
        .filter(bus=bus)
        .order_by('-date', '-created_at', '-id')
        .first()
    )

    if latest:
        bus.last_fuel_refill = latest.date
        bus.last_fuel_mileage = latest.mileage or 0
    else:
        bus.last_fuel_refill = None
        bus.last_fuel_mileage = 0

    bus.save(update_fields=[
        'last_fuel_refill',
        'last_fuel_mileage',
    ])

    _sync_bus_operational_mileage(bus)


def _maintenance_status_for_bus(bus):
    """
    Retorna estado operacional de mantenimiento según kilometraje actual.
    """
    current_km = bus.current_mileage or 0
    next_km = bus.next_maintenance_mileage or 0

    if not next_km:
        return {
            'key': 'none',
            'label': 'Sin programación',
            'color': 'secondary',
            'remaining_km': None,
        }

    remaining = next_km - current_km

    if remaining <= 0:
        return {
            'key': 'overdue',
            'label': 'Vencida',
            'color': 'danger',
            'remaining_km': remaining,
        }

    if remaining <= 1000:
        return {
            'key': 'soon',
            'label': 'Próxima',
            'color': 'warning',
            'remaining_km': remaining,
        }

    return {
        'key': 'ok',
        'label': 'Al día',
        'color': 'success',
        'remaining_km': remaining,
    }


@login_required
@coordinator_required
def maintenance_list(request):
    """
    Listado de mantenciones.

    MULTIEMPRESA:
    - Solo muestra mantenciones de buses accesibles para el usuario.
    - El filtro de buses solo contiene buses de su empresa.
    - Los indicadores se calculan únicamente sobre esos buses.
    """

    bus_id = request.GET.get("bus")
    maint_type = request.GET.get("type")
    status_filter = request.GET.get("status")

    # ============================================================
    # BUSES AUTORIZADOS
    # ============================================================
    allowed_buses = buses_for_user(
        request.user,
        Bus.objects.all(),
    )

    # ============================================================
    # MANTENCIONES AUTORIZADAS
    # ============================================================
    maintenances_qs = (
        Maintenance.objects
        .filter(
            bus__in=allowed_buses
        )
        .select_related(
            "bus",
            "bus__company",
            "created_by",
        )
        .order_by(
            "-date",
            "-created_at",
            "-id",
        )
    )

    # ============================================================
    # FILTRO POR BUS
    # ============================================================
    if bus_id:
        maintenances_qs = (
            maintenances_qs.filter(
                bus_id=bus_id
            )
        )

    # ============================================================
    # FILTRO POR TIPO
    # ============================================================
    if maint_type:
        maintenances_qs = (
            maintenances_qs.filter(
                maintenance_type=maint_type
            )
        )

    maintenances = list(
        maintenances_qs
    )

    # ============================================================
    # ÚLTIMA MANTENCIÓN POR BUS
    # ============================================================
    latest_id_by_bus = {}

    for item in (
        Maintenance.objects
        .filter(
            bus__in=allowed_buses
        )
        .order_by(
            "bus_id",
            "-date",
            "-created_at",
            "-id",
        )
        .values(
            "id",
            "bus_id",
        )
    ):
        latest_id_by_bus.setdefault(
            item["bus_id"],
            item["id"],
        )

    # ============================================================
    # ESTADO DE CADA REGISTRO
    # ============================================================
    for maintenance in maintenances:

        maintenance.is_latest = (
            latest_id_by_bus.get(
                maintenance.bus_id
            )
            == maintenance.id
        )

        if maintenance.is_latest:

            status = (
                _maintenance_status_for_bus(
                    maintenance.bus
                )
            )

            maintenance.status_key = (
                status["key"]
            )

            maintenance.status_label = (
                status["label"]
            )

            maintenance.status_color = (
                status["color"]
            )

            maintenance.remaining_km = (
                status["remaining_km"]
            )

        else:
            maintenance.status_key = (
                "history"
            )

            maintenance.status_label = (
                "Histórico"
            )

            maintenance.status_color = (
                "secondary"
            )

            maintenance.remaining_km = None

    # ============================================================
    # FILTRO POR ESTADO
    # ============================================================
    if status_filter:
        maintenances = [
            maintenance
            for maintenance in maintenances
            if maintenance.status_key
            == status_filter
        ]

    # ============================================================
    # BUSES PARA FILTROS / INDICADORES
    # ============================================================
    buses = list(
        buses_for_user(
            request.user,
            Bus.objects.filter(
                is_active=True
            ),
        ).order_by(
            "plate"
        )
    )

    overdue_buses = 0
    soon_buses = 0
    ok_buses = 0
    unplanned_buses = 0

    for bus in buses:

        status = (
            _maintenance_status_for_bus(
                bus
            )
        )

        bus.maintenance_status_key = (
            status["key"]
        )

        bus.maintenance_status_label = (
            status["label"]
        )

        bus.maintenance_status_color = (
            status["color"]
        )

        bus.maintenance_remaining_km = (
            status["remaining_km"]
        )

        if status["key"] == "overdue":
            overdue_buses += 1

        elif status["key"] == "soon":
            soon_buses += 1

        elif status["key"] == "ok":
            ok_buses += 1

        else:
            unplanned_buses += 1

    # ============================================================
    # RESUMEN
    # ============================================================
    total_records = len(
        maintenances
    )

    total_cost = sum(
        (
            maintenance.cost
            or Decimal("0")
        )
        for maintenance
        in maintenances
    )

    context = {
        "maintenances": maintenances,
        "buses": buses,
        "bus_filter": bus_id,
        "type_filter": maint_type,
        "status_filter": status_filter,
        "total_records": total_records,
        "total_cost": total_cost,
        "overdue_buses": overdue_buses,
        "soon_buses": soon_buses,
        "ok_buses": ok_buses,
        "unplanned_buses": unplanned_buses,
    }

    return render(
        request,
        "coordinator/maintenance_list.html",
        context,
    )


@login_required
@coordinator_required
def maintenance_create(request, bus_id=None):
    """
    Registra una mantención usando los campos reales del modelo Maintenance
    y sincroniza los datos operacionales principales del bus.

    MULTIEMPRESA:
    - Solo permite buses accesibles para el usuario.
    - Impide enviar manualmente un bus_id de otra empresa.
    """

    # ============================================================
    # BUSES AUTORIZADOS
    # ============================================================
    allowed_buses = buses_for_user(
        request.user,
        Bus.objects.all(),
    )

    bus = None

    selected_bus_id = (
        bus_id
        or request.GET.get("bus")
    )

    if selected_bus_id:
        bus = get_object_or_404(
            allowed_buses,
            pk=selected_bus_id,
        )

    # ============================================================
    # POST
    # ============================================================
    if request.method == "POST":
        try:
            bus_post_id = request.POST.get(
                "bus"
            )

            if not bus_post_id:
                raise ValidationError(
                    "Debe seleccionar un bus."
                )

            # ----------------------------------------------------
            # BUS AUTORIZADO
            # ----------------------------------------------------
            maintenance_bus = get_object_or_404(
                buses_for_user(
                    request.user,
                    Bus.objects.filter(
                        is_active=True
                    ),
                ),
                pk=bus_post_id,
            )

            # ----------------------------------------------------
            # KILOMETRAJE
            # ----------------------------------------------------
            mileage_raw = request.POST.get(
                "mileage"
            )

            if mileage_raw in (None, ""):
                raise ValidationError(
                    "Debe ingresar el kilometraje de la mantención."
                )

            mileage = int(
                mileage_raw
            )

            if mileage < 0:
                raise ValidationError(
                    "El kilometraje no puede ser negativo."
                )

            # ====================================================
            # FASE 2.10
            # No retroceder kilometraje operacional.
            # ====================================================
            minimum_allowed = (
                _highest_known_bus_mileage(
                    maintenance_bus
                )
            )

            if mileage < minimum_allowed:
                raise ValidationError(
                    f"Kilometraje inválido. "
                    f"El mayor kilometraje conocido "
                    f"del bus {maintenance_bus.plate} "
                    f"es {minimum_allowed:,} km. "
                    "La mantención no puede registrar "
                    "un valor inferior."
                )

            # ----------------------------------------------------
            # PRÓXIMO MANTENIMIENTO
            # ----------------------------------------------------
            next_km_raw = (
                request.POST.get(
                    "next_maintenance_km"
                )
                or request.POST.get(
                    "next_due"
                )
                or 0
            )

            next_maintenance_km = int(
                next_km_raw
            )

            if next_maintenance_km < 0:
                raise ValidationError(
                    "El próximo mantenimiento "
                    "no puede ser negativo."
                )

            if (
                next_maintenance_km
                and next_maintenance_km <= mileage
            ):
                raise ValidationError(
                    "El próximo mantenimiento debe ser "
                    "mayor al kilometraje actual."
                )

            # ----------------------------------------------------
            # COSTO
            # ----------------------------------------------------
            cost_raw = (
                request.POST.get("cost")
                or "0"
            )

            cost = Decimal(
                cost_raw
            )

            # ====================================================
            # CREAR
            # ====================================================
            with transaction.atomic():

                maintenance = (
                    Maintenance.objects.create(
                        bus=maintenance_bus,
                        maintenance_type=(
                            request.POST.get(
                                "maintenance_type"
                            )
                        ),
                        date=request.POST.get(
                            "date"
                        ),
                        mileage=mileage,
                        description=request.POST.get(
                            "description",
                            "",
                        ),
                        cost=cost,
                        workshop=request.POST.get(
                            "workshop",
                            "",
                        ),
                        next_maintenance_km=(
                            next_maintenance_km
                        ),
                        technician=request.POST.get(
                            "technician",
                            "",
                        ),
                        notes=request.POST.get(
                            "notes",
                            "",
                        ),
                        created_by=request.user,
                    )
                )

                # -----------------------------------------------
                # Sincronización central FASE 2.10
                # -----------------------------------------------
                _sync_bus_operational_mileage(
                    maintenance_bus
                )

                _sync_bus_maintenance_summary(
                    maintenance_bus
                )

            messages.success(
                request,
                (
                    "Mantenimiento registrado para "
                    f"{maintenance.bus.plate}."
                ),
            )

            return redirect(
                "coordinator:maintenance_list"
            )

        except (ValueError, TypeError):
            messages.error(
                request,
                (
                    "Kilometraje, próximo mantenimiento "
                    "o costo tienen un formato inválido."
                ),
            )

        except ValidationError as e:
            messages.error(
                request,
                str(e),
            )

        except Exception as e:
            messages.error(
                request,
                f"Error: {str(e)}",
            )

    # ============================================================
    # BUSES PARA FORMULARIO
    # ============================================================
    buses = buses_for_user(
        request.user,
        Bus.objects.filter(
            is_active=True
        ),
    ).order_by(
        "plate"
    )

    context = {
        "buses": buses,
        "selected_bus": bus,
    }

    return render(
        request,
        "coordinator/maintenance_form.html",
        context,
    )


@login_required
@coordinator_required
def maintenance_edit(request, pk):
    """
    Edita una mantención únicamente dentro del alcance multiempresa.

    - Solo permite editar mantenciones de buses accesibles.
    - El bus destino también debe ser accesible.
    - Protege contra manipulación de IDs por POST.
    - Mantiene la lógica de kilometraje y sincronización.
    """

    # ============================================================
    # BUSES AUTORIZADOS
    # ============================================================

    allowed_buses = buses_for_user(
        request.user,
        Bus.objects.all(),
    )

    # ============================================================
    # MANTENCIÓN AUTORIZADA
    # ============================================================

    maintenance = get_object_or_404(
        Maintenance.objects
        .select_related(
            "bus",
            "bus__company",
        )
        .filter(
            bus__in=allowed_buses
        ),
        pk=pk,
    )

    if request.method == "POST":

        try:
            bus_post_id = request.POST.get(
                "bus"
            )

            if not bus_post_id:
                raise ValidationError(
                    "Debe seleccionar un bus."
                )

            old_bus = maintenance.bus

            # ====================================================
            # BUS DESTINO AUTORIZADO
            # ====================================================

            maintenance_bus = get_object_or_404(
                buses_for_user(
                    request.user,
                    Bus.objects.filter(
                        is_active=True
                    ),
                ),
                pk=bus_post_id,
            )

            # ====================================================
            # KILOMETRAJE
            # ====================================================

            mileage_raw = request.POST.get(
                "mileage"
            )

            if mileage_raw in (None, ""):
                raise ValidationError(
                    "Debe ingresar el kilometraje de la mantención."
                )

            mileage = int(
                mileage_raw
            )

            if mileage < 0:
                raise ValidationError(
                    "El kilometraje no puede ser negativo."
                )

            # ====================================================
            # FASE 2.10
            # NO RETROCEDER KILOMETRAJE
            # ====================================================

            minimum_allowed = (
                _highest_known_bus_mileage(
                    maintenance_bus
                )
            )

            if mileage < minimum_allowed:
                raise ValidationError(
                    f"Kilometraje inválido. "
                    f"El mayor kilometraje conocido "
                    f"del bus {maintenance_bus.plate} "
                    f"es {minimum_allowed:,} km. "
                    "La mantención editada no puede "
                    "registrar un valor inferior."
                )

            # ====================================================
            # PRÓXIMO MANTENIMIENTO
            # ====================================================

            next_km_raw = (
                request.POST.get(
                    "next_maintenance_km"
                )
                or request.POST.get(
                    "next_due"
                )
                or 0
            )

            next_maintenance_km = int(
                next_km_raw
            )

            if next_maintenance_km < 0:
                raise ValidationError(
                    "El próximo mantenimiento "
                    "no puede ser negativo."
                )

            if (
                next_maintenance_km
                and next_maintenance_km <= mileage
            ):
                raise ValidationError(
                    "El próximo mantenimiento debe ser "
                    "mayor al kilometraje actual."
                )

            # ====================================================
            # COSTO
            # ====================================================

            cost = Decimal(
                request.POST.get(
                    "cost"
                )
                or "0"
            )

            # ====================================================
            # GUARDADO ATÓMICO
            # ====================================================

            with transaction.atomic():

                # ------------------------------------------------
                # REBLOQUEAR MANTENCIÓN AUTORIZADA
                # ------------------------------------------------

                locked_maintenance = get_object_or_404(
                    Maintenance.objects
                    .select_for_update()
                    .select_related(
                        "bus",
                        "bus__company",
                    )
                    .filter(
                        bus__in=allowed_buses
                    ),
                    pk=maintenance.pk,
                )

                old_bus = (
                    locked_maintenance.bus
                )

                # ------------------------------------------------
                # REBLOQUEAR BUS DESTINO AUTORIZADO
                # ------------------------------------------------

                locked_bus = get_object_or_404(
                    buses_for_user(
                        request.user,
                        Bus.objects
                        .select_for_update()
                        .filter(
                            is_active=True
                        ),
                    ),
                    pk=maintenance_bus.pk,
                )

                # ------------------------------------------------
                # ACTUALIZAR
                # ------------------------------------------------

                locked_maintenance.bus = (
                    locked_bus
                )

                locked_maintenance.maintenance_type = (
                    request.POST.get(
                        "maintenance_type"
                    )
                )

                locked_maintenance.date = (
                    request.POST.get(
                        "date"
                    )
                )

                locked_maintenance.mileage = (
                    mileage
                )

                locked_maintenance.description = (
                    request.POST.get(
                        "description",
                        "",
                    )
                )

                locked_maintenance.cost = (
                    cost
                )

                locked_maintenance.workshop = (
                    request.POST.get(
                        "workshop",
                        "",
                    )
                )

                locked_maintenance.next_maintenance_km = (
                    next_maintenance_km
                )

                locked_maintenance.technician = (
                    request.POST.get(
                        "technician",
                        "",
                    )
                )

                locked_maintenance.notes = (
                    request.POST.get(
                        "notes",
                        "",
                    )
                )

                locked_maintenance.save()

                # =================================================
                # SINCRONIZAR BUS ANTERIOR Y NUEVO
                # =================================================

                buses_to_sync = {
                    old_bus.pk: old_bus,
                    locked_bus.pk: locked_bus,
                }

                for bus_obj in buses_to_sync.values():

                    _sync_bus_operational_mileage(
                        bus_obj
                    )

                    _sync_bus_maintenance_summary(
                        bus_obj
                    )

                    _sync_bus_fuel_summary(
                        bus_obj
                    )

            messages.success(
                request,
                "Mantenimiento actualizado.",
            )

            return redirect(
                "coordinator:maintenance_list"
            )

        except (
            ValueError,
            TypeError,
        ):
            messages.error(
                request,
                (
                    "Kilometraje, próximo mantenimiento "
                    "o costo tienen un formato inválido."
                ),
            )

        except ValidationError as e:
            messages.error(
                request,
                str(e),
            )

        except Exception as e:
            messages.error(
                request,
                f"Error: {str(e)}",
            )

    # ============================================================
    # BUSES PARA EL FORMULARIO
    # ============================================================

    buses = buses_for_user(
        request.user,
        Bus.objects.filter(
            is_active=True
        ),
    ).order_by(
        "plate"
    )

    context = {
        "maintenance": maintenance,
        "buses": buses,
    }

    return render(
        request,
        "coordinator/maintenance_form.html",
        context,
    )

@login_required
@coordinator_required
@require_POST
def maintenance_delete(request, pk):
    """
    Elimina una mantención únicamente si pertenece
    a un bus accesible para el usuario.
    """

    allowed_buses = buses_for_user(
        request.user,
        Bus.objects.all(),
    )

    maintenance = get_object_or_404(
        Maintenance.objects
        .select_related(
            "bus",
            "bus__company",
        )
        .filter(
            bus__in=allowed_buses
        ),
        pk=pk,
    )

    bus_id = maintenance.bus_id
    plate = maintenance.bus.plate

    with transaction.atomic():

        maintenance.delete()

        # ========================================================
        # Reobtener y bloquear únicamente un bus autorizado
        # ========================================================
        bus = get_object_or_404(
            buses_for_user(
                request.user,
                Bus.objects.select_for_update(),
            ),
            pk=bus_id,
        )

        _sync_bus_maintenance_summary(
            bus
        )

        _sync_bus_fuel_summary(
            bus
        )

        _sync_bus_operational_mileage(
            bus
        )

    messages.success(
        request,
        (
            f"Mantenimiento del bus {plate} "
            "eliminado correctamente."
        ),
    )

    return redirect(
        "coordinator:maintenance_list"
    )

@login_required
@coordinator_required
def fuel_list(request):
    """
    Centro operacional de combustible.

    MULTIEMPRESA:
    - Solo muestra cargas de combustible de buses accesibles.
    - Los indicadores se calculan únicamente sobre esos registros.
    - El filtro de buses solo contiene buses de la empresa.
    """

    bus_id = request.GET.get("bus")

    # ============================================================
    # BUSES AUTORIZADOS
    # ============================================================
    allowed_buses = buses_for_user(
        request.user,
        Bus.objects.all(),
    )

    # ============================================================
    # REGISTROS AUTORIZADOS
    # ============================================================
    qs = (
        FuelRecord.objects
        .filter(
            bus__in=allowed_buses
        )
        .select_related(
            "bus",
            "bus__company",
            "created_by",
        )
    )

    if bus_id:
        qs = qs.filter(
            bus_id=bus_id
        )

    records_asc = list(
        qs.order_by(
            "bus_id",
            "date",
            "created_at",
            "id",
        )
    )

    previous_by_bus = {}
    efficiencies = []
    cost_per_km_values = []

    total_liters = Decimal("0")
    total_cost = Decimal("0")
    total_distance = 0

    for record in records_asc:
        record.distance_since_previous = None
        record.efficiency_kml = None
        record.cost_per_km = None
        record.cost_per_liter = None

        liters = (
            record.liters
            or Decimal("0")
        )

        cost = (
            record.cost
            or Decimal("0")
        )

        total_liters += liters
        total_cost += cost

        if liters > 0:
            record.cost_per_liter = round(
                cost / liters,
                2,
            )

        previous = previous_by_bus.get(
            record.bus_id
        )

        if previous:
            distance = (
                record.mileage
                - previous.mileage
            )

            if (
                distance > 0
                and liters > 0
            ):
                record.distance_since_previous = distance

                record.efficiency_kml = round(
                    Decimal(distance)
                    / liters,
                    2,
                )

                record.cost_per_km = round(
                    cost
                    / Decimal(distance),
                    2,
                )

                efficiencies.append(
                    record.efficiency_kml
                )

                cost_per_km_values.append(
                    record.cost_per_km
                )

                total_distance += distance

        previous_by_bus[
            record.bus_id
        ] = record

    records = sorted(
        records_asc,
        key=lambda r: (
            r.date,
            r.created_at,
            r.id,
        ),
        reverse=True,
    )

    avg_efficiency = (
        round(
            sum(
                efficiencies,
                Decimal("0"),
            )
            / Decimal(
                len(efficiencies)
            ),
            2,
        )
        if efficiencies
        else None
    )

    avg_cost_per_km = (
        round(
            sum(
                cost_per_km_values,
                Decimal("0"),
            )
            / Decimal(
                len(cost_per_km_values)
            ),
            2,
        )
        if cost_per_km_values
        else None
    )

    avg_cost_per_liter = (
        round(
            total_cost
            / total_liters,
            2,
        )
        if total_liters > 0
        else None
    )

    buses = buses_for_user(
        request.user,
        Bus.objects.filter(
            is_active=True
        ),
    ).order_by(
        "plate"
    )

    return render(
        request,
        "coordinator/fuel_list.html",
        {
            "records": records,
            "buses": buses,
            "bus_filter": bus_id,
            "total_liters": total_liters,
            "total_cost": total_cost,
            "avg_efficiency": avg_efficiency,
            "avg_cost_per_km": avg_cost_per_km,
            "avg_cost_per_liter": avg_cost_per_liter,
            "total_distance": total_distance,
            "records_count": len(
                records
            ),
        },
    )


@login_required
@coordinator_required
def fuel_create(request, bus_id=None):
    """
    Registra una carga de combustible protegiendo
    la integridad del kilometraje del bus.

    MULTIEMPRESA:
    - Solo permite buses accesibles para el usuario.
    - Impide forzar un bus_id de otra empresa.
    """

    selected_bus = None

    selected_bus_id = (
        bus_id
        or request.GET.get("bus")
    )

    if selected_bus_id:
        selected_bus = get_object_or_404(
            buses_for_user(
                request.user,
                Bus.objects.filter(
                    is_active=True
                ),
            ),
            pk=selected_bus_id,
        )

    if request.method == "POST":
        try:
            bus_id = request.POST.get(
                "bus"
            )

            if not bus_id:
                raise ValidationError(
                    "Debe seleccionar un bus."
                )

            liters_raw = request.POST.get(
                "liters"
            )

            cost_raw = request.POST.get(
                "cost"
            )

            mileage_raw = request.POST.get(
                "mileage"
            )

            date_raw = request.POST.get(
                "date"
            )

            if not date_raw:
                raise ValidationError(
                    "Debe ingresar la fecha."
                )

            if liters_raw in (
                None,
                "",
            ):
                raise ValidationError(
                    "Debe ingresar los litros cargados."
                )

            if cost_raw in (
                None,
                "",
            ):
                raise ValidationError(
                    "Debe ingresar el costo de la carga."
                )

            if mileage_raw in (
                None,
                "",
            ):
                raise ValidationError(
                    "Debe ingresar el kilometraje actual."
                )

            liters = Decimal(
                liters_raw
            )

            cost = Decimal(
                cost_raw
            )

            mileage = int(
                mileage_raw
            )

            if liters <= 0:
                raise ValidationError(
                    "Los litros deben ser mayores que 0."
                )

            if cost < 0:
                raise ValidationError(
                    "El costo no puede ser negativo."
                )

            if mileage < 0:
                raise ValidationError(
                    "El kilometraje no puede ser negativo."
                )

            with transaction.atomic():

                bus = get_object_or_404(
                    buses_for_user(
                        request.user,
                        Bus.objects.select_for_update()
                        .filter(
                            is_active=True
                        ),
                    ),
                    pk=bus_id,
                )

                minimum_allowed = (
                    _highest_known_bus_mileage(
                        bus
                    )
                )

                if mileage < minimum_allowed:
                    raise ValidationError(
                        f"Kilometraje inválido. "
                        f"El mayor kilometraje conocido "
                        f"del bus {bus.plate} es "
                        f"{minimum_allowed:,} km. "
                        "La nueva carga no puede registrar "
                        "un valor inferior."
                    )

                record = (
                    FuelRecord.objects.create(
                        bus=bus,
                        date=date_raw,
                        liters=liters,
                        cost=cost,
                        mileage=mileage,
                        created_by=request.user,
                    )
                )

                _sync_bus_operational_mileage(
                    bus
                )

                _sync_bus_fuel_summary(
                    bus
                )

            messages.success(
                request,
                (
                    "Carga de combustible registrada "
                    f"para {record.bus.plate}."
                ),
            )

            return redirect(
                "coordinator:fuel_list"
            )

        except ValidationError as e:
            messages.error(
                request,
                str(e),
            )

        except (ValueError, TypeError):
            messages.error(
                request,
                (
                    "Litros, costo o kilometraje "
                    "tienen un formato inválido."
                ),
            )

        except Exception as e:
            messages.error(
                request,
                f"Error: {str(e)}",
            )

    buses = buses_for_user(
        request.user,
        Bus.objects.filter(
            is_active=True
        ),
    ).order_by(
        "plate"
    )

    return render(
        request,
        "coordinator/fuel_form.html",
        {
            "buses": buses,
            "selected_bus": selected_bus,
        },
    )


@login_required
@coordinator_required
@require_POST
def fuel_delete(request, pk):
    """
    Elimina un registro de combustible únicamente
    si pertenece a un bus accesible para el usuario.
    """

    allowed_buses = buses_for_user(
        request.user,
        Bus.objects.all(),
    )

    record = get_object_or_404(
        FuelRecord.objects
        .select_related(
            "bus",
            "bus__company",
        )
        .filter(
            bus__in=allowed_buses
        ),
        pk=pk,
    )

    bus_id = record.bus_id

    with transaction.atomic():

        bus = get_object_or_404(
            buses_for_user(
                request.user,
                Bus.objects.select_for_update(),
            ),
            pk=bus_id,
        )

        record.delete()

        _sync_bus_fuel_summary(
            bus
        )

        _sync_bus_maintenance_summary(
            bus
        )

        _sync_bus_operational_mileage(
            bus
        )

    messages.success(
        request,
        "Registro de combustible eliminado.",
    )

    return redirect(
        "coordinator:fuel_list"
    )

@login_required
@coordinator_required
def bus_maintenance_history(request, bus_id):
    """
    Historial completo de mantenciones de un bus.

    MULTIEMPRESA:
    - Solo permite acceder a buses autorizados para el usuario.
    - Impide consultar por URL el historial de un bus de otra empresa.
    """

    # ============================================================
    # BUS AUTORIZADO
    # ============================================================
    bus = get_object_or_404(
        buses_for_user(
            request.user,
            Bus.objects.select_related(
                "company"
            ),
        ),
        pk=bus_id,
    )

    # ============================================================
    # HISTORIAL DEL BUS
    # ============================================================
    maintenances = list(
        Maintenance.objects
        .filter(
            bus=bus
        )
        .select_related(
            "created_by"
        )
        .order_by(
            "-date",
            "-created_at",
            "-id",
        )
    )

    # ============================================================
    # ESTADO OPERACIONAL
    # ============================================================
    status = _maintenance_status_for_bus(
        bus
    )

    # ============================================================
    # COSTO TOTAL
    # ============================================================
    total_cost = sum(
        (
            maintenance.cost
            or Decimal("0")
        )
        for maintenance
        in maintenances
    )

    # ============================================================
    # CONTADORES
    # ============================================================
    preventive_count = sum(
        1
        for maintenance
        in maintenances
        if maintenance.maintenance_type
        == "preventive"
    )

    corrective_count = sum(
        1
        for maintenance
        in maintenances
        if maintenance.maintenance_type
        == "corrective"
    )

    context = {
        "bus": bus,
        "maintenances": maintenances,
        "maintenance_status": status,
        "total_cost": total_cost,
        "preventive_count": preventive_count,
        "corrective_count": corrective_count,
        "maintenance_count": len(
            maintenances
        ),
    }

    return render(
        request,
        "coordinator/bus_maintenance_history.html",
        context,
    )

# ============================================================================
# MÓDULO DE SEGURIDAD Y LEY 21.719
# ============================================================================


def _security_logs_for_user(user):
    """
    Devuelve los registros de auditoría visibles para el usuario.

    SUPERUSER:
        Puede ver toda la auditoría.

    USUARIO DE EMPRESA:
        Solo ve eventos asociados a usuarios de su empresa.

    NOTA:
        Los logs sin usuario no pueden atribuirse de forma segura
        a una empresa porque AuditLog todavía no posee company.
    """

    qs = AuditLog.objects.select_related(
        "user",
        "user__profile",
        "user__profile__company",
    )

    if user.is_superuser:
        return qs

    scope = get_user_scope(user)

    company = scope.get("company")

    if not company:
        return qs.none()

    return qs.filter(
        user__profile__company=company
    )


def _require_global_backup_access(user):
    """
    Los respaldos contienen la base de datos completa.

    Por seguridad multiempresa únicamente un superusuario
    puede crear, listar, descargar o eliminar respaldos globales.
    """

    if not user.is_superuser:
        raise PermissionDenied(
            "No tiene permisos para administrar respaldos globales."
        )


@login_required
@coordinator_required
def seguridad_dashboard(request):
    """
    Panel principal de seguridad.

    MULTIEMPRESA:
    - Superuser: auditoría global.
    - Resto: auditoría únicamente de su empresa.
    """

    logs = _security_logs_for_user(
        request.user
    )

    total_logs = logs.count()

    login_failed = logs.filter(
        action="login_failed"
    ).count()

    recent_logs = logs.order_by(
        "-timestamp"
    )[:10]

    today = timezone.now().date()

    usuarios_activos = (
        logs.filter(
            action="login",
            timestamp__date=today,
        )
        .exclude(
            user_id__isnull=True
        )
        .values_list(
            "user_id",
            flat=True,
        )
        .distinct()
        .count()
    )

    context = {
        "title": (
            "Ley 21.719 - Seguridad y Protección de Datos"
        ),
        "total_logs": total_logs,
        "login_failed": login_failed,
        "usuarios_activos": usuarios_activos,
        "recent_logs": recent_logs,
    }

    return render(
        request,
        "coordinator/seguridad/dashboard.html",
        context,
    )


@login_required
@login_required
@coordinator_required
def seguridad_auditoria(request):
    """
    Lista de logs de auditoría con filtros.

    MULTIEMPRESA:
    - Superuser ve toda la auditoría.
    - Resto solo ve auditoría de usuarios de su empresa.
    """

    query = request.GET.get("q", "").strip()
    action_filter = request.GET.get("action", "")
    date_from = request.GET.get("date_from", "")
    date_to = request.GET.get("date_to", "")

    logs = _security_logs_for_user(
        request.user
    )

    if query:
        logs = logs.filter(
            Q(user__username__icontains=query)
            |
            Q(object_repr__icontains=query)
            |
            Q(model_name__icontains=query)
        )

    if action_filter:
        logs = logs.filter(
            action=action_filter
        )

    if date_from:
        logs = logs.filter(
            timestamp__date__gte=date_from
        )

    if date_to:
        logs = logs.filter(
            timestamp__date__lte=date_to
        )

    logs = logs.order_by(
        "-timestamp"
    )

    paginator = Paginator(
        logs,
        50,
    )

    page = request.GET.get("page")

    logs_page = paginator.get_page(
        page
    )

    context = {
        "title": "Auditoría - Ley 21.719",
        "logs": logs_page,
        "action_choices": AuditLog.ACTION_CHOICES,
        "query": query,
        "action_filter": action_filter,
        "date_from": date_from,
        "date_to": date_to,
    }

    return render(
        request,
        "coordinator/seguridad/auditoria.html",
        context,
    )


@login_required
@coordinator_required
def seguridad_respaldos(request):
    """
    Gestión de respaldos completos de la base de datos.

    SEGURIDAD:
    Los respaldos contienen información global del sistema,
    por lo que su administración queda reservada al superusuario.
    """

    _require_global_backup_access(
        request.user
    )

    if request.method == "POST":

        action = request.POST.get(
            "action"
        )

        if action == "download_backup":

            backup_file = create_backup()

            if backup_file:

                AuditLog.objects.create(
                    user=request.user,
                    action="download_backup",
                    ip_address=request.META.get(
                        "REMOTE_ADDR"
                    ),
                    user_agent=request.META.get(
                        "HTTP_USER_AGENT",
                        "",
                    ),
                    object_repr=(
                        f"Respaldo: "
                        f"{os.path.basename(backup_file)}"
                    ),
                )

                return FileResponse(
                    open(
                        backup_file,
                        "rb",
                    ),
                    as_attachment=True,
                    filename=os.path.basename(
                        backup_file
                    ),
                )

            messages.error(
                request,
                "Error al generar el respaldo.",
            )

        elif action == "create_backup":

            backup_file = create_backup()

            if backup_file:

                AuditLog.objects.create(
                    user=request.user,
                    action="backup_created",
                    ip_address=request.META.get(
                        "REMOTE_ADDR"
                    ),
                    user_agent=request.META.get(
                        "HTTP_USER_AGENT",
                        "",
                    ),
                    object_repr=(
                        f"Respaldo: "
                        f"{os.path.basename(backup_file)}"
                    ),
                )

                messages.success(
                    request,
                    (
                        "Respaldo creado: "
                        f"{os.path.basename(backup_file)}"
                    ),
                )

            else:
                messages.error(
                    request,
                    "Error al crear el respaldo.",
                )

    backup_dir = get_backup_dir()

    backups = []

    if os.path.exists(
        backup_dir
    ):

        for filename in sorted(
            os.listdir(backup_dir),
            reverse=True,
        ):

            if not (
                filename.endswith(".sql")
                or filename.endswith(".dump")
            ):
                continue

            path = os.path.join(
                backup_dir,
                filename,
            )

            if not os.path.isfile(path):
                continue

            backups.append(
                {
                    "name": filename,
                    "size": os.path.getsize(path),
                    "modified": os.path.getmtime(path),
                    "path": path,
                }
            )

    context = {
        "title": "Respaldos - Ley 21.719",
        "backups": backups[:10],
    }

    return render(
        request,
        "coordinator/seguridad/respaldos.html",
        context,
    )


@login_required
@coordinator_required
def seguridad_incidentes(request):
    """
    Registro de incidentes de seguridad.

    MULTIEMPRESA:
    - Superuser: incidentes globales.
    - Resto: incidentes atribuibles a usuarios de su empresa.
    """

    logs = (
        _security_logs_for_user(
            request.user
        )
        .filter(
            action="login_failed"
        )
        .order_by(
            "-timestamp"
        )
    )

    paginator = Paginator(
        logs,
        50,
    )

    page = request.GET.get(
        "page"
    )

    logs_page = paginator.get_page(
        page
    )

    context = {
        "title": "Incidentes de Seguridad",
        "logs": logs_page,
    }

    return render(
        request,
        "coordinator/seguridad/incidentes.html",
        context,
    )


@login_required
@coordinator_required
def seguridad_descargar_respaldo(request, filename):
    """Descarga un archivo de respaldo."""
    backup_dir = os.path.join(settings.BASE_DIR, 'backups')
    file_path = os.path.join(backup_dir, filename)
    if os.path.exists(file_path) and os.path.isfile(file_path):
        with open(file_path, 'rb') as f:
            response = HttpResponse(f.read(), content_type='application/octet-stream')
            response['Content-Disposition'] = f'attachment; filename="{filename}"'
            return response
    return JsonResponse({'error': 'Archivo no encontrado'}, status=404)


@login_required
@coordinator_required
@require_POST
def seguridad_eliminar_respaldo(
    request,
    filename,
):
    """
    Elimina un respaldo completo.

    Solo superusuario.
    """

    _require_global_backup_access(
        request.user
    )

    safe_filename = os.path.basename(
        filename
    )

    if safe_filename != filename:
        raise PermissionDenied(
            "Nombre de archivo inválido."
        )

    if not (
        safe_filename.endswith(".sql")
        or safe_filename.endswith(".dump")
    ):
        return JsonResponse(
            {
                "error": "Tipo de archivo no permitido."
            },
            status=400,
        )

    try:

        backup_dir = os.path.realpath(
            get_backup_dir()
        )

        file_path = os.path.realpath(
            os.path.join(
                backup_dir,
                safe_filename,
            )
        )

        if os.path.dirname(file_path) != backup_dir:
            raise PermissionDenied(
                "Ruta de respaldo inválida."
            )

        if not (
            os.path.exists(file_path)
            and os.path.isfile(file_path)
        ):
            return JsonResponse(
                {
                    "error": "Archivo no encontrado"
                },
                status=404,
            )

        os.remove(
            file_path
        )

        AuditLog.objects.create(
            user=request.user,
            action="delete",
            model_name="Backup",
            object_repr=(
                f"Respaldo: {safe_filename}"
            ),
            ip_address=request.META.get(
                "REMOTE_ADDR"
            ),
            user_agent=request.META.get(
                "HTTP_USER_AGENT",
                "",
            ),
        )

        return JsonResponse(
            {
                "success": True
            }
        )

    except PermissionDenied:
        raise

    except Exception as e:

        return JsonResponse(
            {
                "success": False,
                "error": str(e),
            },
            status=500,
        )


@login_required
@coordinator_required
@require_POST
def seguridad_guardar_programacion(request):
    try:
        data = json.loads(request.body)
        frequency = data.get('frequency', 'daily')
        time = data.get('time', '03:00')
        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


@login_required
@coordinator_required
@require_POST
def seguridad_resolver_incidente(request, incident_id):
    try:
        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


@login_required
@coordinator_required
@require_POST
def seguridad_crear_respaldo(request):
    """
    Crea un respaldo completo de la base de datos.

    Solo superusuario.
    """

    _require_global_backup_access(
        request.user
    )

    try:

        backup_file = create_backup()

        if not backup_file:

            return JsonResponse(
                {
                    "success": False,
                    "error": (
                        "Error al crear el respaldo"
                    ),
                },
                status=500,
            )

        AuditLog.objects.create(
            user=request.user,
            action="backup_created",
            model_name="Backup",
            ip_address=request.META.get(
                "REMOTE_ADDR"
            ),
            user_agent=request.META.get(
                "HTTP_USER_AGENT",
                "",
            ),
            object_repr=(
                f"Respaldo: "
                f"{os.path.basename(backup_file)}"
            ),
        )

        return JsonResponse(
            {
                "success": True,
                "message": (
                    "Respaldo creado correctamente"
                ),
            }
        )

    except Exception as e:

        return JsonResponse(
            {
                "success": False,
                "error": str(e),
            },
            status=500,
        )
