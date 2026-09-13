# booking/views.py
from __future__ import annotations

import json
import logging
from datetime import datetime, date, timedelta, time
from decimal import Decimal
from typing import Any

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError, PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction, IntegrityError
from django.db.models import Q, Count, Sum, Avg, F
from django.db.models.functions import TruncDate, ExtractHour
from django.http import JsonResponse, Http404, HttpRequest, HttpResponse
from django.shortcuts import render, get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.decorators.http import require_GET, require_POST
from django.views.decorators.csrf import csrf_exempt
from django.contrib.auth import logout

# ===== RATE LIMITING (OPCIONAL) =====
# from django_ratelimit.decorators import ratelimit

# ===== IMPORTACIONES UNIFICADAS DESDE core.decorators =====
from core.decorators import role_required, admin_required, supervisor_required, vendedor_required, cajero_required

from .models import (
    City, OwnerSettlement, OwnerSettlementHistory, OwnerSettlementTicket, Parcel, Promotion, Route, Trip, Bus, Seat, Ticket, SeatHold,
    Terminal, UserProfile, CashRegister, DailyReport,
    Customer, BusLayout, Driver, Company, FleetOwner,
    CompanyContract, ContractEmployee, AuditLog
)
from .forms import DriverForm
from booking.utils import validate_chilean_rut
from booking.access import tickets_for_user
from .models import Season, Promotion
from booking.models import Parcel
from django.core.cache import cache

logger = logging.getLogger(__name__)

# ============================================================================
# 1. HELPERS Y UTILIDADES
# ============================================================================

def _letters_for_cols(cols: int):
    base = [chr(65 + i) for i in range(26)]
    return base[:max(1, cols)]

def _parse_json(request: HttpRequest) -> dict:
    try:
        return json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        return {}

def calcular_tendencia(actual, anterior):
    if anterior == 0:
        return 100 if actual > 0 else 0
    return ((actual - anterior) / anterior) * 100

def _get_role_choices():
    try:
        choices = UserProfile._meta.get_field('role').choices
        if choices:
            return choices
    except Exception:
        pass
    return (
        ('admin', 'Administrador'),
        ('supervisor', 'Supervisor'),
        ('vendedor', 'Vendedor'),
        ('cajero', 'Cajero'),
        ('convenio', 'Gestor de Convenios'),
    )


def _resolve_user_profile_scope(request):
    """
    Valida y resuelve el alcance empresarial enviado desde
    Crear/Editar Usuario.

    MULTIEMPRESA:
    - Superuser puede seleccionar cualquier empresa.
    - Usuario administrativo normal queda forzado a su empresa.
    - role='owner' exige propietario/socio activo de esa empresa.
    - Para otros roles fleet_owner se limpia.
    """

    role = (
        request.POST.get("role")
        or ""
    ).strip()

    posted_company_id = (
        request.POST.get("company")
        or ""
    ).strip()

    fleet_owner_id = (
        request.POST.get("fleet_owner")
        or ""
    ).strip()

    # ============================================================
    # VALIDAR ROL
    # ============================================================
    valid_roles = {
        value
        for value, _label in _get_role_choices()
    }

    if role not in valid_roles:
        raise ValidationError(
            "Debe seleccionar un rol válido."
        )

    # ============================================================
    # RESOLVER EMPRESA
    # ============================================================
    if request.user.is_superuser:

        if not posted_company_id:
            raise ValidationError(
                "Debe seleccionar una empresa operadora."
            )

        company = (
            Company.objects
            .filter(
                pk=posted_company_id
            )
            .first()
        )

        if not company:
            raise ValidationError(
                "La empresa operadora seleccionada no existe."
            )

    else:

        actor_profile = getattr(
            request.user,
            "profile",
            None,
        )

        if (
            not actor_profile
            or not actor_profile.company_id
        ):
            raise PermissionDenied(
                "Su usuario no tiene una empresa asociada."
            )

        # Nunca confiar en company enviado por POST.
        company = actor_profile.company

    # ============================================================
    # PROPIETARIO / SOCIO
    # ============================================================
    fleet_owner = None

    if role == "owner":

        if not fleet_owner_id:
            raise ValidationError(
                "El rol Propietario / Socio requiere "
                "seleccionar un propietario asociado."
            )

        fleet_owner = (
            FleetOwner.objects
            .filter(
                pk=fleet_owner_id,
                company=company,
                is_active=True,
            )
            .first()
        )

        if not fleet_owner:
            raise ValidationError(
                "El propietario / socio no pertenece "
                "a la empresa seleccionada o se "
                "encuentra inactivo."
            )

    return role, company, fleet_owner


def _check_terminal_permission(request, trip):
    profile = getattr(request.user, 'profile', None)
    if profile and profile.terminal:
        if trip.route.origin_terminal != profile.terminal:
            raise PermissionDenied("No tienes permiso para vender en esta ruta.")
    return True

def _audit_log(request, action, model_name=None, object_id=None, object_repr=None, changes=None, success=True):
    try:
        AuditLog.objects.create(
            user=request.user if request.user.is_authenticated else None,
            action=action,
            model_name=model_name,
            object_id=str(object_id) if object_id else None,
            object_repr=object_repr or '',
            changes=changes or {},
            ip_address=request.META.get('REMOTE_ADDR'),
            user_agent=request.META.get('HTTP_USER_AGENT', ''),
            session_key=request.session.session_key if hasattr(request, 'session') else None,
        )
    except Exception as e:
        logger.error(f"Error registrando auditoría: {e}")

# ============================================================================
# 2. VISTAS PRINCIPALES DEL PUNTO DE VENTA (POS)
# ============================================================================
@login_required
@vendedor_required
def pos_home(request):
    now = timezone.localtime()
    q_date = (request.GET.get("date") or "").strip()
    origin_id = (request.GET.get("origin_id") or "").strip()
    dest_id = (request.GET.get("dest_id") or "").strip()

    did_search = bool(q_date or origin_id or dest_id)

    if q_date:
        try:
            the_date = datetime.strptime(q_date, "%Y-%m-%d").date()
        except ValueError:
            the_date = now.date()
    else:
        the_date = now.date()

    if not did_search:
        trips = Trip.objects.none()
    else:
        # ✅ OPTIMIZADO: Una sola consulta con subconsultas
        trips = Trip.objects.select_related(
            "route__origin", "route__destination", "bus"
        ).filter(
            departure__date=the_date,
            departure__gte=now,
        )

        if origin_id:
            if origin_id.isdigit():
                trips = trips.filter(route__origin_id=origin_id)
            else:
                city = City.objects.filter(name__iexact=origin_id).first()
                if city:
                    trips = trips.filter(route__origin_id=city.id)
                else:
                    trips = trips.none()

        if dest_id:
            if dest_id.isdigit():
                trips = trips.filter(route__destination_id=dest_id)
            else:
                city = City.objects.filter(name__iexact=dest_id).first()
                if city:
                    trips = trips.filter(route__destination_id=city.id)
                else:
                    trips = trips.none()

        # ✅ CORREGIDO: Anotaciones usando Subquery en lugar de F('bus__seats__count')
        from django.db.models import Subquery, OuterRef

        trips = trips.annotate(
            sold_count=Count('tickets', distinct=True),
            hold_count=Count('holds', filter=Q(holds__active=True), distinct=True),
            total_seats=Subquery(
                Seat.objects.filter(bus=OuterRef('bus')).values('bus').annotate(
                    total=Count('id')
                ).values('total')
            )
        )

        # Calcular asientos libres en Python
        for t in trips:
            total = t.total_seats or 0
            t.sold = t.sold_count or 0
            t.hold = t.hold_count or 0
            t.total = total
            t.free = max(total - t.sold - t.hold, 0)

    # ✅ OPTIMIZADO: Métricas con agregación
    fecha_hoy = timezone.now().date()
    ventas_hoy = Ticket.objects.filter(
        created_at__date=fecha_hoy,
        created_by=request.user
    ).select_related(
        'trip__route__origin',
        'trip__route__destination',
        'seat'
    )

    metrics = ventas_hoy.aggregate(
        total=Sum('price'),
        total_count=Count('id'),
        rutas_count=Count('trip__route', distinct=True)
    )

    total_ventas = metrics['total'] or Decimal('0.00')
    total_boletos = metrics['total_count'] or 0
    rutas_vendidas = metrics['rutas_count'] or 0
    promedio_venta = total_ventas / total_boletos if total_boletos > 0 else Decimal('0.00')

    # ✅ OPTIMIZADO: Solo traer los últimos 10 con select_related
    ventas_recientes = ventas_hoy.order_by('-created_at')[:10]

    # ✅ OPTIMIZADO: Métricas de ayer con una sola consulta
    ayer = fecha_hoy - timedelta(days=1)
    metrics_ayer = Ticket.objects.filter(
        created_at__date=ayer,
        created_by=request.user
    ).aggregate(
        total=Sum('price'),
        total_count=Count('id')
    )
    total_ventas_ayer = metrics_ayer['total'] or Decimal('0.00')
    total_boletos_ayer = metrics_ayer['total_count'] or 0

    tendencia_ventas = calcular_tendencia(total_ventas, total_ventas_ayer)
    tendencia_boletos = calcular_tendencia(total_boletos, total_boletos_ayer)

    caja_actual = CashRegister.objects.filter(
        user=request.user,
        opening_date__date=fecha_hoy,
        status='open'
    ).first()
    caja_abierta = caja_actual is not None

    ctx = {
        "title": "POS — Panel de Ventas",
        "date": the_date.strftime("%Y-%m-%d"),
        "cities": City.objects.all().order_by("name"),
        "origin_id": origin_id,
        "dest_id": dest_id,
        "trips": trips,
        "did_search": did_search,
        "total_ventas": total_ventas,
        "total_boletos": total_boletos,
        "rutas_vendidas": rutas_vendidas,
        "promedio_venta": promedio_venta,
        "ventas_recientes": ventas_recientes,
        "tendencia_ventas": tendencia_ventas,
        "tendencia_boletos": tendencia_boletos,
        "caja_abierta": caja_abierta,
        "caja_actual": caja_actual,
    }
    return render(request, "booking/pos_home.html", ctx)

