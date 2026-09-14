# client_portal/views.py
# ==================== IMPORTS ====================
import base64
import io
import json
import logging
from datetime import datetime, timedelta
from decimal import Decimal

# Django
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Case, Count, F, IntegerField, Q, Sum, When, DecimalField
from django.db.models.functions import ExtractHour, ExtractWeekDay, TruncDate, TruncMonth
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST
from django.views.decorators.csrf import ensure_csrf_cookie

# Terceros
import qrcode
from bus_tickets import settings
import xlsxwriter

# Proyecto
from booking.models import City, Customer, Promotion, Route, Seat, SeatHold, Ticket, Trip
from booking.views import _build_trip_grid, calculate_final_price

# Configurar logging
logger = logging.getLogger(__name__)

# ReportLab (opcional)
try:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
    from reportlab.lib.pagesizes import A4, letter
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm, inch
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas
    from reportlab.platypus import Image, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    HAS_REPORTLAB = True
except ImportError:
    HAS_REPORTLAB = False

# ... resto del código (todas las funciones)

# ==================== HELPERS ====================

def is_owner_or_manager(user):
    """
    Solo usuarios con rol admin o supervisor pueden ver el dashboard
    """
    if user.is_superuser:
        return True
    if hasattr(user, 'profile'):
        return user.profile.role in ['admin', 'supervisor']
    return False


def mask_rut(rut):
    """Oculta el RUT mostrando solo los primeros 2 dígitos y el último."""
    if not rut:
        return ''
    rut = rut.replace('.', '').replace('-', '').strip()
    if len(rut) <= 4:
        return rut
    body = rut[:-1]
    dv = rut[-1]
    if len(body) > 4:
        visible = body[:2] + 'XXX' + body[-2:]
    else:
        visible = body
    return f"{visible}-{dv}"


def mask_phone(phone):
    """Oculta el teléfono mostrando los primeros 2 y últimos 2 dígitos."""
    if not phone:
        return ''
    phone = phone.replace(' ', '').replace('+', '').replace('-', '').strip()
    if len(phone) <= 4:
        return phone
    return phone[:2] + 'XXXX' + phone[-2:]


# ==================== VISTAS PÚBLICAS DEL CLIENTE ====================

def home(request):
    """Página de inicio con buscador de viajes."""
    # ✅ Obtener estadísticas reales
    total_passengers = Ticket.objects.count()
    years_experience = timezone.now().year - 1998  # Año de fundación
    total_routes = Route.objects.filter(is_active=True).count()
    punctuality = 95  # Podría calcularse de datos reales
    
    # ✅ Obtener próximos viajes
    upcoming_trips = Trip.objects.filter(
        departure__gte=timezone.now()
    ).select_related('route__origin', 'route__destination', 'bus')[:5]
    
    upcoming_trips_data = []
    for trip in upcoming_trips:
        sold = Ticket.objects.filter(trip=trip).count()
        upcoming_trips_data.append({
            'departure_time': trip.departure,
            'origin': trip.route.origin.name,
            'destination': trip.route.destination.name,
            'price': trip.route.base_price,
            'available_seats': trip.seats_total - sold,
            'link': reverse('client_portal:seatmap', args=[trip.id])
        })
    
    context = {
        'paso_actual': 1,
        'stats': {
            'passengers': total_passengers,
            'years': years_experience,
            'routes': total_routes,
            'punctuality': punctuality,
        },
        'upcoming_trips': upcoming_trips_data,
        'popular_destinations': [],  # Obtener de base de datos
        'testimonials': [],  # Obtener de base de datos
        'fleet': [],  # Obtener de base de datos
        'onboard_services': [
            {'icon': 'fas fa-wifi', 'name': 'WiFi Gratis', 'description': 'Conectividad durante todo el viaje'},
            {'icon': 'fas fa-tv', 'name': 'Entretenimiento', 'description': 'Pantallas y contenido a bordo'},
            {'icon': 'fas fa-utensils', 'name': 'Snacks', 'description': 'Servicio de refrigerio incluido'},
            {'icon': 'fas fa-charging-station', 'name': 'Carga USB', 'description': 'Puertos USB en cada asiento'},
        ],
        # ... otros datos
    }
    return render(request, 'client_portal/home.html', context)


def search_trips(request):
    # Obtener parámetros de búsqueda
    origin = request.GET.get('origin', '').strip()
    destination = request.GET.get('destination', '').strip()
    date_str = request.GET.get('date', '').strip()
    return_date = request.GET.get('return_date', '').strip()

    # Validación básica
    if not origin or not destination or not date_str:
        return render(request, 'client_portal/search_results.html', {
            'trips': [],
            'origin': origin,
            'destination': destination,
            'date': date_str,
            'return_date': return_date,
            'error': 'Por favor completa todos los campos de búsqueda'
        })

    # Validar fecha
    try:
        date_obj = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        return render(request, 'client_portal/search_results.html', {
            'trips': [],
            'origin': origin,
            'destination': destination,
            'date': date_str,
            'return_date': return_date,
            'error': 'La fecha ingresada no es válida'
        })

    # ✅ OPTIMIZADO: Una sola consulta con anotaciones
    trips = Trip.objects.filter(
        route__origin__name__iexact=origin,
        route__destination__name__iexact=destination,
        departure__date=date_obj,
        departure__gte=timezone.now()
    ).select_related(
        'route', 'bus', 'route__origin', 'route__destination',
        'route__origin_terminal', 'route__destination_terminal',
        'bus__company'
    ).annotate(
        sold_count=Count('tickets'),
        hold_count=Count('holds', filter=Q(holds__active=True, holds__expires_at__gt=timezone.now()))
    ).order_by('departure')

    # ===== FILTRO POR TIEMPO DE CORTE =====
    available_trips = []
    for trip in trips:
        if trip.cutoff_minutes == 0:
            available_trips.append(trip)
        else:
            cutoff_time = trip.departure - timedelta(minutes=trip.cutoff_minutes)
            if timezone.now() < cutoff_time:
                available_trips.append(trip)

    # Procesar cada viaje con disponibilidad
    trips_data = []
    for trip in available_trips:
        sold = trip.sold_count
        holds = trip.hold_count
        free = trip.seats_total - sold - holds

        service_type = 'semi'
        if trip.bus.service_type == 'cama':
            service_type = 'cama'

        origin_terminal = trip.route.origin_terminal.name if hasattr(trip.route, 'origin_terminal') and trip.route.origin_terminal else 'Terminal Cejer'
        destination_terminal = trip.route.destination_terminal.name if hasattr(trip.route, 'destination_terminal') and trip.route.destination_terminal else 'Terminal Cejer'

        trips_data.append({
            'id': trip.id,
            'departure_time': trip.departure.strftime('%H:%M'),
            'departure_date': trip.departure,
            'arrival_time': trip.arrival.strftime('%H:%M') if trip.arrival else '--:--',
            'origin_terminal': origin_terminal,
            'destination_terminal': destination_terminal,
            'bus_plate': trip.bus.plate,
            'bus_company': trip.bus.company.name if trip.bus.company else 'Cejer',
            'price': float(trip.route.base_price),
            'free_seats': free,
            'total_seats': trip.seats_total,
            'floors': trip.bus.floors,
            'service_type': service_type,
        })

    return render(request, 'client_portal/search_results.html', {
        'trips': trips_data,
        'origin': origin,
        'destination': destination,
        'date': date_str,
        'return_date': return_date,
        'total_results': len(trips_data),
    })


@ensure_csrf_cookie
def seatmap(request, trip_id):
    """
    Mapa interactivo de asientos para venta web.

    ensure_csrf_cookie obliga a Django a enviar la cookie CSRF
    incluso en la primera visita o en ventana incógnita.
    """

    # ============================================================
    # 1. ASEGURAR SESIÓN
    # ============================================================

    if not request.session.session_key:
        request.session.create()

    session_key = request.session.session_key

    # ============================================================
    # 2. USUARIO ACTUAL
    # ============================================================

    current_user = (
        request.user
        if request.user.is_authenticated
        else None
    )

    # ============================================================
    # 3. OBTENER VIAJE
    # ============================================================

    trip = get_object_or_404(
        Trip.objects.select_related(
            "bus",
            "route",
            "route__origin",
            "route__destination",
        ),
        pk=trip_id,
    )

    # ============================================================
    # 4. VALIDACIÓN DE CORTE DE VENTA WEB
    # ============================================================

    if trip.cutoff_minutes > 0:

        cutoff_time = (
            trip.departure
            - timedelta(
                minutes=trip.cutoff_minutes
            )
        )

        if timezone.now() >= cutoff_time:

            messages.warning(
                request,
                (
                    "Las compras web para este viaje se cierran "
                    f"{trip.cutoff_minutes} minutos antes "
                    "de la salida."
                ),
            )

            return redirect(
                "client_portal:search_trips"
            )

    # ============================================================
    # 5. MENSAJE DE CANCELACIÓN
    # ============================================================

    cancel_message = request.session.pop(
        "cancel_message",
        None,
    )

    if cancel_message:
        messages.info(
            request,
            cancel_message,
        )

    # ============================================================
    # 6. CONSTRUIR MAPA
    # ============================================================

    grid_lower, grid_upper, cols = _build_trip_grid(
        trip=trip,
        current_user=current_user,
        session_key=session_key,
    )

    # ============================================================
    # 7. CONTEXTO
    # ============================================================

    context = {
        "paso_actual": 3,
        "trip": trip,

        "grid_lower": json.dumps(
            grid_lower,
            ensure_ascii=False,
        ),

        "grid_upper": json.dumps(
            grid_upper,
            ensure_ascii=False,
        ),

        "cols": cols,
    }

    # ============================================================
    # 8. RENDER
    # ============================================================

    return render(
        request,
        "client_portal/seatmap.html",
        context,
    )


@ensure_csrf_cookie
def seatmap_partial(request, trip_id):
    """
    Retorna el HTML parcial del croquis.

    Mantiene session_key y CSRF consistentes con la vista principal.
    """

    # ============================================================
    # 1. ASEGURAR SESIÓN
    # ============================================================

    if not request.session.session_key:
        request.session.create()

    session_key = request.session.session_key

    # ============================================================
    # 2. USUARIO ACTUAL
    # ============================================================

    current_user = (
        request.user
        if request.user.is_authenticated
        else None
    )

    # ============================================================
    # 3. OBTENER VIAJE
    # ============================================================

    trip = get_object_or_404(
        Trip.objects.select_related(
            "bus",
            "route",
            "route__origin",
            "route__destination",
        ),
        pk=trip_id,
    )

    # ============================================================
    # 4. CONSTRUIR MAPA
    # ============================================================

    grid_lower, grid_upper, cols = _build_trip_grid(
        trip=trip,
        current_user=current_user,
        session_key=session_key,
    )

    # ============================================================
    # 5. CONTEXTO
    # ============================================================

    context = {
        "trip": trip,
        "cols": cols,
        "grid_lower": grid_lower,
        "grid_upper": grid_upper,
    }

    html = render_to_string(
        "client_portal/partials/seatmap.html",
        context,
        request=request,
    )

    return HttpResponse(html)

@require_POST
def hold_seat(request, trip_id):
    """
    Reserva o libera temporalmente un asiento para venta web.

    SEGURIDAD:
    - Compatible con clientes autenticados y anónimos.
    - Usa session_key para identificar visitantes.
    - Usa transaction.atomic().
    - Usa SELECT FOR UPDATE sobre Seat.
    - Verifica nuevamente Ticket dentro de la transacción.
    - Evita reservas simultáneas del mismo asiento.
    """

    # =========================================================
    # 1. ASEGURAR SESSION KEY
    # =========================================================
    if not request.session.session_key:
        request.session.create()

    session_key = request.session.session_key

    # =========================================================
    # 2. LEER REQUEST
    # =========================================================
    try:

        content_type = request.content_type or ""

        if "application/json" in content_type:

            try:
                data = json.loads(
                    request.body.decode("utf-8")
                )
            except (json.JSONDecodeError, UnicodeDecodeError):
                return JsonResponse(
                    {
                        "success": False,
                        "error": "JSON inválido.",
                        "code": "INVALID_JSON",
                    },
                    status=400,
                )

        else:
            data = request.POST

    except Exception:

        logger.exception(
            "Error leyendo datos de reserva de asiento."
        )

        return JsonResponse(
            {
                "success": False,
                "error": "No fue posible procesar la solicitud.",
                "code": "INVALID_REQUEST",
            },
            status=400,
        )

    # =========================================================
    # 3. VALIDAR DATOS
    # =========================================================
    seat_number = str(
        data.get("seat_number", "")
    ).strip()

    if not seat_number:
        return JsonResponse(
            {
                "success": False,
                "error": "Número de asiento requerido.",
                "code": "SEAT_REQUIRED",
            },
            status=400,
        )

    try:
        deck = int(
            data.get("deck", 1)
        )
    except (TypeError, ValueError):
        return JsonResponse(
            {
                "success": False,
                "error": "Piso de asiento inválido.",
                "code": "INVALID_DECK",
            },
            status=400,
        )

    if deck not in (1, 2):
        return JsonResponse(
            {
                "success": False,
                "error": "Piso de asiento inválido.",
                "code": "INVALID_DECK",
            },
            status=400,
        )

    release_requested = str(
        data.get("release", "")
    ).lower() in {
        "true",
        "1",
        "yes",
        "on",
    }

    # =========================================================
    # 4. OBTENER VIAJE
    # =========================================================
    trip = get_object_or_404(
        Trip.objects.select_related(
            "bus",
            "route",
        ),
        pk=trip_id,
    )

    # =========================================================
    # 5. VALIDAR CORTE DE VENTA WEB
    # =========================================================
    if trip.cutoff_minutes > 0:

        cutoff_time = (
            trip.departure
            - timedelta(
                minutes=trip.cutoff_minutes
            )
        )

        if timezone.now() >= cutoff_time:

            return JsonResponse(
                {
                    "success": False,
                    "error": (
                        "La venta web para este viaje "
                        "ya se encuentra cerrada."
                    ),
                    "code": "SALES_CLOSED",
                },
                status=409,
            )

    # =========================================================
    # 6. BUSCAR ASIENTO
    # =========================================================
    seat = (
        Seat.objects
        .filter(
            bus=trip.bus,
            number=seat_number,
            deck=deck,
        )
        .first()
    )

    if seat is None:

        return JsonResponse(
            {
                "success": False,
                "error": "El asiento no existe.",
                "code": "SEAT_NOT_FOUND",
            },
            status=404,
        )

    # Usuario autenticado o None
    current_user = (
        request.user
        if request.user.is_authenticated
        else None
    )

    # =========================================================
    # 7. LIBERAR ASIENTO
    # =========================================================
    if release_requested:

        try:

            released = SeatHold.release(
                trip=trip,
                seat=seat,
                user=current_user,
                session_key=session_key,
            )

            return JsonResponse(
                {
                    "success": True,
                    "released": released > 0,
                    "message": (
                        "Asiento liberado."
                        if released
                        else "El asiento ya estaba liberado."
                    ),
                }
            )

        except Exception:

            logger.exception(
                "Error liberando asiento %s "
                "del viaje %s.",
                seat_number,
                trip_id,
            )

            return JsonResponse(
                {
                    "success": False,
                    "error": (
                        "No fue posible liberar el asiento."
                    ),
                    "code": "RELEASE_ERROR",
                },
                status=500,
            )

    # =========================================================
    # 8. CREAR / RENOVAR RESERVA
    # =========================================================
    try:

        hold = SeatHold.hold(
            trip=trip,
            seat=seat,
            user=current_user,
            session_key=session_key,
            minutes=10,
        )

    except ValueError as exc:

        message = str(exc)

        if "vendido" in message.lower():
            code = "ALREADY_SOLD"
        else:
            code = "HELD_BY_OTHER"

        return JsonResponse(
            {
                "success": False,
                "error": message,
                "code": code,
            },
            status=409,
        )

    except Seat.DoesNotExist:

        return JsonResponse(
            {
                "success": False,
                "error": "El asiento ya no existe.",
                "code": "SEAT_NOT_FOUND",
            },
            status=404,
        )

    except Exception:

        logger.exception(
            "Error reservando asiento %s "
            "del viaje %s.",
            seat_number,
            trip_id,
        )

        return JsonResponse(
            {
                "success": False,
                "error": (
                    "No fue posible reservar el asiento. "
                    "Intenta nuevamente."
                ),
                "code": "HOLD_ERROR",
            },
            status=500,
        )

    # =========================================================
    # 9. RESPUESTA
    # =========================================================
    return JsonResponse(
        {
            "success": True,
            "hold_id": hold.id,
            "seat_number": hold.seat.number,
            "deck": hold.seat.deck,
            "expires_at": hold.expires_at.isoformat(),
            "message": "Asiento reservado por 10 minutos.",
        },
        status=200,
    )

# ==================== CHECKOUT (CORREGIDO) ====================

