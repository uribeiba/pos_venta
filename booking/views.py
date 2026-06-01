# booking/views.py
from __future__ import annotations

import json
from decimal import Decimal
from datetime import datetime, date, timedelta, time
from typing import Any

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError, ObjectDoesNotExist
from django.core.paginator import Paginator
from django.db import transaction, IntegrityError
from django.db.models import Q, Count, Sum, Avg
from django.http import JsonResponse, Http404, HttpRequest, HttpResponse
from django.shortcuts import render, get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.decorators.http import require_GET, require_POST
from django.views.decorators.csrf import csrf_exempt

from coordinator.views import is_coordinator
from .models import (
    City, Route, Trip, Bus, Seat, Ticket, SeatHold,
    Terminal, UserProfile, CashRegister, DailyReport,
    Customer, BusLayout, Driver
)
from .forms import DriverForm
from booking.utils import validate_chilean_rut


# ============================================================================
# 1. HELPERS Y UTILIDADES
# ============================================================================

def _letters_for_cols(cols: int):
    """Retorna lista de letras mayúsculas para las columnas de un bus."""
    base = [chr(65 + i) for i in range(26)]
    return base[:max(1, cols)]


def _parse_json(request: HttpRequest) -> dict:
    """Lee el body JSON de la request de forma segura."""
    try:
        return json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        return {}


def calcular_tendencia(actual, anterior):
    """Calcula porcentaje de tendencia entre dos valores."""
    if anterior == 0:
        return 100 if actual > 0 else 0
    return ((actual - anterior) / anterior) * 100


def _get_role_choices():
    """Obtiene las opciones de roles desde el modelo UserProfile con fallback."""
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
    )


# ============================================================================
# 2. DECORADORES PERSONALIZADOS DE CONTROL DE ACCESO
# ============================================================================