@login_required
@vendedor_required
def pos_trip(request, trip_id):
    trip = get_object_or_404(Trip.objects.select_related("route__origin", "route__destination", "bus"), id=trip_id)
    if not trip.bus:
        return HttpResponse("Este viaje no tiene asignada una unidad de transporte (Bus).", status=400)

    try:
        _check_terminal_permission(request, trip)
    except PermissionDenied as e:
        messages.error(request, str(e))
        return redirect('pos_home')

    grid_lower, grid_upper, cols = _build_trip_grid(trip, request.user)

    context = {
        'trip': trip,
        'cols': cols,
        'grid_lower': json.dumps(grid_lower),
        'grid_upper': json.dumps(grid_upper),
        'mode': request.GET.get('mode', 'normal'),
    }
    response = render(request, "booking/pos_trip.html", context)
    response['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response['Pragma'] = 'no-cache'
    response['Expires'] = '0'
    return response

@xframe_options_exempt
@login_required
def pos_trip_modal(request, trip_id: int):
    trip = get_object_or_404(Trip.objects.select_related("route__origin", "route__destination", "bus"), pk=trip_id)
    grid_lower, grid_upper, cols = _build_trip_grid(trip, request.user)
    ctx = {
        "title": f"Croquis — {trip.route.origin.name} a {trip.route.destination.name}",
        "trip": trip,
        "bus": trip.bus,
        "cols": cols,
        "grid_lower": grid_lower,
        "grid_upper": grid_upper,
    }
    return render(request, "booking/seatmap.html", ctx)

# ============================================================================
# 3. MOTOR UNIFICADO DE MAPAS DE ASIENTOS
# ============================================================================

def _build_trip_grid(
    trip: Trip,
    current_user=None,
    session_key=None,
):
    """
    Construye los grids de asientos de un viaje.

    Estados posibles:
        - free      : asiento disponible
        - my-hold   : reservado temporalmente por esta sesión/usuario
        - occupied  : reservado temporalmente por otra sesión/usuario
        - sold      : asiento vendido definitivamente

    IMPORTANTE:
    Para venta web anónima, la propiedad del hold se determina
    principalmente mediante session_key.

    Para otros flujos que no utilicen session_key (por ejemplo POS),
    puede utilizarse current_user.

    La prioridad de estados es:

        SOLD
          ↓
        MY-HOLD
          ↓
        OCCUPIED
          ↓
        FREE
    """

    from django.db.models import (
        Case,
        When,
        Value,
        CharField,
        Q,
        OuterRef,
        Exists,
    )

    # ============================================================
    # 1. DATOS DEL BUS
    # ============================================================

    bus = trip.bus

    try:
        cols = int(bus.cols or 4)
    except (TypeError, ValueError):
        cols = 4

    if cols <= 0:
        return [], [], 0

    # Letras válidas según cantidad de columnas:
    # A, B, C, D...
    letters = _letters_for_cols(cols)

    pos2col = {
        str(ch).strip().upper(): index
        for index, ch in enumerate(letters)
    }

    now = timezone.now()

    # ============================================================
    # 2. SUBQUERY: ASIENTO VENDIDO
    # ============================================================
    #
    # Ticket es la fuente definitiva.
    #
    # Si existe Ticket para viaje + asiento,
    # el asiento está vendido aunque exista algún hold residual.
    # ============================================================

    sold_subq = Ticket.objects.filter(
        trip=trip,
        seat=OuterRef("pk"),
    ).values("pk")

    # ============================================================
    # 3. SUBQUERY: CUALQUIER HOLD ACTIVO
    # ============================================================

    hold_active_subq = SeatHold.objects.filter(
        trip=trip,
        seat=OuterRef("pk"),
        active=True,
        expires_at__gt=now,
    ).values("pk")

    # ============================================================
    # 4. DETERMINAR PROPIETARIO ACTUAL
    # ============================================================
    #
    # REGLA:
    #
    # Venta web:
    #     session_key
    #
    # POS / usuario autenticado sin session_key:
    #     current_user
    #
    # NO hacemos:
    #
    #     user=current_user
    #
    # cuando current_user es None, porque eso equivaldría a:
    #
    #     user IS NULL
    #
    # y todos los holds anónimos podrían parecer nuestros.
    # ============================================================

    owner_filter = None

    if session_key:

        owner_filter = Q(
            session_key=session_key
        )

    elif (
        current_user is not None
        and getattr(
            current_user,
            "is_authenticated",
            False,
        )
    ):

        owner_filter = Q(
            user=current_user
        )

    # ============================================================
    # 5. SUBQUERY: HOLD DEL PROPIETARIO ACTUAL
    # ============================================================

    hold_by_owner_subq = None

    if owner_filter is not None:

        hold_by_owner_subq = SeatHold.objects.filter(
            owner_filter,
            trip=trip,
            seat=OuterRef("pk"),
            active=True,
            expires_at__gt=now,
        ).values("pk")

    # ============================================================
    # 6. CONSTRUIR PRIORIDAD DE ESTADOS
    # ============================================================

    status_whens = [
        # --------------------------------------------------------
        # PRIORIDAD 1: VENDIDO
        # --------------------------------------------------------
        When(
            Exists(sold_subq),
            then=Value("sold"),
        ),
    ]

    # ------------------------------------------------------------
    # PRIORIDAD 2: MI RESERVA
    # ------------------------------------------------------------

    if hold_by_owner_subq is not None:

        status_whens.append(
            When(
                Exists(hold_by_owner_subq),
                then=Value("my-hold"),
            )
        )

    # ------------------------------------------------------------
    # PRIORIDAD 3: RESERVADO POR OTRO
    # ------------------------------------------------------------

    status_whens.append(
        When(
            Exists(hold_active_subq),
            then=Value("occupied"),
        )
    )

    # ============================================================
    # 7. OBTENER ASIENTOS CON ESTADO
    # ============================================================

    seats_annotated = (
        Seat.objects
        .filter(
            bus=bus
        )
        .annotate(
            status=Case(
                *status_whens,
                default=Value("free"),
                output_field=CharField(),
            )
        )
        .values(
            "id",
            "deck",
            "row",
            "position",
            "number",
            "seat_service",
            "status",
        )
    )

    # ============================================================
    # 8. CREAR ÍNDICE DE ASIENTOS POR PISO/POSICIÓN
    # ============================================================
    #
    # Ejemplo:
    #
    # index_by_deck[1][0] = asiento 1
    # index_by_deck[1][1] = asiento 2
    #
    # Esto evita hacer consultas a BD dentro del grid.
    # ============================================================

    index_by_deck = {
        1: {},
        2: {},
    }

    for seat_data in seats_annotated:

        try:
            deck_num = int(
                seat_data.get("deck") or 1
            )
        except (TypeError, ValueError):
            deck_num = 1

        # Ignorar pisos inválidos para evitar KeyError.
        if deck_num not in index_by_deck:
            continue

        try:
            row_num = int(
                seat_data.get("row") or 1
            )
        except (TypeError, ValueError):
            row_num = 1

        if row_num <= 0:
            row_num = 1

        position = (
            str(
                seat_data.get("position") or ""
            )
            .strip()
            .upper()
        )

        # Si la posición no existe, conservar el comportamiento
        # anterior utilizando la primera columna.
        col_num = pos2col.get(
            position,
            0,
        )

        index = (
            (row_num - 1) * cols
            + col_num
        )

        index_by_deck[deck_num][index] = seat_data

    # ============================================================
    # 9. FUNCIÓN INTERNA PARA CONSTRUIR CADA PISO
    # ============================================================

    def grid_for(
        deck: int,
        rows: int,
        flat_layout,
    ):
        """
        Convierte el layout plano del bus en una matriz
        consumible por seatmap.html.
        """

        output = []

        index = 0

        try:
            rows = int(rows or 0)
        except (TypeError, ValueError):
            rows = 0

        if rows <= 0:
            return output

        # Evitar errores si layout viene como None.
        if not flat_layout:
            flat_layout = []

        for _row in range(rows):

            row = []

            for _col in range(cols):

                # =================================================
                # TIPO DE CELDA
                # =================================================

                try:
                    cell_type = (
                        flat_layout[index]
                        if index < len(flat_layout)
                        else "L"
                    ) or "L"
                except (TypeError, IndexError):
                    cell_type = "L"

                cell_type = str(
                    cell_type
                ).strip().upper()

                # =================================================
                # VALORES POR DEFECTO
                # =================================================

                label = ""
                status = "free"
                hold_user = ""

                seat_id = None

                service = "semi_cama"

                # =================================================
                # CELDA DE ASIENTO
                # =================================================

                if cell_type == "L":

                    seat_data = (
                        index_by_deck
                        .get(deck, {})
                        .get(index)
                    )

                    if seat_data:

                        seat_id = seat_data.get(
                            "id"
                        )

                        label = str(
                            seat_data.get(
                                "number"
                            ) or ""
                        )

                        service = str(
                            seat_data.get(
                                "seat_service"
                            )
                            or "semi_cama"
                        ).lower()

                        status = (
                            seat_data.get(
                                "status"
                            )
                            or "free"
                        )

                        # =========================================
                        # NO HACER CONSULTA ADICIONAL A SeatHold
                        # =========================================
                        #
                        # Antes tenías:
                        #
                        # if status == "occupied" and current_user:
                        #     SeatHold.objects.filter(...)
                        #
                        # Esto provocaba consultas N+1 y además
                        # ignoraba session_key.
                        #
                        # El estado ya fue correctamente calculado
                        # mediante Exists() arriba.
                        # =========================================

                        if status == "occupied":
                            hold_user = "Otro usuario"

                        elif status == "sold":
                            hold_user = ""

                        elif status == "my-hold":
                            hold_user = ""

                # =================================================
                # AGREGAR CELDA AL GRID
                # =================================================

                row.append(
                    {
                        "id": seat_id,

                        "type": cell_type,

                        "label": label,

                        # Mantener number porque seatmap.html
                        # puede utilizar ambos nombres.
                        "number": label,

                        "status": status,

                        # Compatibilidad con templates existentes.
                        "service": service,
                        "seat_service": service,

                        "hold_user": hold_user,
                    }
                )

                index += 1

            output.append(row)

        return output

    # ============================================================
    # 10. PISO INFERIOR
    # ============================================================

    try:
        rows_lower = int(
            bus.rows_lower or 0
        )
    except (TypeError, ValueError):
        rows_lower = 0

    lower = grid_for(
        deck=1,
        rows=rows_lower,
        flat_layout=bus.layout_lower or [],
    )

    # ============================================================
    # 11. PISO SUPERIOR
    # ============================================================

    try:
        floors = int(
            bus.floors or 1
        )
    except (TypeError, ValueError):
        floors = 1

    upper = []

    if floors == 2:

        try:
            rows_upper = int(
                bus.rows_upper or 0
            )
        except (TypeError, ValueError):
            rows_upper = 0

        upper = grid_for(
            deck=2,
            rows=rows_upper,
            flat_layout=bus.layout_upper or [],
        )

    # ============================================================
    # 12. RESULTADO
    # ============================================================

    return lower, upper, cols
# ============================================================================
# 4. ENDPOINTS TRANSACCIONALES (API DE CONTROL DE SEATHOLDS)
# ============================================================================

@require_POST
@login_required
@vendedor_required
# @ratelimit(key='ip', rate='30/m', method='POST', block=True)
# @ratelimit(key='user', rate='100/m', method='POST', block=True)
def api_hold(request, trip_id):
    trip = get_object_or_404(Trip, id=trip_id)
    seat_id_front = request.POST.get('seat_id')
    if not seat_id_front:
        return JsonResponse({'ok': False, 'error': 'Identificador de asiento ausente.'}, status=400)

    try:
        _check_terminal_permission(request, trip)
        seat_obj = trip.bus.seats.filter(Q(id=seat_id_front) | Q(number=seat_id_front)).first()
        if not seat_obj:
            return JsonResponse({'ok': False, 'error': 'Asiento no encontrado.'}, status=404)

        hold = SeatHold.hold(trip, seat_obj, request.user, minutes=10)
        _audit_log(
            request,
            action='seat_hold',
            model_name='SeatHold',
            object_id=hold.id,
            object_repr=f"Asiento {seat_obj.number} reservado",
            changes={'trip_id': trip.id, 'seat_number': seat_obj.number}
        )
        return JsonResponse({'ok': True, 'expires_at': hold.expires_at.isoformat()})
    except ValueError as e:
        return JsonResponse({'ok': False, 'error': str(e)}, status=409)
    except PermissionDenied as e:
        return JsonResponse({'ok': False, 'error': str(e)}, status=403)
    except Exception as e:
        logger.error(f"Error en api_hold: {e}", exc_info=True)
        return JsonResponse({'ok': False, 'error': 'Error interno del servidor.'}, status=500)

@require_POST
@login_required
@vendedor_required
def api_release(request, trip_id):
    trip = get_object_or_404(Trip, id=trip_id)
    seat_id_front = request.POST.get('seat_id')

    try:
        _check_terminal_permission(request, trip)
        seat_obj = trip.bus.seats.filter(Q(id=seat_id_front) | Q(number=seat_id_front)).first()
        if not seat_obj:
            return JsonResponse({'ok': False, 'error': 'Asiento no encontrado.'}, status=404)

        deleted = SeatHold.objects.filter(trip=trip, seat=seat_obj, user=request.user).delete()
        if deleted[0] > 0:
            _audit_log(
                request,
                action='seat_release',
                model_name='SeatHold',
                object_repr=f"Asiento {seat_obj.number} liberado",
                changes={'trip_id': trip.id, 'seat_number': seat_obj.number}
            )
        return JsonResponse({'ok': True})
    except PermissionDenied as e:
        return JsonResponse({'ok': False, 'error': str(e)}, status=403)
    except Exception as e:
        logger.error(f"Error en api_release: {e}", exc_info=True)
        return JsonResponse({'ok': False, 'error': 'Error interno del servidor.'}, status=500)

# ============================================================================
# 5. CIERRE DE CAJA, CHECKOUT Y EMISIÓN DE PASAJES
# ============================================================================

@login_required
@vendedor_required
@transaction.atomic
def pos_checkout(request: HttpRequest, trip_id: int = None):
    """
    Procesa el checkout de la venta POS.
    """
    import json
    from django.contrib import messages
    from django.conf import settings
    from django.db import transaction

    print("=" * 60)
    print("🚀 POS_CHECKOUT - Iniciando proceso de venta")
    print(f"📌 trip_id: {trip_id}")
    print(f"📌 Método: {request.method}")
    print(f"📌 POST data recibida: {request.POST}")
    print("=" * 60)

    # ===== VALIDACIÓN 1: Método POST =====
    if request.method != "POST":
        print("❌ Error: Método no POST")
        messages.error(request, "Acción u método de envío inválido.")
        return redirect("pos_home")
    print("✅ Paso 1: Método POST OK")

    # ===== VALIDACIÓN 2: Método de pago =====
    payment_method = (request.POST.get("payment_method") or "").strip()
    if payment_method not in ("cash", "card", "credit"):
        print(f"❌ Error: Método de pago inválido: {payment_method}")
        messages.error(request, "Debe seleccionar un método válido: Efectivo o Tarjeta.")
        return redirect("pos_trip", trip_id=trip_id or request.POST.get("trip_id"))
    print(f"✅ Paso 2: Método de pago OK: {payment_method}")

    # ===== VALIDACIÓN 3: Caja abierta =====
    today = timezone.now().date()
    cash_register = CashRegister.objects.filter(
        user=request.user,
        status='open',
        opening_date__date=today
    ).first()

    if not cash_register:
        # En desarrollo, crear caja automáticamente
        if settings.DEBUG:
            print(f"⚠️ Caja no abierta - Creando automáticamente (MODO DEBUG)")
            cash_register = CashRegister.objects.create(
                user=request.user,
                opening_balance=0,
                status='open'
            )
            print(f"✅ Caja creada automáticamente (ID: {cash_register.id})")
        else:
            print(f"❌ Error: Caja no abierta para usuario {request.user.username}")
            messages.error(request, "Debes abrir la caja antes de realizar ventas.")
            return redirect('pos_caja')
    else:
        print(f"✅ Paso 3: Caja abierta OK (ID: {cash_register.id})")

    # ===== VALIDACIÓN 4: Obtener viaje =====
    tid = trip_id or request.POST.get("trip_id")
    try:
        trip = get_object_or_404(
            Trip.objects.select_for_update().select_related(
                'route', 'bus', 'route__origin', 'route__destination'
            ),
            pk=tid
        )
        print(f"✅ Paso 4: Viaje encontrado ID: {trip.id}")
    except Exception as e:
        print(f"❌ Error obteniendo viaje: {e}")
        messages.error(request, "Viaje no encontrado.")
        return redirect("pos_home")

    # ===== VALIDACIÓN 5: Permisos de terminal =====
    try:
        _check_terminal_permission(request, trip)
        print("✅ Paso 5: Permisos de terminal OK")
    except PermissionDenied as e:
        print(f"❌ Error: Permiso denegado - {e}")
        messages.error(request, str(e))
        return redirect("pos_home")

    # ===== VALIDACIÓN 6: Código de descuento =====
    discount_code = request.POST.get("discount_code", "").strip()
    print(f"📌 Código descuento: {discount_code}")

    # ===== VALIDACIÓN 7: Parsear asientos =====
    raw_seats = request.POST.get("seats", "").strip()
    print(f"📌 Raw seats: {raw_seats}")

    try:
        chosen = json.loads(raw_seats) if raw_seats else []
        print(f"📌 Asientos parseados: {chosen}")
    except Exception as e:
        print(f"❌ Error parseando seats: {e}")
        chosen = []

    if not isinstance(chosen, list) or not chosen:
        print("❌ Error: No hay asientos seleccionados")
        messages.error(request, "No seleccionó ningún asiento para procesar la transacción.")
        return redirect("pos_trip", trip_id=trip.id)
    print(f"✅ Paso 7: {len(chosen)} asientos seleccionados")

    # ===== CALCULAR PRECIOS =====
    total_amount = trip.route.base_price * len(chosen)
    final_price_per_ticket, applied_discount = calculate_final_price(
        trip, discount_code if discount_code else None, total_amount
    )
    print(f"💰 Precio por ticket: ${final_price_per_ticket}")
    print(f"💰 Total: ${total_amount}")
    print(f"💰 Descuento aplicado: {applied_discount.code if applied_discount else 'Ninguno'}")

    if discount_code and applied_discount is None:
        print("❌ Error: Código de descuento inválido")
        messages.error(request, "El código promocional ingresado no es válido, ha expirado o no alcanza el monto mínimo.")
        return redirect("pos_trip", trip_id=trip.id)

    # ===== CREAR TICKETS (DIRECTAMENTE CON objects.create) =====
    created_tickets = []
    skipped = []

    # Obtener el siguiente número de ticket
    def get_next_ticket_number():
        from django.db import connection
        with connection.cursor() as cursor:
            cursor.execute("SELECT nextval('ticket_number_seq')")
            next_id = cursor.fetchone()[0]
            return f"T-{next_id:06d}"

    for idx, item in enumerate(chosen):
        try:
            deck = int(item.get("deck", 1))
            number = str(item.get("number", "")).strip()
            passenger_rut = item.get("passenger_rut", "").strip()
            passenger_name = item.get("passenger_name", "").strip()
            passenger_phone = item.get("passenger_phone", "").strip()

            print(f"🔍 Procesando asiento {idx+1}/{len(chosen)}: {number} (deck {deck}) - Pasajero: {passenger_name}")
        except (ValueError, TypeError) as e:
            print(f"❌ Error parseando item: {e}")
            skipped.append(item.get("number", "desconocido"))
            continue

        if not number:
            print("❌ Asiento sin número")
            skipped.append("sin número")
            continue

        # Bloquear asiento
        try:
            seat = Seat.objects.select_for_update().get(
                bus=trip.bus,
                number=number,
                deck=deck
            )
            print(f"✅ Asiento encontrado: {seat.number} (ID: {seat.id})")
        except Seat.DoesNotExist:
            print(f"❌ Asiento no existe: {number} (deck {deck})")
            skipped.append(number)
            continue

        # Verificar si está ocupado
        if Ticket.objects.filter(trip=trip, seat=seat).exists():
            print(f"❌ Asiento ya vendido: {number}")
            skipped.append(number)
            continue

        # Verificar holds de otros usuarios
        active_hold = SeatHold.objects.filter(
            trip=trip,
            seat=seat,
            active=True
        ).exclude(user=request.user).first()

        if active_hold:
            print(f"❌ Asiento reservado por otro usuario: {number}")
            skipped.append(number)
            continue

        # ✅ CREAR TICKET DIRECTAMENTE
        try:
            # Liberar hold del usuario actual
            SeatHold.objects.filter(
                trip=trip,
                seat=seat,
                user=request.user,
                active=True
            ).update(active=False)

            # Crear cliente
            customer = None
            if passenger_rut:
                try:
                    customer, created = Customer.objects.get_or_create(
                        national_id=passenger_rut,
                        defaults={'full_name': passenger_name or "Pasajero"}
                    )
                    print(f"✅ Cliente {'creado' if created else 'encontrado'}: {customer.full_name}")
                except Exception as e:
                    print(f"❌ Error creando cliente: {e}")
                    customer = None

            # =========================================================
            # FASE 2.18.3-A2.3.2
            # Emisión centralizada para congelar revenue_bus/revenue_owner
            # =========================================================
            ticket_number = get_next_ticket_number()

            ticket = Ticket.create_for_sale(
                trip=trip,
                seat=seat,
                number=ticket_number,
                buyer_name=passenger_name or "Pasajero",
                national_id=passenger_rut or "",
                price=final_price_per_ticket,
                created_by=request.user,
                payment_method=payment_method,
                customer=customer,
            )
            created_tickets.append(ticket)
            print(f"✅ Ticket creado: {ticket.number} - Asiento {number} - Precio: ${ticket.price}")

        except Exception as e:
            print(f"❌ Error creando ticket para asiento {number}: {e}")
            import traceback
            traceback.print_exc()
            skipped.append(number)

    # ===== VERIFICAR RESULTADO =====
    if not created_tickets:
        error_msg = f"Error: Los asientos solicitados ({', '.join(skipped)}) ya fueron vendidos."
        print(f"❌ {error_msg}")
        messages.error(request, error_msg)
        return redirect("pos_trip", trip_id=trip.id)

    print(f"✅ Paso 8: {len(created_tickets)} tickets creados exitosamente")

    # ===== LIMPIAR HOLDS =====
    SeatHold.objects.filter(
        trip=trip,
        seat__in=[t.seat for t in created_tickets]
    ).delete()
    print(f"🗑️ Holds eliminados para {len(created_tickets)} asientos")

    # ===== ACTUALIZAR DESCUENTO =====
    if applied_discount:
        applied_discount.used_count += 1
        applied_discount.save(update_fields=['used_count'])
        print(f"✅ Descuento aplicado: {applied_discount.code} (usos: {applied_discount.used_count})")

    total = final_price_per_ticket * len(created_tickets)

    # ===== GUARDAR EN SESIÓN =====
    request.session['last_ticket_ids'] = [t.id for t in created_tickets]
    request.session['last_original_total'] = float(total_amount)
    request.session['last_discount_amount'] = float(total_amount - total)
    request.session['last_payment_method'] = payment_method
    request.session['last_buyer_name'] = created_tickets[0].buyer_name if created_tickets else "Cliente General"
    print(f"📌 Sesión guardada: last_ticket_ids = {request.session['last_ticket_ids']}")

    # ===== AUDITORÍA =====
    _audit_log(
        request,
        action='purchase',
        model_name='Ticket',
        object_id=','.join(str(t.id) for t in created_tickets),
        object_repr=f"Compra de {len(created_tickets)} tickets",
        changes={
            'trip_id': trip.id,
            'total': float(total),
            'payment_method': payment_method,
            'discount_applied': applied_discount.discount_value if applied_discount else 0
        }
    )

    print(f"✅ VENTA EXITOSA! {len(created_tickets)} tickets creados")
    print(f"📌 IDs de tickets: {[t.id for t in created_tickets]}")
    print(f"📌 Total: ${total:,.0f}")
    print("=" * 60)

    messages.success(request, f"¡Venta exitosa! {len(created_tickets)} pasaje(s) emitido(s).")
    return redirect('pos_confirmation', trip_id=trip.id)

@login_required
@vendedor_required
def pos_confirmation(request, trip_id):
    """
    Página de confirmación después de una venta exitosa.
    Muestra los tickets recién creados.
    """
    trip = get_object_or_404(Trip.objects.select_related(
        'route__origin', 'route__destination', 'bus'
    ), pk=trip_id)

    # Recuperar IDs de tickets de la sesión
    ticket_ids = request.session.get('last_ticket_ids', [])
    tickets = Ticket.objects.filter(id__in=ticket_ids).select_related('seat', 'customer')

    if not tickets.exists():
        # Si no hay tickets en sesión, mostrar últimos del viaje
        tickets = Ticket.objects.filter(trip=trip).order_by('-created_at')[:10]

    total_amount = sum(t.price for t in tickets)
    ticket_count = tickets.count()

    # Recuperar datos de la sesión
    payment_method = request.session.get('last_payment_method', 'cash')
    buyer_name = request.session.get('last_buyer_name', 'Cliente General')
    discount_amount = request.session.get('last_discount_amount', 0)
    original_total = request.session.get('last_original_total', total_amount)

    seats_info = []
    for t in tickets:
        seats_info.append({
            'number': t.seat.number,
            'deck': getattr(t.seat, 'deck', 1),
            'price': t.price,
            'passenger': t.buyer_name,
            'rut': t.national_id,
        })

    context = {
        'title': 'Venta Confirmada',
        'trip': trip,
        'tickets': tickets,
        'tickets_count': ticket_count,
        'total_amount': total_amount,
        'original_total': original_total,
        'discount_amount': discount_amount,
        'payment_method': payment_method,
        'buyer_name': buyer_name,
        'seats_info': seats_info,
        'ticket_ids': ticket_ids,
    }

    return render(request, 'booking/pos_confirmation.html', context)

# ============================================================================
# Función para calcular precio final
# ============================================================================
def calculate_final_price(trip, discount_code=None, total_amount=None):
    """Calcula el precio final con caching."""
    base_price = trip.route.base_price

    # ✅ CACHE PARA SEASON
    today = timezone.now().date()
    cache_key_season = f"season_{today.isoformat()}"
    season = cache.get(cache_key_season)

    if season is None:
        season = Season.objects.filter(
            start_date__lte=today,
            end_date__gte=today,
            is_active=True
        ).first()
        cache.set(cache_key_season, season, 3600)  # Cache por 1 hora

    if season:
        base_price = base_price * season.multiplier

    # ✅ CACHE PARA PROMOTION
    promotion = None
    if discount_code:
        cache_key_promo = f"promo_{discount_code}_{today.isoformat()}"
        promotion = cache.get(cache_key_promo)

        if promotion is None:
            promotion = Promotion.objects.filter(
                code=discount_code,
                is_active=True
            ).first()

            if promotion:
                # Validar condiciones
                if promotion.valid_from and promotion.valid_from > today:
                    promotion = None
                elif promotion.valid_to and promotion.valid_to < today:
                    promotion = None
                elif promotion.max_uses > 0 and promotion.used_count >= promotion.max_uses:
                    promotion = None
                elif total_amount is not None and total_amount < promotion.min_purchase_amount:
                    promotion = None

            # Cache por 5 minutos (por si cambia el uso)
            cache.set(cache_key_promo, promotion, 300)

    if promotion:
        final_price = base_price * (1 - promotion.discount_value / 100)
    else:
        final_price = base_price

    return final_price, promotion

# ============================================================================
# 6. APIS EXPUESTAS Y CONSUMOS EXTERNOS
# ============================================================================

@require_GET
def trip_seats(request: HttpRequest, trip_id: int):
    try:
        trip = Trip.objects.select_related("bus", "route__origin", "route__destination").get(pk=trip_id)
    except Trip.DoesNotExist:
        return JsonResponse({"error": "El viaje especificado no existe."}, status=404)

    bus = trip.bus
    letters = _letters_for_cols(getattr(bus, "cols", 4))
    pos2col = {ch: i for i, ch in enumerate(letters)}

    tickets = set(Ticket.objects.filter(trip=trip).values_list("seat_id", flat=True))
    holds = set(SeatHold.objects.filter(trip=trip, expires_at__gte=timezone.now()).values_list("seat_id", flat=True))

    seats_qs = Seat.objects.filter(bus=bus).order_by("deck", "row", "position", "number")
    seats = []

    for s in seats_qs:
        col_idx = pos2col.get(s.position) + 1 if isinstance(s.position, str) and s.position in pos2col else (int(s.position) if str(s.position).isdigit() else 1)
        seats.append({
            "id": s.id, "numero": s.number, "piso": int(s.deck or 1), "fila": int(s.row or 0),
            "columna": col_idx, "ventana": bool(s.is_window),
            "ocupado": (s.id in tickets) or (s.id in holds),
        })

    return JsonResponse({
        "viaje": {
            "id": trip.id, "origen": trip.route.origin.name, "destino": trip.route.destination.name,
            "salida": trip.departure.isoformat(), "llegada": trip.arrival.isoformat() if trip.arrival else None,
            "precio_asiento": str(getattr(trip.route, "base_price", "0")),
            "bus": {"id": bus.id, "plate": bus.plate, "floors": getattr(bus, "floors", 1)},
        },
        "asientos": seats,
    }, json_dumps_params={"ensure_ascii": False})

@require_GET
def api_cities(request):
    return JsonResponse(list(City.objects.order_by("name").values("id", "name")), safe=False, json_dumps_params={"ensure_ascii": False})

@require_GET
def api_search_trips(request):
    origen = (request.GET.get("origen") or "").strip()
    destino = (request.GET.get("destino") or "").strip()
    fecha = (request.GET.get("fecha") or "").strip()

    if not (origen and destino and fecha):
        return JsonResponse({"error": "Faltan parámetros obligatorios: origen, destino, fecha"}, status=400)

    try:
        d = datetime.strptime(fecha, "%Y-%m-%d").date()
    except ValueError:
        return JsonResponse({"error": "Formato de fecha inválido. Utilice el formato YYYY-MM-DD"}, status=400)

    start = timezone.make_aware(datetime.combine(d, time.min))
    end = timezone.make_aware(datetime.combine(d, time.max))

    qs = Trip.objects.select_related("route__origin", "route__destination", "bus").filter(
        route__origin__name__iexact=origen, route__destination__name__iexact=destino,
        departure__range=(start, end),
    ).order_by("departure")

    results = []
    for trip in qs:
        sold = Ticket.objects.filter(trip=trip).count()
        total = int(trip.seats_total or 0)
        results.append({
            "id": trip.id, "origen": trip.route.origin.name, "destino": trip.route.destination.name,
            "salida": trip.departure.isoformat(), "llegada": trip.arrival.isoformat() if trip.arrival else None,
            "base_price": str(getattr(trip.route, "base_price", "0")),
            "bus": {"id": trip.bus.id, "plate": trip.bus.plate},
            "seats_total": total, "seats_sold": sold, "seats_free": max(total - sold, 0),
        })
    return JsonResponse(results, safe=False, json_dumps_params={"ensure_ascii": False})

def bus_seatmap(request, pk: int):
    bus = get_object_or_404(Bus, pk=pk)
    return render(request, "booking/seatmap.html", {"bus": bus})

@require_GET
def api_cities_search(request):
    query = request.GET.get('q', '').strip()
    if len(query) < 2:
        return JsonResponse([], safe=False)
    cities = City.objects.filter(name__icontains=query)[:10]
    results = [{'id': c.id, 'name': c.name} for c in cities]
    return JsonResponse(results, safe=False)

@require_GET
@login_required
def validate_discount(request):
    code = request.GET.get('code', '').strip()
    total_amount_str = request.GET.get('total', '')
    trip_id = request.GET.get('trip_id')

    if total_amount_str:
        try:
            total_amount = float(total_amount_str)
        except ValueError:
            total_amount = 0
    else:
        total_amount = 0

    if total_amount == 0 and trip_id:
        try:
            trip_id_int = int(trip_id)
            holds = SeatHold.objects.filter(
                trip_id=trip_id_int,
                user=request.user,
                active=True,
                expires_at__gt=timezone.now()
            )
            if holds.exists():
                trip = holds.first().trip
                base_price = trip.route.base_price
                total_amount = holds.count() * float(base_price)
        except (ValueError, Trip.DoesNotExist):
            pass

    if not code:
        return JsonResponse({'valid': False, 'error': 'Código vacío'})

    promotion = Promotion.objects.filter(code__iexact=code, is_active=True).first()
    if not promotion:
        return JsonResponse({'valid': False, 'error': 'Código inválido'})

    is_valid, msg = promotion.is_valid(total_amount)
    if not is_valid:
        return JsonResponse({'valid': False, 'error': msg})

    discount_value = float(promotion.discount_value)
    if promotion.discount_type == 'percentage':
        return JsonResponse({'valid': True, 'percentage': discount_value})
    else:
        return JsonResponse({'valid': True, 'fixed_amount': discount_value})

# ============================================================================
# 7. SISTEMA DE CAJA
# ============================================================================

@login_required
@cajero_required
def pos_caja(request):
    fecha_str = request.GET.get('fecha', '')
    if fecha_str:
        try:
            fecha = datetime.strptime(fecha_str, '%Y-%m-%d').date()
        except ValueError:
            fecha = timezone.now().date()
    else:
        fecha = timezone.now().date()

    es_admin_o_supervisor = (
        request.user.is_superuser or
        (hasattr(request.user, 'profile') and
         request.user.profile.role in ['admin', 'supervisor'])
    )

    if es_admin_o_supervisor:
        cajas_abiertas = CashRegister.objects.filter(opening_date__date=fecha, status='open').select_related('user')
        caja_actual = cajas_abiertas.filter(user=request.user).first() or cajas_abiertas.first()
        caja_abierta = cajas_abiertas.exists()
        ventas_hoy = Ticket.objects.filter(created_at__date=fecha)
    else:
        cajas_abiertas = CashRegister.objects.none()
        caja_actual = CashRegister.objects.filter(user=request.user, opening_date__date=fecha, status='open').first()
        caja_abierta = caja_actual is not None
        ventas_hoy = Ticket.objects.filter(created_at__date=fecha, created_by=request.user)

    # ===== MÉTRICAS GENERALES =====
    metrics = ventas_hoy.aggregate(
        total=Sum('price'),
        total_count=Count('id')
    )
    total_ventas = metrics['total'] or Decimal('0.00')
    total_boletos = metrics['total_count'] or 0
    rutas_vendidas = ventas_hoy.values('trip__route').distinct().count()
    promedio_venta = total_ventas / total_boletos if total_boletos > 0 else Decimal('0.00')

    # ===== VENTAS POR MÉTODO DE PAGO (CORREGIDO) =====
    efectivo_hoy = ventas_hoy.filter(payment_method='cash').aggregate(
        total=Sum('price')
    )['total'] or Decimal('0.00')

    tarjeta_hoy = ventas_hoy.filter(payment_method='card').aggregate(
        total=Sum('price')
    )['total'] or Decimal('0.00')

    # Transferencia - si existe en tus choices, sino usar 'transfer'
    transferencia_hoy = ventas_hoy.filter(payment_method='transfer').aggregate(
        total=Sum('price')
    )['total'] or Decimal('0.00')

    # Crédito Convenio
    convenio_hoy = ventas_hoy.filter(payment_method='credit').aggregate(
        total=Sum('price')
    )['total'] or Decimal('0.00')

    # ===== VENTAS RECIENTES =====
    ventas_recientes = ventas_hoy.select_related(
        'trip__route__origin', 'trip__route__destination', 'seat', 'customer'
    ).order_by('-created_at')[:15]

    # ===== TENDENCIA (comparación con ayer) =====
    ayer = fecha - timedelta(days=1)
    if es_admin_o_supervisor:
        ventas_ayer = Ticket.objects.filter(created_at__date=ayer)
    else:
        ventas_ayer = Ticket.objects.filter(created_at__date=ayer, created_by=request.user)

    metrics_ayer = ventas_ayer.aggregate(
        total=Sum('price'),
        total_count=Count('id')
    )
    total_ventas_ayer = metrics_ayer['total'] or Decimal('0.00')
    total_boletos_ayer = metrics_ayer['total_count'] or 0

    tendencia_ventas = calcular_tendencia(total_ventas, total_ventas_ayer)
    tendencia_boletos = calcular_tendencia(total_boletos, total_boletos_ayer)

    # ===== PERFIL DE USUARIO =====
    user_profile = getattr(request.user, 'profile', None)
    user_role = user_profile.get_role_display() if user_profile else "Vendedor"

    # ===== ESTADÍSTICAS DE CAJAS ABIERTAS =====
    cajas_abiertas_count = cajas_abiertas.count() if es_admin_o_supervisor else 0

    context = {
        'title': 'POS — Panel de Caja',
        'hoy': fecha.strftime('%Y-%m-%d'),
        'total_ventas': total_ventas,
        'total_boletos': total_boletos,
        'rutas_vendidas': rutas_vendidas,
        'promedio_venta': promedio_venta,
        'ventas_recientes': ventas_recientes,
        'caja_abierta': caja_abierta,
        'caja_actual': caja_actual,
        'tendencia_ventas': tendencia_ventas,
        'tendencia_boletos': tendencia_boletos,
        'es_admin_o_supervisor': es_admin_o_supervisor,
        'cajas_abiertas': cajas_abiertas,
        'user_profile': user_profile,
        'user_role': user_role,
        'usuarios_con_caja_abierta': cajas_abiertas_count,
        # ===== NUEVOS CAMPOS PARA MÉTODOS DE PAGO =====
        'efectivo_hoy': efectivo_hoy,
        'tarjeta_hoy': tarjeta_hoy,
        'transferencia_hoy': transferencia_hoy,
        'convenio_hoy': convenio_hoy,
    }
    return render(request, 'booking/pos_caja.html', context)

@login_required
@require_POST
def abrir_caja(request):
    try:
        fecha_hoy = timezone.now().date()
        if CashRegister.objects.filter(user=request.user, opening_date__date=fecha_hoy, status='open').exists():
            return JsonResponse({'success': False, 'error': 'Ya posees un turno de caja abierto para el día de hoy.'})

        try:
            data = json.loads(request.body.decode('utf-8') or "{}")
            opening_balance = Decimal(str(data.get('opening_balance', 0)))
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            opening_balance = Decimal('0.00')

        if opening_balance < 0:
            return JsonResponse({'success': False, 'error': 'El saldo de apertura no puede ser un valor negativo.'})

        caja = CashRegister.objects.create(
            user=request.user,
            opening_balance=opening_balance,
            status='open'
        )
        return JsonResponse({
            'success': True,
            'message': f'Caja habilitada correctamente. Saldo Inicial: ${opening_balance:.2f}'
        })
    except Exception as e:
        logger.error(f"Error en abrir_caja: {e}", exc_info=True)
        return JsonResponse({'success': False, 'error': 'Error interno del servidor.'}, status=500)

@login_required
@require_POST
@transaction.atomic
def cerrar_caja(request):
    try:
        fecha_hoy = timezone.now().date()

        # ✅ Verificar permisos
        es_admin_o_supervisor = (
            request.user.is_superuser or
            (hasattr(request.user, 'profile') and
             request.user.profile.role in ['admin', 'supervisor'])
        )

        # ✅ Solo permitir cerrar caja propia o si es admin/supervisor
        if es_admin_o_supervisor:
            caja = CashRegister.objects.select_for_update().get(
                opening_date__date=fecha_hoy,
                status='open'
            )
            # Si el usuario es admin, puede cerrar cualquier caja
            if not request.user.is_superuser:
                caja = CashRegister.objects.select_for_update().get(
                    user=request.user,
                    opening_date__date=fecha_hoy,
                    status='open'
                )
        else:
            caja = CashRegister.objects.select_for_update().get(
                user=request.user,
                opening_date__date=fecha_hoy,
                status='open'
            )

        ventas_hoy = Ticket.objects.filter(
            created_at__date=fecha_hoy,
            created_by=caja.user
        )
        total_ventas = ventas_hoy.aggregate(total=Sum('price'))['total'] or Decimal('0.00')
        total_boletos = ventas_hoy.count()

        caja.closing_date = timezone.now()
        caja.total_sales = total_ventas
        caja.total_tickets = total_boletos
        caja.closing_balance = total_ventas
        caja.status = 'closed'
        caja.save()

        reporte, _ = DailyReport.objects.get_or_create(date=fecha_hoy)
        DailyReport.objects.filter(id=reporte.id).update(
            total_tickets=F('total_tickets') + total_boletos,
            total_revenue=F('total_revenue') + total_ventas,
            total_cash_registers=CashRegister.objects.filter(
                opening_date__date=fecha_hoy,
                status='closed'
            ).count()
        )

        # ✅ Log de auditoría
        _audit_log(
            request,
            action='close_cash_register',
            model_name='CashRegister',
            object_id=caja.id,
            object_repr=f"Caja de {caja.user.username} cerrada",
            changes={
                'user': caja.user.username,
                'total_sales': float(total_ventas),
                'total_tickets': total_boletos
            }
        )

        return JsonResponse({
            'success': True,
            'message': f'Caja clausurada con éxito. Recaudación Total: ${total_ventas:.2f}, Tickets Emitidos: {total_boletos}'
        })
    except CashRegister.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'No se encontró ningún registro de caja abierto.'})
    except Exception as e:
        logger.error(f"Error en cerrar_caja: {e}", exc_info=True)
        return JsonResponse({'success': False, 'error': 'Error interno del servidor.'})