@login_required
def checkout(request, trip_id):
    """
    FASE 2B - Checkout con reserva previa al pago.

    NO crea Ticket.
    NO elimina SeatHold.
    NO incrementa used_count de Promotion.

    Crea:
        BookingOrder
        BookingOrderSeat
        PaymentTransaction

    El Ticket se emitirá sólo después de confirmar Webpay en FASE 2C.
    """
    import re
    import uuid

    from decimal import Decimal, ROUND_HALF_UP

    from django.core.exceptions import ValidationError
    from django.core.validators import validate_email
    from django.db import transaction
    from django.http import JsonResponse
    from django.shortcuts import get_object_or_404, redirect, render
    from django.urls import reverse
    from django.utils import timezone

    from booking.models import (
        BookingOrder,
        BookingOrderSeat,
        Customer,
        PaymentTransaction,
        Promotion,
        Seat,
        SeatHold,
        Ticket,
        Trip,
    )
    from booking.utils import validate_chilean_rut
    from booking.views import calculate_final_price

    def json_error(message, status=400, redirect_url=None, code=None):
        payload = {"success": False, "message": message}
        if redirect_url:
            payload["redirect_url"] = redirect_url
        if code:
            payload["code"] = code
        return JsonResponse(payload, status=status)

    def field_key(deck, number):
        safe_number = re.sub(
            r"[^A-Za-z0-9_-]",
            "_",
            str(number or ""),
        )
        return f"{int(deck or 1)}_{safe_number}"

    def build_unique_order_code():
        for _ in range(10):
            code = "RES-" + uuid.uuid4().hex.upper()
            if not BookingOrder.objects.filter(code=code).exists():
                return code
        raise RuntimeError("No fue posible generar un código de reserva único.")

    def build_unique_buy_order():
        """
        Genera un buy_order único compatible con Webpay Plus.

        Webpay permite máximo 26 caracteres.
        Ejemplo:
            WEB260812181530A1B2C3D4

        Largo total: 23 caracteres.
        """
        for _ in range(10):
            buy_order = (
                "WEB"
                + timezone.now().strftime("%y%m%d%H%M%S")
                + uuid.uuid4().hex[:8].upper()
            )

            if not PaymentTransaction.objects.filter(
                buy_order=buy_order
            ).exists():
                return buy_order

        raise RuntimeError(
            "No fue posible generar una orden de pago única."
        )

    trip = get_object_or_404(
        Trip.objects.select_related("route", "bus"),
        pk=trip_id,
    )

    # ============================================================
    # Corte de venta web
    # ============================================================
    if trip.cutoff_minutes > 0:
        cutoff_time = trip.departure - timedelta(
            minutes=trip.cutoff_minutes
        )

        if timezone.now() >= cutoff_time:
            if request.method == "POST":
                return json_error(
                    (
                        "Las compras web para este viaje se cierran "
                        f"{trip.cutoff_minutes} minutos antes de la salida."
                    ),
                    status=409,
                    redirect_url=reverse("client_portal:search_trips"),
                    code="SALES_CLOSED",
                )

            messages.warning(
                request,
                (
                    "Las compras web para este viaje se cierran "
                    f"{trip.cutoff_minutes} minutos antes de la salida."
                ),
            )
            return redirect("client_portal:search_trips")

    # ============================================================
    # Sesión
    # ============================================================
    session_key = request.session.session_key

    if not session_key:
        request.session.create()
        session_key = request.session.session_key

    # ============================================================
    # Holds actuales
    # ============================================================
    reserved_seats = (
        SeatHold.objects
        .filter(
            trip=trip,
            session_key=session_key,
            active=True,
            expires_at__gt=timezone.now(),
        )
        .select_related("seat")
        .order_by(
            "seat__deck",
            "seat__row",
            "seat__position",
            "seat__id",
        )
    )

    if not reserved_seats.exists():
        if request.method == "POST":
            return json_error(
                (
                    "Tu reserva de asientos ya no está activa. "
                    "Por favor selecciona nuevamente."
                ),
                status=409,
                redirect_url=reverse(
                    "client_portal:seatmap",
                    kwargs={"trip_id": trip_id},
                ),
                code="NO_ACTIVE_HOLDS",
            )

        messages.warning(
            request,
            (
                "No tienes asientos reservados para este viaje. "
                "Por favor, selecciona asientos primero."
            ),
        )
        return redirect(
            "client_portal:seatmap",
            trip_id=trip_id,
        )

    # Cliente pendiente existente
    customer_id = request.session.get("pending_customer_id")
    customer = None

    if customer_id:
        try:
            customer = Customer.objects.get(id=customer_id)
        except Customer.DoesNotExist:
            customer = None

    # ============================================================
    # POST - Crear reserva previa
    # ============================================================
    if request.method == "POST":
        buyer_name = request.POST.get("buyer_name", "").strip()
        buyer_rut = request.POST.get("buyer_rut", "").strip()
        buyer_email = request.POST.get("buyer_email", "").strip().lower()
        buyer_email_confirm = (
            request.POST.get("buyer_email_confirm", "").strip().lower()
        )
        buyer_phone = request.POST.get("buyer_phone", "").strip()
        buyer_address = request.POST.get("buyer_address", "").strip()
        payment_method = (
            request.POST.get("payment_method", "webpay").strip().lower()
        )
        discount_code = (
            request.POST.get("applied_coupon_code", "").strip().upper()
        )

        # --------------------------------------------------------
        # Validaciones comprador
        # --------------------------------------------------------
        if not buyer_name:
            return json_error("El nombre del comprador es obligatorio.")

        if not buyer_rut:
            return json_error("El RUT del comprador es obligatorio.")

        if not validate_chilean_rut(buyer_rut):
            return json_error(
                "El RUT del comprador no es válido."
            )

        if not buyer_email:
            return json_error("El correo electrónico es obligatorio.")

        try:
            validate_email(buyer_email)
        except ValidationError:
            return json_error("El correo electrónico no es válido.")

        if buyer_email != buyer_email_confirm:
            return json_error("Los correos electrónicos no coinciden.")

        if not buyer_phone:
            return json_error("El teléfono del comprador es obligatorio.")

        if payment_method != "webpay":
            return json_error(
                "En la venta web sólo está habilitado Webpay."
            )

        now = timezone.now()

        # Leer candidatos, pero revalidarlos dentro de la transacción.
        candidate_holds = list(
            SeatHold.objects
            .filter(
                trip=trip,
                session_key=session_key,
                active=True,
                expires_at__gt=now,
            )
            .values("id", "seat_id")
            .order_by("seat_id", "id")
        )

        if not candidate_holds:
            return json_error(
                "La reserva temporal expiró. Selecciona nuevamente tus asientos.",
                status=409,
                redirect_url=reverse(
                    "client_portal:seatmap",
                    kwargs={"trip_id": trip_id},
                ),
                code="HOLD_EXPIRED",
            )

        hold_ids = [row["id"] for row in candidate_holds]
        seat_ids = sorted({
            row["seat_id"]
            for row in candidate_holds
            if row["seat_id"] is not None
        })

        try:
            with transaction.atomic():
                # ------------------------------------------------
                # 1. Lock Seat primero: mismo orden que SeatHold.hold()
                # ------------------------------------------------
                locked_seats = list(
                    Seat.objects
                    .select_for_update()
                    .filter(
                        pk__in=seat_ids,
                        bus=trip.bus,
                    )
                    .order_by("deck", "row", "position", "pk")
                )

                if len(locked_seats) != len(seat_ids):
                    return json_error(
                        "Uno o más asientos ya no pertenecen al bus de este viaje.",
                        status=409,
                        redirect_url=reverse(
                            "client_portal:seatmap",
                            kwargs={"trip_id": trip_id},
                        ),
                        code="INVALID_SEATS",
                    )

                # ------------------------------------------------
                # 2. Ningún asiento puede tener Ticket
                # ------------------------------------------------
                sold_seat_ids = set(
                    Ticket.objects
                    .filter(
                        trip=trip,
                        seat_id__in=seat_ids,
                    )
                    .values_list("seat_id", flat=True)
                )

                if sold_seat_ids:
                    sold_numbers = [
                        str(seat.number)
                        for seat in locked_seats
                        if seat.id in sold_seat_ids
                    ]

                    return json_error(
                        (
                            "Los siguientes asientos ya fueron vendidos: "
                            + ", ".join(sold_numbers)
                            + ". Selecciona otros asientos."
                        ),
                        status=409,
                        redirect_url=reverse(
                            "client_portal:seatmap",
                            kwargs={"trip_id": trip_id},
                        ),
                        code="SEAT_ALREADY_SOLD",
                    )

                # ------------------------------------------------
                # 3. Lock SeatHold después de Seat
                # ------------------------------------------------
                locked_holds = list(
                    SeatHold.objects
                    .select_for_update()
                    .filter(
                        id__in=hold_ids,
                        trip=trip,
                        session_key=session_key,
                        active=True,
                        expires_at__gt=now,
                    )
                    .select_related("seat")
                    .order_by("seat_id", "id")
                )

                if len(locked_holds) != len(seat_ids):
                    return json_error(
                        (
                            "Algunos asientos ya no están reservados "
                            "por tu sesión. Selecciónalos nuevamente."
                        ),
                        status=409,
                        redirect_url=reverse(
                            "client_portal:seatmap",
                            kwargs={"trip_id": trip_id},
                        ),
                        code="HOLD_CHANGED",
                    )

                if {h.seat_id for h in locked_holds} != set(seat_ids):
                    return json_error(
                        "La reserva temporal cambió. Selecciona nuevamente.",
                        status=409,
                        redirect_url=reverse(
                            "client_portal:seatmap",
                            kwargs={"trip_id": trip_id},
                        ),
                        code="HOLD_MISMATCH",
                    )

                # ------------------------------------------------
                # 4. Pasajeros: piso + número
                # ------------------------------------------------
                passenger_data = {}

                for seat in locked_seats:
                    key = field_key(seat.deck, seat.number)

                    nationality = (
                        request.POST.get(
                            f"passenger_nationality_{key}",
                            "CHI",
                        )
                        .strip()
                        .upper()
                    )
                    document_type = (
                        request.POST.get(
                            f"passenger_doc_type_{key}",
                            "RUT",
                        )
                        .strip()
                        .upper()
                    )
                    document_number = (
                        request.POST.get(
                            f"passenger_doc_number_{key}",
                            "",
                        )
                        .strip()
                    )
                    passenger_name = (
                        request.POST.get(
                            f"passenger_name_{key}",
                            "",
                        )
                        .strip()
                    )
                    passenger_lastname = (
                        request.POST.get(
                            f"passenger_lastname_{key}",
                            "",
                        )
                        .strip()
                    )

                    seat_label = (
                        f"Piso {seat.deck}, asiento {seat.number}"
                    )

                    if not document_number:
                        return json_error(
                            f"El pasajero de {seat_label} debe tener un documento."
                        )

                    if (
                        document_type == "RUT"
                        and not validate_chilean_rut(document_number)
                    ):
                        return json_error(
                            f"El RUT del pasajero de {seat_label} no es válido."
                        )

                    if (
                        document_type != "RUT"
                        and not (3 <= len(document_number) <= 30)
                    ):
                        return json_error(
                            f"El documento del pasajero de {seat_label} no es válido."
                        )

                    if not passenger_name:
                        return json_error(
                            f"Falta el nombre del pasajero de {seat_label}."
                        )

                    if not passenger_lastname:
                        return json_error(
                            f"Falta el apellido del pasajero de {seat_label}."
                        )

                    passenger_data[seat.id] = {
                        "nationality": nationality[:10],
                        "document_type": document_type[:20],
                        "document_number": document_number[:30],
                        "name": passenger_name[:150],
                        "lastname": passenger_lastname[:150],
                    }

                # ------------------------------------------------
                # 5. Precio unitario con temporada, sin promoción
                # ------------------------------------------------
                unit_price, _ = calculate_final_price(
                    trip,
                    None,
                    None,
                )
                unit_price = Decimal(str(unit_price))
                seats_count = len(locked_seats)

                subtotal = (
                    unit_price * Decimal(seats_count)
                ).quantize(
                    Decimal("0.01"),
                    rounding=ROUND_HALF_UP,
                )

                # ------------------------------------------------
                # 6. Promoción: lock + revalidación real
                # ------------------------------------------------
                promotion = None
                discount_amount = Decimal("0.00")

                if discount_code:
                    promotion = (
                        Promotion.objects
                        .select_for_update()
                        .filter(
                            code__iexact=discount_code,
                            is_active=True,
                        )
                        .first()
                    )

                    if not promotion:
                        return json_error(
                            "El código de descuento no existe o ya no está activo."
                        )

                    is_valid, reason = promotion.is_valid(subtotal)

                    if not is_valid:
                        return json_error(
                            f"El código de descuento ya no es válido: {reason}."
                        )

                    discount_amount = Decimal(
                        str(promotion.calculate_discount(subtotal))
                    ).quantize(
                        Decimal("0.01"),
                        rounding=ROUND_HALF_UP,
                    )

                total_amount = max(
                    Decimal("0.00"),
                    subtotal - discount_amount,
                ).quantize(
                    Decimal("0.01"),
                    rounding=ROUND_HALF_UP,
                )

                # ------------------------------------------------
                # 7. Crear/actualizar Customer
                # ------------------------------------------------
                clean_buyer_rut = Customer._clean_rut(buyer_rut)

                customer, _ = Customer.objects.update_or_create(
                    national_id=clean_buyer_rut,
                    defaults={
                        "full_name": buyer_name,
                        "email": buyer_email,
                        "phone": buyer_phone,
                    },
                )

                # ------------------------------------------------
                # 8. Renovar holds 10 minutos para la reserva
                # ------------------------------------------------
                order_expires_at = (
                    timezone.now() + timedelta(minutes=10)
                )

                SeatHold.objects.filter(
                    id__in=[hold.id for hold in locked_holds]
                ).update(
                    expires_at=order_expires_at,
                    active=True,
                )

                # ------------------------------------------------
                # 9. BookingOrder
                # ------------------------------------------------
                order = BookingOrder.objects.create(
                    code=build_unique_order_code(),
                    trip=trip,
                    user=request.user,
                    session_key=session_key,
                    customer=customer,
                    buyer_name=buyer_name,
                    buyer_email=buyer_email,
                    buyer_phone=buyer_phone,
                    buyer_address=buyer_address,
                    discount_code=discount_code,
                    subtotal=subtotal,
                    discount_amount=discount_amount,
                    total_amount=total_amount,
                    status=BookingOrder.STATUS_PENDING,
                    expires_at=order_expires_at,
                )

                # ------------------------------------------------
                # 10. Distribuir total entre BookingOrderSeat
                # ------------------------------------------------
                distributed_unit = (
                    total_amount / Decimal(seats_count)
                ).quantize(
                    Decimal("0.01"),
                    rounding=ROUND_HALF_UP,
                )
                distributed_sum = Decimal("0.00")

                for index, seat in enumerate(locked_seats):
                    if index == seats_count - 1:
                        item_price = total_amount - distributed_sum
                    else:
                        item_price = distributed_unit

                    item_price = Decimal(str(item_price)).quantize(
                        Decimal("0.01"),
                        rounding=ROUND_HALF_UP,
                    )
                    distributed_sum += item_price

                    pdata = passenger_data[seat.id]

                    BookingOrderSeat.objects.create(
                        order=order,
                        seat=seat,
                        passenger_name=pdata["name"],
                        passenger_lastname=pdata["lastname"],
                        passenger_nationality=pdata["nationality"],
                        passenger_document_type=pdata["document_type"],
                        passenger_document=pdata["document_number"],
                        price=item_price,
                    )

                # ------------------------------------------------
                # 11. PaymentTransaction (aún sin llamar a Webpay)
                # ------------------------------------------------
                payment = PaymentTransaction.objects.create(
                    order=order,
                    provider="transbank",
                    token="",
                    buy_order=build_unique_buy_order(),
                    session_id=str(session_key)[:100],
                    amount=total_amount,
                    status=PaymentTransaction.STATUS_CREATED,
                    raw_response={
                        "phase": "FASE_2B",
                        "message": (
                            "Reserva creada; Transbank aún no iniciado."
                        ),
                    },
                )

            request.session["pending_order_code"] = order.code
            request.session["pending_payment_id"] = payment.id
            request.session.modified = True

            return JsonResponse(
                {
                    "success": True,
                    "order_code": order.code,
                    "order_id": order.id,
                    "payment_id": payment.id,
                    "buy_order": payment.buy_order,
                    "subtotal": float(order.subtotal),
                    "discount_amount": float(order.discount_amount),
                    "total_amount": float(order.total_amount),
                    "expires_at": order.expires_at.isoformat(),
                    "status": order.status,
                    "next_phase": "webpay",
                },
                status=200,
            )

        except Exception:
            logger.exception(
                "Error creando BookingOrder trip=%s session=%s user=%s",
                trip_id,
                session_key,
                request.user.pk,
            )

            return json_error(
                (
                    "No fue posible crear la reserva en este momento. "
                    "Por favor intenta nuevamente."
                ),
                status=500,
                code="ORDER_CREATE_ERROR",
            )

    # ============================================================
    # GET
    # ============================================================
    reserved_list = list(reserved_seats)

    context = {
        "paso_actual": 4,
        "trip": trip,
        "reserved_seats": reserved_list,
        "customer": customer,
        "total_amount": (
            len(reserved_list) * trip.route.base_price
        ),
        "seats_count": len(reserved_list),
        "seats_list": [
            hold.seat.number
            for hold in reserved_list
        ],
    }

    return render(
        request,
        "client_portal/checkout.html",
        context,
    )

# ============================================================================
# FASE 2C - WEBPAY PLUS
# ============================================================================

@login_required
def webpay_start(request, order_code):
    """
    Inicia una transacción Webpay Plus en ambiente de integración.

    Recibe una BookingOrder creada previamente en FASE 2B.

    IMPORTANTE:
    - NO crea Ticket.
    - NO elimina SeatHold.
    - NO marca la reserva como pagada.
    - Sólo crea/inicia la transacción en Transbank.
    """

    from booking.models import (
        BookingOrder,
        PaymentTransaction,
    )

    from transbank.common.integration_api_keys import IntegrationApiKeys
    from transbank.common.integration_commerce_codes import (
        IntegrationCommerceCodes,
    )
    from transbank.common.integration_type import IntegrationType
    from transbank.common.options import WebpayOptions
    from transbank.webpay.webpay_plus.transaction import Transaction

    # ========================================================================
    # 1. OBTENER RESERVA
    # ========================================================================

    order = get_object_or_404(
        BookingOrder.objects.select_related(
            "trip",
            "trip__route",
            "customer",
        ),
        code=order_code,
    )

    # ========================================================================
    # 2. VALIDAR SESIÓN / PROPIETARIO
    # ========================================================================

    session_key = request.session.session_key

    if not session_key:
        messages.error(
            request,
            "No se pudo validar tu sesión.",
        )
        return redirect(
            "client_portal:checkout",
            trip_id=order.trip_id,
        )

    if (
        order.session_key != session_key
        and order.user_id != request.user.id
    ):
        logger.warning(
            "Intento de acceso a reserva ajena. "
            "order=%s user=%s session=%s",
            order.code,
            request.user.pk,
            session_key,
        )

        messages.error(
            request,
            "No tienes permiso para iniciar el pago de esta reserva.",
        )

        return redirect(
            "client_portal:search_trips"
        )

    # ========================================================================
    # 3. VALIDAR ESTADO DE LA RESERVA
    # ========================================================================

    if order.status == BookingOrder.STATUS_PAID:
        return redirect(
            "client_portal:confirmation",
            trip_id=order.trip_id,
        )

    if order.status not in [
        BookingOrder.STATUS_PENDING,
        BookingOrder.STATUS_PAYMENT_STARTED,
    ]:
        messages.error(
            request,
            "Esta reserva ya no está disponible para pago.",
        )

        return redirect(
            "client_portal:checkout",
            trip_id=order.trip_id,
        )

    # ========================================================================
    # 4. VALIDAR EXPIRACIÓN
    # ========================================================================

    if order.expires_at <= timezone.now():

        order.status = BookingOrder.STATUS_EXPIRED

        order.save(
            update_fields=[
                "status",
                "updated_at",
            ]
        )

        messages.warning(
            request,
            "La reserva expiró. Selecciona nuevamente tus asientos.",
        )

        return redirect(
            "client_portal:seatmap",
            trip_id=order.trip_id,
        )

    # ========================================================================
    # 5. OBTENER PaymentTransaction
    # ========================================================================

    payment = (
        PaymentTransaction.objects
        .filter(
            order=order,
            status=PaymentTransaction.STATUS_CREATED,
        )
        .order_by("-created_at")
        .first()
    )

    if not payment:
        messages.error(
            request,
            "No existe una transacción de pago válida para esta reserva.",
        )

        return redirect(
            "client_portal:checkout",
            trip_id=order.trip_id,
        )

    # ========================================================================
    # 6. CONFIGURAR WEBPAY EN AMBIENTE DE INTEGRACIÓN
    # ========================================================================

    tx = Transaction(
        WebpayOptions(
            IntegrationCommerceCodes.WEBPAY_PLUS,
            IntegrationApiKeys.WEBPAY,
            IntegrationType.TEST,
        )
    )

    return_url = request.build_absolute_uri(
        reverse(
            "client_portal:webpay_return"
        )
    )

    # ========================================================================
    # 7. EVITAR RE-CREAR TRANSACCIÓN SI YA TENEMOS TOKEN
    # ========================================================================

    if payment.token:

        saved_create_response = (
            payment.raw_response or {}
        ).get(
            "create",
            {}
        )

        saved_webpay_url = saved_create_response.get(
            "url"
        )

        if saved_webpay_url:

            context = {
                "webpay_url": saved_webpay_url,
                "token_ws": payment.token,
                "order": order,
            }

            return render(
                request,
                "client_portal/webpay_redirect.html",
                context,
            )

    # ========================================================================
    # 8. CREAR TRANSACCIÓN EN TRANSBANK
    # ========================================================================

    try:

        response = tx.create(
            buy_order=payment.buy_order,
            session_id=payment.session_id,
            amount=int(payment.amount),
            return_url=return_url,
        )

    except Exception:

        logger.exception(
            "Error creando transacción Webpay "
            "order=%s payment=%s",
            order.code,
            payment.id,
        )

        payment.status = (
            PaymentTransaction.STATUS_ERROR
        )

        payment.save(
            update_fields=[
                "status",
                "updated_at",
            ]
        )

        messages.error(
            request,
            (
                "No fue posible conectar con Webpay. "
                "Intenta nuevamente."
            ),
        )

        return redirect(
            "client_portal:checkout",
            trip_id=order.trip_id,
        )

    # ========================================================================
    # 9. GUARDAR TOKEN Y RESPUESTA CREATE
    # ========================================================================

    payment.token = response["token"]

    payment.raw_response = {
        "create": response,
    }

    payment.save(
        update_fields=[
            "token",
            "raw_response",
            "updated_at",
        ]
    )

    order.status = (
        BookingOrder.STATUS_PAYMENT_STARTED
    )

    order.save(
        update_fields=[
            "status",
            "updated_at",
        ]
    )

    logger.info(
        "Webpay iniciado. order=%s payment=%s buy_order=%s",
        order.code,
        payment.id,
        payment.buy_order,
    )

    # ========================================================================
    # 10. REDIRECCIÓN A WEBPAY
    # ========================================================================

    context = {
        "webpay_url": response["url"],
        "token_ws": response["token"],
        "order": order,
    }

    return render(
        request,
        "client_portal/webpay_redirect.html",
        context,
    )