def role_required(allowed_roles, login_url=None, message=None):
    """ Decorador avanzado para control de acceso basado en roles del sistema. """
    def decorator(view_func):
        def wrapper(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return redirect(login_url or 'admin:login')

            if request.user.is_superuser:
                return view_func(request, *args, **kwargs)

            if not request.user.is_staff:
                messages.error(request, message or "Acceso denegado. Se requieren permisos de staff.")
                return redirect('pos_caja')

            if not hasattr(request.user, 'profile'):
                try:
                    UserProfile.objects.get_or_create(
                        user=request.user,
                        defaults={'role': 'vendedor'}
                    )
                    request.user.refresh_from_db()
                except Exception:
                    messages.error(request, "Error en la configuración de su perfil administrativo.")
                    return redirect('pos_caja')

            user_role = getattr(request.user.profile, 'role', 'vendedor')
            if user_role in allowed_roles:
                return view_func(request, *args, **kwargs)

            role_display = {'admin': 'Admin', 'supervisor': 'Supervisor', 'vendedor': 'Vendedor', 'cajero': 'Cajero'}.get(user_role, user_role)
            allowed_display = [{'admin': 'Admin', 'supervisor': 'Supervisor', 'vendedor': 'Vendedor', 'cajero': 'Cajero'}.get(r, r) for r in allowed_roles]

            messages.error(
                request,
                message or f"Acceso denegado. Su rol ({role_display}) no tiene permisos. Requiere: {', '.join(allowed_display)}"
            )
            return redirect('pos_caja')
        return wrapper
    return decorator

def admin_required(view_func): return role_required(['admin'])(view_func)
def supervisor_required(view_func): return role_required(['admin', 'supervisor'])(view_func)
def vendedor_required(view_func): return role_required(['admin', 'supervisor', 'vendedor'])(view_func)
def cajero_required(view_func): return role_required(['admin', 'supervisor', 'vendedor', 'cajero'])(view_func)


# ============================================================================
# 3. VISTAS PRINCIPALES DEL PUNTO DE VENTA (POS)
# ============================================================================
@login_required
@vendedor_required
def pos_home(request):
    """ Lista de viajes filtrables para la terminal. Solo muestra salidas vigentes.
        Además, muestra el dashboard del vendedor con métricas, caja y ventas recientes.
    """
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

    # ========================================================================
    # 1. LÓGICA DE BÚSQUEDA DE VIAJES (EXISTENTE, SIN CAMBIOS)
    # ========================================================================
    if not did_search:
        trips = Trip.objects.none()
    else:
        trips = Trip.objects.select_related(
            "route__origin", "route__destination", "bus"
        ).filter(
            departure__date=the_date,
            departure__gte=now,
        )
        
        # Filtro por origen (permite ID o nombre)
        if origin_id:
            if origin_id.isdigit():
                trips = trips.filter(route__origin_id=origin_id)
            else:
                city = City.objects.filter(name__iexact=origin_id).first()
                if city:
                    trips = trips.filter(route__origin_id=city.id)
                else:
                    trips = trips.none()

        # Filtro por destino (permite ID o nombre)
        if dest_id:
            if dest_id.isdigit():
                trips = trips.filter(route__destination_id=dest_id)
            else:
                city = City.objects.filter(name__iexact=dest_id).first()
                if city:
                    trips = trips.filter(route__destination_id=city.id)
                else:
                    trips = trips.none()

        # Optimizaciones de Conteos Colectivos
        sold_map = dict(Ticket.objects.filter(trip__in=trips).values("trip").annotate(c=Count("id")).values_list("trip", "c"))
        hold_map = dict(SeatHold.objects.filter(trip__in=trips).values("trip").annotate(c=Count("id")).values_list("trip", "c"))
        
        bus_ids = list(trips.values_list("bus_id", flat=True))
        total_by_bus = dict(Seat.objects.filter(bus_id__in=bus_ids).values("bus_id").annotate(c=Count("id")).values_list("bus_id", "c"))

        for t in trips:
            t.sold = sold_map.get(t.id, 0)
            t.hold = hold_map.get(t.id, 0)
            t.total = total_by_bus.get(t.bus_id, 0)
            t.free = max(t.total - t.sold - t.hold, 0)

    # ========================================================================
    # 2. MÉTRICAS DEL VENDEDOR (VENTAS HOY, TENDENCIAS, CAJA)
    # ========================================================================
    fecha_hoy = timezone.now().date()
    
    # Ventas del día (solo del vendedor actual)
    ventas_hoy = Ticket.objects.filter(created_at__date=fecha_hoy, created_by=request.user)
    metrics = ventas_hoy.aggregate(total=Sum('price'), total_count=Count('id'))
    total_ventas = metrics['total'] or Decimal('0.00')
    total_boletos = metrics['total_count'] or 0
    rutas_vendidas = ventas_hoy.values('trip__route').distinct().count()
    promedio_venta = total_ventas / total_boletos if total_boletos > 0 else Decimal('0.00')
    
    # Últimas 10 ventas del vendedor
    ventas_recientes = ventas_hoy.select_related(
        'trip__route__origin', 'trip__route__destination', 'seat'
    ).order_by('-created_at')[:10]

    # Cálculo de tendencias (ayer vs hoy)
    ayer = fecha_hoy - timedelta(days=1)
    ventas_ayer = Ticket.objects.filter(created_at__date=ayer, created_by=request.user)
    metrics_ayer = ventas_ayer.aggregate(total=Sum('price'), total_count=Count('id'))
    total_ventas_ayer = metrics_ayer['total'] or Decimal('0.00')
    total_boletos_ayer = metrics_ayer['total_count'] or 0

    tendencia_ventas = calcular_tendencia(total_ventas, total_ventas_ayer)
    tendencia_boletos = calcular_tendencia(total_boletos, total_boletos_ayer)

    # Estado de la caja del vendedor hoy
    caja_actual = CashRegister.objects.filter(
        user=request.user, opening_date__date=fecha_hoy, status='open'
    ).first()
    caja_abierta = caja_actual is not None

    # ========================================================================
    # 3. CONSTRUCCIÓN DEL CONTEXTO
    # ========================================================================
    ctx = {
        # Datos del buscador de viajes (originales)
        "title": "POS — Panel de Ventas",
        "date": the_date.strftime("%Y-%m-%d"),
        "cities": City.objects.all().order_by("name"),
        "origin_id": origin_id,
        "dest_id": dest_id,
        "trips": trips,
        "did_search": did_search,
        # Datos del dashboard de ventas
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
    """ Vista operacional de venta del viaje. Carga el croquis dinámico unificado. """
    trip = get_object_or_404(Trip.objects.select_related("route__origin", "route__destination", "bus"), id=trip_id)
    if not trip.bus:
        return HttpResponse("Este viaje no tiene asignada una unidad de transporte (Bus).", status=400)

    # 1. Recuperamos las matrices usando el motor unificado de cálculo
    grid_lower, grid_upper, cols = _build_trip_grid(trip, request.user)

    context = {
        'trip': trip,
        'cols': cols,
        'grid_lower': json.dumps(grid_lower),
        'grid_upper': json.dumps(grid_upper),
    }
    return render(request, "booking/pos_trip.html", context)


@xframe_options_exempt
@login_required
def pos_trip_modal(request, trip_id: int):
    """ Modal interactivo / Iframe con el mapa de visualización de asientos. """
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
# 4. MOTOR UNIFICADO E INTEGRAL DE MAPAS DE ASIENTOS (CONCURRENCIA CONTROLADA)
# ============================================================================

def _build_trip_grid(trip: Trip, current_user=None):
    """
    Motor Único de Renderizado con una sola consulta SQL.
    Usa annotate para marcar estado (free, sold, my-hold, occupied).
    """
    from django.db.models import Case, When, Value, CharField, Q, OuterRef, Exists
    from django.db.models import F

    bus = trip.bus
    cols = int(bus.cols or 4)
    if cols <= 0:
        return [], [], 0

    letters = _letters_for_cols(cols)
    pos2col = {ch: i for i, ch in enumerate(letters)}

    # --- Subconsultas para determinar estado ---
    # 1. ¿Está vendido?
    sold_subq = Ticket.objects.filter(trip=trip, seat=OuterRef('pk')).values('pk')
    # 2. ¿Está retenido activo?
    hold_active_subq = SeatHold.objects.filter(
        trip=trip, seat=OuterRef('pk'),
        expires_at__gt=timezone.now(), active=True
    ).values('pk')
    # 3. ¿La retención es del usuario actual?
    hold_by_user_subq = SeatHold.objects.filter(
        trip=trip, seat=OuterRef('pk'), user=current_user,
        expires_at__gt=timezone.now(), active=True
    ).values('pk')

    # Anotamos el estado en cada asiento
    seats_annotated = Seat.objects.filter(bus=bus).annotate(
        status=Case(
            When(Exists(sold_subq), then=Value('sold')),
            When(Exists(hold_by_user_subq), then=Value('my-hold')),
            When(Exists(hold_active_subq), then=Value('occupied')),
            default=Value('free'),
            output_field=CharField()
        )
    ).values('id', 'deck', 'row', 'position', 'number', 'seat_service', 'status')

    # Indexar por deck y posición lineal
    index_by_deck = {1: {}, 2: {}}
    for s in seats_annotated:
        deck_num = int(s.get('deck') or 1)
        r_num = int(s.get('row') or 1)
        pos_val = s.get('position')
        c_num = pos2col[pos_val] if pos_val in pos2col else 0
        idx = (r_num - 1) * cols + c_num
        index_by_deck[deck_num][idx] = s

    def grid_for(deck: int, rows: int, flat_layout: list[str]):
        out, i = [], 0
        for _r in range(rows):
            row = []
            for _c in range(cols):
                typ = (flat_layout[i] if i < len(flat_layout) else "L") or "L"
                label, status, hold_user = "", "free", ""
                seat_id = None
                srv = "semi_cama"

                if typ == "L":
                    s = index_by_deck.get(deck, {}).get(i)
                    if s:
                        seat_id = s.get('id')
                        label = s.get('number') or ''
                        srv = (s.get('seat_service') or 'semi_cama').lower()
                        status = s.get('status', 'free')
                        # Si es 'occupied' y es del usuario, forzamos 'my-hold' (ya viene así desde annotate)
                        # pero añadimos info de usuario para tooltip
                        if status == 'occupied' and current_user:
                            hold = SeatHold.objects.filter(trip=trip, seat_id=seat_id, user=current_user).first()
                            if hold:
                                status = 'my-hold'
                        if status != 'free' and status != 'my-hold':
                            hold_user = "Otro vendedor"
                row.append({
                    "id": seat_id, "type": typ, "label": label, "number": label,
                    "status": status, "service": srv, "seat_service": srv, "hold_user": hold_user
                })
                i += 1
            out.append(row)
        return out

    lower = grid_for(1, int(bus.rows_lower or 0), bus.layout_lower or [])
    upper = grid_for(2, int(bus.rows_upper or 0), bus.layout_upper or []) if int(bus.floors or 1) == 2 else []

    return lower, upper, cols


# ============================================================================
# 5. ENDPOINTS TRANSACCIONALES (API DE CONTROL DE SEATHOLDS)
# ============================================================================

@require_POST
@login_required
def api_hold(request, trip_id):
    """Bloquea temporalmente un asiento. Devuelve siempre expires_at si ok=True."""
    trip = get_object_or_404(Trip, id=trip_id)
    seat_id_front = request.POST.get('seat_id')
    
    if not seat_id_front:
        return JsonResponse({'ok': False, 'error': 'Identificador de asiento ausente.'}, status=400)

    with transaction.atomic():
        # Limpieza de expirados
        SeatHold.objects.filter(trip=trip, expires_at__lt=timezone.now()).delete()

        seat_obj = trip.bus.seats.filter(Q(id=seat_id_front) | Q(number=seat_id_front)).first()
        if not seat_obj:
            return JsonResponse({'ok': False, 'error': 'El asiento no existe.'}, status=404)

        if Ticket.objects.filter(trip=trip, seat=seat_obj).exists():
            return JsonResponse({'ok': False, 'error': 'El asiento ya se vendió.'})

        existing_hold = SeatHold.objects.filter(trip=trip, seat=seat_obj).first()
        if existing_hold:
            if existing_hold.user == request.user:
                # Devuelve la expiración del hold existente
                return JsonResponse({
                    'ok': True,
                    'message': 'Ya posees la reserva activa.',
                    'expires_at': existing_hold.expires_at.isoformat()
                })
            return JsonResponse({'ok': False, 'error': 'Asiento retenido por otro vendedor.'})

        expires_at = timezone.now() + timedelta(minutes=7)
        SeatHold.objects.create(
            trip=trip, seat=seat_obj, user=request.user,
            expires_at=expires_at
        )
        return JsonResponse({'ok': True, 'expires_at': expires_at.isoformat()})

@require_POST
@login_required
def api_release(request, trip_id):
    """ Libera de inmediato el asiento retenido si la transacción se cancela. """
    trip = get_object_or_404(Trip, id=trip_id)
    seat_id_front = request.POST.get('seat_id')
    
    seat_obj = trip.bus.seats.filter(Q(id=seat_id_front) | Q(number=seat_id_front)).first()
    if not seat_obj:
        return JsonResponse({'ok': False, 'error': 'Asiento no encontrado.'}, status=404)

    SeatHold.objects.filter(trip=trip, seat=seat_obj, user=request.user).delete()
    return JsonResponse({'ok': True})


# ============================================================================
# 6. CIERRE DE CAJA, CHECKOUT Y EMISIÓN DE PASAJES DEFINITIVOS
# ============================================================================

@login_required
@vendedor_required
@transaction.atomic
def pos_checkout(request: HttpRequest, trip_id: int = None):
    """ Procesa el pago y emite los tickets físicos correspondientes blindado contra concurrencia. """
    if request.method != "POST":
        messages.error(request, "Acción u método de envío inválido.")
        return redirect("pos_home")

    payment_method = (request.POST.get("payment_method") or "").strip()
    if payment_method not in ("cash", "card"):
        messages.error(request, "Debe seleccionar un método válido: Efectivo o Tarjeta.")
        return redirect("pos_trip", trip_id=trip_id or request.POST.get("trip_id"))

    payment_method_label = "💳 Tarjeta" if payment_method == "card" else "💵 Efectivo"

    # Capturar Viaje con bloqueo transaccional estricto (Pessimistic Locking)
    tid = trip_id or request.POST.get("trip_id")
    trip = get_object_or_404(Trip.objects.select_for_update(), pk=tid)

    raw_seats = request.POST.get("seats", "").strip()
    try:
        chosen = json.loads(raw_seats) if raw_seats else []
    except Exception:
        chosen = []

    if not isinstance(chosen, list) or not chosen:
        messages.error(request, "No seleccionó ningún asiento para procesar la transacción.")
        return redirect("pos_trip", trip_id=trip.id)

    buyer = (request.POST.get("buyer") or request.POST.get("buyer_name") or "").strip()
    passenger = (request.POST.get("passenger") or request.POST.get("passenger_name") or "").strip()
    doc = (request.POST.get("document") or "").strip()
    buyer_display = buyer or passenger or "Cliente General"

    customer_id = request.POST.get("customer_id")
    customer = Customer.objects.filter(id=customer_id).first() if customer_id else None

    unit_price = Decimal(str(getattr(trip.route, "base_price", 0) or 0))
    created_tickets = []
    skipped = []

    # Bloqueo estricto de asientos seleccionados para evitar sobreventa (Race Conditions)
    numbers = [str(item.get("number")).strip() for item in chosen if item.get("number")]
    decks = [int(item.get("deck") or 1) for item in chosen if item.get("deck")]
    
    seats_db = {
        f"{s.deck}-{s.number}": s 
        for s in Seat.objects.select_for_update().filter(bus=trip.bus, number__in=numbers, deck__in=decks)
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

        # Validación cruzada de seguridad: si ya tiene pasaje activo en el viaje
        if Ticket.objects.filter(trip=trip, seat=seat).exists():
            skipped.append(number)
            continue

        try:
            # Creación a través de la lógica de negocio del modelo
            t = Ticket.create_for_sale(
                trip=trip, seat=seat, buyer_name=buyer or passenger or "Pasajero",
                national_id=doc, price=unit_price, created_by=request.user,
                payment_method=payment_method, customer=customer,
            )
            created_tickets.append(t)
        except Exception:
            skipped.append(number)

    if not created_tickets:
        messages.error(request, f"Error: Los asientos solicitados ({', '.join(skipped)}) ya fueron vendidos.")
        return redirect("pos_trip", trip_id=trip.id)

    # Limpieza inmediata de retenciones asociadas
    SeatHold.objects.filter(trip=trip, seat__in=[t.seat for t in created_tickets]).delete()

    # Cálculos monetarios finales
    total = unit_price * len(created_tickets)
    total_cash = sum([t.price for t in created_tickets if t.payment_method == "cash"])
    total_card = sum([t.price for t in created_tickets if t.payment_method == "card"])

    items = [{"ticket": t, "seat": {"number": t.seat.number, "deck": getattr(t.seat, "deck", 1)}} for t in created_tickets]

    ctx = {
        "trip": trip, "tickets": created_tickets, "ticket": created_tickets[0],
        "ticket_numbers": [t.number for t in created_tickets], "items": items,
        "seats": [{"number": t.seat.number, "deck": getattr(t.seat, "deck", 1)} for t in created_tickets],
        "price": unit_price, "total": total, "buyer": buyer_display, "skipped": skipped,
        "payment_method": payment_method, "payment_method_label": payment_method_label,
        "total_cash": total_cash, "total_card": total_card,
    }
    return render(request, "pos/receipt.html", ctx)


# ============================================================================
# 7. APIS EXPUESTAS Y CONSUMOS EXTERNOS (CLIENTE INTEGRADO)
# ============================================================================

@require_GET
def trip_seats(request: HttpRequest, trip_id: int):
    """ API optimizada de consulta de distribución física para aplicaciones móviles/web. """
    try:
        trip = Trip.objects.select_related("bus", "route__origin", "route__destination").get(pk=trip_id)
    except Trip.DoesNotExist:
        return JsonResponse({"error": "El viaje especificado no existe."}, status=404)

    bus = trip.bus
    letters = _letters_for_cols(getattr(bus, "cols", 4))
    pos2col = {ch: i for i, ch in enumerate(letters)}

    # Extraer estructuras indexadas eficientemente
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
    """ Catálogo rápido autocompletable de ciudades. """
    return JsonResponse(list(City.objects.order_by("name").values("id", "name")), safe=False, json_dumps_params={"ensure_ascii": False})


@require_GET
def api_search_trips(request):
    """ Motor externo de búsquedas comerciales de itinerarios y pasajes disponibles. """
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
    """ Renderizador plano estático de croquis de fábrica del Bus. """
    bus = get_object_or_404(Bus, pk=pk)
    return render(request, "booking/seatmap.html", {"bus": bus})


# ============================================================================
# 7. SISTEMA DE CAJA (TAQUILLAS Y TURNOS)
# ============================================================================

from django.db.models.functions import TruncDate, ExtractHour
from django.db.models import F

@login_required
@staff_member_required
@cajero_required
def pos_caja(request):
    """Dashboard principal de control de cajas y cuadratura de turnos diarios."""
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

    # Filtrar según jerarquía y permisos del operador de caja
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

    # Cálculo exacto de métricas comerciales
    metrics = ventas_hoy.aggregate(total=Sum('price'), total_count=Count('id'))
    total_ventas = metrics['total'] or Decimal('0.00')
    total_boletos = metrics['total_count'] or 0
    rutas_vendidas = ventas_hoy.values('trip__route').distinct().count()
    promedio_venta = total_ventas / total_boletos if total_boletos > 0 else Decimal('0.00')
    
    ventas_recientes = ventas_hoy.select_related(
        'trip__route__origin', 'trip__route__destination', 'seat'
    ).order_by('-created_at')[:10]

    # Cálculos comparativos de tendencias (Ayer vs Hoy)
    ayer = fecha - timedelta(days=1)
    if es_admin_o_supervisor:
        ventas_ayer = Ticket.objects.filter(created_at__date=ayer)
    else:
        ventas_ayer = Ticket.objects.filter(created_at__date=ayer, created_by=request.user)
        
    metrics_ayer = ventas_ayer.aggregate(total=Sum('price'), total_count=Count('id'))
    total_ventas_ayer = metrics_ayer['total'] or Decimal('0.00')
    total_boletos_ayer = metrics_ayer['total_count'] or 0

    tendencia_ventas = calcular_tendencia(total_ventas, total_ventas_ayer)
    tendencia_boletos = calcular_tendencia(total_boletos, total_boletos_ayer)

    user_profile = getattr(request.user, 'profile', None)
    user_role = user_profile.get_role_display() if user_profile else "Vendedor"

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
        'usuarios_con_caja_abierta': cajas_abiertas.count() if es_admin_o_supervisor else 0,
    }
    return render(request, 'booking/pos_caja.html', context)


@login_required
@staff_member_required
@require_POST
def abrir_caja(request):
    """ Inicializa un turno de caja para el usuario actual. """
    try:
        fecha_hoy = timezone.now().date()
        if CashRegister.objects.filter(user=request.user, opening_date__date=fecha_hoy, status='open').exists():
            return JsonResponse({'success': False, 'error': 'Ya posees un turno de caja abierto para el día de hoy.'})

        try:
            data = json.loads(request.body.decode('utf-8') or "{}")
            opening_balance = Decimal(str(data.get('opening_balance', 0)))
        except (json.JSONDecodeError, KeyError, ValueError):
            opening_balance = Decimal('0.00')

        if opening_balance < 0:
            return JsonResponse({'success': False, 'error': 'El saldo de apertura no puede ser un valor negativo.'})

        CashRegister.objects.create(user=request.user, opening_balance=opening_balance, status='open')
        return JsonResponse({
            'success': True, 
            'message': f'Caja habilitada correctamente. Saldo Inicial: ${opening_balance:.2f}'
        })
    except Exception as e:
        return JsonResponse({'success': False, 'error': f'Error interno: {str(e)}'})


@login_required
@staff_member_required
@require_POST
@transaction.atomic
def cerrar_caja(request):
    """ Realiza la cuadratura final del turno de caja y actualiza los reportes generales del día. """
    try:
        fecha_hoy = timezone.now().date()
        caja = CashRegister.objects.select_for_update().get(user=request.user, opening_date__date=fecha_hoy, status='open')
        
        # Consolidar transacciones del usuario
        ventas_hoy = Ticket.objects.filter(created_at__date=fecha_hoy, created_by=request.user)
        total_ventas = ventas_hoy.aggregate(total=Sum('price'))['total'] or Decimal('0.00')
        total_boletos = ventas_hoy.count()

        # Actualizar estado de la caja individual
        caja.closing_date = timezone.now()
        caja.total_sales = total_ventas
        caja.total_tickets = total_boletos
        caja.closing_balance = total_ventas
        caja.status = 'closed'
        caja.save()

        # Consolidación atómica en el reporte diario global (Evita race conditions)
        reporte, _ = DailyReport.objects.get_or_create(date=fecha_hoy)
        DailyReport.objects.filter(id=reporte.id).update(
            total_tickets=F('total_tickets') + total_boletos,
            total_revenue=F('total_revenue') + total_ventas,
            total_cash_registers=CashRegister.objects.filter(opening_date__date=fecha_hoy, status='closed').count()
        )

        return JsonResponse({
            'success': True, 
            'message': f'Caja clausurada con éxito. Recaudación Total: ${total_ventas:.2f}, Tickets Emitidos: {total_boletos}'
        })
    except CashRegister.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'No se encontró ningún registro de caja abierto para tu usuario.'})
    except Exception as e:
        return JsonResponse({'success': False, 'error': f'Error en el proceso de cierre: {str(e)}'})