# ============================================================================
# 8. SISTEMA DE REPORTES
# ============================================================================

@login_required
@supervisor_required
def pos_reportes(request):
    """
    FASE 2.18.3-A2.3.3-B

    Reportes POS respetando el alcance empresarial del usuario.

    - Superusuario:
        acceso global.

    - Admin / Supervisor normal:
        sólo tickets pertenecientes a su empresa económica.

    - La fuente económica se obtiene mediante tickets_for_user(),
      que utiliza revenue_bus / revenue_owner cuando corresponde.

    IMPORTANTE:
    Los filtros posteriores (usuario, fecha, vendedor, etc.)
    se aplican SIEMPRE sobre el queryset ya autorizado.
    """

    fecha_inicio_str = request.GET.get(
        'fecha_inicio',
        ''
    )
    fecha_fin_str = request.GET.get(
        'fecha_fin',
        ''
    )
    usuario_id = request.GET.get(
        'usuario',
        ''
    )

    # ============================================================
    # RANGO DE FECHAS
    # ============================================================

    if not fecha_inicio_str or not fecha_fin_str:
        fecha_fin = timezone.now().date()
        fecha_inicio = (
            fecha_fin - timedelta(days=7)
        )
    else:
        try:
            fecha_inicio = datetime.strptime(
                fecha_inicio_str,
                '%Y-%m-%d'
            ).date()

            fecha_fin = datetime.strptime(
                fecha_fin_str,
                '%Y-%m-%d'
            ).date()

        except ValueError:
            fecha_fin = timezone.now().date()
            fecha_inicio = (
                fecha_fin - timedelta(days=7)
            )

    # ============================================================
    # PERFIL / ROL
    # ============================================================

    user_profile = getattr(
        request.user,
        'profile',
        None,
    )

    es_admin_o_supervisor = (
        request.user.is_superuser
        or (
            user_profile
            and user_profile.role
            in ['admin', 'supervisor']
        )
    )

    # ============================================================
    # QUERYSET BASE
    # ============================================================
    # MUY IMPORTANTE:
    # Primero construimos el queryset por fecha.
    #
    # Después tickets_for_user() aplica el alcance autorizado.
    #
    # Todo filtro posterior se deriva de este queryset.
    # ============================================================

    ventas_base = Ticket.objects.filter(
        created_at__date__range=[
            fecha_inicio,
            fecha_fin,
        ]
    ).select_related(
        'trip__route__origin',
        'trip__route__destination',
        'created_by',
        'revenue_bus',
        'revenue_owner',
    )

    ventas = tickets_for_user(
        request.user,
        ventas_base,
    )

    # ============================================================
    # FILTRO POR VENDEDOR
    # ============================================================

    if usuario_id and usuario_id != 'todos':
        ventas = ventas.filter(
            created_by_id=usuario_id
        )

    # ============================================================
    # MÉTRICAS GENERALES
    # ============================================================

    stats = ventas.aggregate(
        total_ventas=Sum('price'),
        total_boletos=Count('id'),
        ticket_promedio=Avg('price'),
    )

    total_ventas = (
        stats['total_ventas']
        or Decimal('0.00')
    )

    total_boletos = (
        stats['total_boletos']
        or 0
    )

    ticket_promedio_general = (
        stats['ticket_promedio']
        or Decimal('0.00')
    )

    dias_rango = (
        fecha_fin - fecha_inicio
    ).days + 1

    promedio_diario = (
        total_ventas / dias_rango
        if dias_rango > 0
        else Decimal('0.00')
    )

    # ============================================================
    # VENTAS POR DÍA
    # ============================================================

    ventas_por_dia = (
        ventas
        .annotate(
            fecha=TruncDate('created_at')
        )
        .values('fecha')
        .annotate(
            total=Sum('price'),
            cantidad=Count('id'),
        )
        .order_by('fecha')
    )

    ventas_por_dia_procesadas = [
        {
            'fecha': item['fecha'],
            'total': item['total'],
            'cantidad': item['cantidad'],
            'promedio': (
                item['total']
                / item['cantidad']
                if item['cantidad'] > 0
                else Decimal('0.00')
            ),
        }
        for item in ventas_por_dia
    ]

    # ============================================================
    # RUTAS POPULARES
    # ============================================================

    rutas_populares = (
        ventas
        .values(
            'trip__route__origin__name',
            'trip__route__destination__name',
        )
        .annotate(
            total=Sum('price'),
            cantidad=Count('id'),
            promedio=Avg('price'),
        )
        .order_by('-total')[:10]
    )

    # ============================================================
    # VENTAS POR HORA
    # ============================================================

    ventas_por_hora = (
        ventas
        .annotate(
            hora=ExtractHour('created_at')
        )
        .values('hora')
        .annotate(
            total=Sum('price'),
            cantidad=Count('id'),
            promedio=Avg('price'),
        )
        .order_by('hora')
    )

    # ============================================================
    # VENTAS POR VENDEDOR
    # ============================================================
    # IMPORTANTE:
    # Ya NO usamos Ticket.objects.filter(...) nuevamente.
    #
    # Se parte del queryset autorizado para evitar mezclar
    # vendedores/tickets de otras empresas.
    # ============================================================

    ventas_por_vendedor = []

    if es_admin_o_supervisor:

        ventas_vendedor_base = (
            Ticket.objects
            .filter(
                created_at__date__range=[
                    fecha_inicio,
                    fecha_fin,
                ]
            )
        )

        ventas_vendedor_base = tickets_for_user(
            request.user,
            ventas_vendedor_base,
        )

        ventas_por_vendedor = (
            ventas_vendedor_base
            .values(
                'created_by__id',
                'created_by__username',
                'created_by__first_name',
                'created_by__last_name',
            )
            .annotate(
                total=Sum('price'),
                cantidad=Count('id'),
                promedio=Avg('price'),
            )
            .order_by('-total')
        )

    # ============================================================
    # USUARIOS DISPONIBLES PARA FILTRO
    # ============================================================
    # Sólo aparecen usuarios que tengan tickets dentro del
    # alcance autorizado.
    # ============================================================

    usuarios_lista = []

    if es_admin_o_supervisor:

        tickets_usuarios = (
            Ticket.objects
            .filter(
                created_at__date__range=[
                    fecha_inicio,
                    fecha_fin,
                ]
            )
        )

        tickets_usuarios = tickets_for_user(
            request.user,
            tickets_usuarios,
        )

        usuarios_ids = (
            tickets_usuarios
            .exclude(created_by_id__isnull=True)
            .values_list(
                'created_by_id',
                flat=True,
            )
            .distinct()
        )

        usuarios_lista = (
            User.objects
            .filter(
                id__in=usuarios_ids
            )
            .order_by('username')
        )

    # ============================================================
    # USUARIO SELECCIONADO
    # ============================================================

    usuario_filtrado_obj = None

    if usuario_id and usuario_id != 'todos':

        # Seguridad adicional:
        # sólo aceptar un usuario presente dentro del alcance
        # autorizado.
        usuario_filtrado_obj = (
            User.objects
            .filter(
                id=usuario_id,
                id__in=usuarios_lista.values_list(
                    'id',
                    flat=True,
                ),
            )
            .first()
        )

    user_role = (
        user_profile.get_role_display()
        if user_profile
        else "Vendedor"
    )

    # ============================================================
    # CONTEXTO
    # ============================================================

    context = {
        'title': 'POS — Reportes Detallados',

        'fecha_inicio': (
            fecha_inicio.strftime('%Y-%m-%d')
        ),
        'fecha_fin': (
            fecha_fin.strftime('%Y-%m-%d')
        ),

        'total_ventas': total_ventas,
        'total_boletos': total_boletos,
        'ticket_promedio_general':
            ticket_promedio_general,

        'promedio_diario': promedio_diario,

        'ventas_por_dia':
            ventas_por_dia_procesadas,

        'rutas_populares':
            rutas_populares,

        'ventas_por_hora':
            list(ventas_por_hora),

        'dias_rango':
            dias_rango,

        'es_admin_o_supervisor':
            es_admin_o_supervisor,

        'ventas_por_vendedor':
            ventas_por_vendedor,

        'usuarios_lista':
            usuarios_lista,

        'usuario_seleccionado':
            usuario_id,

        'user_profile':
            user_profile,

        'user_role':
            user_role,

        'filtro_usuario_aplicado':
            usuario_id if usuario_id else None,

        'usuario_filtrado': (
            usuario_filtrado_obj.get_full_name()
            if usuario_filtrado_obj
            else None
        ),
    }

    return render(
        request,
        'booking/pos_reportes.html',
        context,
    )