# ============================================================================
# WEBPAY RETURN
# ============================================================================

@csrf_exempt
def webpay_return(request):
    """
    Procesa el retorno desde Webpay Plus.

    Sólo después de confirmar una transacción autorizada:

        PaymentTransaction -> authorized
        BookingOrder       -> paid
        BookingOrderSeat   -> Ticket
        SeatHold           -> inactive

    IMPORTANTE:
    - Nunca crea Ticket antes de confirmar el pago.
    - Es idempotente: si el pago ya fue confirmado, reutiliza el commit guardado.
    - Si Transbank autorizó pero falla la emisión, NO permite interpretar el pago
      como rechazado ni obliga al cliente a pagar nuevamente.
    """

    from decimal import Decimal

    from django.contrib.auth.models import User
    from django.db import transaction
    from django.db.models import F

    from booking.models import (
        BookingOrder,
        PaymentTransaction,
        Promotion,
        Seat,
        SeatHold,
        Ticket,
    )

    from transbank.common.integration_api_keys import IntegrationApiKeys
    from transbank.common.integration_commerce_codes import (
        IntegrationCommerceCodes,
    )
    from transbank.common.integration_type import IntegrationType
    from transbank.common.options import WebpayOptions
    from transbank.webpay.webpay_plus.transaction import Transaction

    # ========================================================================
    # 1. RECIBIR TOKEN DESDE TRANSBANK
    # ========================================================================

    token_ws = (
        request.POST.get("token_ws")
        or request.GET.get("token_ws")
    )

    tbk_token = (
        request.POST.get("TBK_TOKEN")
        or request.GET.get("TBK_TOKEN")
    )

    tbk_order = (
        request.POST.get("TBK_ORDEN_COMPRA")
        or request.GET.get("TBK_ORDEN_COMPRA")
    )

    # ========================================================================
    # 2. CLIENTE ABORTÓ / NO VOLVIÓ CON token_ws
    # ========================================================================

    if not token_ws:
        payment = None

        if tbk_token:
            payment = (
                PaymentTransaction.objects
                .filter(token=tbk_token)
                .select_related(
                    "order",
                    "order__trip",
                )
                .first()
            )

        if payment is None and tbk_order:
            payment = (
                PaymentTransaction.objects
                .filter(buy_order=tbk_order)
                .select_related(
                    "order",
                    "order__trip",
                )
                .first()
            )

        if payment:
            order = payment.order

            # Si ya fue autorizado previamente, nunca degradamos el pago.
            if payment.status == PaymentTransaction.STATUS_AUTHORIZED:
                messages.warning(
                    request,
                    (
                        "El pago ya fue autorizado, pero la operación no terminó "
                        "correctamente. No vuelvas a pagar."
                    ),
                )
                return redirect(
                    "client_portal:checkout",
                    trip_id=order.trip_id,
                )

            payment.status = PaymentTransaction.STATUS_ABORTED
            payment.raw_response = {
                **(payment.raw_response or {}),
                "abort": {
                    "TBK_TOKEN": tbk_token,
                    "TBK_ORDEN_COMPRA": tbk_order,
                },
            }
            payment.save(
                update_fields=[
                    "status",
                    "raw_response",
                    "updated_at",
                ]
            )

            if order.status != BookingOrder.STATUS_PAID:
                order.status = BookingOrder.STATUS_FAILED
                order.save(
                    update_fields=[
                        "status",
                        "updated_at",
                    ]
                )

            messages.warning(
                request,
                "El pago fue cancelado o abortado en Webpay.",
            )

        return redirect(
            "client_portal:payment_result",
            order_code=order.code,
)

        messages.error(
            request,
            "Webpay no devolvió un token válido.",
        )

        return redirect(
            "client_portal:search_trips"
        )

    # ========================================================================
    # 3. BUSCAR TRANSACCIÓN LOCAL POR TOKEN
    # ========================================================================

    payment = get_object_or_404(
        PaymentTransaction.objects.select_related(
            "order",
            "order__trip",
            "order__customer",
        ),
        token=token_ws,
    )

    order = payment.order

    # ========================================================================
    # 4. IDEMPOTENCIA TEMPRANA: ORDEN YA FINALIZADA
    # ========================================================================

    if (
        order.status == BookingOrder.STATUS_PAID
        and payment.status == PaymentTransaction.STATUS_AUTHORIZED
    ):
        ticket_ids = list(
            Ticket.objects
            .filter(
                trip=order.trip,
                seat_id__in=order.items.values_list(
                    "seat_id",
                    flat=True,
                ),
            )
            .values_list(
                "id",
                flat=True,
            )
        )

        request.session["last_ticket_ids"] = ticket_ids
        request.session.modified = True

        return redirect(
            "client_portal:confirmation",
            trip_id=order.trip_id,
        )

    # ========================================================================
    # 5. CONFIGURAR WEBPAY
    # ========================================================================

    tx = Transaction(
        WebpayOptions(
            IntegrationCommerceCodes.WEBPAY_PLUS,
            IntegrationApiKeys.WEBPAY,
            IntegrationType.TEST,
        )
    )

    # ========================================================================
    # 6. CONFIRMAR TRANSACCIÓN / REUTILIZAR COMMIT AUTORIZADO
    # ========================================================================

    stored_commit = (
        (payment.raw_response or {}).get("commit")
        or {}
    )

    already_committed = (
        payment.status == PaymentTransaction.STATUS_AUTHORIZED
        and payment.response_code == 0
        and stored_commit.get("status") == "AUTHORIZED"
        and stored_commit.get("response_code") == 0
    )

    if already_committed:
        # Recuperación segura de una autorización ya confirmada.
        response = stored_commit

        logger.warning(
            (
                "Reutilizando commit Webpay previamente autorizado "
                "para completar emisión. order=%s payment=%s"
            ),
            order.code,
            payment.id,
        )

    else:
        try:
            response = tx.commit(token_ws)

        except Exception:
            logger.exception(
                "Error confirmando Webpay token=%s payment=%s order=%s",
                token_ws,
                payment.id,
                order.code,
            )

            payment.status = PaymentTransaction.STATUS_ERROR
            payment.save(
                update_fields=[
                    "status",
                    "updated_at",
                ]
            )

            messages.error(
                request,
                (
                    "No fue posible confirmar el resultado del pago "
                    "con Webpay."
                ),
            )

            return redirect(
                "client_portal:checkout",
                trip_id=order.trip_id,
            )

    # ========================================================================
    # 7. EXTRAER DATOS IMPORTANTES
    # ========================================================================

    response_code = response.get("response_code")
    webpay_status = response.get("status")
    response_buy_order = response.get("buy_order")
    response_amount = response.get("amount")

    authorized = (
        response_code == 0
        and webpay_status == "AUTHORIZED"
    )

    # ========================================================================
    # 8. VALIDAR BUY ORDER
    # ========================================================================

    if (
        response_buy_order
        and response_buy_order != payment.buy_order
    ):
        logger.error(
            "Webpay buy_order no coincide. local=%s transbank=%s",
            payment.buy_order,
            response_buy_order,
        )

        payment.status = PaymentTransaction.STATUS_ERROR
        payment.raw_response = {
            **(payment.raw_response or {}),
            "commit": response,
            "integrity_error": "BUY_ORDER_MISMATCH",
        }
        payment.save(
            update_fields=[
                "status",
                "raw_response",
                "updated_at",
            ]
        )

        messages.error(
            request,
            "No fue posible validar la orden del pago.",
        )

        return redirect(
            "client_portal:checkout",
            trip_id=order.trip_id,
        )

    # ========================================================================
    # 9. PAGO RECHAZADO
    # ========================================================================

    if not authorized:
        payment.status = PaymentTransaction.STATUS_REJECTED
        payment.response_code = response_code
        payment.raw_response = {
            **(payment.raw_response or {}),
            "commit": response,
        }
        payment.save(
            update_fields=[
                "status",
                "response_code",
                "raw_response",
                "updated_at",
            ]
        )

        if order.status != BookingOrder.STATUS_PAID:
            order.status = BookingOrder.STATUS_FAILED
            order.save(
                update_fields=[
                    "status",
                    "updated_at",
                ]
            )

        messages.error(
            request,
            "El pago fue rechazado por Webpay.",
        )

        return redirect(
        "client_portal:payment_result",
        order_code=order.code,
)

    # ========================================================================
    # 10. VALIDAR MONTO AUTORIZADO
    # ========================================================================

    try:
        transbank_amount = Decimal(str(response_amount))

    except Exception:
        logger.exception(
            "Monto Webpay inválido. order=%s",
            order.code,
        )

        payment.status = PaymentTransaction.STATUS_ERROR
        payment.raw_response = {
            **(payment.raw_response or {}),
            "commit": response,
            "integrity_error": "INVALID_AMOUNT",
        }
        payment.save(
            update_fields=[
                "status",
                "raw_response",
                "updated_at",
            ]
        )

        messages.error(
            request,
            "No fue posible validar el monto pagado.",
        )

        return redirect(
            "client_portal:checkout",
            trip_id=order.trip_id,
        )

    if transbank_amount != order.total_amount:
        logger.error(
            "Monto Webpay no coincide. order=%s esperado=%s recibido=%s",
            order.code,
            order.total_amount,
            transbank_amount,
        )

        payment.status = PaymentTransaction.STATUS_ERROR
        payment.raw_response = {
            **(payment.raw_response or {}),
            "commit": response,
            "integrity_error": "AMOUNT_MISMATCH",
        }
        payment.save(
            update_fields=[
                "status",
                "raw_response",
                "updated_at",
            ]
        )

        messages.error(
            request,
            "El monto confirmado por Webpay no coincide con la reserva.",
        )

        return redirect(
            "client_portal:checkout",
            trip_id=order.trip_id,
        )

    # ========================================================================
    # 11. PAGO AUTORIZADO - EMITIR TICKETS
    # ========================================================================

    # IMPORTANTE:
    # Debe existir ANTES del try/atomic para que también esté disponible
    # después de completar correctamente la transacción.
    created_tickets = []

    try:
        with transaction.atomic():

            # ----------------------------------------------------
            # Bloquear BookingOrder SIN select_related nullable
            # ----------------------------------------------------
            order = (
                BookingOrder.objects
                .select_for_update()
                .get(pk=order.pk)
            )

            # ----------------------------------------------------
            # Bloquear PaymentTransaction
            # ----------------------------------------------------
            payment = (
                PaymentTransaction.objects
                .select_for_update()
                .get(pk=payment.pk)
            )

            # ----------------------------------------------------
            # IDEMPOTENCIA DENTRO DEL LOCK
            # ----------------------------------------------------
            if order.status == BookingOrder.STATUS_PAID:
                existing_ticket_ids = list(
                    Ticket.objects
                    .filter(
                        trip_id=order.trip_id,
                        seat_id__in=order.items.values_list(
                            "seat_id",
                            flat=True,
                        ),
                    )
                    .values_list(
                        "id",
                        flat=True,
                    )
                )

                request.session["last_ticket_ids"] = existing_ticket_ids
                request.session.modified = True

                return redirect(
                    "client_portal:confirmation",
                    trip_id=order.trip_id,
                )

             # ----------------------------------------------------
            # Registrar autorización de Transbank dentro del lock
            # ----------------------------------------------------
            #
            # En este punto YA validamos arriba:
            #   response_code == 0
            #   status == "AUTHORIZED"
            #   buy_order correcto
            #   monto correcto
            #
            # El PaymentTransaction local todavía puede estar
            # "payment_started", porque recién estamos finalizando
            # la compra después del commit de Transbank.
            payment.status = PaymentTransaction.STATUS_AUTHORIZED
            payment.authorization_code = str(
                response.get("authorization_code", "") or ""
            )
            payment.response_code = response_code
            payment.raw_response = {
                **(payment.raw_response or {}),
                "commit": response,
            }

            # ----------------------------------------------------
            # Obtener items/asientos de la reserva
            # ----------------------------------------------------
            items = list(
                order.items
                .select_related("seat")
                .order_by("seat_id")
            )

            if not items:
                raise ValueError(
                    "La reserva no contiene asientos."
                )

            seat_ids = [
                item.seat_id
                for item in items
            ]

            # ----------------------------------------------------
            # Bloquear físicamente los Seat
            # ----------------------------------------------------
            locked_seats = {
                seat.id: seat
                for seat in (
                    Seat.objects
                    .select_for_update()
                    .filter(pk__in=seat_ids)
                    .order_by("pk")
                )
            }

            if len(locked_seats) != len(seat_ids):
                raise ValueError(
                    "No fue posible bloquear todos los asientos."
                )

            # ----------------------------------------------------
            # Evitar emisión duplicada
            # ----------------------------------------------------
            existing_ticket_seats = set(
                Ticket.objects
                .filter(
                    trip_id=order.trip_id,
                    seat_id__in=seat_ids,
                )
                .values_list(
                    "seat_id",
                    flat=True,
                )
            )

            if existing_ticket_seats:
                raise ValueError(
                    "Uno o más asientos ya tienen un boleto emitido."
                )

            # ----------------------------------------------------
            # Usuario técnico de venta web
            # ----------------------------------------------------
            system_user = (
                User.objects
                .filter(username="ventas_web")
                .first()
            )

            if not system_user:
                system_user = (
                    User.objects
                    .filter(is_superuser=True)
                    .order_by("id")
                    .first()
                )

            if not system_user:
                raise ValueError(
                    "No existe un usuario para registrar la venta web."
                )

            # ----------------------------------------------------
            # Crear Tickets
            # ----------------------------------------------------
            for item in items:
                seat = locked_seats[item.seat_id]

                passenger_full_name = (
                    f"{item.passenger_name} "
                    f"{item.passenger_lastname}"
                ).strip()

                ticket = Ticket.create_for_sale(
                    trip=order.trip,
                    seat=seat,
                    buyer_name=passenger_full_name,
                    national_id=item.passenger_document,
                    price=item.price,
                    created_by=system_user,
                    payment_method="card",
                    customer=order.customer,
                )

                created_tickets.append(ticket)

            # ----------------------------------------------------
            # Liberar sólo los SeatHold de esta orden
            # ----------------------------------------------------
            SeatHold.objects.filter(
                trip_id=order.trip_id,
                session_key=order.session_key,
                seat_id__in=seat_ids,
                active=True,
            ).update(
                active=False
            )

            # ----------------------------------------------------
            # Consumir promoción únicamente tras pago autorizado
            # ----------------------------------------------------
            if order.discount_code:
                promotion = (
                    Promotion.objects
                    .select_for_update()
                    .filter(
                        code__iexact=order.discount_code
                    )
                    .first()
                )

                if promotion:
                    Promotion.objects.filter(
                        pk=promotion.pk
                    ).update(
                        used_count=F("used_count") + 1
                    )

            # ----------------------------------------------------
            # PaymentTransaction = AUTHORIZED
            # ----------------------------------------------------
            clean_raw_response = {
                **(payment.raw_response or {}),
                "commit": response,
            }

            # Si estamos recuperando un intento anterior exitosamente,
            # eliminar la marca de error de emisión.
            clean_raw_response.pop(
                "ticket_emission_error",
                None,
            )

            payment.status = PaymentTransaction.STATUS_AUTHORIZED
            payment.authorization_code = str(
                response.get("authorization_code", "") or ""
            )
            payment.response_code = response_code
            payment.raw_response = clean_raw_response
            payment.save(
                update_fields=[
                    "status",
                    "authorization_code",
                    "response_code",
                    "raw_response",
                    "updated_at",
                ]
            )

            # ----------------------------------------------------
            # BookingOrder = PAID
            # ----------------------------------------------------
            order.status = BookingOrder.STATUS_PAID
            order.save(
                update_fields=[
                    "status",
                    "updated_at",
                ]
            )

    except Exception:
        logger.exception(
            (
                "PAGO AUTORIZADO pero error emitiendo tickets. "
                "order=%s payment=%s"
            ),
            order.code,
            payment.id,
        )

        # Transbank YA autorizó el dinero:
        # jamás degradamos la transacción a rejected/error.
        PaymentTransaction.objects.filter(
            pk=payment.pk
        ).update(
            status=PaymentTransaction.STATUS_AUTHORIZED,
            authorization_code=str(
                response.get("authorization_code", "") or ""
            ),
            response_code=response_code,
            raw_response={
                **(payment.raw_response or {}),
                "commit": response,
                "ticket_emission_error": True,
            },
        )

        messages.error(
            request,
            (
                "El pago fue autorizado por Webpay, pero ocurrió un problema "
                "al emitir los pasajes. NO vuelvas a pagar. "
                "Contacta al administrador."
            ),
        )

        return redirect(
            "client_portal:checkout",
            trip_id=order.trip_id,
        )

    # ========================================================================
    # 12. GUARDAR DATOS PARA confirmation()
    # ========================================================================

    request.session["last_ticket_ids"] = [
        ticket.id
        for ticket in created_tickets
    ]

    request.session["last_order_code"] = order.code
    request.session["last_payment_id"] = payment.id

    request.session["last_original_total"] = float(
    order.subtotal
    )

    request.session["last_discount_amount"] = float(
    order.discount_amount
    )

    request.session.pop(
        "pending_order_code",
        None,
    )

    request.session.pop(
        "pending_payment_id",
        None,
    )

    request.session.modified = True

    logger.info(
        "Pago Webpay completado. order=%s payment=%s tickets=%s",
        order.code,
        payment.id,
        len(created_tickets),
    )

    messages.success(
        request,
        (
            "Pago autorizado. "
            f"Se emitieron {len(created_tickets)} pasajes."
        ),
    )

    return redirect(
        "client_portal:confirmation",
        trip_id=order.trip_id,
    )