# ============================================================================
# 8. SISTEMA DE REPORTES COMERCIALES (PORTABLE SQL)
# ============================================================================

@login_required
@staff_member_required
@supervisor_required
def pos_reportes(request):
    """ Panel analítico y generador de estadísticas de rendimiento financiero. """
    fecha_inicio_str = request.GET.get('fecha_inicio', '')
    fecha_fin_str = request.GET.get('fecha_fin', '')
    usuario_id = request.GET.get('usuario', '')

    if not fecha_inicio_str or not fecha_fin_str:
        fecha_fin = timezone.now().date()
        fecha_inicio = fecha_fin - timedelta(days=7)
    else:
        try:
            fecha_inicio = datetime.strptime(fecha_inicio_str, '%Y-%m-%d').date()
            fecha_fin = datetime.strptime(fecha_fin_str, '%Y-%m-%d').date()
        except ValueError:
            fecha_fin = timezone.now().date()
            fecha_inicio = fecha_fin - timedelta(days=7)

    es_admin_o_supervisor = (
        request.user.is_superuser or
        (hasattr(request.user, 'profile') and request.user.profile.role in ['admin', 'supervisor'])
    )

    if es_admin_o_supervisor:
        ventas = Ticket.objects.filter(created_at__date__range=[fecha_inicio, fecha_fin]).select_related(
            'trip__route__origin', 'trip__route__destination', 'created_by')
        if usuario_id and usuario_id != 'todos':
            ventas = ventas.filter(created_by_id=usuario_id)
    else:
        ventas = Ticket.objects.filter(
            created_at__date__range=[fecha_inicio, fecha_fin], created_by=request.user
        ).select_related('trip__route__origin', 'trip__route__destination')

    stats = ventas.aggregate(total_ventas=Sum('price'), total_boletos=Count('id'), ticket_promedio=Avg('price'))
    total_ventas = stats['total_ventas'] or Decimal('0.00')
    total_boletos = stats['total_boletos'] or 0
    ticket_promedio_general = stats['ticket_promedio'] or Decimal('0.00')
    
    dias_rango = (fecha_fin - fecha_inicio).days + 1
    promedio_diario = total_ventas / dias_rango if dias_rango > 0 else Decimal('0.00')

    # Agrupaciones usando ORM nativo y portable (Evita el anti-patrón de Raw SQL con .extra())
    ventas_por_dia = ventas.annotate(fecha=TruncDate('created_at')).values('fecha').annotate(
        total=Sum('price'), cantidad=Count('id')).order_by('fecha')
        
    ventas_por_dia_procesadas = [{
        'fecha': v['fecha'],
        'total': v['total'],
        'cantidad': v['cantidad'],
        'promedio': (v['total'] / v['cantidad']) if v['cantidad'] > 0 else Decimal('0.00')
    } for v in ventas_por_dia]

    rutas_populares = ventas.values(
        'trip__route__origin__name', 'trip__route__destination__name'
    ).annotate(total=Sum('price'), cantidad=Count('id'), promedio=Avg('price')).order_by('-total')[:10]

    ventas_por_hora = ventas.annotate(hora=ExtractHour('created_at')).values('hora').annotate(
        total=Sum('price'), cantidad=Count('id'), promedio=Avg('price')).order_by('hora')

    ventas_por_vendedor = []
    if es_admin_o_supervisor:
        ventas_por_vendedor = Ticket.objects.filter(created_at__date__range=[fecha_inicio, fecha_fin]).values(
            'created_by__id', 'created_by__username', 'created_by__first_name', 'created_by__last_name'
        ).annotate(total=Sum('price'), cantidad=Count('id'), promedio=Avg('price')).order_by('-total')

    user_profile = getattr(request.user, 'profile', None)
    user_role = user_profile.get_role_display() if user_profile else "Vendedor"

    usuarios_lista = []
    if es_admin_o_supervisor:
        usuarios_lista = User.objects.filter(
            is_staff=True, tickets_sold__created_at__date__range=[fecha_inicio, fecha_fin]
        ).distinct().order_by('username')

    usuario_filtrado_obj = User.objects.filter(id=usuario_id).first() if usuario_id and usuario_id != 'todos' else None

    context = {
        'title': 'POS — Reportes Detallados',
        'fecha_inicio': fecha_inicio.strftime('%Y-%m-%d'),
        'fecha_fin': fecha_fin.strftime('%Y-%m-%d'),
        'total_ventas': total_ventas,
        'total_boletos': total_boletos,
        'ticket_promedio_general': ticket_promedio_general,
        'promedio_diario': promedio_diario,
        'ventas_por_dia': ventas_por_dia_procesadas,
        'rutas_populares': rutas_populares,
        'ventas_por_hora': list(ventas_por_hora),
        'dias_rango': dias_rango,
        'es_admin_o_supervisor': es_admin_o_supervisor,
        'ventas_por_vendedor': ventas_por_vendedor,
        'usuarios_lista': usuarios_lista,
        'usuario_seleccionado': usuario_id,
        'user_profile': user_profile,
        'user_role': user_role,
        'filtro_usuario_aplicado': usuario_id if usuario_id else None,
        'usuario_filtrado': usuario_filtrado_obj.get_full_name() if usuario_filtrado_obj else None,
    }
    return render(request, 'booking/pos_reportes.html', context)