# ============================================================================
# 9. GESTIÓN ADMINISTRATIVA DE USUARIOS
# ============================================================================

@login_required
@role_required(['admin', 'supervisor', 'coordinator'])
def gestion_usuarios(request):

    query = (
        request.GET.get("q")
        or ""
    ).strip()

    qs = (
        User.objects
        .select_related(
            "profile",
            "profile__terminal",
            "profile__terminal__city",
            "profile__company",
            "profile__fleet_owner",
        )
    )

    # ============================================================
    # AISLAMIENTO MULTIEMPRESA
    # ============================================================
    if request.user.is_superuser:

        # Superuser puede ver todo.
        pass

    else:

        actor_profile = getattr(
            request.user,
            "profile",
            None,
        )

        if (
            not actor_profile
            or not actor_profile.company_id
        ):
            raise PermissionDenied(
                "Su usuario no tiene una empresa asociada."
            )

        qs = qs.filter(
            profile__company=actor_profile.company,
            is_superuser=False,
        )

    qs = qs.order_by(
        "username"
    )

    # ============================================================
    # BÚSQUEDA
    # ============================================================
    if query:

        qs = qs.filter(
            Q(
                username__icontains=query
            )
            |
            Q(
                first_name__icontains=query
            )
            |
            Q(
                last_name__icontains=query
            )
            |
            Q(
                email__icontains=query
            )
            |
            Q(
                profile__company__name__icontains=query
            )
            |
            Q(
                profile__fleet_owner__first_name__icontains=query
            )
            |
            Q(
                profile__fleet_owner__last_name__icontains=query
            )
        ).distinct()

    # ============================================================
    # CONTEXTO
    # ============================================================
    context = {
        "title": "Gestión de Usuarios",
        "usuarios": qs,
        "total_count": qs.count(),

        "admins_count": qs.filter(
            profile__role="admin"
        ).count(),

        "supervisores_count": qs.filter(
            profile__role="supervisor"
        ).count(),

        "coordinadores_count": qs.filter(
            profile__role="coordinator"
        ).count(),

        "vendedores_count": qs.filter(
            profile__role="vendedor"
        ).count(),

        "owners_count": qs.filter(
            profile__role="owner"
        ).count(),

        "executives_count": qs.filter(
            profile__role="executive"
        ).count(),

        "secretaries_count": qs.filter(
            profile__role="secretary"
        ).count(),

        "activos_count": qs.filter(
            profile__is_active=True
        ).count(),

        "inactivos_count": qs.filter(
            profile__is_active=False
        ).count(),

        "roles": _get_role_choices(),

        # Terminales continúan siendo geografía global.
        "terminales": (
            Terminal.objects
            .select_related(
                "city"
            )
            .all()
            .order_by(
                "city__name",
                "name",
            )
        ),
    }

    return render(
        request,
        "booking/gestion_usuarios.html",
        context,
    )