@csrf_exempt
def save_customer_from_checkout(request):
    """
    Guarda o actualiza los datos del cliente después de seleccionar asientos.
    Verifica que los asientos sigan disponibles.
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'Método no permitido'}, status=405)

    try:
        if request.content_type == 'application/json':
            data = json.loads(request.body)
        else:
            data = request.POST

        session_key = request.session.session_key
        if not session_key:
            request.session.create()
            session_key = request.session.session_key

        rut = data.get('rut', '').strip()
        full_name = data.get('full_name', '').strip()
        email = data.get('email', '').strip()
        phone = data.get('phone', '').strip()
        trip_id = data.get('trip_id')
        selected_seats = data.get('selected_seats', [])

        if not rut:
            return JsonResponse({'error': 'El RUT es obligatorio'}, status=400)
        if not full_name:
            return JsonResponse({'error': 'El nombre completo es obligatorio'}, status=400)
        if not trip_id:
            return JsonResponse({'error': 'ID de viaje requerido'}, status=400)

        from booking.utils import validate_chilean_rut
        if not validate_chilean_rut(rut):
            return JsonResponse({'error': 'RUT inválido. Formato: 12345678-9'}, status=400)

        trip = get_object_or_404(Trip, pk=trip_id)

        # ✅ VERIFICAR QUE LOS ASIENTOS SIGAN DISPONIBLES
        if selected_seats:
            for seat_data in selected_seats:
                seat_number = seat_data.get('number')
                deck = seat_data.get('deck', 1)
                
                seat = Seat.objects.filter(bus=trip.bus, number=seat_number, deck=deck).first()
                if not seat:
                    return JsonResponse({
                        'error': f'El asiento {seat_number} no existe'
                    }, status=400)
                
                # Verificar si está vendido
                if Ticket.objects.filter(trip=trip, seat=seat).exists():
                    return JsonResponse({
                        'error': f'El asiento {seat_number} ya está vendido'
                    }, status=409)
                
                # Verificar si está reservado por otro usuario
                hold = SeatHold.objects.filter(
                    trip=trip,
                    seat=seat,
                    active=True,
                    expires_at__gt=timezone.now()
                ).exclude(session_key=session_key).first()
                
                if hold:
                    return JsonResponse({
                        'error': f'El asiento {seat_number} está reservado por otro usuario'
                    }, status=409)

        from booking.models import Customer
        customer, created = Customer.objects.get_or_create(
            national_id=rut,
            defaults={
                'full_name': full_name,
                'email': email,
                'phone': phone
            }
        )

        if not created:
            if customer.full_name != full_name and full_name:
                customer.full_name = full_name
            if customer.email != email and email:
                customer.email = email
            if customer.phone != phone and phone:
                customer.phone = phone
            customer.save()

        request.session['pending_customer_id'] = customer.id
        request.session['pending_trip_id'] = trip_id
        request.session['pending_seats'] = selected_seats

        return JsonResponse({
            'success': True,
            'created': created,
            'customer': {
                'id': customer.id,
                'rut': customer.national_id,
                'full_name': customer.full_name,
                'email': customer.email,
                'phone': customer.phone
            },
            'message': 'Cliente registrado exitosamente' if created else 'Datos del cliente actualizados'
        })

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)

def confirmation(request, trip_id):
    """
    FASE 2D.1 - Confirmación profesional de compra.
    """
    from decimal import Decimal
    from booking.models import BookingOrder, PaymentTransaction, Ticket, Trip

    trip = get_object_or_404(
        Trip.objects.select_related(
            "route",
            "route__origin",
            "route__destination",
            "route__origin_terminal",
            "route__destination_terminal",
            "bus",
        ),
        pk=trip_id,
    )

    ticket_ids = request.session.get("last_ticket_ids", [])
    last_order_code = request.session.get("last_order_code")
    last_payment_id = request.session.get("last_payment_id")

    order = None
    payment = None

    if last_order_code:
        order = (
            BookingOrder.objects
            .filter(
                code=last_order_code,
                trip=trip,
                status=BookingOrder.STATUS_PAID,
            )
            .select_related("customer", "trip", "trip__route")
            .first()
        )

    if order is None:
        order_qs = (
            BookingOrder.objects
            .filter(
                trip=trip,
                status=BookingOrder.STATUS_PAID,
            )
            .select_related("customer", "trip", "trip__route")
            .order_by("-updated_at", "-created_at")
        )

        session_key = request.session.session_key

        if request.user.is_authenticated:
            order_qs = order_qs.filter(user=request.user)
        elif session_key:
            order_qs = order_qs.filter(session_key=session_key)
        else:
            order_qs = order_qs.none()

        order = order_qs.first()

    if order:
        if last_payment_id:
            payment = (
                PaymentTransaction.objects
                .filter(
                    pk=last_payment_id,
                    order=order,
                    status=PaymentTransaction.STATUS_AUTHORIZED,
                )
                .first()
            )

        if payment is None:
            payment = (
                PaymentTransaction.objects
                .filter(
                    order=order,
                    status=PaymentTransaction.STATUS_AUTHORIZED,
                )
                .order_by("-updated_at", "-created_at")
                .first()
            )

    tickets = list(
        Ticket.objects
        .filter(id__in=ticket_ids, trip=trip)
        .select_related("seat", "customer")
        .order_by("seat__deck", "seat__row", "seat__position", "id")
    )

    if not tickets and order:
        order_seat_ids = list(
            order.items.values_list("seat_id", flat=True)
        )
        tickets = list(
            Ticket.objects
            .filter(trip=trip, seat_id__in=order_seat_ids)
            .select_related("seat", "customer")
            .order_by("seat__deck", "seat__row", "seat__position", "id")
        )

    total_amount = sum(
        (ticket.price for ticket in tickets),
        Decimal("0.00"),
    )
    tickets_count = len(tickets)

    if order:
        original_total = order.subtotal
        discount_amount = order.discount_amount
        total_amount = order.total_amount
    else:
        original_total = Decimal(
            str(request.session.get("last_original_total", total_amount))
        )
        discount_amount = Decimal(
            str(request.session.get("last_discount_amount", 0))
        )

    authorization_code = ""
    card_last_four = ""

    if payment:
        authorization_code = payment.authorization_code or ""
        commit_data = (payment.raw_response or {}).get("commit") or {}
        card_detail = commit_data.get("card_detail") or {}
        card_last_four = card_detail.get("card_number", "") or ""

    buyer_name = ""
    buyer_email = ""
    buyer_rut = ""

    if order:
        buyer_name = order.buyer_name or ""
        buyer_email = order.buyer_email or ""
        if order.customer:
            buyer_rut = order.customer.national_id or ""

    context = {
        "paso_actual": 5,
        "trip": trip,
        "tickets": tickets,
        "tickets_count": tickets_count,
        "total_amount": total_amount,
        "original_total": original_total,
        "discount_amount": discount_amount,
        "order": order,
        "payment": payment,
        "order_code": order.code if order else "",
        "authorization_code": authorization_code,
        "card_last_four": card_last_four,
        "buyer_name": buyer_name,
        "buyer_email": buyer_email,
        "buyer_rut": buyer_rut,
        "payment_approved": bool(
            order
            and payment
            and order.status == BookingOrder.STATUS_PAID
            and payment.status == PaymentTransaction.STATUS_AUTHORIZED
            and payment.response_code == 0
        ),
    }

    return render(
        request,
        "client_portal/confirmation.html",
        context,
    )



def payment_result(request, order_code):
    """
    FASE 2D.2 - Resultado visual de pago Webpay.

    Estados visuales:
    - rejected  -> pago rechazado
    - aborted   -> pago cancelado/abortado
    - expired   -> reserva expirada
    - error     -> problema al procesar pago

    Seguridad:
    - La orden debe pertenecer al usuario autenticado o a la sesión actual.
    - Nunca altera una orden PAID.
    - Nunca crea Ticket.
    - Nunca vuelve a llamar a Transbank.
    """
    from booking.models import BookingOrder, PaymentTransaction, SeatHold

    order = get_object_or_404(
        BookingOrder.objects.select_related(
            "trip",
            "trip__route",
            "trip__route__origin",
            "trip__route__destination",
            "customer",
        ),
        code=order_code,
    )

    session_key = request.session.session_key

    belongs_to_user = (
        request.user.is_authenticated
        and order.user_id == request.user.id
    )
    belongs_to_session = (
        session_key
        and order.session_key == session_key
    )

    if not belongs_to_user and not belongs_to_session:
        messages.error(request, "No tienes permiso para ver esta operación.")
        return redirect("client_portal:search_trips")

    if order.status == BookingOrder.STATUS_PAID:
        return redirect(
            "client_portal:confirmation",
            trip_id=order.trip_id,
        )

    payment = order.payments.order_by("-created_at").first()

    seat_ids = list(
        order.items.values_list("seat_id", flat=True)
    )

    now = timezone.now()

    holds_active = SeatHold.objects.filter(
        trip_id=order.trip_id,
        session_key=order.session_key,
        seat_id__in=seat_ids,
        active=True,
        expires_at__gt=now,
    ).exists()

    remaining_seconds = max(
        0,
        int((order.expires_at - now).total_seconds()),
    )

    result_type = "error"

    if (
        order.status == BookingOrder.STATUS_EXPIRED
        or order.expires_at <= now
    ):
        result_type = "expired"
    elif payment and payment.status == PaymentTransaction.STATUS_REJECTED:
        result_type = "rejected"
    elif payment and payment.status == PaymentTransaction.STATUS_ABORTED:
        result_type = "cancelled"
    elif payment and payment.status == PaymentTransaction.STATUS_ERROR:
        result_type = "error"
    elif order.status == BookingOrder.STATUS_FAILED:
        result_type = "cancelled"

    configs = {
        "rejected": {
            "title": "Pago rechazado",
            "message": "Webpay no autorizó el pago. No se emitió ningún pasaje.",
            "icon": "fa-times-circle",
            "css_class": "result-rejected",
        },
        "cancelled": {
            "title": "Pago cancelado",
            "message": "El proceso de pago fue cancelado. No se emitió ningún pasaje.",
            "icon": "fa-ban",
            "css_class": "result-cancelled",
        },
        "expired": {
            "title": "Reserva expirada",
            "message": "El tiempo de reserva terminó. Debes seleccionar nuevamente tus asientos.",
            "icon": "fa-hourglass-end",
            "css_class": "result-expired",
        },
        "error": {
            "title": "No pudimos confirmar el pago",
            "message": (
                "Ocurrió un problema durante el proceso. "
                "Si ves un cobro en tu banco, no vuelvas a pagar y contacta al administrador."
            ),
            "icon": "fa-exclamation-triangle",
            "css_class": "result-error",
        },
    }

    result = configs[result_type]

    has_authorized_payment = order.payments.filter(
        status=PaymentTransaction.STATUS_AUTHORIZED
    ).exists()

    can_retry = (
        result_type in ["rejected", "cancelled"]
        and holds_active
        and remaining_seconds > 0
        and not has_authorized_payment
    )

    context = {
        "paso_actual": 4,
        "order": order,
        "payment": payment,
        "trip": order.trip,
        "result_type": result_type,
        "result_title": result["title"],
        "result_message": result["message"],
        "result_icon": result["icon"],
        "result_css_class": result["css_class"],
        "can_retry": can_retry,
        "holds_active": holds_active,
        "remaining_seconds": remaining_seconds,
        "remaining_minutes": remaining_seconds // 60,
        "remaining_secs": remaining_seconds % 60,
        "seat_count": len(seat_ids),
    }

    return render(
        request,
        "client_portal/payment_result.html",
        context,
    )


@login_required
@require_POST
def webpay_retry(request, order_code):
    """
    FASE 2D.3 - Reintento robusto de pago.

    Reutiliza la MISMA BookingOrder.

    Crea:
        - un nuevo PaymentTransaction
        - un nuevo buy_order

    NO crea:
        - otra BookingOrder
        - BookingOrderSeat nuevos
        - Ticket

    Seguridad:
        - sólo permite reintentar FAILED/CANCELLED;
        - el hold debe seguir vigente;
        - nunca permite reintento con pago AUTHORIZED;
        - bloquea orden/asientos/holds;
        - doble clic no crea dos PaymentTransaction.
    """
    import uuid

    from django.db import transaction
    from django.utils import timezone

    from booking.models import (
        BookingOrder,
        PaymentTransaction,
        Seat,
        SeatHold,
        Ticket,
    )

    def build_retry_buy_order():
        """
        Máximo 26 caracteres para Webpay.
        Ejemplo:
            WEB260814154500A1B2C3D4
        """
        for _ in range(10):

            buy_order = (
                "WEB"
                + timezone.now().strftime("%y%m%d%H%M%S")
                + uuid.uuid4().hex[:8].upper()
            )

            if not PaymentTransaction.objects.filter(
                buy_order=buy_order
            ).exists():
                return buy_order

        raise RuntimeError(
            "No fue posible generar un buy_order único."
        )

    session_key = request.session.session_key

    if not session_key:
        messages.error(
            request,
            "No fue posible validar tu sesión."
        )
        return redirect(
            "client_portal:search_trips"
        )

    try:

        with transaction.atomic():

            # ============================================================
            # 1. BLOQUEAR BOOKING ORDER
            # ============================================================

            order = (
                BookingOrder.objects
                .select_for_update()
                .get(
                    code=order_code
                )
            )

            # ============================================================
            # 2. SEGURIDAD / PROPIEDAD
            # ============================================================

            belongs_to_user = (
                request.user.is_authenticated
                and order.user_id == request.user.id
            )

            belongs_to_session = (
                order.session_key == session_key
            )

            if not belongs_to_user and not belongs_to_session:
                messages.error(
                    request,
                    "No tienes permiso para reintentar este pago."
                )

                return redirect(
                    "client_portal:search_trips"
                )

            # ============================================================
            # 3. ORDEN YA PAGADA
            # ============================================================

            if order.status == BookingOrder.STATUS_PAID:

                return redirect(
                    "client_portal:confirmation",
                    trip_id=order.trip_id,
                )

            # ============================================================
            # 4. PROHIBIR REINTENTO SI EXISTE AUTHORIZED
            # ============================================================

            if order.payments.filter(
                status=PaymentTransaction.STATUS_AUTHORIZED
            ).exists():

                logger.error(
                    (
                        "Intento de re-pago bloqueado: "
                        "orden con pago AUTHORIZED. order=%s"
                    ),
                    order.code,
                )

                messages.error(
                    request,
                    (
                        "Esta reserva ya posee un pago autorizado. "
                        "No vuelvas a pagar."
                    ),
                )

                return redirect(
                    "client_portal:payment_result",
                    order_code=order.code,
                )

            # ============================================================
            # 5. SÓLO FAILED / CANCELLED
            # ============================================================

            if order.status not in [
                BookingOrder.STATUS_FAILED,
                BookingOrder.STATUS_CANCELLED,
            ]:

                messages.warning(
                    request,
                    (
                        "Esta reserva ya tiene un intento de pago "
                        "en proceso."
                    ),
                )

                return redirect(
                    "client_portal:payment_result",
                    order_code=order.code,
                )

            now = timezone.now()

            # ============================================================
            # 6. EXPIRACIÓN
            # ============================================================

            if order.expires_at <= now:

                order.status = BookingOrder.STATUS_EXPIRED

                order.save(
                    update_fields=[
                        "status",
                        "updated_at",
                    ]
                )

                PaymentTransaction.objects.filter(
                    order=order,
                    status=PaymentTransaction.STATUS_CREATED,
                ).update(
                    status=PaymentTransaction.STATUS_ABORTED
                )

                SeatHold.objects.filter(
                    trip_id=order.trip_id,
                    session_key=order.session_key,
                    active=True,
                    expires_at__lte=now,
                ).update(
                    active=False
                )

                return redirect(
                    "client_portal:payment_result",
                    order_code=order.code,
                )

            # ============================================================
            # 7. ASIENTOS DE LA MISMA BOOKING ORDER
            # ============================================================

            seat_ids = list(
                order.items
                .values_list(
                    "seat_id",
                    flat=True,
                )
                .order_by(
                    "seat_id"
                )
            )

            if not seat_ids:

                raise ValueError(
                    "La reserva no contiene asientos."
                )

            # ============================================================
            # 8. BLOQUEAR SEAT FÍSICOS
            # ============================================================

            locked_seats = list(
                Seat.objects
                .select_for_update()
                .filter(
                    pk__in=seat_ids
                )
                .order_by(
                    "pk"
                )
            )

            if len(locked_seats) != len(seat_ids):

                raise ValueError(
                    "No fue posible bloquear todos los asientos."
                )

            # ============================================================
            # 9. NINGÚN ASIENTO PUEDE ESTAR VENDIDO
            # ============================================================

            if Ticket.objects.filter(
                trip_id=order.trip_id,
                seat_id__in=seat_ids,
            ).exists():

                messages.error(
                    request,
                    (
                        "Uno de los asientos de esta reserva "
                        "ya fue vendido."
                    ),
                )

                return redirect(
                    "client_portal:seatmap",
                    trip_id=order.trip_id,
                )

            # ============================================================
            # 10. BLOQUEAR Y VALIDAR LOS HOLDS
            # ============================================================

            locked_holds = list(
                SeatHold.objects
                .select_for_update()
                .filter(
                    trip_id=order.trip_id,
                    session_key=order.session_key,
                    seat_id__in=seat_ids,
                    active=True,
                    expires_at__gt=now,
                )
                .order_by(
                    "seat_id",
                    "id",
                )
            )

            active_hold_seats = {
                hold.seat_id
                for hold in locked_holds
            }

            if active_hold_seats != set(seat_ids):

                order.status = BookingOrder.STATUS_EXPIRED

                order.save(
                    update_fields=[
                        "status",
                        "updated_at",
                    ]
                )

                messages.warning(
                    request,
                    (
                        "La reserva de tus asientos expiró. "
                        "Selecciona nuevamente."
                    ),
                )

                return redirect(
                    "client_portal:payment_result",
                    order_code=order.code,
                )

            # ============================================================
            # 11. CREAR NUEVO INTENTO DE PAGO
            # ============================================================

            attempt_number = (
                order.payments.count() + 1
            )

            payment = PaymentTransaction.objects.create(
                order=order,
                provider="transbank",
                token="",
                buy_order=build_retry_buy_order(),
                session_id=str(session_key)[:100],
                amount=order.total_amount,
                status=PaymentTransaction.STATUS_CREATED,
                raw_response={
                    "phase": "FASE_2D_3_RETRY",
                    "attempt": attempt_number,
                    "message": (
                        "Nuevo intento de pago sobre "
                        "la misma BookingOrder."
                    ),
                },
            )

            # ============================================================
            # 12. CAMBIO DE ESTADO ATÓMICO
            # ============================================================

            order.status = BookingOrder.STATUS_PENDING

            order.save(
                update_fields=[
                    "status",
                    "updated_at",
                ]
            )

            request.session[
                "pending_order_code"
            ] = order.code

            request.session[
                "pending_payment_id"
            ] = payment.id

            request.session.modified = True

        logger.info(
            (
                "Reintento Webpay creado. "
                "order=%s payment=%s attempt=%s"
            ),
            order.code,
            payment.id,
            attempt_number,
        )

        # El webpay_start existente utilizará el NUEVO
        # PaymentTransaction STATUS_CREATED.
        return redirect(
            "client_portal:webpay_start",
            order_code=order.code,
        )

    except BookingOrder.DoesNotExist:

        messages.error(
            request,
            "La reserva indicada no existe."
        )

        return redirect(
            "client_portal:search_trips"
        )

    except Exception:

        logger.exception(
            "Error preparando reintento Webpay order=%s",
            order_code,
        )

        messages.error(
            request,
            (
                "No fue posible preparar el nuevo intento "
                "de pago."
            ),
        )

        return redirect(
            "client_portal:payment_result",
            order_code=order_code,
        )




def resultados(request):
    """Página de resultados de búsqueda"""
    origen = request.GET.get('origin', '')
    destino = request.GET.get('destination', '')
    fecha = request.GET.get('date', '')

    context = {
        'origen': origen,
        'destino': destino,
        'fecha': fecha,
    }
    return render(request, 'client_portal/resultados.html', context)


def terms(request):
    """Página de términos y condiciones"""
    return render(request, 'client_portal/terms.html')


# ==================== VISTAS DE USUARIO AUTENTICADO ====================

@login_required
def my_reservations(request):
    """
    Página de historial de reservas del usuario autenticado
    """
    status_filter = request.GET.get('status', 'all')
    search_query = request.GET.get('search', '').strip()

    tickets = Ticket.objects.filter(
        created_by=request.user
    ).select_related(
        'trip', 'trip__route', 'trip__route__origin', 'trip__route__destination',
        'seat', 'trip__bus'
    ).order_by('-created_at')

    now = timezone.now()

    if status_filter == 'upcoming':
        tickets = tickets.filter(trip__departure__gte=now)
    elif status_filter == 'past':
        tickets = tickets.filter(trip__departure__lt=now)
    elif status_filter == 'today':
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today_end = now.replace(hour=23, minute=59, second=59, microsecond=999999)
        tickets = tickets.filter(trip__departure__range=(today_start, today_end))

    if search_query:
        tickets = tickets.filter(
            Q(number__icontains=search_query) |
            Q(trip__route__origin__name__icontains=search_query) |
            Q(trip__route__destination__name__icontains=search_query) |
            Q(buyer_name__icontains=search_query)
        )

    paginator = Paginator(tickets, 10)
    page_number = request.GET.get('page', 1)
    page_obj = paginator.get_page(page_number)

    total_reservations = Ticket.objects.filter(created_by=request.user).count()
    upcoming_reservations = Ticket.objects.filter(
        created_by=request.user,
        trip__departure__gte=now
    ).count()

    context = {
        'page_obj': page_obj,
        'total_reservations': total_reservations,
        'upcoming_reservations': upcoming_reservations,
        'status_filter': status_filter,
        'search_query': search_query,
        'paso_actual': 0,
    }
    return render(request, 'client_portal/my_reservations.html', context)


@login_required
def reservation_detail(request, ticket_id):
    """
    Detalle de una reserva específica
    """
    ticket = get_object_or_404(
        Ticket.objects.select_related(
            'trip', 'trip__route', 'trip__route__origin', 'trip__route__destination',
            'seat', 'trip__bus', 'customer'
        ),
        id=ticket_id,
        created_by=request.user
    )

    trip_tickets = Ticket.objects.filter(
        trip=ticket.trip,
        created_by=request.user
    ).select_related('seat')

    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(f"TICKET:{ticket.number}\nVIAJE:{ticket.trip.id}\nASIENTO:{ticket.seat.number}")
    qr.make(fit=True)

    img = qr.make_image(fill_color="black", back_color="white")
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    qr_base64 = base64.b64encode(buffer.getvalue()).decode()

    now = timezone.now()
    is_upcoming = ticket.trip.departure > now
    is_past = ticket.trip.departure < now
    is_today = ticket.trip.departure.date() == now.date()

    can_cancel = is_upcoming and (ticket.trip.departure - now).total_seconds() > 4 * 3600

    total_amount = sum(t.price for t in trip_tickets)

    context = {
        'ticket': ticket,
        'trip_tickets': trip_tickets,
        'qr_code': qr_base64,
        'total_amount': total_amount,
        'is_upcoming': is_upcoming,
        'is_past': is_past,
        'is_today': is_today,
        'can_cancel': can_cancel,
        'paso_actual': 0,
    }
    return render(request, 'client_portal/reservation_detail.html', context)


@login_required
@require_POST
def cancel_reservation(request, ticket_id):
    """
    Cancela una reserva (solo si es posible)
    """
    from booking.models import Ticket

    ticket = get_object_or_404(
        Ticket,
        id=ticket_id,
        created_by=request.user
    )

    now = timezone.now()
    
    # ✅ VERIFICAR QUE EL TICKET NO ESTÉ YA CANCELADO
    # Nota: Si no hay un campo `is_cancelled`, asumimos que existe
    # y lo verificamos. Si no existe, podemos usar un campo de estado.
    # Por ahora, verificamos si el ticket aún existe (ya que si se canceló,
    # podría haber sido eliminado con el método de abajo)
    
    # Intentar obtener el ticket nuevamente para verificar que existe
    try:
        ticket.refresh_from_db()
    except Ticket.DoesNotExist:
        messages.error(request, "Este ticket ya fue cancelado o no existe.")
        return redirect('client_portal:my_reservations')

    if ticket.trip.departure <= now:
        messages.error(request, "No puedes cancelar un viaje que ya ha partido.")
        return redirect('client_portal:reservation_detail', ticket_id=ticket.id)

    if (ticket.trip.departure - now).total_seconds() < 4 * 3600:
        messages.error(request, "No puedes cancelar un viaje con menos de 4 horas de anticipación.")
        return redirect('client_portal:reservation_detail', ticket_id=ticket.id)

    try:
        with transaction.atomic():
            # ✅ BLOQUEAR EL TICKET Y EL ASIENTO
            ticket = Ticket.objects.select_for_update().get(pk=ticket_id)
            
            trip_tickets = Ticket.objects.filter(
                trip=ticket.trip,
                created_by=request.user
            ).select_for_update()

            for t in trip_tickets:
                if hasattr(t.seat, 'is_occupied'):
                    t.seat.is_occupied = False
                    t.seat.save(update_fields=['is_occupied'])

            # ✅ ELIMINAR TICKETS Y REGISTRAR EN AUDITORÍA
            ticket_numbers = [t.number for t in trip_tickets]
            trip_tickets.delete()
            
            # Registrar en auditoría (si tienes el modelo)
            try:
                from booking.models import AuditLog
                AuditLog.objects.create(
                    user=request.user,
                    action='cancel_reservation',
                    model_name='Ticket',
                    object_repr=f"Cancelación de {', '.join(ticket_numbers)}",
                    changes={
                        'trip_id': ticket.trip.id,
                        'ticket_numbers': ticket_numbers,
                        'cancelled_at': str(timezone.now())
                    }
                )
            except Exception:
                pass

            messages.success(request, f"Tu reserva para {ticket.trip.route.origin.name} → {ticket.trip.route.destination.name} ha sido cancelada exitosamente.")

    except Exception as e:
        messages.error(request, f"Error al cancelar la reserva: {str(e)}")

    return redirect('client_portal:my_reservations')




# ==================== VISTAS DE ADMINISTRACIÓN / DUEÑOS ====================

@user_passes_test(is_owner_or_manager, login_url='client_portal:home')
def admin_dashboard(request):
    """
    Dashboard para dueños/jefes - SOLO LECTURA
    Muestra estadísticas de ventas web y POS
    """
    context = {
        'title': 'Dashboard de Ventas',
        'paso_actual': 0,
    }
    return render(request, 'client_portal/admin_dashboard.html', context)


@user_passes_test(is_owner_or_manager)
def dashboard_stats(request):
    """
    API para obtener estadísticas del dashboard con caché y optimizaciones.
    
    Esta vista utiliza caching para reducir la carga de la base de datos
    y optimiza las consultas agregadas.
    """
    from django.core.cache import cache
    from django.db.models import Case, When, IntegerField, DecimalField, Sum, Count, Q
    
    try:
        days = int(request.GET.get('days', 30))
        
        # ===== CACHÉ =====
        cache_key = f'dashboard_stats_{days}_{timezone.now().date().isoformat()}'
        cached_data = cache.get(cache_key)
        
        if cached_data:
            return JsonResponse(cached_data)
        
        start_date = timezone.now() - timedelta(days=days)
        today = timezone.now().date()
        now = timezone.now()

        # ===== MÉTRICAS PRINCIPALES =====
        total_tickets = Ticket.objects.filter(
            created_at__gte=start_date
        ).count()

        total_revenue = Ticket.objects.filter(
            created_at__gte=start_date
        ).aggregate(total=Sum('price'))['total'] or Decimal('0')

        tickets_today = Ticket.objects.filter(
            created_at__date=today
        ).count()

        revenue_today = Ticket.objects.filter(
            created_at__date=today
        ).aggregate(total=Sum('price'))['total'] or Decimal('0')

        month_start = timezone.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        tickets_month = Ticket.objects.filter(
            created_at__gte=month_start
        ).count()

        revenue_month = Ticket.objects.filter(
            created_at__gte=month_start
        ).aggregate(total=Sum('price'))['total'] or Decimal('0')

        # ===== CANALES (OPTIMIZADO) =====
        from django.db.models import DecimalField
        
        channel_stats = Ticket.objects.filter(
            created_at__gte=start_date
        ).aggregate(
            web_tickets=Count(Case(When(created_by__is_superuser=True, then=1), output_field=IntegerField())),
            pos_tickets=Count(Case(When(created_by__is_superuser=False, then=1), output_field=IntegerField())),
            web_revenue=Sum(Case(
                When(created_by__is_superuser=True, then='price'),
                default=0,
                output_field=DecimalField(max_digits=12, decimal_places=2)
            )),
            pos_revenue=Sum(Case(
                When(created_by__is_superuser=False, then='price'),
                default=0,
                output_field=DecimalField(max_digits=12, decimal_places=2)
            )),
        )

        web_tickets = channel_stats['web_tickets'] or 0
        pos_tickets = channel_stats['pos_tickets'] or 0
        web_revenue = channel_stats['web_revenue'] or Decimal('0')
        pos_revenue = channel_stats['pos_revenue'] or Decimal('0')

        # ===== RUTAS MÁS VENDIDAS =====
        route_sales = Ticket.objects.filter(
            created_at__gte=start_date
        ).values(
            'trip__route__origin__name',
            'trip__route__destination__name'
        ).annotate(
            total_tickets=Count('id'),
            total_revenue=Sum('price')
        ).order_by('-total_revenue')[:10]

        route_sales_data = []
        for route in route_sales:
            route_sales_data.append({
                'route': f"{route['trip__route__origin__name']} → {route['trip__route__destination__name']}",
                'tickets': route['total_tickets'],
                'revenue': float(route['total_revenue'] or 0)
            })

        # ===== VENTAS DIARIAS =====
        daily_sales = Ticket.objects.filter(
            created_at__gte=start_date
        ).annotate(
            day=TruncDate('created_at')
        ).values('day').annotate(
            total=Sum('price'),
            count=Count('id')
        ).order_by('day')

        days_data = []
        revenue_data = []
        tickets_data = []

        date_range = []
        current_date = start_date.date()
        end_date = timezone.now().date()
        while current_date <= end_date:
            date_range.append(current_date)
            current_date += timedelta(days=1)

        sales_by_day = {s['day']: s for s in daily_sales}

        for date in date_range:
            days_data.append(date.strftime('%d/%m'))
            day_sales = sales_by_day.get(date, {})
            revenue_data.append(float(day_sales.get('total', 0)))
            tickets_data.append(day_sales.get('count', 0))

        # ===== TOP VENDEDORES =====
        top_sellers = Ticket.objects.filter(
            created_at__gte=start_date,
            created_by__is_superuser=False
        ).values(
            'created_by__username',
            'created_by__first_name',
            'created_by__last_name'
        ).annotate(
            total_tickets=Count('id'),
            total_revenue=Sum('price')
        ).order_by('-total_tickets')[:5]

        sellers_data = []
        for seller in top_sellers:
            full_name = f"{seller['created_by__first_name']} {seller['created_by__last_name']}".strip()
            if not full_name or full_name == '':
                full_name = seller['created_by__username'] or 'Anónimo'
            sellers_data.append({
                'name': full_name,
                'tickets': seller['total_tickets'],
                'revenue': float(seller['total_revenue'] or 0)
            })

        # ===== OCUPACIÓN DE VIAJES PRÓXIMOS =====
        upcoming_trips = Trip.objects.filter(
            departure__gte=now,
            departure__lte=now + timedelta(days=7)
        ).select_related('route', 'bus').annotate(
            sold_count=Count('tickets')
        ).order_by('departure')[:10]

        occupancy_data = []
        for trip in upcoming_trips:
            sold = trip.sold_count
            total = trip.seats_total or 0
            occupancy = round((sold / total * 100) if total > 0 else 0, 1)
            occupancy_data.append({
                'trip': str(trip),
                'departure': trip.departure.strftime('%d/%m %H:%M'),
                'sold': sold,
                'total': total,
                'occupancy': occupancy,
                'route': str(trip.route)
            })

        # ===== MÉTODOS DE PAGO =====
        payment_methods = Ticket.objects.filter(
            created_at__gte=start_date
        ).values('payment_method').annotate(
            count=Count('id'),
            total=Sum('price')
        )

        payment_data = []
        for pm in payment_methods:
            method = pm['payment_method'] or 'No especificado'
            method_map = {
                'cash': 'Efectivo',
                'card': 'Tarjeta',
                'credit': 'Crédito Convenio',
            }
            method = method_map.get(method, method)
            payment_data.append({
                'method': method,
                'tickets': pm['count'],
                'revenue': float(pm['total'] or 0)
            })

        # ===== VENTAS POR HORA =====
        sales_by_hour = Ticket.objects.filter(
            created_at__gte=start_date
        ).annotate(
            hour=ExtractHour('created_at')
        ).values('hour').annotate(
            count=Count('id'),
            total=Sum('price')
        ).order_by('hour')

        hours_data = []
        hours_count = []
        hours_revenue = []

        hour_map = {h: {'count': 0, 'total': 0} for h in range(24)}
        for item in sales_by_hour:
            h = item['hour']
            if h in hour_map:
                hour_map[h]['count'] = item['count']
                hour_map[h]['total'] = float(item['total'] or 0)

        for h in range(24):
            hours_data.append(f"{h:02d}:00")
            hours_count.append(hour_map[h]['count'])
            hours_revenue.append(hour_map[h]['total'])

        # ===== VENTAS POR DÍA DE LA SEMANA =====
        sales_by_weekday = Ticket.objects.filter(
            created_at__gte=start_date
        ).annotate(
            weekday=ExtractWeekDay('created_at')
        ).values('weekday').annotate(
            count=Count('id'),
            total=Sum('price')
        ).order_by('weekday')

        weekday_names = ['Lunes', 'Martes', 'Miércoles', 'Jueves', 'Viernes', 'Sábado', 'Domingo']
        weekday_count = []
        weekday_revenue = []

        weekday_map = {i: {'count': 0, 'total': 0} for i in range(1, 8)}
        for item in sales_by_weekday:
            wd = item['weekday']
            if wd in weekday_map:
                weekday_map[wd]['count'] = item['count']
                weekday_map[wd]['total'] = float(item['total'] or 0)

        for i in range(1, 8):
            weekday_count.append(weekday_map[i]['count'])
            weekday_revenue.append(weekday_map[i]['total'])

        # ===== VENTAS POR AGENCIA =====
        agency_sales = []
        try:
            agency_sales_raw = Ticket.objects.filter(
                created_at__gte=start_date,
                created_by__is_superuser=False
            ).values(
                'created_by__profile__terminal__name'
            ).annotate(
                count=Count('id'),
                total=Sum('price')
            ).order_by('-total')

            for item in agency_sales_raw:
                name = item['created_by__profile__terminal__name'] or 'Sin agencia'
                agency_sales.append({
                    'agency': name,
                    'tickets': item['count'],
                    'revenue': float(item['total'] or 0)
                })
        except Exception:
            pass

        # ===== COMPARACIÓN CON PERÍODO ANTERIOR =====
        days_back = days
        current_period = Ticket.objects.filter(
            created_at__gte=start_date
        ).aggregate(
            revenue=Sum('price'),
            count=Count('id')
        )

        previous_start = start_date - timedelta(days=days_back)
        previous_period = Ticket.objects.filter(
            created_at__gte=previous_start,
            created_at__lt=start_date
        ).aggregate(
            revenue=Sum('price'),
            count=Count('id')
        )

        current_revenue = float(current_period['revenue'] or 0)
        previous_revenue = float(previous_period['revenue'] or 0)

        revenue_change = 0
        if previous_revenue > 0:
            revenue_change = ((current_revenue - previous_revenue) / previous_revenue) * 100

        current_count = current_period['count'] or 0
        previous_count = previous_period['count'] or 0
        tickets_change = 0
        if previous_count > 0:
            tickets_change = ((current_count - previous_count) / previous_count) * 100

        # ===== CONSTRUIR RESPUESTA =====
        response_data = {
            'success': True,
            'summary': {
                'total_tickets': total_tickets,
                'total_revenue': float(total_revenue),
                'tickets_today': tickets_today,
                'revenue_today': float(revenue_today),
                'tickets_month': tickets_month,
                'revenue_month': float(revenue_month),
                'revenue_change': round(revenue_change, 1),
                'tickets_change': round(tickets_change, 1),
            },
            'channels': {
                'web_tickets': web_tickets,
                'pos_tickets': pos_tickets,
                'web_revenue': float(web_revenue),
                'pos_revenue': float(pos_revenue),
            },
            'daily_sales': {
                'days': days_data,
                'revenue': revenue_data,
                'tickets': tickets_data
            },
            'route_sales': route_sales_data,
            'top_sellers': sellers_data,
            'occupancy': occupancy_data,
            'payment_methods': payment_data,
            'hourly_sales': {
                'labels': hours_data,
                'count': hours_count,
                'revenue': hours_revenue
            },
            'weekday_sales': {
                'labels': weekday_names,
                'count': weekday_count,
                'revenue': weekday_revenue
            },
            'agency_sales': agency_sales,
            # ===== METADATOS =====
            'metadata': {
                'period_days': days,
                'start_date': start_date.isoformat(),
                'end_date': timezone.now().isoformat(),
                'generated_at': timezone.now().isoformat(),
            }
        }
        
        # ===== GUARDAR EN CACHÉ =====
        cache.set(cache_key, response_data, 300)  # 5 minutos

        return JsonResponse(response_data)

    except Exception as e:
        import traceback
        traceback.print_exc()
        return JsonResponse({
            'success': False,
            'error': str(e),
            'traceback': traceback.format_exc() if settings.DEBUG else None
        }, status=500)
        

@user_passes_test(is_owner_or_manager)
def export_dashboard_pdf(request):
    """
    Exporta el dashboard en formato PDF con diseño profesional y optimizaciones.
    
    Características:
    - Diseño corporativo con logo y colores de la empresa
    - Tablas con formato profesional
    - Resumen ejecutivo completo
    - Gráficos de barras simples (opcional)
    - Footer con número de página
    """
    if not HAS_REPORTLAB:
        return JsonResponse({
            'error': 'ReportLab no está instalado. Ejecuta: pip install reportlab'
        }, status=500)

    try:
        days = int(request.GET.get('days', 30))
        start_date = timezone.now() - timedelta(days=days)
        end_date = timezone.now()

        # ===== OBTENER DATOS =====
        total_tickets = Ticket.objects.filter(created_at__gte=start_date).count()
        total_revenue = Ticket.objects.filter(created_at__gte=start_date).aggregate(total=Sum('price'))['total'] or Decimal('0')
        
        # Tickets por canal
        web_tickets = Ticket.objects.filter(
            created_at__gte=start_date,
            created_by__is_superuser=True
        ).count()
        pos_tickets = total_tickets - web_tickets

        # Rutas más vendidas
        route_sales = Ticket.objects.filter(
            created_at__gte=start_date
        ).values(
            'trip__route__origin__name',
            'trip__route__destination__name'
        ).annotate(
            total_tickets=Count('id'),
            total_revenue=Sum('price')
        ).order_by('-total_revenue')[:10]

        # Ventas diarias (últimos 14 días)
        daily_sales = Ticket.objects.filter(
            created_at__gte=start_date
        ).annotate(
            day=TruncDate('created_at')
        ).values('day').annotate(
            total=Sum('price'),
            count=Count('id')
        ).order_by('-day')[:14]

        # Top vendedores
        top_sellers = Ticket.objects.filter(
            created_at__gte=start_date,
            created_by__is_superuser=False
        ).values(
            'created_by__username',
            'created_by__first_name',
            'created_by__last_name'
        ).annotate(
            total_tickets=Count('id'),
            total_revenue=Sum('price')
        ).order_by('-total_tickets')[:5]

        # Métodos de pago
        payment_methods = Ticket.objects.filter(
            created_at__gte=start_date
        ).values('payment_method').annotate(
            count=Count('id'),
            total=Sum('price')
        )

        # ===== CONFIGURAR DOCUMENTO =====
        response = HttpResponse(content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename="reporte_ventas_{datetime.now().strftime("%Y%m%d")}.pdf"'

        # Estilos de página
        doc = SimpleDocTemplate(
            response,
            pagesize=A4,
            rightMargin=72,
            leftMargin=72,
            topMargin=72,
            bottomMargin=72,
        )

        styles = getSampleStyleSheet()
        
        # Estilos personalizados
        title_style = ParagraphStyle(
            'CustomTitle',
            parent=styles['Heading1'],
            alignment=TA_CENTER,
            fontSize=20,
            textColor=colors.HexColor('#e30613'),  # Color Cejer
            spaceAfter=20,
            fontName='Helvetica-Bold'
        )
        
        subtitle_style = ParagraphStyle(
            'CustomSubtitle',
            parent=styles['Normal'],
            alignment=TA_CENTER,
            fontSize=11,
            textColor=colors.grey,
            spaceAfter=30,
        )
        
        section_style = ParagraphStyle(
            'CustomSection',
            parent=styles['Heading2'],
            fontSize=14,
            textColor=colors.HexColor('#1a1a2e'),
            spaceBefore=20,
            spaceAfter=10,
            fontName='Helvetica-Bold'
        )
        
        footer_style = ParagraphStyle(
            'CustomFooter',
            parent=styles['Normal'],
            fontSize=8,
            textColor=colors.grey,
            alignment=TA_CENTER,
        )
        
        normal_style = styles['Normal']
        
        # ===== CONSTRUIR ELEMENTOS =====
        elements = []

        # --- TÍTULO ---
        elements.append(Paragraph("📊 REPORTE DE VENTAS", title_style))
        elements.append(Paragraph(
            f"Período: {start_date.strftime('%d/%m/%Y')} - {end_date.strftime('%d/%m/%Y')} | "
            f"Generado: {end_date.strftime('%d/%m/%Y %H:%M')}",
            subtitle_style
        ))
        elements.append(Spacer(1, 0.1 * inch))

        # --- RESUMEN GENERAL ---
        elements.append(Paragraph("1. RESUMEN GENERAL", section_style))
        elements.append(Spacer(1, 0.05 * inch))

        summary_data = [
            ['Métrica', 'Valor', ''],
            ['Total Boletos', f"{total_tickets:,}", ''],
            ['Total Ingresos', f"${float(total_revenue):,.0f}", ''],
            ['Boletos Web', f"{web_tickets:,}", f"{round(web_tickets/total_tickets*100, 1) if total_tickets > 0 else 0}%"],
            ['Boletos POS', f"{pos_tickets:,}", f"{round(pos_tickets/total_tickets*100, 1) if total_tickets > 0 else 0}%"],
        ]

        summary_table = Table(summary_data, colWidths=[2.5*inch, 2*inch, 1.5*inch])
        summary_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#e30613')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 10),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 8),
            ('TOPPADDING', (0, 0), (-1, 0), 8),
            ('BACKGROUND', (0, 1), (-1, -1), colors.HexColor('#f8f9fa')),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.lightgrey),
            ('FONTSIZE', (0, 1), (-1, -1), 9),
            ('TOPPADDING', (0, 1), (-1, -1), 5),
            ('BOTTOMPADDING', (0, 1), (-1, -1), 5),
        ]))
        elements.append(summary_table)
        elements.append(Spacer(1, 0.15 * inch))

        # --- VENTAS POR RUTA ---
        elements.append(Paragraph("2. VENTAS POR RUTA (Top 10)", section_style))
        elements.append(Spacer(1, 0.05 * inch))

        route_data = [['#', 'Ruta', 'Boletos', 'Ingresos']]
        for idx, route in enumerate(route_sales, 1):
            route_data.append([
                str(idx),
                f"{route['trip__route__origin__name']} → {route['trip__route__destination__name']}",
                str(route['total_tickets']),
                f"${float(route['total_revenue'] or 0):,.0f}"
            ])

        if len(route_data) > 1:
            route_table = Table(route_data, colWidths=[0.5*inch, 2.5*inch, 1.5*inch, 1.5*inch])
            route_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#2c3e50')),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, 0), 9),
                ('BOTTOMPADDING', (0, 0), (-1, 0), 6),
                ('TOPPADDING', (0, 0), (-1, 0), 6),
                ('BACKGROUND', (0, 1), (-1, -1), colors.HexColor('#f8f9fa')),
                ('GRID', (0, 0), (-1, -1), 0.5, colors.lightgrey),
                ('FONTSIZE', (0, 1), (-1, -1), 8),
                ('TOPPADDING', (0, 1), (-1, -1), 4),
                ('BOTTOMPADDING', (0, 1), (-1, -1), 4),
                # Alternar colores de filas
                ('BACKGROUND', (0, 1), (-1, 1), colors.HexColor('#ffffff')),
                ('BACKGROUND', (0, 2), (-1, 2), colors.HexColor('#f8f9fa')),
                ('BACKGROUND', (0, 3), (-1, 3), colors.HexColor('#ffffff')),
                ('BACKGROUND', (0, 4), (-1, 4), colors.HexColor('#f8f9fa')),
                ('BACKGROUND', (0, 5), (-1, 5), colors.HexColor('#ffffff')),
                ('BACKGROUND', (0, 6), (-1, 6), colors.HexColor('#f8f9fa')),
                ('BACKGROUND', (0, 7), (-1, 7), colors.HexColor('#ffffff')),
                ('BACKGROUND', (0, 8), (-1, 8), colors.HexColor('#f8f9fa')),
                ('BACKGROUND', (0, 9), (-1, 9), colors.HexColor('#ffffff')),
                ('BACKGROUND', (0, 10), (-1, 10), colors.HexColor('#f8f9fa')),
            ]))
            elements.append(route_table)
        else:
            elements.append(Paragraph(
                "No hay datos de rutas en este período.",
                normal_style
            ))
        elements.append(Spacer(1, 0.15 * inch))

        # --- TOP VENDEDORES ---
        elements.append(Paragraph("3. TOP VENDEDORES", section_style))
        elements.append(Spacer(1, 0.05 * inch))

        seller_data = [['#', 'Vendedor', 'Boletos', 'Ingresos']]
        for idx, seller in enumerate(top_sellers, 1):
            name = f"{seller['created_by__first_name']} {seller['created_by__last_name']}".strip()
            if not name or name == '':
                name = seller['created_by__username'] or 'Anónimo'
            seller_data.append([
                str(idx),
                name,
                str(seller['total_tickets']),
                f"${float(seller['total_revenue'] or 0):,.0f}"
            ])

        if len(seller_data) > 1:
            seller_table = Table(seller_data, colWidths=[0.5*inch, 2.5*inch, 1.5*inch, 1.5*inch])
            seller_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#2c3e50')),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, 0), 9),
                ('BOTTOMPADDING', (0, 0), (-1, 0), 6),
                ('TOPPADDING', (0, 0), (-1, 0), 6),
                ('BACKGROUND', (0, 1), (-1, -1), colors.HexColor('#f8f9fa')),
                ('GRID', (0, 0), (-1, -1), 0.5, colors.lightgrey),
                ('FONTSIZE', (0, 1), (-1, -1), 8),
                ('TOPPADDING', (0, 1), (-1, -1), 4),
                ('BOTTOMPADDING', (0, 1), (-1, -1), 4),
            ]))
            elements.append(seller_table)
        else:
            elements.append(Paragraph(
                "No hay datos de vendedores en este período.",
                normal_style
            ))

        # --- NUEVA PÁGINA ---
        elements.append(PageBreak())

        # --- VENTAS DIARIAS ---
        elements.append(Paragraph("4. VENTAS DIARIAS (últimos 14 días)", section_style))
        elements.append(Spacer(1, 0.05 * inch))

        daily_data = [['Fecha', 'Boletos', 'Ingresos']]
        for sale in daily_sales[:14]:
            daily_data.append([
                sale['day'].strftime('%d/%m/%Y'),
                str(sale['count']),
                f"${float(sale['total'] or 0):,.0f}"
            ])

        if len(daily_data) > 1:
            daily_table = Table(daily_data, colWidths=[2.5*inch, 1.5*inch, 2*inch])
            daily_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#2c3e50')),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, 0), 9),
                ('BOTTOMPADDING', (0, 0), (-1, 0), 6),
                ('TOPPADDING', (0, 0), (-1, 0), 6),
                ('BACKGROUND', (0, 1), (-1, -1), colors.HexColor('#f8f9fa')),
                ('GRID', (0, 0), (-1, -1), 0.5, colors.lightgrey),
                ('FONTSIZE', (0, 1), (-1, -1), 8),
                ('TOPPADDING', (0, 1), (-1, -1), 4),
                ('BOTTOMPADDING', (0, 1), (-1, -1), 4),
                # Alternar colores de filas
                ('BACKGROUND', (0, 1), (-1, 1), colors.HexColor('#ffffff')),
                ('BACKGROUND', (0, 2), (-1, 2), colors.HexColor('#f8f9fa')),
                ('BACKGROUND', (0, 3), (-1, 3), colors.HexColor('#ffffff')),
                ('BACKGROUND', (0, 4), (-1, 4), colors.HexColor('#f8f9fa')),
                ('BACKGROUND', (0, 5), (-1, 5), colors.HexColor('#ffffff')),
                ('BACKGROUND', (0, 6), (-1, 6), colors.HexColor('#f8f9fa')),
                ('BACKGROUND', (0, 7), (-1, 7), colors.HexColor('#ffffff')),
                ('BACKGROUND', (0, 8), (-1, 8), colors.HexColor('#f8f9fa')),
                ('BACKGROUND', (0, 9), (-1, 9), colors.HexColor('#ffffff')),
                ('BACKGROUND', (0, 10), (-1, 10), colors.HexColor('#f8f9fa')),
                ('BACKGROUND', (0, 11), (-1, 11), colors.HexColor('#ffffff')),
                ('BACKGROUND', (0, 12), (-1, 12), colors.HexColor('#f8f9fa')),
                ('BACKGROUND', (0, 13), (-1, 13), colors.HexColor('#ffffff')),
                ('BACKGROUND', (0, 14), (-1, 14), colors.HexColor('#f8f9fa')),
            ]))
            elements.append(daily_table)
        else:
            elements.append(Paragraph(
                "No hay datos diarios en este período.",
                normal_style
            ))
        elements.append(Spacer(1, 0.15 * inch))

        # --- MÉTODOS DE PAGO ---
        elements.append(Paragraph("5. MÉTODOS DE PAGO", section_style))
        elements.append(Spacer(1, 0.05 * inch))

        payment_data = [['Método', 'Boletos', 'Ingresos', '%']]
        total_payment_tickets = sum(p['count'] for p in payment_methods)
        
        for pm in payment_methods:
            method = pm['payment_method'] or 'No especificado'
            method_map = {
                'cash': 'Efectivo',
                'card': 'Tarjeta',
                'credit': 'Crédito Convenio',
            }
            method = method_map.get(method, method)
            percentage = round(pm['count'] / total_payment_tickets * 100, 1) if total_payment_tickets > 0 else 0
            payment_data.append([
                method,
                str(pm['count']),
                f"${float(pm['total'] or 0):,.0f}",
                f"{percentage}%"
            ])

        if len(payment_data) > 1:
            payment_table = Table(payment_data, colWidths=[2*inch, 1.5*inch, 2*inch, 1.5*inch])
            payment_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#2c3e50')),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, 0), 9),
                ('BOTTOMPADDING', (0, 0), (-1, 0), 6),
                ('TOPPADDING', (0, 0), (-1, 0), 6),
                ('BACKGROUND', (0, 1), (-1, -1), colors.HexColor('#f8f9fa')),
                ('GRID', (0, 0), (-1, -1), 0.5, colors.lightgrey),
                ('FONTSIZE', (0, 1), (-1, -1), 8),
                ('TOPPADDING', (0, 1), (-1, -1), 4),
                ('BOTTOMPADDING', (0, 1), (-1, -1), 4),
            ]))
            elements.append(payment_table)
        else:
            elements.append(Paragraph(
                "No hay datos de métodos de pago en este período.",
                normal_style
            ))

        # --- FOOTER ---
        elements.append(Spacer(1, 0.5 * inch))
        elements.append(Paragraph(
            f"Reporte generado el {timezone.now().strftime('%d/%m/%Y %H:%M')} - Cejer S.A.",
            ParagraphStyle(
                'CustomFooter',
                parent=normal_style,
                fontSize=8,
                textColor=colors.grey,
                alignment=TA_CENTER,
                spaceBefore=20,
            )
        ))

        # ===== CONSTRUIR PDF =====
        doc.build(elements)
        return response

    except ImportError as e:
        return JsonResponse({
            'error': f'Error de importación: {str(e)}. Asegúrate de que ReportLab esté instalado correctamente.'
        }, status=500)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JsonResponse({
            'error': f'Error al generar el PDF: {str(e)}',
            'traceback': traceback.format_exc() if settings.DEBUG else None
        }, status=500)


@user_passes_test(is_owner_or_manager)
def export_dashboard_excel(request):
    """
    Exporta el dashboard en formato Excel
    """
    try:
        days = int(request.GET.get('days', 30))
        start_date = timezone.now() - timedelta(days=days)

        output = io.BytesIO()
        workbook = xlsxwriter.Workbook(output)

        worksheet1 = workbook.add_worksheet('Resumen')
        header_format = workbook.add_format({'bold': True, 'bg_color': '#f5821f', 'color': 'white'})

        worksheet1.merge_range('A1:D1', '📊 Reporte de Ventas', workbook.add_format({'bold': True, 'font_size': 18}))
        worksheet1.write('A2', f'Período: {start_date.strftime("%d/%m/%Y")} - {timezone.now().strftime("%d/%m/%Y")}')

        total_tickets = Ticket.objects.filter(created_at__gte=start_date).count()
        total_revenue = Ticket.objects.filter(created_at__gte=start_date).aggregate(total=Sum('price'))['total'] or Decimal('0')

        worksheet1.write('A4', 'Métrica', header_format)
        worksheet1.write('B4', 'Valor', header_format)

        worksheet1.write('A5', 'Total Boletos')
        worksheet1.write('B5', total_tickets)
        worksheet1.write('A6', 'Total Ingresos')
        worksheet1.write('B6', f'${float(total_revenue):,.0f}')

        worksheet2 = workbook.add_worksheet('Ventas por Ruta')

        worksheet2.write('A1', 'Ruta', header_format)
        worksheet2.write('B1', 'Boletos', header_format)
        worksheet2.write('C1', 'Ingresos', header_format)

        route_sales = Ticket.objects.filter(
            created_at__gte=start_date
        ).values(
            'trip__route__origin__name',
            'trip__route__destination__name'
        ).annotate(
            total_tickets=Count('id'),
            total_revenue=Sum('price')
        ).order_by('-total_revenue')[:10]

        row = 2
        for route in route_sales:
            worksheet2.write(f'A{row}', f"{route['trip__route__origin__name']} → {route['trip__route__destination__name']}")
            worksheet2.write(f'B{row}', route['total_tickets'])
            worksheet2.write(f'C{row}', f"${float(route['total_revenue'] or 0):,.0f}")
            row += 1

        worksheet3 = workbook.add_worksheet('Ventas Diarias')

        worksheet3.write('A1', 'Fecha', header_format)
        worksheet3.write('B1', 'Boletos', header_format)
        worksheet3.write('C1', 'Ingresos', header_format)

        daily_sales = Ticket.objects.filter(
            created_at__gte=start_date
        ).annotate(
            day=TruncDate('created_at')
        ).values('day').annotate(
            total=Sum('price'),
            count=Count('id')
        ).order_by('-day')[:30]

        row = 2
        for sale in daily_sales:
            worksheet3.write(f'A{row}', sale['day'].strftime('%d/%m/%Y'))
            worksheet3.write(f'B{row}', sale['count'])
            worksheet3.write(f'C{row}', f"${float(sale['total'] or 0):,.0f}")
            row += 1

        worksheet4 = workbook.add_worksheet('Top Vendedores')

        worksheet4.write('A1', 'Vendedor', header_format)
        worksheet4.write('B1', 'Boletos', header_format)
        worksheet4.write('C1', 'Ingresos', header_format)

        top_sellers = Ticket.objects.filter(
            created_at__gte=start_date,
            created_by__is_superuser=False
        ).values(
            'created_by__username',
            'created_by__first_name',
            'created_by__last_name'
        ).annotate(
            total_tickets=Count('id'),
            total_revenue=Sum('price')
        ).order_by('-total_tickets')[:5]

        row = 2
        for seller in top_sellers:
            name = f"{seller['created_by__first_name']} {seller['created_by__last_name']}".strip()
            if not name or name == '':
                name = seller['created_by__username'] or 'Anónimo'
            worksheet4.write(f'A{row}', name)
            worksheet4.write(f'B{row}', seller['total_tickets'])
            worksheet4.write(f'C{row}', f"${float(seller['total_revenue'] or 0):,.0f}")
            row += 1

        for worksheet in [worksheet1, worksheet2, worksheet3, worksheet4]:
            worksheet.set_column('A:A', 30)
            worksheet.set_column('B:B', 15)
            worksheet.set_column('C:C', 20)

        workbook.close()

        output.seek(0)
        response = HttpResponse(
            output.read(),
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )
        response['Content-Disposition'] = f'attachment; filename="reporte_ventas_{datetime.now().strftime("%Y%m%d")}.xlsx"'
        return response

    except Exception as e:
        import traceback
        traceback.print_exc()
        return JsonResponse({'error': str(e)}, status=500)


# ==================== ENDPOINTS API ====================

@csrf_exempt
@require_http_methods(["POST"])
def validate_coupon(request):
    """
    API para validar cupón de descuento (usando el modelo unificado Promotion).
    """
    try:
        data = json.loads(request.body)
        coupon_code = data.get('coupon_code', '').strip().upper()
        trip_id = data.get('trip_id')
        total_amount = data.get('total_amount')

        # Si no se envía total_amount, calcularlo desde la sesión
        if total_amount is None:
            session_key = request.session.session_key
            if session_key and trip_id:
                holds = SeatHold.objects.filter(
                    trip_id=trip_id,
                    session_key=session_key,
                    active=True,
                    expires_at__gt=timezone.now()
                ).select_related('trip')
                if holds.exists():
                    trip = holds.first().trip
                    base_price = trip.route.base_price
                    total_amount = holds.count() * float(base_price)
                else:
                    total_amount = 0
            else:
                total_amount = 0

        print(f"[DEBUG] validate_coupon: code={coupon_code}, trip={trip_id}, total={total_amount}")

        if not coupon_code:
            return JsonResponse({
                'valid': False,
                'message': 'Ingresa un código de promoción'
            }, status=400)

        # Buscar la promoción (usando iexact para que sea insensible a mayúsculas)
        try:
            promotion = Promotion.objects.get(code__iexact=coupon_code, is_active=True)
        except Promotion.DoesNotExist:
            return JsonResponse({
                'valid': False,
                'message': 'El código ingresado no es válido o no está activo'
            }, status=404)

        # Validar vigencia y usos usando el método del modelo
        valid, message = promotion.is_valid(total_amount)
        if not valid:
            return JsonResponse({
                'valid': False,
                'message': message  # Ej: "Monto mínimo $20,000", "Límite de usos alcanzado", etc.
            }, status=400)

        # Calcular descuento
        discount_amount = promotion.calculate_discount(total_amount)

        return JsonResponse({
            'valid': True,
            'message': f'Descuento de {promotion.discount_value}{"%" if promotion.discount_type == "percentage" else "$"} aplicado',
            'promotion': {
                'code': promotion.code,
                'discount_type': promotion.discount_type,
                'discount_value': float(promotion.discount_value),
                'min_purchase': float(promotion.min_purchase_amount),
            },
            'discount_amount': discount_amount,
            'final_total': total_amount - discount_amount
        })

    except json.JSONDecodeError:
        return JsonResponse({
            'valid': False,
            'message': 'Error en el formato de datos'
        }, status=400)
    except Exception as e:
        import traceback
        print("Error en validate_coupon:", str(e))
        traceback.print_exc()
        return JsonResponse({
            'valid': False,
            'message': f'Error al validar el cupón: {str(e)}'
        }, status=500)


@require_POST
def renew_hold(request, trip_id):
    """
    Renueva los SeatHold activos pertenecientes a la sesión actual.

    Seguridad y concurrencia:
    - Requiere POST.
    - Mantiene activa la protección CSRF de Django.
    - NO crea una sesión nueva al renovar: si no existe session_key,
      el cliente no puede demostrar propiedad sobre ningún hold.
    - Sólo renueva holds del viaje y de la session_key actual.
    - Usa transaction.atomic().
    - Bloquea Seat primero y luego SeatHold para mantener el mismo
      orden de bloqueo utilizado por la creación/liberación de holds.
    - No renueva asientos que ya fueron vendidos.
    - No expone excepciones internas al cliente.
    """

    session_key = request.session.session_key

    if not session_key:
        return JsonResponse(
            {
                "success": False,
                "message": "La sesión de reserva ya no es válida.",
                "renewed": 0,
                "code": "SESSION_NOT_FOUND",
            },
            status=404,
        )

    trip = get_object_or_404(
        Trip.objects.select_related("bus"),
        pk=trip_id,
    )

    now = timezone.now()
    new_expiry = now + timedelta(minutes=10)

    try:
        with transaction.atomic():

            # -------------------------------------------------
            # 1. Localizar únicamente holds activos de ESTA sesión
            # -------------------------------------------------
            candidate_holds = list(
                SeatHold.objects.filter(
                    trip=trip,
                    session_key=session_key,
                    active=True,
                    expires_at__gt=now,
                )
                .values("id", "seat_id")
                .order_by("seat_id", "id")
            )

            if not candidate_holds:
                return JsonResponse(
                    {
                        "success": False,
                        "message": "No hay asientos reservados para renovar.",
                        "renewed": 0,
                        "code": "NO_ACTIVE_HOLDS",
                    },
                    status=404,
                )

            seat_ids = sorted(
                {
                    item["seat_id"]
                    for item in candidate_holds
                    if item["seat_id"] is not None
                }
            )

            hold_ids = [
                item["id"]
                for item in candidate_holds
            ]

            # -------------------------------------------------
            # 2. Bloquear primero los Seat
            #
            # Mantiene el mismo orden de lock que SeatHold.hold():
            # Seat -> SeatHold
            # -------------------------------------------------
            list(
                Seat.objects
                .select_for_update()
                .filter(
                    pk__in=seat_ids,
                    bus=trip.bus,
                )
                .order_by("pk")
                .values_list("pk", flat=True)
            )

            # -------------------------------------------------
            # 3. Bloquear los SeatHold que vamos a renovar
            # -------------------------------------------------
            holds = list(
                SeatHold.objects
                    .select_for_update()
                    .filter(
                        id__in=hold_ids,
                        trip=trip,
                        session_key=session_key,
                        active=True,
                        expires_at__gt=now,
                    )
                    .select_related("seat")
                    .order_by("seat_id", "id")
            )

            if not holds:
                return JsonResponse(
                    {
                        "success": False,
                        "message": "La reserva temporal ya expiró.",
                        "renewed": 0,
                        "code": "HOLD_EXPIRED",
                    },
                    status=409,
                )

            # -------------------------------------------------
            # 4. No renovar holds cuyo asiento ya fue vendido
            # -------------------------------------------------
            locked_seat_ids = [
                hold.seat_id
                for hold in holds
            ]

            sold_seat_ids = set(
                Ticket.objects.filter(
                    trip=trip,
                    seat_id__in=locked_seat_ids,
                ).values_list(
                    "seat_id",
                    flat=True,
                )
            )

            renewable_holds = []
            sold_hold_ids = []

            for hold in holds:
                if hold.seat_id in sold_seat_ids:
                    sold_hold_ids.append(hold.id)
                else:
                    renewable_holds.append(hold)

            # Si por alguna inconsistencia un asiento ya fue vendido,
            # desactivar el hold residual.
            if sold_hold_ids:
                SeatHold.objects.filter(
                    id__in=sold_hold_ids,
                ).update(active=False)

            if not renewable_holds:
                return JsonResponse(
                    {
                        "success": False,
                        "message": (
                            "Los asientos reservados ya no están disponibles."
                        ),
                        "renewed": 0,
                        "code": "SEATS_NO_LONGER_AVAILABLE",
                    },
                    status=409,
                )

            # -------------------------------------------------
            # 5. Renovar únicamente los holds válidos
            # -------------------------------------------------
            renewable_ids = [
                hold.id
                for hold in renewable_holds
            ]

            renewed_count = SeatHold.objects.filter(
                id__in=renewable_ids,
                trip=trip,
                session_key=session_key,
                active=True,
            ).update(
                expires_at=new_expiry
            )

        return JsonResponse(
            {
                "success": True,
                "message": (
                    f"Renovados {renewed_count} "
                    "asientos por 10 minutos."
                ),
                "renewed": renewed_count,
                "new_expiry": new_expiry.isoformat(),
            },
            status=200,
        )

    except Exception:
        logger.exception(
            "Error renovando holds del viaje %s para la sesión %s.",
            trip_id,
            session_key,
        )

        return JsonResponse(
            {
                "success": False,
                "message": (
                    "No fue posible renovar la reserva temporal. "
                    "Intenta nuevamente."
                ),
                "renewed": 0,
                "code": "RENEW_ERROR",
            },
            status=500,
        )


def check_new_sales(request):
    """
    Verifica si hay nuevas ventas desde la última verificación
    """
    try:
        last_check = request.GET.get('last_check')
        if last_check:
            try:
                last_check = datetime.fromisoformat(last_check)
            except:
                last_check = timezone.now() - timedelta(minutes=5)
        else:
            last_check = timezone.now() - timedelta(minutes=5)

        new_tickets = Ticket.objects.filter(
            created_at__gt=last_check
        ).select_related('trip__route__origin', 'trip__route__destination', 'created_by').order_by('-created_at')[:10]

        new_count = new_tickets.count()

        notifications = []
        for ticket in new_tickets:
            notifications.append({
                'id': ticket.id,
                'number': ticket.number,
                'route': f"{ticket.trip.route.origin.name} → {ticket.trip.route.destination.name}",
                'buyer': ticket.buyer_name,
                'price': float(ticket.price),
                'created_at': ticket.created_at.isoformat(),
                'created_by': ticket.created_by.username if ticket.created_by else 'Sistema'
            })

        return JsonResponse({
            'success': True,
            'new_count': new_count,
            'notifications': notifications,
            'timestamp': timezone.now().isoformat()
        })

    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=500)


@csrf_exempt
def client_sync(request):
    """
    API para sincronizar datos entre el POS del vendedor
    y la pantalla del cliente.

    Estados de pantalla:
    - idle: sin venta activa
    - active: venta en proceso
    - completed: venta realizada
    """
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            trip_id = data.get('trip_id')
            action = data.get('action')

            if not trip_id:
                return JsonResponse({
                    'success': False,
                    'error': 'trip_id es obligatorio'
                }, status=400)

            # ============================================================
            # VENTA ACTIVA - ASIENTOS
            # ============================================================
            if action == 'update_seats':
                seats = data.get('seats', [])

                request.session[f'client_seats_{trip_id}'] = seats
                request.session[f'client_active_deck_{trip_id}'] = data.get(
                    'active_deck',
                    1
                )

                # Si existen asientos seleccionados, la venta está activa.
                # Si no quedan asientos, volvemos al modo espera.
                if seats:
                    request.session[
                        f'client_display_state_{trip_id}'
                    ] = 'active'
                else:
                    request.session[
                        f'client_display_state_{trip_id}'
                    ] = 'idle'

                request.session.modified = True

                return JsonResponse({
                    'success': True,
                    'state': request.session.get(
                        f'client_display_state_{trip_id}',
                        'idle'
                    )
                })

            # ============================================================
            # DATOS DEL PASAJERO
            # ============================================================
            elif action == 'update_customer':
                request.session[
                    f'client_customer_{trip_id}'
                ] = data.get('customer', {})

                # Mantener venta activa si existen asientos seleccionados
                seats = request.session.get(
                    f'client_seats_{trip_id}',
                    []
                )

                if seats:
                    request.session[
                        f'client_display_state_{trip_id}'
                    ] = 'active'

                request.session.modified = True

                return JsonResponse({
                    'success': True
                })

            # ============================================================
            # ESTABLECER VIAJE PARA LA PANTALLA CLIENTE
            # ============================================================
            elif action == 'set_trip':
                request.session['client_display_trip_id'] = trip_id

                # Solo establecer idle si todavía no existe estado.
                state_key = f'client_display_state_{trip_id}'

                if state_key not in request.session:
                    request.session[state_key] = 'idle'

                request.session.modified = True

                return JsonResponse({
                    'success': True,
                    'state': request.session[state_key]
                })

            # ============================================================
            # LIMPIAR / VOLVER A PANTALLA DE ESPERA
            # ============================================================
            elif action == 'clear':
                request.session.pop(
                    f'client_seats_{trip_id}',
                    None
                )

                request.session.pop(
                    f'client_customer_{trip_id}',
                    None
                )

                request.session.pop(
                    f'client_active_deck_{trip_id}',
                    None
                )

                request.session.pop(
                    f'client_completed_{trip_id}',
                    None
                )

                request.session[
                    f'client_display_state_{trip_id}'
                ] = 'idle'

                request.session.modified = True

                return JsonResponse({
                    'success': True,
                    'state': 'idle'
                })

            return JsonResponse({
                'success': False,
                'error': 'Acción no reconocida'
            }, status=400)

        except Exception as e:
            return JsonResponse({
                'success': False,
                'error': str(e)
            }, status=500)

    # ================================================================
    # CONSULTA DESDE LA PANTALLA DEL CLIENTE
    # ================================================================
    elif request.method == 'GET':
        trip_id = request.GET.get('trip_id')

        if trip_id:
            return JsonResponse({
                'seats': request.session.get(
                    f'client_seats_{trip_id}',
                    []
                ),

                'customer': request.session.get(
                    f'client_customer_{trip_id}',
                    {}
                ),

                'trip_id': trip_id,

                'active_deck': request.session.get(
                    f'client_active_deck_{trip_id}',
                    1
                ),

                'state': request.session.get(
                    f'client_display_state_{trip_id}',
                    'idle'
                ),

                'completed': request.session.get(
                    f'client_completed_{trip_id}',
                    {}
                ),
            })

    return JsonResponse({
        'success': False,
        'error': 'Método no permitido'
    }, status=405)


def client_display_launcher(request, trip_id):
    """
    Vista que lanza la pantalla del cliente en una nueva ventana/pestaña.
    """
    request.session['client_display_trip_id'] = trip_id
    session_key = request.session.session_key
    return redirect(f'/client/client-display/{trip_id}/?session={session_key}')


# ==================== PANTALLA DEL CLIENTE (MODO EXHIBICIÓN) ====================

def client_display(request, trip_id=None):
    """
    Pantalla para el cliente en segunda pantalla / modo exhibición.
    Muestra los asientos, precios y estado de la compra en tiempo real.
    Los datos sensibles (RUT, teléfono) aparecen parcialmente ocultos.
    """
    from booking.models import Trip

    if not trip_id:
        trip_id = request.session.get('client_display_trip_id')

    if not trip_id:
        return render(request, 'client_portal/client_display.html', {
            'waiting': True,
            'message': 'Esperando selección de viaje...'
        })

    trip = get_object_or_404(Trip.objects.select_related(
        'route__origin', 'route__destination', 'bus', 'bus__company'
    ), pk=trip_id)

    selected_seats = request.session.get(f'client_seats_{trip_id}', [])

    customer_data = request.session.get(f'client_customer_{trip_id}', {})

    customer_data_display = {
        'name': customer_data.get('name', ''),
        'rut': mask_rut(customer_data.get('rut', '')),
        'phone': mask_phone(customer_data.get('phone', '')),
        'email': customer_data.get('email', ''),
    }

    grid_lower, grid_upper, cols = _build_trip_grid(trip)

    total = sum([s.get('price', 0) for s in selected_seats])

    context = {
        'trip': trip,
        'grid_lower': json.dumps(grid_lower),
        'grid_upper': json.dumps(grid_upper),
        'cols': cols,
        'selected_seats': selected_seats,
        'selected_count': len(selected_seats),
        'total': total,
        'customer': customer_data_display,
        'waiting': False,
        'company_name': (
            trip.bus.company.name
            if trip.bus.company
            else 'Cejer'
        ),

        'display_state': request.session.get(
            f'client_display_state_{trip_id}',
            'idle'
        ),

        'completed_data': request.session.get(
            f'client_completed_{trip_id}',
            {}
        ),
    }

    return render(request, 'client_portal/client_display.html', context)


@login_required
def download_ticket_pdf(request, ticket_id):
    """
    FASE 2D.4 - Comprobante PDF individual + QR seguro.

    Seguridad:
    - Permite descargar el ticket si pertenece a una BookingOrder PAID
      del usuario actual.
    - Mantiene compatibilidad con tickets antiguos creados directamente
      por el usuario.
    - No usa datos personales directamente dentro del QR.
    - No modifica Ticket, BookingOrder ni PaymentTransaction.
    """

    import io

    import qrcode

    from django.core import signing
    from django.http import HttpResponse
    from django.utils import timezone

    from booking.models import (
        BookingOrder,
        PaymentTransaction,
        Ticket,
    )

    try:
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_CENTER, TA_LEFT
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import (
            ParagraphStyle,
            getSampleStyleSheet,
        )
        from reportlab.lib.units import cm
        from reportlab.platypus import (
            Image,
            Paragraph,
            SimpleDocTemplate,
            Spacer,
            Table,
            TableStyle,
        )

    except ImportError:
        messages.error(
            request,
            "ReportLab no está instalado. No se puede generar el PDF.",
        )

        return redirect(
            "client_portal:my_reservations"
        )

    # ========================================================================
    # 1. TICKET
    # ========================================================================

    ticket = get_object_or_404(
        Ticket.objects.select_related(
            "trip",
            "trip__route",
            "trip__route__origin",
            "trip__route__destination",
            "trip__bus",
            "seat",
            "customer",
            "created_by",
        ),
        pk=ticket_id,
    )

    # ========================================================================
    # 2. BUSCAR BOOKINGORDER WEB QUE ORIGINÓ EL TICKET
    # ========================================================================

    order = (
        BookingOrder.objects
        .filter(
            trip=ticket.trip,
            user=request.user,
            status=BookingOrder.STATUS_PAID,
            items__seat=ticket.seat,
        )
        .select_related(
            "customer",
            "trip",
            "trip__route",
        )
        .distinct()
        .order_by(
            "-updated_at",
            "-created_at",
        )
        .first()
    )

    # ========================================================================
    # 3. SEGURIDAD
    # ========================================================================

    belongs_to_web_order = (
        order is not None
        and order.user_id == request.user.id
    )

    # Compatibilidad con ventas antiguas/POS donde created_by sí era
    # directamente el usuario comprador/vendedor.
    belongs_to_legacy_ticket = (
        ticket.created_by_id == request.user.id
    )

    if not belongs_to_web_order and not belongs_to_legacy_ticket:

        messages.error(
            request,
            "No tienes permiso para descargar este pasaje.",
        )

        return redirect(
            "client_portal:my_reservations"
        )

    # ========================================================================
    # 4. PAGO AUTORIZADO
    # ========================================================================

    payment = None

    if order:
        payment = (
            PaymentTransaction.objects
            .filter(
                order=order,
                status=PaymentTransaction.STATUS_AUTHORIZED,
            )
            .order_by(
                "-updated_at",
                "-created_at",
            )
            .first()
        )

    # ========================================================================
    # 5. QR SEGURO
    # ========================================================================
    #
    # No incluimos:
    # - nombre
    # - RUT
    # - teléfono
    # - email
    #
    # El token está firmado con SECRET_KEY.
    # En una futura fase de CHECK-IN podremos validarlo con signing.loads().
    # ========================================================================

    qr_payload = signing.dumps(
        {
            "ticket_id": ticket.id,
            "ticket_number": ticket.number,
        },
        salt="cejer-ticket-qr-v1",
        compress=True,
    )

    qr_data = f"CEJER:TICKET:{qr_payload}"

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=2,
    )

    qr.add_data(qr_data)
    qr.make(fit=True)

    qr_pil = qr.make_image(
        fill_color="black",
        back_color="white",
    )

    qr_buffer = io.BytesIO()
    qr_pil.save(
        qr_buffer,
        format="PNG",
    )
    qr_buffer.seek(0)

    qr_image = Image(
        qr_buffer,
        width=3.2 * cm,
        height=3.2 * cm,
    )

    # ========================================================================
    # 6. PDF RESPONSE
    # ========================================================================

    response = HttpResponse(
        content_type="application/pdf"
    )

    safe_ticket_number = (
        str(ticket.number)
        .replace("/", "-")
        .replace("\\", "-")
    )

    response["Content-Disposition"] = (
        f'attachment; filename="pasaje_{safe_ticket_number}.pdf"'
    )

    doc = SimpleDocTemplate(
        response,
        pagesize=A4,
        rightMargin=1.5 * cm,
        leftMargin=1.5 * cm,
        topMargin=1.3 * cm,
        bottomMargin=1.3 * cm,
    )

    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        "TicketTitle",
        parent=styles["Heading1"],
        alignment=TA_CENTER,
        fontSize=21,
        leading=25,
        textColor=colors.HexColor("#f5821f"),
        fontName="Helvetica-Bold",
        spaceAfter=4,
    )

    subtitle_style = ParagraphStyle(
        "TicketSubtitle",
        parent=styles["Normal"],
        alignment=TA_CENTER,
        fontSize=10,
        textColor=colors.HexColor("#64748b"),
        spaceAfter=12,
    )

    section_style = ParagraphStyle(
        "SectionTitle",
        parent=styles["Heading2"],
        fontSize=11,
        leading=14,
        textColor=colors.HexColor("#0f172a"),
        fontName="Helvetica-Bold",
        spaceBefore=10,
        spaceAfter=6,
    )

    normal_style = ParagraphStyle(
        "NormalCustom",
        parent=styles["Normal"],
        fontSize=9,
        leading=13,
        textColor=colors.HexColor("#334155"),
    )

    small_style = ParagraphStyle(
        "SmallCustom",
        parent=styles["Normal"],
        fontSize=7.5,
        leading=10,
        textColor=colors.HexColor("#64748b"),
    )

    elements = []

    # ========================================================================
    # 7. CABECERA
    # ========================================================================

    elements.append(
        Paragraph(
            "CEJER",
            title_style,
        )
    )

    elements.append(
        Paragraph(
            "PASAJE ELECTRÓNICO",
            subtitle_style,
        )
    )

    header = Table(
        [
            [
                Paragraph(
                    (
                        "<b>Pago aprobado</b><br/>"
                        "Comprobante válido de viaje"
                    ),
                    ParagraphStyle(
                        "Approved",
                        parent=normal_style,
                        textColor=colors.HexColor("#166534"),
                        fontSize=10,
                        leading=14,
                    ),
                ),
                qr_image,
            ]
        ],
        colWidths=[
            12.5 * cm,
            4 * cm,
        ],
    )

    header.setStyle(
        TableStyle(
            [
                (
                    "BACKGROUND",
                    (0, 0),
                    (-1, -1),
                    colors.HexColor("#f8fafc"),
                ),
                (
                    "BOX",
                    (0, 0),
                    (-1, -1),
                    0.7,
                    colors.HexColor("#e2e8f0"),
                ),
                (
                    "VALIGN",
                    (0, 0),
                    (-1, -1),
                    "MIDDLE",
                ),
                (
                    "ALIGN",
                    (1, 0),
                    (1, 0),
                    "CENTER",
                ),
                (
                    "TOPPADDING",
                    (0, 0),
                    (-1, -1),
                    10,
                ),
                (
                    "BOTTOMPADDING",
                    (0, 0),
                    (-1, -1),
                    10,
                ),
            ]
        )
    )

    elements.append(header)
    elements.append(
        Spacer(
            1,
            0.25 * cm,
        )
    )

    # ========================================================================
    # 8. INFORMACIÓN DEL PASAJE
    # ========================================================================

    elements.append(
        Paragraph(
            "INFORMACIÓN DEL VIAJE",
            section_style,
        )
    )

    departure_local = timezone.localtime(
        ticket.trip.departure
    )

    arrival_text = "No especificada"

    if ticket.trip.arrival:
        arrival_local = timezone.localtime(
            ticket.trip.arrival
        )

        arrival_text = arrival_local.strftime(
            "%d/%m/%Y %H:%M hrs"
        )

    ticket_data = [
        [
            "N° Ticket",
            ticket.number,
        ],
        [
            "Ruta",
            (
                f"{ticket.trip.route.origin.name} "
                f"→ {ticket.trip.route.destination.name}"
            ),
        ],
        [
            "Fecha salida",
            departure_local.strftime(
                "%d/%m/%Y"
            ),
        ],
        [
            "Hora salida",
            departure_local.strftime(
                "%H:%M hrs"
            ),
        ],
        [
            "Llegada",
            arrival_text,
        ],
        [
            "Asiento",
            (
                f"{ticket.seat.number} "
                f"(Piso {ticket.seat.deck})"
            ),
        ],
        [
            "Pasajero",
            ticket.buyer_name,
        ],
        [
            "Documento",
            ticket.national_id or "No especificado",
        ],
        [
            "Precio",
            f"${ticket.price:,.0f}".replace(",", "."),
        ],
        [
            "Método de pago",
            ticket.get_payment_method_display(),
        ],
    ]

    ticket_table = Table(
        ticket_data,
        colWidths=[
            4.2 * cm,
            11.8 * cm,
        ],
    )

    ticket_table.setStyle(
        TableStyle(
            [
                (
                    "GRID",
                    (0, 0),
                    (-1, -1),
                    0.5,
                    colors.HexColor("#e2e8f0"),
                ),
                (
                    "BACKGROUND",
                    (0, 0),
                    (0, -1),
                    colors.HexColor("#f8fafc"),
                ),
                (
                    "FONTNAME",
                    (0, 0),
                    (0, -1),
                    "Helvetica-Bold",
                ),
                (
                    "FONTNAME",
                    (1, 0),
                    (1, -1),
                    "Helvetica",
                ),
                (
                    "FONTSIZE",
                    (0, 0),
                    (-1, -1),
                    9,
                ),
                (
                    "TEXTCOLOR",
                    (0, 0),
                    (0, -1),
                    colors.HexColor("#64748b"),
                ),
                (
                    "TEXTCOLOR",
                    (1, 0),
                    (1, -1),
                    colors.HexColor("#0f172a"),
                ),
                (
                    "VALIGN",
                    (0, 0),
                    (-1, -1),
                    "MIDDLE",
                ),
                (
                    "TOPPADDING",
                    (0, 0),
                    (-1, -1),
                    6,
                ),
                (
                    "BOTTOMPADDING",
                    (0, 0),
                    (-1, -1),
                    6,
                ),
            ]
        )
    )

    elements.append(ticket_table)

    # ========================================================================
    # 9. INFORMACIÓN DE RESERVA / WEBPAY
    # ========================================================================

    if order:

        elements.append(
            Paragraph(
                "INFORMACIÓN DE LA COMPRA",
                section_style,
            )
        )

        payment_data = [
            [
                "Código de reserva",
                order.code,
            ],
            [
                "Estado",
                "PAGADA",
            ],
            [
                "Total de la compra",
                (
                    f"${order.total_amount:,.0f}"
                    .replace(",", ".")
                ),
            ],
        ]

        if payment:

            payment_data.append(
                [
                    "Autorización Webpay",
                    payment.authorization_code or "-",
                ]
            )

            payment_data.append(
                [
                    "Orden de pago",
                    payment.buy_order,
                ]
            )

            commit_data = (
                (payment.raw_response or {})
                .get("commit")
                or {}
            )

            card_detail = (
                commit_data.get("card_detail")
                or {}
            )

            last_four = (
                card_detail.get("card_number")
                or ""
            )

            if last_four:
                payment_data.append(
                    [
                        "Tarjeta",
                        f"•••• {last_four}",
                    ]
                )

        payment_table = Table(
            payment_data,
            colWidths=[
                4.2 * cm,
                11.8 * cm,
            ],
        )

        payment_table.setStyle(
            TableStyle(
                [
                    (
                        "GRID",
                        (0, 0),
                        (-1, -1),
                        0.5,
                        colors.HexColor("#e2e8f0"),
                    ),
                    (
                        "BACKGROUND",
                        (0, 0),
                        (0, -1),
                        colors.HexColor("#fff7ed"),
                    ),
                    (
                        "FONTNAME",
                        (0, 0),
                        (0, -1),
                        "Helvetica-Bold",
                    ),
                    (
                        "FONTSIZE",
                        (0, 0),
                        (-1, -1),
                        9,
                    ),
                    (
                        "TOPPADDING",
                        (0, 0),
                        (-1, -1),
                        6,
                    ),
                    (
                        "BOTTOMPADDING",
                        (0, 0),
                        (-1, -1),
                        6,
                    ),
                ]
            )
        )

        elements.append(payment_table)

    # ========================================================================
    # 10. VALIDACIÓN QR
    # ========================================================================

    elements.append(
        Paragraph(
            "CÓDIGO DE VALIDACIÓN",
            section_style,
        )
    )

    qr_text = Paragraph(
        (
            "<b>Presenta este código al abordar.</b><br/>"
            "El código identifica digitalmente este pasaje.<br/>"
            "No contiene tu nombre ni tu documento en texto plano."
        ),
        normal_style,
    )

    qr_info = Table(
        [
            [
                qr_image,
                qr_text,
            ]
        ],
        colWidths=[
            4 * cm,
            12 * cm,
        ],
    )

    qr_info.setStyle(
        TableStyle(
            [
                (
                    "BOX",
                    (0, 0),
                    (-1, -1),
                    0.5,
                    colors.HexColor("#cbd5e1"),
                ),
                (
                    "BACKGROUND",
                    (0, 0),
                    (-1, -1),
                    colors.HexColor("#f8fafc"),
                ),
                (
                    "VALIGN",
                    (0, 0),
                    (-1, -1),
                    "MIDDLE",
                ),
                (
                    "ALIGN",
                    (0, 0),
                    (0, 0),
                    "CENTER",
                ),
                (
                    "TOPPADDING",
                    (0, 0),
                    (-1, -1),
                    8,
                ),
                (
                    "BOTTOMPADDING",
                    (0, 0),
                    (-1, -1),
                    8,
                ),
            ]
        )
    )

    elements.append(qr_info)

    # ========================================================================
    # 11. INDICACIONES
    # ========================================================================

    elements.append(
        Spacer(
            1,
            0.25 * cm,
        )
    )

    elements.append(
        Paragraph(
            (
                "Presenta este comprobante, impreso o digital, "
                "junto con tu documento de identidad al momento de abordar."
            ),
            normal_style,
        )
    )

    elements.append(
        Spacer(
            1,
            0.15 * cm,
        )
    )

    elements.append(
        Paragraph(
            (
                "Documento generado el "
                f"{timezone.localtime().strftime('%d/%m/%Y %H:%M')}."
            ),
            small_style,
        )
    )

    # ========================================================================
    # 12. GENERAR PDF
    # ========================================================================

    doc.build(elements)

    return response


@login_required
def download_purchase_pdf(request, order_code):
    """
    FASE 2D.4.1

    Descarga todos los pasajes pertenecientes a una BookingOrder PAID
    en un único PDF.

    - Un QR independiente por Ticket.
    - Sólo permite descargar compras del usuario actual.
    - No modifica ningún dato.
    - No toca Webpay.
    """

    import io
    import qrcode

    from django.core import signing
    from django.http import HttpResponse
    from django.utils import timezone

    from booking.models import (
        BookingOrder,
        PaymentTransaction,
        Ticket,
    )

    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import (
        ParagraphStyle,
        getSampleStyleSheet,
    )
    from reportlab.lib.units import cm
    from reportlab.platypus import (
        Image,
        PageBreak,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    # ============================================================
    # 1. RECUPERAR BOOKINGORDER
    # ============================================================

    order = get_object_or_404(
        BookingOrder.objects.select_related(
            "trip",
            "trip__route",
            "trip__route__origin",
            "trip__route__destination",
            "customer",
        ),
        code=order_code,
        status=BookingOrder.STATUS_PAID,
        user=request.user,
    )

    # ============================================================
    # 2. OBTENER ASIENTOS DE ESTA ORDEN
    # ============================================================

    seat_ids = list(
        order.items.values_list(
            "seat_id",
            flat=True,
        )
    )

    if not seat_ids:
        messages.error(
            request,
            "Esta compra no contiene pasajes.",
        )

        return redirect(
            "client_portal:confirmation",
            trip_id=order.trip_id,
        )

    # ============================================================
    # 3. TICKETS REALES DE ESTA COMPRA
    # ============================================================

    tickets = list(
        Ticket.objects
        .filter(
            trip=order.trip,
            seat_id__in=seat_ids,
        )
        .select_related(
            "seat",
            "trip",
            "trip__route",
            "trip__route__origin",
            "trip__route__destination",
            "customer",
        )
        .order_by(
            "seat__deck",
            "seat__row",
            "seat__position",
            "id",
        )
    )

    if not tickets:
        messages.error(
            request,
            "No se encontraron pasajes emitidos para esta compra.",
        )

        return redirect(
            "client_portal:confirmation",
            trip_id=order.trip_id,
        )

    # ============================================================
    # 4. PAGO AUTORIZADO
    # ============================================================

    payment = (
        PaymentTransaction.objects
        .filter(
            order=order,
            status=PaymentTransaction.STATUS_AUTHORIZED,
        )
        .order_by(
            "-updated_at",
            "-created_at",
        )
        .first()
    )

    # ============================================================
    # 5. RESPONSE PDF
    # ============================================================

    response = HttpResponse(
        content_type="application/pdf"
    )

    response["Content-Disposition"] = (
        f'attachment; filename="pasajes_{order.code}.pdf"'
    )

    doc = SimpleDocTemplate(
        response,
        pagesize=A4,
        rightMargin=1.5 * cm,
        leftMargin=1.5 * cm,
        topMargin=1.3 * cm,
        bottomMargin=1.3 * cm,
    )

    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        "PurchaseTitle",
        parent=styles["Heading1"],
        alignment=TA_CENTER,
        fontSize=21,
        textColor=colors.HexColor("#f5821f"),
        fontName="Helvetica-Bold",
        spaceAfter=4,
    )

    subtitle_style = ParagraphStyle(
        "PurchaseSubtitle",
        parent=styles["Normal"],
        alignment=TA_CENTER,
        fontSize=10,
        textColor=colors.HexColor("#64748b"),
        spaceAfter=14,
    )

    section_style = ParagraphStyle(
        "PurchaseSection",
        parent=styles["Heading2"],
        fontSize=11,
        textColor=colors.HexColor("#0f172a"),
        fontName="Helvetica-Bold",
        spaceBefore=10,
        spaceAfter=6,
    )

    normal_style = ParagraphStyle(
        "PurchaseNormal",
        parent=styles["Normal"],
        fontSize=9,
        leading=13,
        textColor=colors.HexColor("#334155"),
    )

    elements = []

    # ============================================================
    # 6. UNA PÁGINA POR TICKET
    # ============================================================

    for index, ticket in enumerate(tickets):

        if index > 0:
            elements.append(
                PageBreak()
            )

        # --------------------------------------------------------
        # QR ÚNICO
        # --------------------------------------------------------

        qr_payload = signing.dumps(
            {
                "ticket_id": ticket.id,
                "ticket_number": ticket.number,
            },
            salt="cejer-ticket-qr-v1",
            compress=True,
        )

        qr_data = (
            f"CEJER:TICKET:{qr_payload}"
        )

        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=8,
            border=2,
        )

        qr.add_data(qr_data)
        qr.make(fit=True)

        qr_pil = qr.make_image(
            fill_color="black",
            back_color="white",
        )

        qr_buffer = io.BytesIO()

        qr_pil.save(
            qr_buffer,
            format="PNG",
        )

        qr_buffer.seek(0)

        qr_image = Image(
            qr_buffer,
            width=3.2 * cm,
            height=3.2 * cm,
        )

        # --------------------------------------------------------
        # CABECERA
        # --------------------------------------------------------

        elements.append(
            Paragraph(
                "CEJER",
                title_style,
            )
        )

        elements.append(
            Paragraph(
                "PASAJE ELECTRÓNICO",
                subtitle_style,
            )
        )

        header_table = Table(
            [
                [
                    Paragraph(
                        (
                            "<b>Pago aprobado</b><br/>"
                            f"Reserva {order.code}"
                        ),
                        normal_style,
                    ),
                    qr_image,
                ]
            ],
            colWidths=[
                12.5 * cm,
                4 * cm,
            ],
        )

        header_table.setStyle(
            TableStyle(
                [
                    (
                        "BACKGROUND",
                        (0, 0),
                        (-1, -1),
                        colors.HexColor("#f8fafc"),
                    ),
                    (
                        "BOX",
                        (0, 0),
                        (-1, -1),
                        0.5,
                        colors.HexColor("#e2e8f0"),
                    ),
                    (
                        "VALIGN",
                        (0, 0),
                        (-1, -1),
                        "MIDDLE",
                    ),
                    (
                        "ALIGN",
                        (1, 0),
                        (1, 0),
                        "CENTER",
                    ),
                    (
                        "TOPPADDING",
                        (0, 0),
                        (-1, -1),
                        10,
                    ),
                    (
                        "BOTTOMPADDING",
                        (0, 0),
                        (-1, -1),
                        10,
                    ),
                ]
            )
        )

        elements.append(header_table)

        # --------------------------------------------------------
        # INFORMACIÓN
        # --------------------------------------------------------

        elements.append(
            Paragraph(
                "INFORMACIÓN DEL PASAJE",
                section_style,
            )
        )

        departure = timezone.localtime(
            ticket.trip.departure
        )

        data = [
            [
                "N° Ticket",
                ticket.number,
            ],
            [
                "Ruta",
                (
                    f"{ticket.trip.route.origin.name} "
                    f"→ {ticket.trip.route.destination.name}"
                ),
            ],
            [
                "Fecha",
                departure.strftime(
                    "%d/%m/%Y"
                ),
            ],
            [
                "Hora",
                departure.strftime(
                    "%H:%M hrs"
                ),
            ],
            [
                "Asiento",
                (
                    f"{ticket.seat.number} "
                    f"(Piso {ticket.seat.deck})"
                ),
            ],
            [
                "Pasajero",
                ticket.buyer_name,
            ],
            [
                "Documento",
                ticket.national_id,
            ],
            [
                "Precio",
                (
                    f"${ticket.price:,.0f}"
                    .replace(",", ".")
                ),
            ],
        ]

        table = Table(
            data,
            colWidths=[
                4.2 * cm,
                11.8 * cm,
            ],
        )

        table.setStyle(
            TableStyle(
                [
                    (
                        "GRID",
                        (0, 0),
                        (-1, -1),
                        0.5,
                        colors.HexColor("#e2e8f0"),
                    ),
                    (
                        "BACKGROUND",
                        (0, 0),
                        (0, -1),
                        colors.HexColor("#f8fafc"),
                    ),
                    (
                        "FONTNAME",
                        (0, 0),
                        (0, -1),
                        "Helvetica-Bold",
                    ),
                    (
                        "FONTSIZE",
                        (0, 0),
                        (-1, -1),
                        9,
                    ),
                    (
                        "TOPPADDING",
                        (0, 0),
                        (-1, -1),
                        6,
                    ),
                    (
                        "BOTTOMPADDING",
                        (0, 0),
                        (-1, -1),
                        6,
                    ),
                ]
            )
        )

        elements.append(table)

        # --------------------------------------------------------
        # WEBPAY
        # --------------------------------------------------------

        elements.append(
            Paragraph(
                "INFORMACIÓN DE LA COMPRA",
                section_style,
            )
        )

        payment_rows = [
            [
                "Código de reserva",
                order.code,
            ],
            [
                "Estado",
                "PAGADA",
            ],
            [
                "Total compra",
                (
                    f"${order.total_amount:,.0f}"
                    .replace(",", ".")
                ),
            ],
        ]

        if payment:

            payment_rows.append(
                [
                    "Autorización Webpay",
                    payment.authorization_code or "-",
                ]
            )

            payment_rows.append(
                [
                    "Orden de pago",
                    payment.buy_order,
                ]
            )

        payment_table = Table(
            payment_rows,
            colWidths=[
                4.2 * cm,
                11.8 * cm,
            ],
        )

        payment_table.setStyle(
            TableStyle(
                [
                    (
                        "GRID",
                        (0, 0),
                        (-1, -1),
                        0.5,
                        colors.HexColor("#e2e8f0"),
                    ),
                    (
                        "BACKGROUND",
                        (0, 0),
                        (0, -1),
                        colors.HexColor("#fff7ed"),
                    ),
                    (
                        "FONTNAME",
                        (0, 0),
                        (0, -1),
                        "Helvetica-Bold",
                    ),
                    (
                        "FONTSIZE",
                        (0, 0),
                        (-1, -1),
                        9,
                    ),
                    (
                        "TOPPADDING",
                        (0, 0),
                        (-1, -1),
                        6,
                    ),
                    (
                        "BOTTOMPADDING",
                        (0, 0),
                        (-1, -1),
                        6,
                    ),
                ]
            )
        )

        elements.append(payment_table)

        elements.append(
            Spacer(
                1,
                0.25 * cm,
            )
        )

        elements.append(
            Paragraph(
                (
                    "<b>Presenta este código QR al abordar.</b><br/>"
                    "Cada pasaje posee un código de validación independiente."
                ),
                normal_style,
            )
        )

    # ============================================================
    # 7. GENERAR
    # ============================================================

    doc.build(elements)

    return response