# ============================================================================
# 9. GESTIÓN ADMINISTRATIVA DE USUARIOS
# ============================================================================

@login_required
@staff_member_required
@role_required(['admin', 'supervisor'])
def gestion_usuarios(request):
    """ Panel de control de credenciales, roles y comisiones de la empresa. """
    qs = User.objects.select_related('profile').order_by('username')
    context = {
        "title": "Gestión de Usuarios",
        "usuarios": qs,
        "total_count": qs.count(),
        "admins_count": qs.filter(profile__role="admin").count(),
        "supervisores_count": qs.filter(profile__role="supervisor").count(),
        "vendedores_count": qs.filter(profile__role="vendedor").count(),
        "activos_count": qs.filter(profile__is_active=True).count(),
        "inactivos_count": qs.filter(profile__is_active=False).count(),
        "roles": _get_role_choices(),
        "terminales": Terminal.objects.all(),
    }
    return render(request, "booking/gestion_usuarios.html", context)


@login_required
@staff_member_required
@role_required(['admin', 'supervisor'])
def editar_usuario(request, user_id):
    """ Modifica parámetros de perfil, topes de descuento y asignaciones de terminal. """
    usuario = get_object_or_404(User, id=user_id)
    if request.method == 'POST':
        try:
            with transaction.atomic():
                usuario.first_name = request.POST.get('first_name', '').strip()
                usuario.last_name = request.POST.get('last_name', '').strip()
                usuario.email = request.POST.get('email', '').strip()
                usuario.save()

                profile, _ = UserProfile.objects.get_or_create(user=usuario)
                profile.role = request.POST.get('role', 'vendedor')
                profile.terminal_id = request.POST.get('terminal') or None
                
                try:
                    profile.commission_rate = Decimal(request.POST.get('commission_rate', '0') or '0')
                    profile.max_discount = Decimal(request.POST.get('max_discount', '0') or '0')
                except (ValueError, TypeError):
                    profile.commission_rate = Decimal('0.00')
                    profile.max_discount = Decimal('0.00')
                    
                profile.is_active = 'is_active' in request.POST
                profile.save()
                
            messages.success(request, f"El usuario {usuario.username} ha sido actualizado con éxito.")
            return redirect('gestion_usuarios')
        except Exception as e:
            messages.error(request, f"Error al procesar la actualización: {str(e)}")

    context = {
        'title': f'Editar Usuario - {usuario.username}',
        'usuario': usuario,
        'terminales': Terminal.objects.all(),
        'roles': _get_role_choices(),
    }
    return render(request, 'booking/editar_usuario.html', context)