@login_required
@role_required(['admin', 'supervisor', 'coordinator'])
def editar_usuario(request, user_id):

    # ============================================================
    # QUERYSET AUTORIZADO
    # ============================================================
    users_allowed = (
        User.objects
        .select_related(
            "profile",
            "profile__company",
            "profile__fleet_owner",
            "profile__terminal",
        )
    )

    actor_company = None

    if not request.user.is_superuser:

        actor_profile = getattr(
            request.user,
            "profile",
            None,
        )

        if (
            not actor_profile
            or not actor_profile.company_id
        ):
            raise PermissionDenied(
                "Su usuario no tiene una empresa asociada."
            )

        actor_company = actor_profile.company

        users_allowed = users_allowed.filter(
            profile__company=actor_company,
            is_superuser=False,
        )

    usuario = get_object_or_404(
        users_allowed,
        id=user_id,
    )

    # ============================================================
    # POST
    # ============================================================
    if request.method == "POST":

        try:

            role, company, fleet_owner = (
                _resolve_user_profile_scope(
                    request
                )
            )

            with transaction.atomic():

                usuario.first_name = (
                    request.POST
                    .get(
                        "first_name",
                        "",
                    )
                    .strip()
                )

                usuario.last_name = (
                    request.POST
                    .get(
                        "last_name",
                        "",
                    )
                    .strip()
                )

                usuario.email = (
                    request.POST
                    .get(
                        "email",
                        "",
                    )
                    .strip()
                )

                # =================================================
                # CONTRASEÑA
                # =================================================
                new_password = request.POST.get(
                    "password",
                    "",
                )

                confirm_password = request.POST.get(
                    "confirm_password",
                    "",
                )

                if new_password or confirm_password:

                    if new_password != confirm_password:
                        raise ValidationError(
                            "Las contraseñas no coinciden."
                        )

                    if len(new_password) < 8:
                        raise ValidationError(
                            "La nueva contraseña debe tener "
                            "al menos 8 caracteres."
                        )

                    usuario.set_password(
                        new_password
                    )

                usuario.save()

                # =================================================
                # PERFIL
                # =================================================
                profile, _ = (
                    UserProfile.objects
                    .get_or_create(
                        user=usuario
                    )
                )

                profile.role = role
                profile.company = company
                profile.fleet_owner = fleet_owner

                profile.terminal_id = (
                    request.POST.get(
                        "terminal"
                    )
                    or None
                )

                try:

                    profile.commission_rate = Decimal(
                        request.POST.get(
                            "commission_rate",
                            "0",
                        )
                        or "0"
                    )

                    profile.max_discount = Decimal(
                        request.POST.get(
                            "max_discount",
                            "0",
                        )
                        or "0"
                    )

                except (
                    ValueError,
                    TypeError,
                ):

                    profile.commission_rate = Decimal(
                        "0.00"
                    )

                    profile.max_discount = Decimal(
                        "0.00"
                    )

                profile.is_active = (
                    "is_active"
                    in request.POST
                )

                profile.save()

            messages.success(
                request,
                (
                    f"El usuario {usuario.username} "
                    "ha sido actualizado con éxito."
                ),
            )

            return redirect(
                "gestion_usuarios"
            )

        except ValidationError as e:

            messages.error(
                request,
                "; ".join(
                    e.messages
                ),
            )

        except PermissionDenied:
            raise

        except Exception as e:

            messages.error(
                request,
                (
                    "Error al procesar la actualización: "
                    f"{str(e)}"
                ),
            )

    # ============================================================
    # VALORES SELECCIONADOS
    # ============================================================
    profile = getattr(
        usuario,
        "profile",
        None,
    )

    if request.user.is_superuser:

        selected_company_id = (
            request.POST.get(
                "company"
            )
            if request.method == "POST"
            else (
                profile.company_id
                if profile
                else None
            )
        )

    else:

        selected_company_id = (
            actor_company.id
        )

    selected_owner_id = (
        request.POST.get(
            "fleet_owner"
        )
        if request.method == "POST"
        else (
            profile.fleet_owner_id
            if profile
            else None
        )
    )

    # ============================================================
    # EMPRESAS Y PROPIETARIOS AUTORIZADOS
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

        companies = Company.objects.filter(
            pk=actor_company.pk
        )

        fleet_owners = (
            FleetOwner.objects
            .filter(
                company=actor_company,
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
    # CONTEXTO
    # ============================================================
    context = {
        "title": (
            f"Editar Usuario - "
            f"{usuario.username}"
        ),

        "usuario": usuario,

        "terminales": (
            Terminal.objects
            .select_related(
                "city"
            )
            .all()
        ),

        "roles": _get_role_choices(),

        "companies": companies,

        "fleet_owners": fleet_owners,

        "selected_company_id": (
            selected_company_id
        ),

        "selected_owner_id": (
            selected_owner_id
        ),

        "is_superuser": (
            request.user.is_superuser
        ),

        "current_company": (
            actor_company
        ),
    }

    return render(
        request,
        "booking/editar_usuario.html",
        context,
    )

@login_required
@role_required(['admin', 'supervisor', 'coordinator'])
@transaction.atomic
def crear_usuario(request):

    actor_company = None

    # ============================================================
    # EMPRESA DEL USUARIO QUE ADMINISTRA
    # ============================================================
    if not request.user.is_superuser:

        actor_profile = getattr(
            request.user,
            "profile",
            None,
        )

        if (
            not actor_profile
            or not actor_profile.company_id
        ):
            raise PermissionDenied(
                "Su usuario no tiene una empresa asociada."
            )

        actor_company = actor_profile.company

    # ============================================================
    # POST
    # ============================================================
    if request.method == "POST":

        username = (
            request.POST
            .get(
                "username",
                "",
            )
            .strip()
            .lower()
        )

        password = request.POST.get(
            "password",
            "",
        )

        confirm = request.POST.get(
            "confirm_password",
            "",
        )

        if not username:

            messages.error(
                request,
                "El nombre de usuario es mandatorio.",
            )

        elif User.objects.filter(
            username__iexact=username
        ).exists():

            messages.error(
                request,
                (
                    "El nombre de usuario ingresado "
                    "ya se encuentra en uso."
                ),
            )

        elif password != confirm:

            messages.error(
                request,
                (
                    "Las contraseñas de verificación "
                    "no coinciden."
                ),
            )

        elif len(password) < 8:

            messages.error(
                request,
                (
                    "La contraseña debe tener "
                    "al menos 8 caracteres."
                ),
            )

        else:

            try:

                # ================================================
                # VALIDAR ALCANCE ANTES DE CREAR CUENTA
                # ================================================
                role, company, fleet_owner = (
                    _resolve_user_profile_scope(
                        request
                    )
                )

                user = User.objects.create_user(
                    username=username,
                    email=(
                        request.POST
                        .get(
                            "email",
                            "",
                        )
                        .strip()
                    ),
                    password=password,
                    first_name=(
                        request.POST
                        .get(
                            "first_name",
                            "",
                        )
                        .strip()
                    ),
                    last_name=(
                        request.POST
                        .get(
                            "last_name",
                            "",
                        )
                        .strip()
                    ),
                )

                profile, _ = (
                    UserProfile.objects
                    .get_or_create(
                        user=user
                    )
                )

                profile.role = role
                profile.company = company
                profile.fleet_owner = fleet_owner

                # Terminal permanece global.
                profile.terminal_id = (
                    request.POST.get(
                        "terminal"
                    )
                    or None
                )

                try:

                    profile.commission_rate = Decimal(
                        request.POST.get(
                            "commission_rate",
                            "0",
                        )
                        or "0"
                    )

                    profile.max_discount = Decimal(
                        request.POST.get(
                            "max_discount",
                            "0",
                        )
                        or "0"
                    )

                except (
                    ValueError,
                    TypeError,
                ):

                    profile.commission_rate = Decimal(
                        "0.00"
                    )

                    profile.max_discount = Decimal(
                        "0.00"
                    )

                profile.is_active = True

                profile.save()

                messages.success(
                    request,
                    (
                        f"Usuario operativo {username} "
                        "creado de forma exitosa."
                    ),
                )

                return redirect(
                    "gestion_usuarios"
                )

            except ValidationError as e:

                messages.error(
                    request,
                    "; ".join(
                        e.messages
                    ),
                )

            except PermissionDenied:
                raise

            except Exception as e:

                messages.error(
                    request,
                    (
                        "Fallo al registrar usuario: "
                        f"{str(e)}"
                    ),
                )

    # ============================================================
    # OPCIONES AUTORIZADAS PARA EL FORMULARIO
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

        selected_company_id = (
            request.POST.get(
                "company"
            )
        )

    else:

        companies = (
            Company.objects
            .filter(
                pk=actor_company.pk
            )
        )

        fleet_owners = (
            FleetOwner.objects
            .filter(
                company=actor_company,
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

        selected_company_id = (
            actor_company.pk
        )

    # ============================================================
    # CONTEXTO
    # ============================================================
    context = {
        "title": "Crear Usuario",

        "roles": _get_role_choices(),

        "terminales": (
            Terminal.objects
            .select_related(
                "city"
            )
            .all()
        ),

        "companies": companies,

        "fleet_owners": fleet_owners,

        "selected_company_id": (
            selected_company_id
        ),

        "selected_owner_id": (
            request.POST.get(
                "fleet_owner"
            )
        ),

        "is_superuser": (
            request.user.is_superuser
        ),

        "current_company": (
            actor_company
        ),
    }

    return render(
        request,
        "booking/crear_usuario.html",
        context,
    )


# ============================================================================
# 10. CLIENTES
# ============================================================================

@login_required
def search_customer(request):
    try:
        query = (request.GET.get("q") or "").strip()
        if not query or len(query) < 2:
            return JsonResponse({"customers": []})

        query_clean = Customer._clean_rut(query) if hasattr(Customer, '_clean_rut') else query
        rut_q = Q(national_id__icontains=query_clean)
        if query_clean != query:
            rut_q = rut_q | Q(national_id__icontains=query)

        customers = Customer.objects.filter(rut_q | Q(full_name__icontains=query)).order_by("full_name")[:10]
        results = [{
            "id": c.id,
            "national_id": c.national_id,
            "full_name": c.full_name,
            "phone": c.phone or "",
            "email": c.email or "",
        } for c in customers]
        return JsonResponse({"customers": results})
    except Exception as e:
        logger.error(f"Error en search_customer: {e}", exc_info=True)
        return JsonResponse({"customers": [], "error": "Error interno"}, status=500)

@login_required
@require_POST
def create_customer(request):
    try:
        national_id = request.POST.get('national_id', '').strip()
        if not validate_chilean_rut(national_id):
            return JsonResponse({'success': False, 'error': 'RUT inválido. Formato: 12345678-9'}, status=400)

        full_name = request.POST.get('full_name', '').strip()
        phone = request.POST.get('phone', '').strip()
        email = request.POST.get('email', '').strip()

        if not national_id:
            return JsonResponse({'success': False, 'error': 'El documento identificador (RUT) es un campo obligatorio.'})

        if not full_name:
            full_name = "Pasajero General"

        existing = Customer.objects.filter(national_id=national_id).first()
        if existing:
            return JsonResponse({
                'success': True,
                'created': False,
                'message': 'El cliente ya se encuentra registrado.',
                'customer': {
                    'id': existing.id,
                    'national_id': existing.national_id,
                    'full_name': existing.full_name,
                    'phone': existing.phone or '',
                    'email': existing.email or ''
                }
            })

        customer = Customer.objects.create(
            national_id=national_id, full_name=full_name, phone=phone, email=email
        )
        return JsonResponse({
            'success': True,
            'created': True,
            'message': 'Cliente registrado con éxito.',
            'customer': {
                'id': customer.id,
                'national_id': customer.national_id,
                'full_name': customer.full_name,
                'phone': customer.phone or '',
                'email': customer.email or ''
            }
        })
    except Exception as e:
        logger.error(f"Error en create_customer: {e}", exc_info=True)
        return JsonResponse({'success': False, 'error': 'Error interno del servidor.'}, status=500)

# ============================================================================
# 11. DISEÑO FÍSICO Y COMPRA DIRECTA (API PURCHASE)
# ============================================================================

@csrf_exempt
def save_layout_template(request):
    if request.method != "POST":
        return JsonResponse({"error": "Método de solicitud no permitido"}, status=405)
    try:
        data = json.loads(request.body.decode('utf-8') or "{}")
        layout = BusLayout.objects.create(
            name=data.get("name"),
            floors=data.get("floors"),
            rows_lower=data.get("rows_lower"),
            rows_upper=data.get("rows_upper"),
            cols=data.get("cols"),
            layout_lower=data.get("layout_lower", []),
            layout_upper=data.get("layout_upper", []),
            numbers_lower=data.get("numbers_lower", []),
            numbers_upper=data.get("numbers_upper", []),
            prefix_lower=data.get("prefix_lower", ""),
            prefix_upper=data.get("prefix_upper", "")
        )
        return JsonResponse({"success": True, "id": layout.id})
    except Exception as e:
        logger.error(f"Error en save_layout_template: {e}", exc_info=True)
        return JsonResponse({"error": "Error interno del servidor."}, status=500)

@require_POST
@login_required
@vendedor_required
@transaction.atomic
def api_purchase(request, trip_id):
    trip = get_object_or_404(Trip.objects.select_for_update(), id=trip_id)

    try:
        _check_terminal_permission(request, trip)

        data = json.loads(request.body.decode('utf-8') or "{}")
        seats_data = data.get('seats')
        payment_method = data.get('payment_method', 'cash')
        customer_id = data.get('customer_id')
        buyer_name = data.get('buyer_name', 'Cliente General')
        discount_code = data.get('discount_code', '').strip()

        if not seats_data:
            return JsonResponse({'ok': False, 'error': 'No se seleccionaron asientos.'}, status=400)

        if not isinstance(seats_data, list) or not all(isinstance(s, dict) for s in seats_data):
            return JsonResponse({'ok': False, 'error': 'Estructura de asientos inválida.'}, status=400)

        for s in seats_data:
            if 'number' not in s or not s['number']:
                return JsonResponse({'ok': False, 'error': 'Cada asiento debe tener un número.'}, status=400)

        total_amount = trip.route.base_price * len(seats_data)
        final_price_per_ticket, applied_discount = calculate_final_price(
            trip, discount_code if discount_code else None, total_amount
        )

        if discount_code and applied_discount is None:
            return JsonResponse({'ok': False, 'error': 'Código de descuento inválido, expirado o no alcanza el monto mínimo.'}, status=400)

        customer = Customer.objects.filter(id=customer_id).first() if customer_id else None
        created_tickets = []

        numbers = [str(s.get('number')).strip() for s in seats_data if s.get('number')]
        decks = [int(s.get('deck', 1)) for s in seats_data if s.get('deck')]

        seats_db = {
            f"{seat.deck}-{seat.number}": seat
            for seat in Seat.objects.select_for_update()
                     .filter(bus=trip.bus, number__in=numbers, deck__in=decks)
                     .order_by('id')
        }

        for s in seats_data:
            seat_num = str(s.get('number')).strip()
            deck_num = int(s.get('deck', 1))
            key = f"{deck_num}-{seat_num}"

            seat_obj = seats_db.get(key)
            if not seat_obj:
                return JsonResponse({'ok': False, 'error': f'Asiento {seat_num} no existe.'}, status=400)

            if Ticket.objects.filter(trip=trip, seat=seat_obj).exists():
                return JsonResponse({'ok': False, 'error': f'Asiento {seat_num} ya vendido.'}, status=409)

            ticket = Ticket.create_for_sale(
                trip=trip,
                seat=seat_obj,
                buyer_name=customer.full_name if customer else buyer_name,
                national_id=customer.national_id if customer else "",
                price=final_price_per_ticket,
                created_by=request.user,
                payment_method=payment_method,
                customer=customer,
            )
            created_tickets.append(ticket)

        SeatHold.objects.filter(trip=trip, seat__in=[t.seat for t in created_tickets]).delete()

        if applied_discount:
            applied_discount.used_count += 1
            applied_discount.save(update_fields=['used_count'])

        total = final_price_per_ticket * len(created_tickets)

        _audit_log(
            request,
            action='purchase',
            model_name='Ticket',
            object_id=','.join(str(t.id) for t in created_tickets),
            object_repr=f"Compra API de {len(created_tickets)} tickets",
            changes={
                'trip_id': trip.id,
                'total': float(total),
                'payment_method': payment_method,
                'discount_applied': applied_discount.discount_value if applied_discount else 0
            }
        )

        return JsonResponse({
            'ok': True,
            'message': f'¡Compra exitosa! {len(created_tickets)} pasaje(s) emitido(s).',
            'total': float(total),
            'discount_applied': applied_discount.discount_value if applied_discount else 0,
            'redirect_url': reverse('pos_home')
        })

    except PermissionDenied as e:
        return JsonResponse({'ok': False, 'error': str(e)}, status=403)
    except json.JSONDecodeError:
        return JsonResponse({'ok': False, 'error': 'JSON inválido.'}, status=400)
    except Exception as e:
        logger.error(f"Error en api_purchase: {e}", exc_info=True)
        return JsonResponse({'ok': False, 'error': 'Error interno del servidor.'}, status=500)

# ============================================================================
# 12. ENCOMIENDAS
# ============================================================================

@require_POST
@login_required
@vendedor_required
def api_create_parcel(request, trip_id):
    trip = get_object_or_404(Trip, pk=trip_id)
    try:
        data = json.loads(request.body)
        parcel = Parcel.objects.create(
            trip=trip,
            sender_name=data.get('sender_name', '').strip(),
            sender_phone=data.get('sender_phone', '').strip(),
            recipient_name=data.get('recipient_name', '').strip(),
            recipient_phone=data.get('recipient_phone', '').strip(),
            recipient_rut=data.get('recipient_rut', '').strip(),
            description=data.get('description', '').strip(),
            weight=Decimal(data.get('weight', 1)),
            price=Decimal(data.get('price', 0)),
            payment_method=data.get('payment_method', 'cash'),
            created_by=request.user,
            notes=data.get('notes', ''),
        )
        return JsonResponse({
            'success': True,
            'tracking_number': parcel.tracking_number,
            'message': f'Encomienda registrada con N° {parcel.tracking_number}'
        })
    except Exception as e:
        logger.error(f"Error en api_create_parcel: {e}", exc_info=True)
        return JsonResponse({'success': False, 'error': 'Error interno del servidor.'}, status=400)

@require_POST
@login_required
@role_required(['admin', 'coordinator'])
def parcel_deliver(request, parcel_id):
    parcel = get_object_or_404(Parcel, pk=parcel_id)
    if parcel.status == 'pending':
        parcel.deliver()
        messages.success(request, f'Encomienda {parcel.tracking_number} marcada como entregada.')
    else:
        messages.warning(request, 'La encomienda ya no está pendiente.')
    return redirect('coordinator:parcel_list')

@login_required
def parcel_receipt(request, parcel_id):
    parcel = get_object_or_404(Parcel, pk=parcel_id)
    return render(request, 'booking/parcel_receipt.html', {'parcel': parcel})

# ============================================================================
# 13. OTRAS VISTAS MISCELÁNEAS
# ============================================================================

def resultados(request):
    return render(request, 'client_portal/resultados.html')

@login_required
def owner_dashboard(request):
    """
    FASE 2.18.3-A3.2

    Dashboard económico del Propietario / Socio.

    Alcance económico:
    - Ticket.revenue_owner
    - Ticket.revenue_bus

    Filtros disponibles:
    - Hoy
    - Últimos 7 días
    - Este mes
    - Todo
    - Rango personalizado
    - Bus económico

    IMPORTANTE:
    Los filtros se aplican DESPUÉS de tickets_for_user(), por lo que
    un propietario nunca puede consultar tickets económicos ajenos,
    incluso manipulando manualmente los parámetros GET.
    """

    user = request.user
    profile = getattr(user, "profile", None)

    # ------------------------------------------------------------------
    # 1. VALIDACIÓN DEL PERFIL
    # ------------------------------------------------------------------
    if not profile:
        messages.error(
            request,
            "El usuario no tiene un perfil operativo configurado."
        )
        return redirect("dashboard_redirect")

    if not profile.is_active:
        messages.error(
            request,
            "Su cuenta se encuentra desactivada."
        )
        logout(request)
        return redirect("login")

    # Superusuario permitido para soporte / pruebas.
    if not user.is_superuser and profile.role != "owner":
        raise PermissionDenied(
            "No tiene permisos para acceder al portal del propietario."
        )

    if not user.is_superuser:
        if not profile.company_id or not profile.fleet_owner_id:
            messages.error(
                request,
                "El perfil de propietario no tiene empresa o propietario asociado."
            )
            return redirect("dashboard_redirect")

    # ------------------------------------------------------------------
    # 2. QUERYSET ECONÓMICO BASE
    # ------------------------------------------------------------------
    tickets_base = tickets_for_user(
        user,
        Ticket.objects.all()
    ).select_related(
        "revenue_bus",
        "revenue_owner",
        "revenue_bus__company",
        "trip",
        "trip__route",
    )

    # ------------------------------------------------------------------
    # 3. BUSES DISPONIBLES PARA EL FILTRO
    #
    # Se obtienen desde el queryset económico autorizado.
    # No usamos Bus.objects.all().
    # ------------------------------------------------------------------
    buses_disponibles = list(
        tickets_base
        .exclude(revenue_bus__isnull=True)
        .values(
            "revenue_bus_id",
            "revenue_bus__plate",
        )
        .distinct()
        .order_by("revenue_bus__plate")
    )

    # ------------------------------------------------------------------
    # 4. PARÁMETROS GET
    # ------------------------------------------------------------------
    periodo = request.GET.get("period", "month").strip().lower()
    bus_id = request.GET.get("bus", "").strip()

    fecha_desde_raw = request.GET.get("date_from", "").strip()
    fecha_hasta_raw = request.GET.get("date_to", "").strip()

    periodos_validos = {
        "today",
        "7days",
        "month",
        "custom",
        "all",
    }

    if periodo not in periodos_validos:
        periodo = "month"

    tickets = tickets_base

    hoy = timezone.localdate()

    fecha_desde = None
    fecha_hasta = None

    # ------------------------------------------------------------------
    # 5. FILTRO DE FECHA
    # ------------------------------------------------------------------
    if periodo == "today":

        fecha_desde = hoy
        fecha_hasta = hoy

        tickets = tickets.filter(
            created_at__date=hoy
        )

    elif periodo == "7days":

        fecha_desde = hoy - timedelta(days=6)
        fecha_hasta = hoy

        tickets = tickets.filter(
            created_at__date__range=[
                fecha_desde,
                fecha_hasta,
            ]
        )

    elif periodo == "month":

        fecha_desde = hoy.replace(day=1)
        fecha_hasta = hoy

        tickets = tickets.filter(
            created_at__date__range=[
                fecha_desde,
                fecha_hasta,
            ]
        )

    elif periodo == "custom":

        try:
            if fecha_desde_raw:
                fecha_desde = timezone.datetime.strptime(
                    fecha_desde_raw,
                    "%Y-%m-%d"
                ).date()

            if fecha_hasta_raw:
                fecha_hasta = timezone.datetime.strptime(
                    fecha_hasta_raw,
                    "%Y-%m-%d"
                ).date()

        except (TypeError, ValueError):
            fecha_desde = None
            fecha_hasta = None

            messages.warning(
                request,
                "El rango de fechas ingresado no es válido."
            )

        if fecha_desde and fecha_hasta:

            if fecha_desde > fecha_hasta:
                messages.warning(
                    request,
                    "La fecha inicial no puede ser posterior a la fecha final."
                )

                fecha_desde = None
                fecha_hasta = None

            else:
                tickets = tickets.filter(
                    created_at__date__range=[
                        fecha_desde,
                        fecha_hasta,
                    ]
                )

        elif fecha_desde:

            tickets = tickets.filter(
                created_at__date__gte=fecha_desde
            )

        elif fecha_hasta:

            tickets = tickets.filter(
                created_at__date__lte=fecha_hasta
            )

    # periodo == "all":
    # no se aplica filtro de fecha.

    # ------------------------------------------------------------------
    # 6. FILTRO DE BUS
    # ------------------------------------------------------------------
    bus_seleccionado = None

    if bus_id:

        try:
            bus_id_int = int(bus_id)

        except (TypeError, ValueError):
            bus_id_int = None

        if bus_id_int:

            # Importante:
            # filtramos sobre tickets_base/tickets ya autorizados.
            tickets = tickets.filter(
                revenue_bus_id=bus_id_int
            )

            bus_seleccionado = next(
                (
                    bus
                    for bus in buses_disponibles
                    if bus["revenue_bus_id"] == bus_id_int
                ),
                None,
            )

    # ------------------------------------------------------------------
    # 7. KPIs DEL PERÍODO FILTRADO
    # ------------------------------------------------------------------
    resumen = tickets.aggregate(
        total_tickets=Count("id"),
        total_ingresos=Sum("price"),
        ticket_promedio=Avg("price"),
    )

    total_tickets = resumen["total_tickets"] or 0
    total_ingresos = resumen["total_ingresos"] or Decimal("0")
    ticket_promedio = resumen["ticket_promedio"] or Decimal("0")

    buses_con_ventas = (
        tickets
        .exclude(revenue_bus__isnull=True)
        .values("revenue_bus_id")
        .distinct()
        .count()
    )

    # ------------------------------------------------------------------
    # 8. RESUMEN POR BUS
    # ------------------------------------------------------------------
    ventas_por_bus = (
        tickets
        .exclude(revenue_bus__isnull=True)
        .values(
            "revenue_bus_id",
            "revenue_bus__plate",
        )
        .annotate(
            total_tickets=Count("id"),
            total_ingresos=Sum("price"),
            ticket_promedio=Avg("price"),
        )
        .order_by("-total_ingresos")
    )

    # ------------------------------------------------------------------
    # 9. ÚLTIMAS VENTAS DEL PERÍODO
    # ------------------------------------------------------------------
    ultimas_ventas = tickets.order_by(
        "-created_at",
        "-id",
    )[:50]

    # ------------------------------------------------------------------
    # 10. ETIQUETA DEL PERÍODO
    # ------------------------------------------------------------------
    etiquetas_periodo = {
        "today": "Hoy",
        "7days": "Últimos 7 días",
        "month": "Este mes",
        "custom": "Rango personalizado",
        "all": "Todo el historial",
    }

    periodo_label = etiquetas_periodo.get(
        periodo,
        "Este mes",
    )

    # ------------------------------------------------------------------
    # 11. CONTEXTO
    # ------------------------------------------------------------------
    context = {
        "profile": profile,
        "owner": getattr(profile, "fleet_owner", None),
        "company": getattr(profile, "company", None),

        # KPIs
        "total_tickets": total_tickets,
        "total_ingresos": total_ingresos,
        "ticket_promedio": ticket_promedio,
        "buses_con_ventas": buses_con_ventas,

        # Datos
        "ventas_por_bus": ventas_por_bus,
        "ultimas_ventas": ultimas_ventas,
        "buses_disponibles": buses_disponibles,

        # Filtros
        "periodo": periodo,
        "periodo_label": periodo_label,
        "fecha_desde": fecha_desde,
        "fecha_hasta": fecha_hasta,
        "fecha_desde_raw": fecha_desde_raw,
        "fecha_hasta_raw": fecha_hasta_raw,
        "bus_id": bus_id,
        "bus_seleccionado": bus_seleccionado,
    }

    return render(
        request,
        "booking/owner_dashboard.html",
        context,
    )



@login_required
def owner_bus_detail(request, bus_id):
    """
    FASE 2.18.3-A3.2-D

    Detalle económico de un bus para el Propietario / Socio.

    La autorización se basa exclusivamente en tickets_for_user().
    Nunca se consulta el propietario operacional actual del bus
    para reconstruir ventas históricas.
    """

    user = request.user
    profile = getattr(user, "profile", None)

    # --------------------------------------------------------------
    # 1. VALIDACIÓN DEL PERFIL
    # --------------------------------------------------------------
    if not profile:
        messages.error(
            request,
            "El usuario no tiene un perfil operativo configurado."
        )
        return redirect("dashboard_redirect")

    if not profile.is_active:
        messages.error(
            request,
            "Su cuenta se encuentra desactivada."
        )
        logout(request)
        return redirect("login")

    if not user.is_superuser and profile.role != "owner":
        raise PermissionDenied(
            "No tiene permisos para acceder al portal del propietario."
        )

    if not user.is_superuser:
        if not profile.company_id or not profile.fleet_owner_id:
            messages.error(
                request,
                "El perfil de propietario no tiene empresa o propietario asociado."
            )
            return redirect("dashboard_redirect")

    # --------------------------------------------------------------
    # 2. TICKETS ECONÓMICOS AUTORIZADOS
    # --------------------------------------------------------------
    tickets_base = tickets_for_user(
        user,
        Ticket.objects.all()
    ).select_related(
        "revenue_bus",
        "revenue_owner",
        "revenue_bus__company",
        "trip",
        "trip__route",
    )

    # --------------------------------------------------------------
    # 3. VALIDAR ACCESO ECONÓMICO AL BUS
    # --------------------------------------------------------------
    bus_info = (
        tickets_base
        .filter(revenue_bus_id=bus_id)
        .values(
            "revenue_bus_id",
            "revenue_bus__plate",
            "revenue_bus__company__name",
        )
        .first()
    )

    if not bus_info:
        raise PermissionDenied(
            "No tiene acceso económico a este bus."
        )

    # --------------------------------------------------------------
    # 4. FILTROS
    # --------------------------------------------------------------
    periodo = request.GET.get("period", "month").strip().lower()

    fecha_desde_raw = request.GET.get("date_from", "").strip()
    fecha_hasta_raw = request.GET.get("date_to", "").strip()

    periodos_validos = {
        "today",
        "7days",
        "month",
        "custom",
        "all",
    }

    if periodo not in periodos_validos:
        periodo = "month"

    tickets = tickets_base.filter(
        revenue_bus_id=bus_id
    )

    hoy = timezone.localdate()

    fecha_desde = None
    fecha_hasta = None

    if periodo == "today":

        fecha_desde = hoy
        fecha_hasta = hoy

        tickets = tickets.filter(
            created_at__date=hoy
        )

    elif periodo == "7days":

        fecha_desde = hoy - timedelta(days=6)
        fecha_hasta = hoy

        tickets = tickets.filter(
            created_at__date__range=[
                fecha_desde,
                fecha_hasta,
            ]
        )

    elif periodo == "month":

        fecha_desde = hoy.replace(day=1)
        fecha_hasta = hoy

        tickets = tickets.filter(
            created_at__date__range=[
                fecha_desde,
                fecha_hasta,
            ]
        )

    elif periodo == "custom":

        try:
            if fecha_desde_raw:
                fecha_desde = datetime.strptime(
                    fecha_desde_raw,
                    "%Y-%m-%d"
                ).date()

            if fecha_hasta_raw:
                fecha_hasta = datetime.strptime(
                    fecha_hasta_raw,
                    "%Y-%m-%d"
                ).date()

        except (TypeError, ValueError):
            fecha_desde = None
            fecha_hasta = None

            messages.warning(
                request,
                "El rango de fechas ingresado no es válido."
            )

        if fecha_desde and fecha_hasta:

            if fecha_desde > fecha_hasta:

                messages.warning(
                    request,
                    "La fecha inicial no puede ser posterior a la fecha final."
                )

                fecha_desde = None
                fecha_hasta = None

            else:

                tickets = tickets.filter(
                    created_at__date__range=[
                        fecha_desde,
                        fecha_hasta,
                    ]
                )

        elif fecha_desde:

            tickets = tickets.filter(
                created_at__date__gte=fecha_desde
            )

        elif fecha_hasta:

            tickets = tickets.filter(
                created_at__date__lte=fecha_hasta
            )

    # --------------------------------------------------------------
    # 5. KPIs DEL BUS
    # --------------------------------------------------------------
    resumen = tickets.aggregate(
        total_tickets=Count("id"),
        total_ingresos=Sum("price"),
        ticket_promedio=Avg("price"),
    )

    total_tickets = resumen["total_tickets"] or 0
    total_ingresos = resumen["total_ingresos"] or Decimal("0")
    ticket_promedio = resumen["ticket_promedio"] or Decimal("0")

    # --------------------------------------------------------------
    # 6. RUTAS DEL BUS
    # --------------------------------------------------------------
    ventas_por_ruta = (
        tickets
        .values(
            "trip__route__origin",
            "trip__route__destination",
        )
        .annotate(
            total_tickets=Count("id"),
            total_ingresos=Sum("price"),
        )
        .order_by("-total_ingresos")
    )

    # --------------------------------------------------------------
    # 7. VENTAS
    # --------------------------------------------------------------
    ventas = tickets.order_by(
        "-created_at",
        "-id",
    )[:100]

    etiquetas_periodo = {
        "today": "Hoy",
        "7days": "Últimos 7 días",
        "month": "Este mes",
        "custom": "Rango personalizado",
        "all": "Todo el historial",
    }

    periodo_label = etiquetas_periodo.get(
        periodo,
        "Este mes",
    )

    context = {
        "profile": profile,
        "owner": getattr(profile, "fleet_owner", None),
        "company": getattr(profile, "company", None),

        "bus_id": bus_id,
        "bus_info": bus_info,

        "total_tickets": total_tickets,
        "total_ingresos": total_ingresos,
        "ticket_promedio": ticket_promedio,

        "ventas_por_ruta": ventas_por_ruta,
        "ventas": ventas,

        "periodo": periodo,
        "periodo_label": periodo_label,
        "fecha_desde": fecha_desde,
        "fecha_hasta": fecha_hasta,
        "fecha_desde_raw": fecha_desde_raw,
        "fecha_hasta_raw": fecha_hasta_raw,
    }

    return render(
        request,
        "booking/owner_bus_detail.html",
        context,
    )

@login_required
def owner_settlements(request):
    """
    FASE 2.18.3-A3.3-C

    Listado de liquidaciones visibles para el Propietario / Socio.
    """

    user = request.user
    profile = getattr(user, "profile", None)

    if not profile:
        raise PermissionDenied(
            "El usuario no tiene un perfil operativo configurado."
        )

    if not profile.is_active:
        raise PermissionDenied(
            "La cuenta se encuentra desactivada."
        )

    if not user.is_superuser and profile.role != "owner":
        raise PermissionDenied(
            "No tiene permisos para ver liquidaciones."
        )

    settlements = OwnerSettlement.objects.select_related(
        "owner",
        "company",
        "created_by",
    )

    if not user.is_superuser:
        if not profile.company_id or not profile.fleet_owner_id:
            raise PermissionDenied(
                "El perfil de propietario no está completamente configurado."
            )

        settlements = settlements.filter(
            company=profile.company,
            owner=profile.fleet_owner,
        )

    settlements = settlements.order_by(
        "-date_to",
        "-created_at",
    )

    context = {
        "profile": profile,
        "owner": getattr(profile, "fleet_owner", None),
        "company": getattr(profile, "company", None),
        "settlements": settlements,
    }

    return render(
        request,
        "booking/owner_settlements.html",
        context,
    )


@login_required
def owner_settlement_detail(request, settlement_id):
    """
    FASE 2.18.3-A3.3-C

    Detalle de una liquidación individual.
    """

    user = request.user
    profile = getattr(user, "profile", None)

    if not profile:
        raise PermissionDenied(
            "El usuario no tiene un perfil operativo configurado."
        )

    if not profile.is_active:
        raise PermissionDenied(
            "La cuenta se encuentra desactivada."
        )

    if not user.is_superuser and profile.role != "owner":
        raise PermissionDenied(
            "No tiene permisos para ver esta liquidación."
        )

    settlements = OwnerSettlement.objects.select_related(
        "owner",
        "company",
        "created_by",
    ).prefetch_related(
        "items__ticket__revenue_bus",
        "items__ticket__trip__route",
    )

    if not user.is_superuser:
        if not profile.company_id or not profile.fleet_owner_id:
            raise PermissionDenied(
                "El perfil de propietario no está completamente configurado."
            )

        settlements = settlements.filter(
            company=profile.company,
            owner=profile.fleet_owner,
        )

    settlement = get_object_or_404(
        settlements,
        pk=settlement_id,
    )

    context = {
        "profile": profile,
        "owner": getattr(profile, "fleet_owner", None),
        "company": getattr(profile, "company", None),
        "settlement": settlement,
        "items": settlement.items.select_related(
            "ticket",
            "ticket__revenue_bus",
            "ticket__trip",
            "ticket__trip__route",
        ).order_by(
            "ticket__created_at",
            "ticket__id",
        ),
    }

    return render(
        request,
        "booking/owner_settlement_detail.html",
        context,
    )


@login_required
@supervisor_required
def owner_settlement_admin_list(request):
    """
    FASE 2.18.3-A3.3-D

    Administración de liquidaciones de propietarios.

    - Superuser: ve todas.
    - Admin/Supervisor normal: solamente su empresa.
    - Propietario: no tiene acceso a esta vista.
    """

    user = request.user
    profile = getattr(user, "profile", None)

    settlements = (
        OwnerSettlement.objects
        .select_related(
            "company",
            "owner",
            "created_by",
        )
        .prefetch_related(
            "items",
        )
    )

    # Superusuario puede administrar todas las empresas.
    if not user.is_superuser:

        if not profile or not profile.is_active:
            raise PermissionDenied(
                "El usuario no tiene un perfil operativo activo."
            )

        if not profile.company_id:
            raise PermissionDenied(
                "El usuario no tiene una empresa asociada."
            )

        settlements = settlements.filter(
            company=profile.company
        )

    # --------------------------------------------------------------
    # FILTROS
    # --------------------------------------------------------------
    status_filter = request.GET.get(
        "status",
        ""
    ).strip()

    owner_filter = request.GET.get(
        "owner",
        ""
    ).strip()

    if status_filter in {
        OwnerSettlement.STATUS_PENDING,
        OwnerSettlement.STATUS_REVIEW,
        OwnerSettlement.STATUS_PAID,
        OwnerSettlement.STATUS_CANCELLED,
    }:
        settlements = settlements.filter(
            status=status_filter
        )

    if owner_filter:
        try:
            owner_id = int(owner_filter)
        except (TypeError, ValueError):
            owner_id = None

        if owner_id:
            settlements = settlements.filter(
                owner_id=owner_id
            )

    settlements = settlements.order_by(
        "-date_to",
        "-created_at",
    )

    # --------------------------------------------------------------
    # PROPIETARIOS DISPONIBLES
    # --------------------------------------------------------------
    owners = FleetOwner.objects.filter(
        is_active=True
    )

    if not user.is_superuser:
        owners = owners.filter(
            company=profile.company
        )

    owners = owners.order_by(
        "first_name",
        "last_name",
    )

    context = {
        "settlements": settlements,
        "owners": owners,

        "status_filter": status_filter,
        "owner_filter": owner_filter,

        "status_choices": OwnerSettlement.STATUS_CHOICES,
    }

    return render(
        request,
        "booking/owner_settlement_admin_list.html",
        context,
    )


@login_required
@supervisor_required
@require_POST
def owner_settlement_change_status(request, settlement_id):
    """
    FASE 2.18.3-A3.3-D / A3.3-G.1 / A3.3-I

    Cambia el estado de una liquidación y registra trazabilidad.

    Transiciones permitidas:

        pending -> review
        review  -> paid

    Una liquidación pagada no puede volver atrás desde esta operación.

    Si el cambio se ejecuta desde el detalle administrativo,
    vuelve al detalle de la misma liquidación.
    Si se ejecuta desde el listado, vuelve al listado general.

    Cada cambio válido queda registrado en OwnerSettlementHistory.
    """

    user = request.user
    profile = getattr(user, "profile", None)

    # --------------------------------------------------------------
    # DESTINO DESPUÉS DEL CAMBIO
    # --------------------------------------------------------------
    next_url = request.POST.get(
        "next",
        ""
    ).strip()

    with transaction.atomic():

        # ----------------------------------------------------------
        # 1. BLOQUEAR LIQUIDACIÓN
        # ----------------------------------------------------------
        settlements = (
            OwnerSettlement.objects
            .select_for_update()
            .select_related(
                "company",
                "owner",
            )
        )

        # ----------------------------------------------------------
        # 2. ALCANCE EMPRESARIAL
        # ----------------------------------------------------------
        if not user.is_superuser:

            if not profile or not profile.is_active:

                raise PermissionDenied(
                    "El usuario no tiene un perfil operativo activo."
                )

            if not profile.company_id:

                raise PermissionDenied(
                    "El usuario no tiene una empresa asociada."
                )

            settlements = settlements.filter(
                company=profile.company
            )

        # ----------------------------------------------------------
        # 3. OBTENER LIQUIDACIÓN
        # ----------------------------------------------------------
        settlement = get_object_or_404(
            settlements,
            pk=settlement_id,
        )

        # ----------------------------------------------------------
        # 4. ESTADO SOLICITADO
        # ----------------------------------------------------------
        new_status = request.POST.get(
            "status",
            ""
        ).strip()

        # ----------------------------------------------------------
        # 5. TRANSICIONES PERMITIDAS
        # ----------------------------------------------------------
        allowed_transitions = {

            OwnerSettlement.STATUS_PENDING: {
                OwnerSettlement.STATUS_REVIEW,
            },

            OwnerSettlement.STATUS_REVIEW: {
                OwnerSettlement.STATUS_PAID,
            },

            OwnerSettlement.STATUS_PAID: set(),

            OwnerSettlement.STATUS_CANCELLED: set(),
        }

        current_status = settlement.status

        # ----------------------------------------------------------
        # 6. VALIDAR TRANSICIÓN
        # ----------------------------------------------------------
        if new_status not in allowed_transitions.get(
            current_status,
            set(),
        ):

            messages.error(
                request,
                (
                    "No se puede cambiar la liquidación "
                    f"de '{settlement.get_status_display()}' "
                    "al estado solicitado."
                )
            )

            # ------------------------------------------------------
            # REDIRECCIÓN SEGURA
            # ------------------------------------------------------
            allowed_next = reverse(
                "owner_settlement_admin_detail",
                args=[settlement.id],
            )

            if next_url == allowed_next:

                return redirect(
                    allowed_next
                )

            return redirect(
                "owner_settlement_admin_list"
            )

        # ----------------------------------------------------------
        # 7. GUARDAR ESTADO ANTERIOR PARA AUDITORÍA
        # ----------------------------------------------------------
        previous_status = settlement.status

        # ----------------------------------------------------------
        # 8. CAMBIAR ESTADO
        # ----------------------------------------------------------
        settlement.status = new_status

        update_fields = [
            "status",
            "updated_at",
        ]

        # ----------------------------------------------------------
        # 9. MARCAR FECHA DE PAGO
        # ----------------------------------------------------------
        if new_status == OwnerSettlement.STATUS_PAID:

            settlement.paid_at = timezone.now()

            update_fields.append(
                "paid_at"
            )

        settlement.save(
            update_fields=update_fields
        )

        # ----------------------------------------------------------
        # 10. REGISTRAR HISTORIAL
        # ----------------------------------------------------------
        if new_status == OwnerSettlement.STATUS_REVIEW:

            history_note = (
                "Liquidación enviada a revisión desde administración."
            )

        elif new_status == OwnerSettlement.STATUS_PAID:

            history_note = (
                "Liquidación marcada como pagada desde administración."
            )

        else:

            history_note = (
                "Cambio de estado realizado desde administración."
            )

        OwnerSettlementHistory.objects.create(
            settlement=settlement,
            previous_status=previous_status,
            new_status=new_status,
            changed_by=user,
            note=history_note,
        )

    # --------------------------------------------------------------
    # 11. MENSAJES
    # --------------------------------------------------------------
    if new_status == OwnerSettlement.STATUS_REVIEW:

        messages.success(
            request,
            (
                f"Liquidación #{settlement.id} "
                "enviada a revisión."
            )
        )

    elif new_status == OwnerSettlement.STATUS_PAID:

        messages.success(
            request,
            (
                f"Liquidación #{settlement.id} "
                "marcada como pagada correctamente."
            )
        )

    # --------------------------------------------------------------
    # 12. REDIRECCIÓN SEGURA
    # --------------------------------------------------------------
    allowed_next = reverse(
        "owner_settlement_admin_detail",
        args=[settlement.id],
    )

    # Si el formulario vino desde el detalle administrativo,
    # permanece en la misma liquidación.
    if next_url == allowed_next:

        return redirect(
            allowed_next
        )

    # Si vino desde el listado, regresa al listado general.
    return redirect(
        "owner_settlement_admin_list"
    )
    
    
@login_required
@supervisor_required
@require_POST
def owner_settlement_cancel(request, settlement_id):
    """
    FASE 2.18.3-A3.3-H / A3.3-I

    Anulación controlada de una liquidación con trazabilidad.

    Permitido:
        pending -> cancelled
        review  -> cancelled

    No permitido:
        paid      -> cancelled
        cancelled -> cancelled

    Al anular:
    - Se conserva snapshot de tickets para auditoría.
    - Se registra el cambio en OwnerSettlementHistory.
    - Se liberan OwnerSettlementTicket.
    - Los tickets pueden entrar en una liquidación futura.
    """

    user = request.user
    profile = getattr(user, "profile", None)

    cancellation_reason = request.POST.get(
        "cancellation_reason",
        ""
    ).strip()

    next_url = request.POST.get(
        "next",
        ""
    ).strip()

    # --------------------------------------------------------------
    # 1. MOTIVO OBLIGATORIO
    # --------------------------------------------------------------
    if not cancellation_reason:

        messages.error(
            request,
            "Debe ingresar el motivo de la anulación."
        )

        allowed_next = reverse(
            "owner_settlement_admin_detail",
            args=[settlement_id],
        )

        if next_url == allowed_next:
            return redirect(
                allowed_next
            )

        return redirect(
            "owner_settlement_admin_list"
        )

    # --------------------------------------------------------------
    # 2. VALIDAR LARGO DEL MOTIVO
    # --------------------------------------------------------------
    if len(cancellation_reason) > 1000:

        messages.error(
            request,
            "El motivo de anulación no puede superar los 1000 caracteres."
        )

        allowed_next = reverse(
            "owner_settlement_admin_detail",
            args=[settlement_id],
        )

        if next_url == allowed_next:
            return redirect(
                allowed_next
            )

        return redirect(
            "owner_settlement_admin_list"
        )

    # --------------------------------------------------------------
    # 3. OPERACIÓN ATÓMICA
    # --------------------------------------------------------------
    with transaction.atomic():

        settlements = (
            OwnerSettlement.objects
            .select_for_update()
            .select_related(
                "company",
                "owner",
            )
        )

        # ----------------------------------------------------------
        # 4. ALCANCE EMPRESARIAL
        # ----------------------------------------------------------
        if not user.is_superuser:

            if not profile or not profile.is_active:

                raise PermissionDenied(
                    "El usuario no tiene un perfil operativo activo."
                )

            if not profile.company_id:

                raise PermissionDenied(
                    "El usuario no tiene una empresa asociada."
                )

            settlements = settlements.filter(
                company=profile.company
            )

        # ----------------------------------------------------------
        # 5. OBTENER LIQUIDACIÓN
        # ----------------------------------------------------------
        settlement = get_object_or_404(
            settlements,
            pk=settlement_id,
        )

        # ----------------------------------------------------------
        # 6. VALIDAR ESTADO
        # ----------------------------------------------------------
        allowed_statuses = {
            OwnerSettlement.STATUS_PENDING,
            OwnerSettlement.STATUS_REVIEW,
        }

        if settlement.status not in allowed_statuses:

            messages.error(
                request,
                (
                    f"La liquidación #{settlement.id} "
                    f"se encuentra '{settlement.get_status_display()}' "
                    "y no puede ser anulada."
                )
            )

            allowed_next = reverse(
                "owner_settlement_admin_detail",
                args=[settlement.id],
            )

            if next_url == allowed_next:
                return redirect(
                    allowed_next
                )

            return redirect(
                "owner_settlement_admin_list"
            )

        # ----------------------------------------------------------
        # 7. GUARDAR ESTADO ANTERIOR
        # ----------------------------------------------------------
        previous_status = settlement.status

        # ----------------------------------------------------------
        # 8. BLOQUEAR ITEMS
        # ----------------------------------------------------------
        items = list(
            OwnerSettlementTicket.objects
            .select_for_update()
            .select_related(
                "ticket",
            )
            .filter(
                settlement=settlement
            )
            .order_by("id")
        )

        # ----------------------------------------------------------
        # 9. SNAPSHOT PARA AUDITORÍA
        # ----------------------------------------------------------
        ticket_snapshot = []

        for item in items:

            ticket = item.ticket
            bus = ticket.revenue_bus

            ticket_snapshot.append(
                {
                    "ticket_id": ticket.id,
                    "number": ticket.number,
                    "amount": str(item.amount),

                    "bus_id": (
                        bus.id
                        if bus
                        else None
                    ),

                    "bus_plate": (
                        bus.plate
                        if bus
                        else None
                    ),
                }
            )

        # ----------------------------------------------------------
        # 10. MARCAR LIQUIDACIÓN ANULADA
        # ----------------------------------------------------------
        settlement.status = OwnerSettlement.STATUS_CANCELLED

        settlement.cancelled_at = timezone.now()

        settlement.cancelled_by = user

        settlement.cancellation_reason = cancellation_reason

        settlement.cancelled_ticket_snapshot = ticket_snapshot

        settlement.save(
            update_fields=[
                "status",
                "cancelled_at",
                "cancelled_by",
                "cancellation_reason",
                "cancelled_ticket_snapshot",
                "updated_at",
            ]
        )

        # ----------------------------------------------------------
        # 11. REGISTRAR HISTORIAL
        # ----------------------------------------------------------
        OwnerSettlementHistory.objects.create(
            settlement=settlement,
            previous_status=previous_status,
            new_status=OwnerSettlement.STATUS_CANCELLED,
            changed_by=user,
            note=cancellation_reason,
        )

        # ----------------------------------------------------------
        # 12. LIBERAR TICKETS
        # ----------------------------------------------------------
        OwnerSettlementTicket.objects.filter(
            settlement=settlement
        ).delete()

    # --------------------------------------------------------------
    # 13. MENSAJE FINAL
    # --------------------------------------------------------------
    messages.success(
        request,
        (
            f"Liquidación #{settlement.id} anulada correctamente. "
            f"{len(ticket_snapshot)} ticket(s) quedaron disponibles "
            "para una nueva liquidación."
        )
    )

    # --------------------------------------------------------------
    # 14. REDIRECCIÓN SEGURA
    # --------------------------------------------------------------
    allowed_next = reverse(
        "owner_settlement_admin_detail",
        args=[settlement.id],
    )

    if next_url == allowed_next:

        return redirect(
            allowed_next
        )

    return redirect(
        "owner_settlement_admin_list"
    )
@login_required
@supervisor_required
@require_POST
def owner_settlement_generate(request):
    """
    FASE 2.18.3-A3.3-F

    Genera una liquidación desde la interfaz administrativa.

    Reglas:
    - Solo Admin / Supervisor / Superuser.
    - Admin/Supervisor solo puede liquidar propietarios de su empresa.
    - Solo incluye tickets del revenue_owner seleccionado.
    - Solo incluye tickets NO liquidados.
    - Bloquea tickets durante la operación para evitar carreras.
    - OwnerSettlementTicket.ticket es OneToOne, por lo que
      un mismo ticket no puede pertenecer a dos liquidaciones.
    """

    user = request.user
    profile = getattr(user, "profile", None)

    owner_id_raw = request.POST.get("owner", "").strip()
    date_from_raw = request.POST.get("date_from", "").strip()
    date_to_raw = request.POST.get("date_to", "").strip()

    # --------------------------------------------------------------
    # 1. VALIDAR DATOS RECIBIDOS
    # --------------------------------------------------------------
    try:
        owner_id = int(owner_id_raw)

    except (TypeError, ValueError):

        messages.error(
            request,
            "Debe seleccionar un propietario válido."
        )

        return redirect(
            "owner_settlement_admin_list"
        )

    try:
        date_from = datetime.strptime(
            date_from_raw,
            "%Y-%m-%d",
        ).date()

        date_to = datetime.strptime(
            date_to_raw,
            "%Y-%m-%d",
        ).date()

    except (TypeError, ValueError):

        messages.error(
            request,
            "Debe ingresar un rango de fechas válido."
        )

        return redirect(
            "owner_settlement_admin_list"
        )

    if date_from > date_to:

        messages.error(
            request,
            "La fecha inicial no puede ser posterior a la fecha final."
        )

        return redirect(
            "owner_settlement_admin_list"
        )

    # --------------------------------------------------------------
    # 2. VALIDAR PROPIETARIO Y EMPRESA
    # --------------------------------------------------------------
    owners = (
        FleetOwner.objects
        .filter(
            pk=owner_id,
            is_active=True,
        )
        .select_related(
            "company",
        )
    )

    # Usuario administrativo normal:
    # solo puede trabajar con propietarios de su empresa.
    if not user.is_superuser:

        if not profile or not profile.is_active:

            raise PermissionDenied(
                "El usuario no tiene un perfil operativo activo."
            )

        if not profile.company_id:

            raise PermissionDenied(
                "El usuario no tiene una empresa asociada."
            )

        owners = owners.filter(
            company=profile.company
        )

    owner = get_object_or_404(
        owners,
        pk=owner_id,
    )

    # --------------------------------------------------------------
    # 3. GENERACIÓN ATÓMICA
    # --------------------------------------------------------------
    with transaction.atomic():

        # ----------------------------------------------------------
        # Tickets que ya pertenecen a alguna liquidación.
        #
        # Se usa una subconsulta en vez de:
        #
        # owner_settlement_item__isnull=True
        #
        # porque esa expresión genera un LEFT OUTER JOIN y PostgreSQL
        # no permite SELECT FOR UPDATE sobre el lado nullable de ese JOIN.
        # ----------------------------------------------------------
        tickets_ya_liquidados = (
            OwnerSettlementTicket.objects
            .values_list(
                "ticket_id",
                flat=True,
            )
        )

        # ----------------------------------------------------------
        # Tickets disponibles para liquidar
        # ----------------------------------------------------------
        tickets = list(
            Ticket.objects
            .select_for_update()
            .filter(
                revenue_owner=owner,
                revenue_bus__company=owner.company,
                created_at__date__range=[
                    date_from,
                    date_to,
                ],
            )
            .exclude(
                pk__in=tickets_ya_liquidados
            )
            .select_related(
                "revenue_bus",
            )
            .order_by(
                "created_at",
                "id",
            )
        )

        # ----------------------------------------------------------
        # 4. SIN TICKETS LIQUIDABLES
        # ----------------------------------------------------------
        if not tickets:

            messages.warning(
                request,
                (
                    f"No existen tickets pendientes de liquidar para "
                    f"{owner.display_name} entre "
                    f"{date_from.strftime('%d/%m/%Y')} y "
                    f"{date_to.strftime('%d/%m/%Y')}."
                )
            )

            return redirect(
                "owner_settlement_admin_list"
            )

        # ----------------------------------------------------------
        # 5. CALCULAR TOTALES
        # ----------------------------------------------------------
        gross_amount = sum(
            (
                ticket.price
                for ticket in tickets
            ),
            Decimal("0"),
        )

        # Por ahora no existe comisión configurada.
        commission_amount = Decimal("0")

        net_amount = (
            gross_amount
            - commission_amount
        )

        # ----------------------------------------------------------
        # 6. CREAR CABECERA DE LIQUIDACIÓN
        # ----------------------------------------------------------
        settlement = OwnerSettlement.objects.create(
            company=owner.company,
            owner=owner,

            date_from=date_from,
            date_to=date_to,

            gross_amount=gross_amount,
            commission_amount=commission_amount,
            net_amount=net_amount,

            status=OwnerSettlement.STATUS_PENDING,

            created_by=user,

            notes=(
                "Liquidación generada desde administración."
            ),
        )

        # ----------------------------------------------------------
        # 7. ASOCIAR TICKETS
        # ----------------------------------------------------------
        settlement_items = [
            OwnerSettlementTicket(
                settlement=settlement,
                ticket=ticket,

                # Congelamos el monto utilizado en la liquidación.
                amount=ticket.price,
            )
            for ticket in tickets
        ]

        OwnerSettlementTicket.objects.bulk_create(
            settlement_items
        )

    # --------------------------------------------------------------
    # 8. MENSAJE FINAL
    # --------------------------------------------------------------
    messages.success(
        request,
        (
            f"Liquidación #{settlement.id} creada correctamente. "
            f"{len(tickets)} ticket(s) incluidos. "
            f"Monto líquido: ${net_amount:,.0f}."
        )
    )

    return redirect(
        "owner_settlement_admin_list"
    )
    
@login_required
@supervisor_required
def owner_settlement_admin_detail(request, settlement_id):
    """
    FASE 2.18.3-A3.3-G

    Detalle administrativo de una liquidación.

    - Superuser: puede ver cualquier liquidación.
    - Admin/Supervisor: solo liquidaciones de su empresa.
    - Propietario: no tiene acceso a esta vista.
    """

    user = request.user
    profile = getattr(user, "profile", None)

    settlements = (
        OwnerSettlement.objects
        .select_related(
            "company",
            "owner",
            "created_by",
        )
    )

    # --------------------------------------------------------------
    # ALCANCE DE EMPRESA
    # --------------------------------------------------------------
    if not user.is_superuser:

        if not profile or not profile.is_active:
            raise PermissionDenied(
                "El usuario no tiene un perfil operativo activo."
            )

        if not profile.company_id:
            raise PermissionDenied(
                "El usuario no tiene una empresa asociada."
            )

        settlements = settlements.filter(
            company=profile.company
        )

    settlement = get_object_or_404(
        settlements,
        pk=settlement_id,
    )

    # --------------------------------------------------------------
    # TICKETS DE LA LIQUIDACIÓN
    # --------------------------------------------------------------
    items = (
        settlement.items
        .select_related(
            "ticket",
            "ticket__revenue_bus",
            "ticket__revenue_owner",
            "ticket__trip",
            "ticket__trip__route",
            "ticket__seat",
        )
        .order_by(
            "ticket__created_at",
            "ticket__id",
        )
    )

    context = {
        "settlement": settlement,
        "items": items,
    }

    return render(
        request,
        "booking/owner_settlement_admin_detail.html",
        context,
    )

@login_required
def dashboard_redirect(request):
    """
    Punto único de entrada después del login.

    Reglas:
    - Superuser -> panel administrativo global.
    - Admin -> panel administrativo/operacional de empresa.
    - Coordinator -> panel operacional.
    - Supervisor/Vendedor -> POS.
    - Cajero -> Caja.
    - Convenio -> Convenios.
    - Owner -> Panel propietario.
    - Executive/Secretary -> mensajes temporales.
    """

    user = request.user

    # ============================================================
    # SUPERUSER
    # ============================================================
    if user.is_superuser:
        return redirect(
            "coordinator:dashboard"
        )

    # ============================================================
    # PERFIL
    # ============================================================
    profile = getattr(
        user,
        "profile",
        None,
    )

    if not profile:
        messages.error(
            request,
            "El usuario no tiene un perfil operativo configurado.",
        )

        logout(request)

        return redirect(
            "login"
        )

    if not profile.is_active:
        messages.error(
            request,
            "Su cuenta se encuentra desactivada.",
        )

        logout(request)

        return redirect(
            "login"
        )

    role = profile.role

    # ============================================================
    # ADMINISTRACIÓN / OPERACIONES
    # ============================================================
    if role == "admin":
        return redirect(
            "coordinator:dashboard"
        )

    if role == "coordinator":
        return redirect(
            "coordinator:dashboard"
        )

    # ============================================================
    # POS
    # ============================================================
    if role == "supervisor":
        return redirect(
            "pos_home"
        )

    if role == "vendedor":
        return redirect(
            "pos_home"
        )

    if role == "cajero":
        return redirect(
            "pos_caja"
        )

    # ============================================================
    # CONVENIOS
    # ============================================================
    if role == "convenio":
        return redirect(
            "contract_dashboard"
        )

    # ============================================================
    # PROPIETARIO
    # ============================================================
    if role == "owner":
        return redirect(
            "owner_dashboard"
        )

    # ============================================================
    # ROLES FUTUROS
    # ============================================================
    if role == "executive":
        return HttpResponse(
            (
                "Perfil Dueño / Gerencia configurado correctamente. "
                "El dashboard ejecutivo será habilitado "
                "en la siguiente fase."
            ),
            status=200,
        )

    if role == "secretary":
        return HttpResponse(
            (
                "Perfil Secretaria configurado correctamente. "
                "Su panel operativo será habilitado "
                "en la siguiente fase."
            ),
            status=200,
        )

    # ============================================================
    # ROL SIN DESTINO
    # ============================================================
    messages.error(
        request,
        "El usuario tiene un rol sin página de inicio configurada.",
    )

    logout(request)

    return redirect(
        "login"
    )

@login_required
@role_required(['admin', 'supervisor', 'convenio'])
def contract_dashboard(request):
    if request.user.is_superuser or (hasattr(request.user, 'profile') and request.user.profile.role in ['admin', 'supervisor']):
        contracts = CompanyContract.objects.filter(is_active=True)
    else:
        contracts = CompanyContract.objects.filter(is_active=True)

    total_contracts = contracts.count()
    total_credit_available = sum(c.available_credit for c in contracts)
    total_credit_used = sum(c.used_credit for c in contracts)

    contract_id = request.GET.get('contract_id')
    selected_contract = None
    employees = []
    recent_purchases = []
    if contract_id and contract_id.isdigit():
        selected_contract = get_object_or_404(CompanyContract, pk=int(contract_id))
        employees = ContractEmployee.objects.filter(contract=selected_contract, is_active=True).select_related('customer')
        recent_purchases = Ticket.objects.filter(
            contract=selected_contract,
            payment_method='credit'
        ).order_by('-created_at')[:20]

    context = {
        'title': 'Panel de Convenios',
        'contracts': contracts,
        'selected_contract': selected_contract,
        'employees': employees,
        'recent_purchases': recent_purchases,
        'total_contracts': total_contracts,
        'total_credit_available': total_credit_available,
        'total_credit_used': total_credit_used,
    }
    return render(request, 'booking/contract_dashboard.html', context)

@login_required
@role_required(['admin', 'supervisor', 'convenio'])
def contract_employees(request, contract_id):
    contract = get_object_or_404(CompanyContract, pk=contract_id)
    employees = ContractEmployee.objects.filter(contract=contract).select_related('customer')

    if request.method == 'POST':
        rut_list = request.POST.get('rut_list', '').strip()
        if rut_list:
            import re
            ruts = re.split(r'[\n,\s]+', rut_list)
            ruts = [r.strip() for r in ruts if r.strip()]
            added_count = 0
            errors = []
            for rut in ruts:
                rut_clean = re.sub(r'[^0-9kK]', '', rut)
                if not rut_clean:
                    continue
                customer, created = Customer.objects.get_or_create(
                    national_id=rut_clean,
                    defaults={'full_name': f"Trabajador {rut_clean}"}
                )
                if ContractEmployee.objects.filter(contract=contract, customer=customer).exists():
                    errors.append(f"RUT {rut_clean} ya está registrado en este contrato.")
                    continue
                ContractEmployee.objects.create(
                    contract=contract,
                    customer=customer,
                    is_active=True
                )
                added_count += 1
            if added_count > 0:
                messages.success(request, f"Se agregaron {added_count} empleados correctamente.")
            if errors:
                messages.warning(request, f"Errores: {', '.join(errors[:5])}" + (f" y {len(errors)-5} más" if len(errors) > 5 else ""))
            return redirect('contract_employees', contract_id=contract.id)
        else:
            customer_id = request.POST.get('customer_id')
            employee_id = request.POST.get('employee_id', '').strip()
            customer = None

            if customer_id:
                customer = get_object_or_404(Customer, pk=customer_id)
            else:
                rut = request.POST.get('rut', '').strip()
                name = request.POST.get('full_name', '').strip()
                phone = request.POST.get('phone', '').strip()
                email = request.POST.get('email', '').strip()
                if rut and name:
                    customer, _ = Customer.objects.get_or_create(
                        national_id=rut,
                        defaults={'full_name': name, 'phone': phone, 'email': email}
                    )
                else:
                    messages.error(request, "Debe ingresar al menos RUT y nombre completo.")
                    return redirect('contract_employees', contract_id=contract.id)

            if customer:
                if ContractEmployee.objects.filter(contract=contract, customer=customer).exists():
                    messages.warning(request, f"El empleado {customer.full_name} ya está registrado en este contrato.")
                else:
                    ContractEmployee.objects.create(
                        contract=contract,
                        customer=customer,
                        employee_id=employee_id,
                        is_active=True
                    )
                    messages.success(request, f"Empleado {customer.full_name} agregado al contrato.")
            return redirect('contract_employees', contract_id=contract.id)

    search = request.GET.get('search', '').strip()
    if search:
        employees = employees.filter(
            Q(customer__full_name__icontains=search) |
            Q(customer__national_id__icontains=search) |
            Q(employee_id__icontains=search)
        )

    context = {
        'title': f'Empleados - {contract.company.name}',
        'contract': contract,
        'employees': employees,
        'search': search,
    }
    return render(request, 'booking/contract_employees.html', context)

@login_required
@role_required(['admin', 'supervisor', 'convenio'])
def contract_employee_toggle(request, employee_id):
    if request.method == 'POST':
        employee = get_object_or_404(ContractEmployee, pk=employee_id)
        employee.is_active = not employee.is_active
        employee.save()
        return JsonResponse({'success': True, 'is_active': employee.is_active})
    return JsonResponse({'success': False}, status=400)

@login_required
@role_required(['admin', 'supervisor', 'convenio'])
def api_contract_employees(request):
    contract_id = request.GET.get('contract_id')
    query = request.GET.get('q', '').strip()

    if not contract_id or not query:
        return JsonResponse([], safe=False)

    contract = get_object_or_404(CompanyContract, pk=contract_id)
    employees = ContractEmployee.objects.filter(
        contract=contract,
        is_active=True
    ).filter(
        Q(customer__national_id__icontains=query) |
        Q(customer__full_name__icontains=query)
    ).select_related('customer')[:10]

    results = [{
        'id': emp.customer.id,
        'national_id': emp.customer.national_id,
        'full_name': emp.customer.full_name,
        'phone': emp.customer.phone,
        'employee_id': emp.employee_id,
    } for emp in employees]

    return JsonResponse(results, safe=False)

@login_required
@role_required(['admin', 'supervisor', 'convenio'])
def pos_trip_convenio(request, trip_id):
    trip = get_object_or_404(Trip.objects.select_related("route__origin", "route__destination", "bus"), id=trip_id)
    if not trip.bus:
        return HttpResponse("Este viaje no tiene asignada una unidad de transporte (Bus).", status=400)

    try:
        _check_terminal_permission(request, trip)
    except PermissionDenied as e:
        messages.error(request, str(e))
        return redirect('pos_home')

    grid_lower, grid_upper, cols = _build_trip_grid(trip, request.user)

    if request.user.is_superuser or (hasattr(request.user, 'profile') and request.user.profile.role in ['admin', 'supervisor']):
        contracts = CompanyContract.objects.filter(is_active=True)
    else:
        contracts = CompanyContract.objects.filter(is_active=True)

    context = {
        'trip': trip,
        'cols': cols,
        'grid_lower': json.dumps(grid_lower),
        'grid_upper': json.dumps(grid_upper),
        'mode': 'convenio',
        'contracts': contracts,
    }
    return render(request, "booking/pos_trip.html", context)

@login_required
@role_required(['admin', 'supervisor', 'convenio'])
@transaction.atomic
def pos_checkout_convenio(request, trip_id):
    if request.method != "POST":
        messages.error(request, "Acción u método de envío inválido.")
        return redirect("pos_home")

    trip = get_object_or_404(Trip.objects.select_for_update(), pk=trip_id)

    try:
        _check_terminal_permission(request, trip)
    except PermissionDenied as e:
        messages.error(request, str(e))
        return redirect('pos_home')

    raw_seats = request.POST.get("seats", "").strip()
    try:
        chosen = json.loads(raw_seats) if raw_seats else []
    except Exception:
        chosen = []

    if not isinstance(chosen, list) or not chosen:
        messages.error(request, "No seleccionó ningún asiento para procesar la transacción.")
        return redirect("pos_trip_convenio", trip_id=trip.id)

    contract_id = request.POST.get("contract_id")
    if not contract_id:
        messages.error(request, "Debe seleccionar un contrato de convenio.")
        return redirect("pos_trip_convenio", trip_id=trip.id)

    contract = get_object_or_404(CompanyContract, pk=contract_id)
    total_amount = trip.route.base_price * len(chosen)

    if not contract.can_purchase(total_amount):
        messages.error(request, f"Crédito insuficiente. Disponible: ${contract.available_credit:,.0f}")
        return redirect("pos_trip_convenio", trip_id=trip.id)

    discount_factor = 1 - (contract.discount_percentage / 100)
    final_price_per_ticket = trip.route.base_price * discount_factor

    created_tickets = []
    skipped = []

    numbers = [str(item.get("number")).strip() for item in chosen if item.get("number")]
    decks = [int(item.get("deck") or 1) for item in chosen if item.get("deck")]

    seats_db = {
        f"{s.deck}-{s.number}": s
        for s in Seat.objects.select_for_update()
                 .filter(bus=trip.bus, number__in=numbers, deck__in=decks)
                 .order_by('id')
    }

    for item in chosen:
        try:
            deck = int(item.get("deck"))
            number = str(item.get("number")).strip()
        except (ValueError, TypeError):
            continue

        key = f"{deck}-{number}"
        seat = seats_db.get(key)

        if not seat:
            skipped.append(number)
            continue

        if Ticket.objects.filter(trip=trip, seat=seat).exists():
            skipped.append(number)
            continue

        passenger_rut = item.get("passenger_rut", "").strip()
        passenger_name = item.get("passenger_name", "").strip()
        passenger_phone = item.get("passenger_phone", "").strip()

        customer = None
        if passenger_rut:
            customer, _ = Customer.objects.get_or_create(
                national_id=passenger_rut,
                defaults={
                    'full_name': passenger_name or "Pasajero Convenio",
                    'phone': passenger_phone
                }
            )
        else:
            customer, _ = Customer.objects.get_or_create(
                national_id=f"CONV-{timezone.now().timestamp()}",
                defaults={'full_name': passenger_name or "Pasajero Convenio"}
            )

        ticket = Ticket.create_for_sale(
            trip=trip,
            seat=seat,
            buyer_name=customer.full_name if customer else "Pasajero Convenio",
            national_id=customer.national_id if customer else "",
            price=final_price_per_ticket,
            created_by=request.user,
            payment_method='credit',
            customer=customer,
            contract=contract,
        )
        created_tickets.append(ticket)

        if customer and not ContractEmployee.objects.filter(contract=contract, customer=customer).exists():
            ContractEmployee.objects.create(
                contract=contract,
                customer=customer,
                is_active=True
            )

    if not created_tickets:
        messages.error(request, f"Error: Los asientos solicitados ({', '.join(skipped)}) ya fueron vendidos.")
        return redirect("pos_trip_convenio", trip_id=trip.id)

    SeatHold.objects.filter(trip=trip, seat__in=[t.seat for t in created_tickets]).delete()

    total = final_price_per_ticket * len(created_tickets)
    contract.used_credit += total
    contract.save(update_fields=['used_credit'])

    _audit_log(
        request,
        action='purchase_convenio',
        model_name='Ticket',
        object_id=','.join(str(t.id) for t in created_tickets),
        object_repr=f"Compra convenio de {len(created_tickets)} tickets",
        changes={
            'trip_id': trip.id,
            'total': float(total),
            'contract_id': contract.id,
            'discount_percentage': float(contract.discount_percentage)
        }
    )

    messages.success(request, f"Compra por convenio realizada con éxito. {len(created_tickets)} pasaje(s) emitido(s).")
    return redirect('contract_dashboard')

# ============================================================================
# 15. CIERRE DE CAJA Y LOGOUT
# ============================================================================

@login_required
def cerrar_caja_y_logout(request):
    try:
        fecha_hoy = timezone.now().date()
        caja = CashRegister.objects.get(user=request.user, opening_date__date=fecha_hoy, status='open')
        ventas_hoy = Ticket.objects.filter(created_at__date=fecha_hoy, created_by=request.user)
        total_ventas = ventas_hoy.aggregate(total=Sum('price'))['total'] or Decimal('0.00')
        total_boletos = ventas_hoy.count()

        caja.closing_date = timezone.now()
        caja.total_sales = total_ventas
        caja.total_tickets = total_boletos
        caja.closing_balance = total_ventas
        caja.status = 'closed'
        caja.save()

        reporte, _ = DailyReport.objects.get_or_create(date=fecha_hoy)
        reporte.total_tickets += total_boletos
        reporte.total_revenue += total_ventas
        reporte.total_cash_registers = CashRegister.objects.filter(opening_date__date=fecha_hoy, status='closed').count()
        reporte.save()

        messages.success(request, f"Caja cerrada con éxito. Total recaudado: ${total_ventas:,.0f} - Boletos: {total_boletos}")
    except CashRegister.DoesNotExist:
        messages.warning(request, "No tenías una caja abierta. Se cerrará la sesión de todos modos.")
    except Exception as e:
        logger.error(f"Error en cerrar_caja_y_logout: {e}", exc_info=True)
        messages.error(request, f"Error al cerrar la caja: {str(e)}. Cerrando sesión de todos modos.")

    logout(request)
    return redirect('login')

def logout_view(request):
    logout(request)
    return redirect('login')


@require_POST
@login_required
@vendedor_required
def api_clean_all_holds(request):
    """
    Limpia TODOS los holds expirados de un viaje.
    """
    import json
    try:
        data = json.loads(request.body)
        trip_id = data.get('trip_id')

        if not trip_id:
            return JsonResponse({'error': 'trip_id requerido'}, status=400)

        # Limpiar TODOS los holds expirados del viaje
        count = SeatHold.objects.filter(
            trip_id=trip_id,
            active=True,
            expires_at__lt=timezone.now()
        ).update(active=False)

        # También limpiar holds que están activos pero con usuario nulo (anónimos)
        count_anon = SeatHold.objects.filter(
            trip_id=trip_id,
            user__isnull=True,
            active=True,
            expires_at__lt=timezone.now()
        ).update(active=False)

        total = count + count_anon

        return JsonResponse({
            'success': True,
            'cleaned': total,
            'message': f'Se limpiaron {total} holds expirados'
        })

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)