@login_required
@staff_member_required
@role_required(['admin', 'supervisor'])
@transaction.atomic
def crear_usuario(request):
    """ Registra un nuevo operador en el sistema configurando sus límites transaccionales. """
    if request.method == "POST":
        username = request.POST.get("username", "").strip().lower()
        password = request.POST.get("password", "")
        confirm = request.POST.get("confirm_password", "")

        if not username:
            messages.error(request, "El nombre de usuario es mandatorio.")
        elif User.objects.filter(username__iexact=username).exists():
            messages.error(request, "El nombre de usuario ingresado ya se encuentra en uso.")
        elif password != confirm:
            messages.error(request, "Las contraseñas de verificación no coinciden.")
        else:
            try:
                user = User.objects.create_user(
                    username=username,
                    email=request.POST.get("email", "").strip(),
                    password=password,
                    first_name=request.POST.get("first_name", "").strip(),
                    last_name=request.POST.get("last_name", "").strip(),
                )
                
                profile, _ = UserProfile.objects.get_or_create(user=user)
                profile.role = request.POST.get("role", "vendedor")
                profile.terminal_id = request.POST.get("terminal") or None
                
                try:
                    profile.commission_rate = Decimal(request.POST.get("commission_rate", '0') or '0')
                    profile.max_discount = Decimal(request.POST.get("max_discount", '0') or '0')
                except (ValueError, TypeError):
                    profile.commission_rate = Decimal('0.00')
                    profile.max_discount = Decimal('0.00')
                    
                profile.is_active = True
                profile.save()
                
                messages.success(request, f"Usuario operativo {username} creado de forma exitosa.")
                return redirect("gestion_usuarios")
            except Exception as e:
                messages.error(request, f"Fallo al registrar usuario: {str(e)}")

    context = {
        "title": "Crear Usuario",
        "roles": _get_role_choices(),
        "terminales": Terminal.objects.all(),
    }
    return render(request, "booking/crear_usuario.html", context)


# ============================================================================
# 10. CLIENTES (CUSTOMERS Y RUT CHILENO/IDENTIFICADORES)
# ============================================================================

@login_required
def search_customer(request):
    """ API de autocompletado para localizar clientes mediante RUT o Nombre Completo. """
    try:
        query = (request.GET.get("q") or "").strip()
        if not query or len(query) < 2:
            return JsonResponse({"customers": []})

        # Utilizar la función del modelo si existe para limpiar formateos extraños
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
        return JsonResponse({"customers": [], "error": str(e)}, status=500)


@csrf_exempt
@login_required
@require_POST
def create_customer(request):
    """ Registra de forma exprés a un cliente desde la pantalla de pasajes. """
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
        return JsonResponse({'success': False, 'error': f'Error al instanciar cliente: {str(e)}'}, status=500)


# ============================================================================
# 11. DISEÑO FÍSICO Y COMPRA DIRECTA (API PURCHASE CORREGIDA)
# ============================================================================

@csrf_exempt
def save_layout_template(request):
    """ Guarda la plantilla de distribución de asientos de un Bus. """
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
        return JsonResponse({"error": str(e)}, status=500)


@require_POST
@login_required
@transaction.atomic
def api_purchase(request, trip_id):
    """
    🛠️ CORREGIDO COMPLETAMENTE: Procesa la compra formal vinculando los asientos 
    reales del Bus del viaje, blindando la transacción contra sobreventas simultáneas.
    """
    trip = get_object_or_404(Trip.objects.select_for_update(), id=trip_id)
    
    try:
        seats_data = request.POST.get('seats')
        payment_method = request.POST.get('payment_method', 'cash')
        customer_id = request.POST.get('customer_id')
        buyer_name = request.POST.get('buyer_name', 'Cliente General')

        if not seats_data:
            return JsonResponse({'ok': False, 'error': 'No se han seleccionado asientos válidos.'}, status=400)

        seats_list = json.loads(seats_data)
        customer = Customer.objects.filter(id=customer_id).first() if customer_id else None
        base_price = Decimal(str(getattr(trip.route, "base_price", 0) or 0))

        tickets_creados = []
        
        # Mapear asientos físicos del bus asignado para evitar inyecciones fraudulentas
        numbers = [str(s.get('number')).strip() for s in seats_list if s.get('number')]
        decks = [int(s.get('deck', 1)) for s in seats_list if s.get('deck')]
        
        seats_db = {
            f"{seat.deck}-{seat.number}": seat 
            for seat in Seat.objects.select_for_update().filter(bus=trip.bus, number__in=numbers, deck__in=decks)
        }

        for s in seats_list:
            seat_num = str(s.get('number')).strip()
            deck_num = int(s.get('deck', 1))
            key = f"{deck_num}-{seat_num}"
            
            seat_obj = seats_db.get(key)
            if not seat_obj:
                return JsonResponse({'ok': False, 'error': f'El asiento {seat_num} en el piso {deck_num} no existe en este Bus.'}, status=400)

            # Validar disponibilidad real en base a Tickets activos
            already_sold = Ticket.objects.filter(trip=trip, seat=seat_obj).exists()
            if already_sold:
                return JsonResponse({'ok': False, 'error': f'El asiento {seat_num} acaba de ser vendido por otra boletería.'}, status=400)

            # Creación consistente con el modelo transaccional
            ticket = Ticket.create_for_sale(
                trip=trip,
                seat=seat_obj,
                buyer_name=customer.full_name if customer else buyer_name,
                national_id=customer.national_id if customer else "",
                price=base_price,
                created_by=request.user,
                payment_method=payment_method,
                customer=customer,
            )
            tickets_creados.append(ticket)

        # Disolver las retenciones temporales asociadas para liberar espacio
        SeatHold.objects.filter(trip=trip, seat__in=[t.seat for t in tickets_creados]).delete()

        return JsonResponse({
            'ok': True, 
            'message': f'¡Venta procesada! Se emitieron {len(tickets_creados)} pasajes exitosamente.',
            'redirect_url': reverse('pos_home')
        })

    except Exception as e:
        return JsonResponse({'ok': False, 'error': f'Error en el procesador de pagos: {str(e)}'}, status=500)


@require_GET
def api_cities_search(request):
    """Autocompletado de ciudades (para el buscador del POS)."""
    query = request.GET.get('q', '').strip()
    if len(query) < 2:
        return JsonResponse([], safe=False)
    cities = City.objects.filter(name__icontains=query)[:10]
    results = [{'id': c.id, 'name': c.name} for c in cities]
    return JsonResponse(results, safe=False)