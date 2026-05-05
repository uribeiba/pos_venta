# booking/views.py
from __future__ import annotations

import json
from decimal import Decimal
from typing import Any
from datetime import datetime, date as _date, date, timedelta, time
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.db import transaction, IntegrityError
from django.db.models import Q, Count, Sum, Avg
from django.http import JsonResponse, Http404, HttpRequest, HttpResponse
from django.shortcuts import render, get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.decorators.http import require_GET, require_POST
from django.views.decorators.csrf import csrf_exempt
from django.core.exceptions import ValidationError

# ✅ Solo modelos (sin User porque ya lo importamos de auth)
from .models import (
    City, Route, Trip, Bus, Seat, Ticket, SeatHold,
    Terminal, UserProfile, CashRegister, DailyReport,
    Customer, BusLayout,
)

from django.core.exceptions import ObjectDoesNotExist
import json

from django.db.models import Count




# =========================
# Helpers
# =========================
def _letters_for_cols(cols: int):
    # A, B, C, D, E, F...
    base = [chr(65 + i) for i in range(26)]
    return base[: max(1, cols)]


def _trip_payload(trip: Trip, user) -> dict[str, Any]:
    """
    Estructura que consumen los templates del POS.
    {
      "trip": {id, route, departure, price},
      "cols": N,
      "lower": [celdas...],
      "upper": [celdas...]  (si hay segundo piso)
    }

    Celda asiento:
      {"type":"L","number":"12","seat_id":1,"status":"free|hold|my_hold|occupied"}
    Celda no-asiento:
      {"type":"P"}  (o X/E/D/B)
    """
    # ✅ Limpieza de holds vencidos antes de computar el estado
    try:
        # import local para no tocar imports globales
        from .models import SeatHold
        SeatHold.cleanup()
    except Exception:
        # si por alguna razón el import falla, no rompemos la vista
        pass

    bus = trip.bus
    cols = int(getattr(bus, "cols", 0) or 0)

    def grid_for(deck: int, rows: int, flat_layout: list[str]):
        # index Seat -> idx lineal por piso
        letters = _letters_for_cols(cols)
        pos2col = {ch: i for i, ch in enumerate(letters)}
        seat_index = {}
        for s in Seat.objects.filter(bus=bus, deck=deck):
            c = pos2col.get(s.position, 0)
            idx = (int(s.row or 1) - 1) * cols + c
            seat_index[idx] = s

        # estados
        ticketed_ids = set(Ticket.objects.filter(trip=trip).values_list("seat_id", flat=True))

        # tras cleanup(), con active=True basta (los vencidos ya quedaron inactivos)
        holds = list(SeatHold.objects.filter(trip=trip, active=True))
        hold_by_seat = {h.seat_id: h for h in holds}

        out, i = [], 0
        for _r in range(rows):
            for _c in range(cols):
                t = (flat_layout[i] if i < len(flat_layout) else "L") or "L"
                if t == "L":
                    s = seat_index.get(i)
                    if not s:
                        out.append({"type": "L"})
                    else:
                        status = "free"
                        if s.id in ticketed_ids or getattr(s, "is_occupied", False):
                            status = "occupied"
                        elif s.id in hold_by_seat:
                            status = "hold"
                            if hold_by_seat[s.id].user_id == getattr(user, "id", 0):
                                status = "my_hold"
                        out.append({
                            "type": "L",
                            "number": s.number,
                            "seat_id": s.id,
                            "status": status,
                        })
                else:
                    out.append({"type": t})
                i += 1
        return out

    lower = grid_for(1, int(bus.rows_lower or 0), bus.layout_lower or [])
    upper = []
    if int(bus.floors or 1) == 2 and int(bus.rows_upper or 0) > 0:
        upper = grid_for(2, int(bus.rows_upper or 0), bus.layout_upper or [])

    price = getattr(getattr(trip, "route", None), "base_price", None)
    return {
        "trip": {
            "id": trip.id,
            "route": str(trip.route),
            "departure": trip.departure.strftime("%Y-%m-%d %H:%M"),
            "price": str(price) if price is not None else "0",
        },
        "cols": cols,
        "lower": lower,
        "upper": upper,
    }



def _parse_json(request: HttpRequest) -> dict:
    """Lee body JSON de manera segura."""
    try:
        return json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        return {}


def _build_trip_grid(trip: Trip):
    """
    Devuelve:
      - grid_lower, grid_upper: matrices con celdas dict(type, label, status, hold_user)
      - cols: número de columnas del bus
    """
    bus = trip.bus
    cols = int(bus.cols or 0)
    if cols <= 0:
        return [], [], 0

    letters = _letters_for_cols(cols)
    pos2col = {ch: i for i, ch in enumerate(letters)}

    # Index Seat -> índice lineal por piso (0-index)
    index_by_deck = {1: {}, 2: {}}
    for s in Seat.objects.filter(bus=bus).only("id", "deck", "row", "position", "number", "is_occupied"):
        c = pos2col.get(s.position, 0)
        idx = (int(s.row or 1) - 1) * cols + c
        index_by_deck.setdefault(int(s.deck or 1), {})[idx] = s

    # Estados actuales
    ticketed_ids = set(
        Ticket.objects.filter(trip=trip).values_list("seat_id", flat=True)
    )
    hold_by_seat = {
        h.seat_id: h
        for h in SeatHold.objects.filter(trip=trip, active=True).select_related("user")
    }

    def grid_for(deck: int, rows: int, flat_layout: list[str], labels: list[str]):
        out, i = [], 0
        for _r in range(rows):
            row = []
            for _c in range(cols):
                typ = (flat_layout[i] if i < len(flat_layout) else "L") or "L"
                label, status, hold_user = "", "free", ""
                if typ == "L":
                    s = index_by_deck.get(deck, {}).get(i)
                    if s:
                        label = s.number
                        if s.id in ticketed_ids or s.is_occupied:
                            status = "occupied"
                        elif s.id in hold_by_seat:
                            status = "hold"
                            hold_user = getattr(hold_by_seat[s.id].user, "username", "")
                row.append({"type": typ, "label": label, "status": status, "hold_user": hold_user})
                i += 1
            out.append(row)
        return out

    lower = grid_for(1, int(bus.rows_lower or 0), bus.layout_lower or [], bus.numbers_lower or [])
    upper = []
    if int(bus.floors or 1) == 2 and int(bus.rows_upper or 0) > 0:
        upper = grid_for(2, int(bus.rows_upper or 0), bus.layout_upper or [], bus.numbers_upper or [])

    return lower, upper, cols


def calcular_tendencia(actual, anterior):
    """Calcular porcentaje de tendencia"""
    if anterior == 0:
        return 100 if actual > 0 else 0
    return ((actual - anterior) / anterior) * 100


# =========================
# Decoradores de permisos
# =========================
def role_required(allowed_roles, login_url=None, message=None):
    """
    Decorador para control de acceso basado en roles.
    
    Args:
        allowed_roles: Lista de roles permitidos ['admin', 'supervisor', 'vendedor', 'cajero']
        login_url: URL a redirigir si no está autenticado (opcional)
        message: Mensaje personalizado de error (opcional)
    """
    def decorator(view_func):
        def wrapper(request, *args, **kwargs):
            # ✅ MANTENIDO: Verificación de autenticación
            if not request.user.is_authenticated:
                redirect_url = login_url or 'admin:login'
                return redirect(redirect_url)
            
            # ✅ MANTENIDO: Superusuarios tienen acceso total
            if request.user.is_superuser:
                return view_func(request, *args, **kwargs)
            
            # ✅ MEJORADO: Verificación de staff para usuarios no-superuser
            if not request.user.is_staff:
                messages.error(
                    request, 
                    message or "Acceso denegado. Se requieren permisos de staff."
                )
                return redirect('pos_caja')
            
            # ✅ MEJORADO: Verificación de perfil con manejo de errores robusto
            if not hasattr(request.user, 'profile'):
                # Si no tiene perfil pero es staff, crear uno por defecto
                try:
                    from .models import UserProfile
                    UserProfile.objects.get_or_create(
                        user=request.user,
                        defaults={'role': 'vendedor'}
                    )
                    # Recargar el usuario para obtener el perfil
                    request.user.refresh_from_db()
                except Exception as e:
                    # Si hay error al crear perfil, redirigir con mensaje
                    messages.error(
                        request, 
                        "Error en la configuración de perfil. Contacte al administrador."
                    )
                    return redirect('pos_caja')
            
            # ✅ MANTENIDO: Verificar rol del usuario
            user_role = getattr(request.user.profile, 'role', 'vendedor')
            
            if user_role in allowed_roles:
                return view_func(request, *args, **kwargs)
            
            # ✅ MEJORADO: Mensaje de error más descriptivo
            role_display = {
                'admin': 'Administrador',
                'supervisor': 'Supervisor', 
                'vendedor': 'Vendedor',
                'cajero': 'Cajero'
            }.get(user_role, user_role)
            
            allowed_roles_display = [
                {
                    'admin': 'Administrador',
                    'supervisor': 'Supervisor',
                    'vendedor': 'Vendedor', 
                    'cajero': 'Cajero'
                }.get(role, role) for role in allowed_roles
            ]
            
            messages.error(
                request,
                message or (
                    f"Acceso denegado. Su rol actual ({role_display}) no tiene permisos "
                    f"para esta sección. Se requieren: {', '.join(allowed_roles_display)}"
                )
            )
            return redirect('pos_caja')
        
        return wrapper
    return decorator

# ✅ NUEVO: Decoradores predefinidos para roles específicos (opcional)
def admin_required(view_func):
    """Decorador para acceso exclusivo de administradores"""
    return role_required(['admin'])(view_func)

def supervisor_required(view_func):
    """Decorador para acceso de supervisores y administradores"""
    return role_required(['admin', 'supervisor'])(view_func)

def vendedor_required(view_func):
    """Decorador para acceso de vendedores, supervisores y administradores"""
    return role_required(['admin', 'supervisor', 'vendedor'])(view_func)

def cajero_required(view_func):
    """Decorador para acceso de cajeros, vendedores, supervisores y administradores"""
    return role_required(['admin', 'supervisor', 'vendedor', 'cajero'])(view_func)




@staff_member_required
def pos_home(request):
    """
    Lista de viajes filtrables por fecha/origen/destino + CTA 'Mapa' (modal).
    ✅ NUEVO: por defecto NO lista viajes hasta que el usuario busque (GET con filtros).
    ✅ NUEVO: oculta viajes "hechos" (departure < ahora).
    """
    now = timezone.localtime()

    # --- filtros (tal como lo tenías) ---
    q_date = (request.GET.get("date") or "").strip()
    origin_id = (request.GET.get("origin_id") or "").strip()
    dest_id = (request.GET.get("dest_id") or "").strip()

    # ✅ NUEVO: detectar si el usuario realmente hizo una búsqueda
    # (si vienes con ?date=... o ?origin_id=... o ?dest_id=... entonces busca)
    did_search = bool(q_date or origin_id or dest_id)

    # ✅ Mantener fecha para el input (si no hay, mostrar hoy)
    if q_date:
        try:
            the_date = datetime.strptime(q_date, "%Y-%m-%d").date()
        except Exception:
            try:
                the_date = datetime.strptime(q_date, "%d/%m/%Y").date()
            except Exception:
                the_date = now.date()
    else:
        the_date = now.date()

    # ==========================================
    # ✅ NUEVO: por defecto NO mostrar viajes
    # ==========================================
    if not did_search:
        trips = Trip.objects.none()
        bus_ids = []
        sold_map = {}
        hold_map = {}
        total_by_bus = {}
    else:
        # --- consulta base (igual que antes, pero con 2 mejoras) ---
        trips = Trip.objects.select_related(
            "route", "bus", "route__origin", "route__destination"
        ).filter(
            departure__date=the_date,
            departure__gte=now,  # ✅ NUEVO: no mostrar viajes ya salidos ("hechos")
        )

        if origin_id:
            trips = trips.filter(route__origin_id=origin_id)
        if dest_id:
            trips = trips.filter(route__destination_id=dest_id)

        # --- stats por viaje (vendidos / en hold / total asientos) ---
        sold_map = dict(
            Ticket.objects.filter(trip__in=trips)
            .values("trip")
            .annotate(c=Count("id"))
            .values_list("trip", "c")
        )
        hold_map = dict(
            SeatHold.objects.filter(trip__in=trips, active=True)
            .values("trip")
            .annotate(c=Count("id"))
            .values_list("trip", "c")
        )

        bus_ids = list(trips.values_list("bus_id", flat=True))
        total_by_bus = dict(
            Seat.objects.filter(bus_id__in=bus_ids)
            .values("bus_id")
            .annotate(c=Count("id"))
            .values_list("bus_id", "c")
        )

        # anotar dinámicos para mostrar (igual que antes)
        for t in trips:
            t.sold = int(sold_map.get(t.id, 0) or 0)
            t.hold = int(hold_map.get(t.id, 0) or 0)
            t.total = int(total_by_bus.get(t.bus_id, 0) or 0)
            t.free = max(t.total - t.sold - t.hold, 0)

    ctx = {
        "title": "POS — Salidas",
        "date": the_date.strftime("%Y-%m-%d"),  # mantiene el input
        "cities": City.objects.all().order_by("name"),
        "origin_id": origin_id,
        "dest_id": dest_id,
        "trips": trips,

        # ✅ NUEVO (opcional): útil por si quieres mostrar un mensaje tipo “Use filtros para buscar”
        "did_search": did_search,
    }
    return render(request, "booking/pos_home.html", ctx)

# ---------------- POS: Mapa (modal) ----------------
@xframe_options_exempt
def pos_trip_modal(request, trip_id: int):
    """
    Contenido del modal (iframe) con el mapa de asientos del viaje.
    Reutiliza 'booking/seatmap.html'.
    """
    trip = get_object_or_404(
        Trip.objects.select_related("route", "bus", "route__origin", "route__destination"),
        pk=trip_id,
    )
    grid_lower, grid_upper, cols = _build_trip_grid(trip)

    ctx = {
        "title": f"Mapa de asientos — {trip.route.origin.name} → {trip.route.destination.name}",
        "trip": trip,
        "bus": trip.bus,               # <- importante para que no salga "Bus: —"
        "cols": cols,
        "grid_lower": grid_lower,
        "grid_upper": grid_upper,
    }
    return render(request, "booking/seatmap.html", ctx)


# ---------------- POS: Selección de asientos ----------------
@staff_member_required
def pos_trip(request, trip_id: int):
    """
    Vista completa para selección de asientos en el POS
    """
    trip = get_object_or_404(
        Trip.objects.select_related("route", "bus", "route__origin", "route__destination"),
        pk=trip_id,
    )
    grid_lower, grid_upper, cols = _build_trip_grid(trip)

    ctx = {
        "title": f"POS — Selección de asientos — {trip.route}",
        "trip": trip,
        "cols": cols,
        "grid_lower": grid_lower,
        "grid_upper": grid_upper,
    }
    # Usar el template de selección interactiva
    return render(request, "booking/pos_trip.html", ctx)


# =========================
# POS (APIs)
# =========================

@login_required
@require_POST
@transaction.atomic
def api_hold(request, trip_id: int):
    """
    Crea/renueva un hold de asiento para este usuario.
    Parámetros:
      - seat_id (POST)           -> recomendado
      - o deck + number (POST)   -> alternativo
    Respuesta JSON:
      {"ok": True, "seat_id": <id>, "status": "my_hold"}  // 200
      {"ok": False, "error": "..."}                       // 4xx
    """
    trip = get_object_or_404(Trip.objects.select_for_update(), pk=trip_id)

    seat_id = request.POST.get("seat_id")
    deck = request.POST.get("deck")
    number = request.POST.get("number")

    if seat_id:
        seat = get_object_or_404(Seat.objects.select_for_update(), pk=seat_id, bus=trip.bus)
    elif deck and number:
        try:
            seat = Seat.objects.select_for_update().get(bus=trip.bus, deck=int(deck), number=str(number).strip())
        except Seat.DoesNotExist:
            return JsonResponse({"ok": False, "error": "Asiento no encontrado."}, status=404)
    else:
        return JsonResponse({"ok": False, "error": "seat_id o (deck, number) requerido."}, status=400)

    # Si ya tiene ticket, bloquear
    if Ticket.objects.filter(trip=trip, seat=seat).exists() or getattr(seat, "is_occupied", False):
        return JsonResponse({"ok": False, "error": "Asiento ocupado."}, status=409)

    try:
        SeatHold.hold(trip=trip, seat=seat, user=request.user, minutes=10)
        return JsonResponse({"ok": True, "seat_id": seat.id, "status": "my_hold"})
    except ValueError as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=409)
    except Exception:
        return JsonResponse({"ok": False, "error": "No se pudo bloquear el asiento."}, status=500)


@login_required
@require_POST
@transaction.atomic
def api_release(request, trip_id: int):
    """
    Libera el hold del usuario sobre un asiento.
    Parámetros:
      - seat_id (POST)           -> recomendado
      - o deck + number (POST)   -> alternativo
    Respuesta JSON:
      {"ok": True, "seat_id": <id>, "status": "free"}     // 200
      {"ok": False, "error": "..."}                       // 4xx
    """
    trip = get_object_or_404(Trip.objects.select_for_update(), pk=trip_id)

    seat_id = request.POST.get("seat_id")
    deck = request.POST.get("deck")
    number = request.POST.get("number")

    if seat_id:
        seat = get_object_or_404(Seat.objects.select_for_update(), pk=seat_id, bus=trip.bus)
    elif deck and number:
        try:
            seat = Seat.objects.select_for_update().get(bus=trip.bus, deck=int(deck), number=str(number).strip())
        except Seat.DoesNotExist:
            return JsonResponse({"ok": False, "error": "Asiento no encontrado."}, status=404)
    else:
        return JsonResponse({"ok": False, "error": "seat_id o (deck, number) requerido."}, status=400)

    # Libera solo el hold del usuario actual
    SeatHold.release(trip=trip, seat=seat, user=request.user)
    return JsonResponse({"ok": True, "seat_id": seat.id, "status": "free"})




@require_POST
@staff_member_required
def api_purchase(request: HttpRequest, trip_id: int):
    """
    Body JSON:
    {
      "seat_ids": [1,2,3],
      "buyer_name": "Juan Perez",
      "national_id": "DNI..."
    }
    Emite tickets para los asientos seleccionados.
    """
    trip = get_object_or_404(Trip, pk=trip_id)
    payload = _parse_json(request)
    seat_ids = payload.get("seat_ids") or []
    buyer_name = (payload.get("buyer_name") or "").strip()
    national_id = (payload.get("national_id") or "").strip()
    try:
        tickets = Ticket.purchase(trip, seat_ids, buyer_name, national_id, request.user)
        return JsonResponse({"ok": True, "tickets": [t.number for t in tickets]})
    except Exception as e:
        return JsonResponse({"ok": False, "error": str(e)})


# =========================
# API genérica usada por el front cliente (opcional)
# =========================
@require_GET
def trip_seats(request: HttpRequest, trip_id: int):
    """
    API para front cliente (listado de asientos del bus del viaje).
    Estructura:
    {
      "viaje": {...},
      "asientos": [{id, numero, piso, fila, columna, ventana, ocupado}]
    }
    """
    try:
        trip = Trip.objects.select_related("bus", "route__origin", "route__destination").get(pk=trip_id)
    except Trip.DoesNotExist:
        raise Http404("Viaje no existe")

    bus = trip.bus
    letters = _letters_for_cols(getattr(bus, "cols", 0))
    pos2col = {ch: i for i, ch in enumerate(letters)}

    tickets = set(Ticket.objects.filter(trip=trip).values_list("seat_id", flat=True))
    seats_qs = Seat.objects.filter(bus=bus).order_by("deck", "row", "position", "number")
    seats = []
    for s in seats_qs:
        seats.append({
            "id": s.id,
            "numero": s.number,
            "piso": int(s.deck or 1),
            "fila": int(s.row or 0),
            "columna": int(pos2col.get(s.position, 0)) + 1,
            "ventana": bool(s.is_window),
            "ocupado": (s.id in tickets) or bool(s.is_occupied),
        })

    data = {
        "viaje": {
            "id": trip.id,
            "origen": trip.route.origin.name,
            "destino": trip.route.destination.name,
            "salida": trip.departure.isoformat(),
            "llegada": trip.arrival.isoformat(),
            "precio_asiento": str(getattr(trip.route, "base_price", "")),
            "bus": {
                "id": bus.id,
                "plate": bus.plate,
                "floors": getattr(bus, "floors", 1),
            },
        },
        "asientos": seats,
    }
    return JsonResponse(data, json_dumps_params={"ensure_ascii": False})



@require_GET
def api_cities(request):
    # Devuelve ciudades para datalist del buscador
    data = list(City.objects.order_by("name").values("name"))
    return JsonResponse(data, safe=False, json_dumps_params={"ensure_ascii": False})


@require_GET
def api_search_trips(request):
    origen  = (request.GET.get("origen") or "").strip()
    destino = (request.GET.get("destino") or "").strip()
    fecha   = (request.GET.get("fecha") or "").strip()  # YYYY-MM-DD

    if not (origen and destino and fecha):
        return JsonResponse({"error": "Faltan parámetros: origen, destino, fecha"}, status=400)

    # Rango de día (00:00 - 23:59:59) en tz local
    try:
        d = datetime.strptime(fecha, "%Y-%m-%d").date()
    except ValueError:
        return JsonResponse({"error": "Fecha inválida. Usa YYYY-MM-DD"}, status=400)

    start = timezone.make_aware(datetime.combine(d, time.min))
    end   = timezone.make_aware(datetime.combine(d, time.max))

    qs = (
        Trip.objects
        .select_related("route__origin", "route__destination", "bus")
        .filter(
            route__origin__name__iexact=origen,
            route__destination__name__iexact=destino,
            departure__range=(start, end),
        )
        .order_by("departure")
    )

    # Contar vendidos por viaje (tickets)
    # (esto es simple y estable; después optimizamos con anotaciones más pro si quieres)
    results = []
    for trip in qs:
        sold = Ticket.objects.filter(trip=trip).count()
        total = int(trip.seats_total or 0)
        libres = max(total - sold, 0)

        results.append({
            "id": trip.id,
            "origen": trip.route.origin.name,
            "destino": trip.route.destination.name,
            "salida": trip.departure.isoformat(),
            "llegada": trip.arrival.isoformat(),
            "base_price": str(getattr(trip.route, "base_price", "")),
            "bus": {"id": trip.bus.id, "plate": trip.bus.plate},
            "seats_total": total,
            "seats_sold": sold,
            "seats_free": libres,
        })

    return JsonResponse(results, safe=False, json_dumps_params={"ensure_ascii": False})


def bus_seatmap(request, pk:int):
    bus = get_object_or_404(Bus, pk=pk)
    return render(request, "booking/seatmap.html", {"bus": bus})


@transaction.atomic
def pos_checkout(request: HttpRequest, trip_id: int = None):
    """
    Crea tickets para asientos seleccionados (POST).
    Soporta tus dos variantes de modelo Ticket:
      - buyer_name / passenger_name / created_by si existen.
    El template envía 'seats' como JSON: [{"deck":1,"number":"12"}, ...]
    """
    if request.method != "POST":
        messages.error(request, "Operación inválida.")
        return redirect("pos_home")

    if not request.user.is_authenticated:
        messages.error(request, "Debes iniciar sesión para emitir boletos.")
        return redirect("pos_home")

    # ✅ NUEVO: método de pago (obligatorio)
    payment_method = (request.POST.get("payment_method") or "").strip()
    if payment_method not in ("cash", "card"):
        messages.error(request, "Debes seleccionar un método de pago (efectivo o tarjeta).")
        return redirect("pos_trip", trip_id=trip_id or request.POST.get("trip_id"))

    # ✅ NUEVO (Pack Caja #4): etiqueta amigable para el recibo
    payment_method_label = "💳 Tarjeta" if payment_method == "card" else "💵 Efectivo"

    # Limpieza rápida de holds vencidos
    try:
        SeatHold.cleanup()
    except Exception:
        pass

    # trip_id puede venir en URL o en hidden
    tid = trip_id or request.POST.get("trip_id")
    trip = get_object_or_404(Trip.objects.select_for_update(), pk=tid)

    # Parseo de asientos seleccionados (JSON)
    raw = request.POST.get("seats", "").strip()
    try:
        chosen = json.loads(raw) if raw else []
    except Exception:
        chosen = []
    if not isinstance(chosen, list) or not chosen:
        messages.error(request, "No hay asientos seleccionados.")
        return redirect("pos_trip", trip_id=trip.id)

    # Datos del comprador/pasajero
    buyer = (request.POST.get("buyer") or request.POST.get("buyer_name") or "").strip()
    passenger = (request.POST.get("passenger") or request.POST.get("passenger_name") or "").strip()
    doc = (request.POST.get("document") or "").strip()
    buyer_display = buyer or passenger or ""

    # Precio base desde la ruta (si no hay, 0)
    from decimal import Decimal
    unit_price = getattr(trip.route, "base_price", 0) or 0
    try:
        unit_price = Decimal(str(unit_price))
    except Exception:
        unit_price = Decimal("0")

    created_tickets = []
    skipped = []

    # Emisión — buscamos Seat por (bus, deck, number)
    for item in chosen:
        try:
            deck = int(item.get("deck"))
            number = str(item.get("number")).strip()
        except Exception:
            continue
        if not number:
            continue

        try:
            seat = Seat.objects.select_for_update().get(bus=trip.bus, deck=deck, number=number)
        except Seat.DoesNotExist:
            skipped.append(number)
            continue

        # Si ya está ocupado, saltar
        if Ticket.objects.filter(trip=trip, seat=seat).exists() or getattr(seat, "is_occupied", False):
            skipped.append(number)
            continue

        try:
            buyer_name_val = buyer or passenger or "Pasajero"
            t = Ticket.create_for_sale(
                trip=trip,
                seat=seat,
                buyer_name=buyer_name_val,
                national_id=doc,
                price=unit_price,
                created_by=request.user,
                # ✅ NUEVO: guardar método de pago
                payment_method=payment_method,
            )
            created_tickets.append(t)
        except Exception:
            skipped.append(number)
            continue

    # Datos para el recibo
    seats_for_receipt = [{"number": t.seat.number, "deck": getattr(t.seat, "deck", 1)} for t in created_tickets]
    total = unit_price * len(seats_for_receipt)

    # ✅ NUEVO (Pack Caja #1): totales por método de pago
    total_cash = Decimal("0")
    total_card = Decimal("0")
    for t in created_tickets:
        # Si por algún motivo viene vacío, lo contamos como cash (seguro y compatible)
        pm = (getattr(t, "payment_method", "") or "cash").strip()
        if pm == "card":
            total_card += t.price
        else:
            total_cash += t.price

    # ✅ Compatibilidad con templates antiguos:
    first_ticket = created_tickets[0] if created_tickets else None
    items = [{"ticket": t, "seat": {"number": t.seat.number, "deck": getattr(t.seat, "deck", 1)}} for t in created_tickets]
    ticket_numbers = [t.number for t in created_tickets]

    ctx = {
        "trip": trip,
        "tickets": created_tickets,
        "ticket": first_ticket,
        "ticket_numbers": ticket_numbers,
        "items": items,
        "seats": seats_for_receipt,
        "price": unit_price,
        "total": total,
        "buyer": buyer_display,
        "skipped": skipped,

        # ✅ NUEVO: mostrar método en receipt.html
        "payment_method": payment_method,
        "payment_method_label": payment_method_label,

        # ✅ NUEVO: mostrar totales por método
        "total_cash": total_cash,
        "total_card": total_card,
    }
    return render(request, "pos/receipt.html", ctx)


# =========================
# Sistema de Caja
# =========================
@staff_member_required
def pos_caja(request):
    """Dashboard principal de caja"""
    # Filtro de fecha (hoy por defecto)
    fecha_str = request.GET.get('fecha', '')
    if fecha_str:
        try:
            fecha = datetime.strptime(fecha_str, '%Y-%m-%d').date()
        except ValueError:
            fecha = timezone.now().date()
    else:
        fecha = timezone.now().date()
    
    # ✅ NUEVO: Lógica multi-usuario
    # Para administradores y supervisores: ver todas las cajas
    # Para vendedores y cajeros: ver solo su caja
    es_admin_o_supervisor = (
        request.user.is_superuser or 
        (hasattr(request.user, 'profile') and 
         request.user.profile.role in ['admin', 'supervisor'])
    )
    
    if es_admin_o_supervisor:
        caja_abierta = CashRegister.objects.filter(
            opening_date__date=fecha,
            status='open'
        ).exists()
        
        caja_actual = CashRegister.objects.filter(
            opening_date__date=fecha,
            status='open'
        ).first() if caja_abierta else None
        
        # Estadísticas para todos los usuarios
        ventas_hoy = Ticket.objects.filter(created_at__date=fecha)
        
        # ✅ NUEVO: Obtener cajas abiertas de todos los usuarios
        cajas_abiertas = CashRegister.objects.filter(
            opening_date__date=fecha,
            status='open'
        ).select_related('user')
    else:
        # Usuario normal solo ve su propia caja (comportamiento original)
        caja_abierta = CashRegister.objects.filter(
            user=request.user,
            opening_date__date=fecha,
            status='open'
        ).exists()
        
        caja_actual = CashRegister.objects.filter(
            user=request.user,
            opening_date__date=fecha,
            status='open'
        ).first() if caja_abierta else None
        
        ventas_hoy = Ticket.objects.filter(
            created_at__date=fecha,
            created_by=request.user
        )
        
        # ✅ NUEVO: Para usuarios normales, cajas_abiertas vacío
        cajas_abiertas = CashRegister.objects.none()
    
    # ✅ MANTENIDO: Tu código original de estadísticas
    total_ventas = ventas_hoy.aggregate(total=Sum('price'))['total'] or 0
    total_boletos = ventas_hoy.count()
    
    # Rutas vendidas hoy
    rutas_vendidas = ventas_hoy.values('trip__route').distinct().count()
    
    # Ticket promedio
    promedio_venta = total_ventas / total_boletos if total_boletos > 0 else 0
    
    # Ventas recientes (últimas 10)
    ventas_recientes = ventas_hoy.select_related(
        'trip__route__origin', 
        'trip__route__destination',
        'seat'
    ).order_by('-created_at')[:10]
    
    # ✅ MANTENIDO: Estadísticas comparativas (ayer)
    ayer = fecha - timedelta(days=1)
    
    # Ajustar consulta de ayer según permisos
    if es_admin_o_supervisor:
        ventas_ayer = Ticket.objects.filter(created_at__date=ayer)
    else:
        ventas_ayer = Ticket.objects.filter(
            created_at__date=ayer,
            created_by=request.user
        )
    
    total_ventas_ayer = ventas_ayer.aggregate(total=Sum('price'))['total'] or 0
    total_boletos_ayer = ventas_ayer.count()
    
    # ✅ MANTENIDO: Calcular tendencias
    tendencia_ventas = calcular_tendencia(total_ventas, total_ventas_ayer)
    tendencia_boletos = calcular_tendencia(total_boletos, total_boletos_ayer)
    
    # ✅ NUEVO: Información del perfil de usuario
    user_profile = None
    user_role = "Vendedor"
    if hasattr(request.user, 'profile'):
        user_profile = request.user.profile
        user_role = user_profile.get_role_display()
    
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
        
        # ✅ NUEVO: Datos para multi-usuario
        'es_admin_o_supervisor': es_admin_o_supervisor,
        'cajas_abiertas': cajas_abiertas,
        'user_profile': user_profile,
        'user_role': user_role,
        'usuarios_con_caja_abierta': cajas_abiertas.count() if es_admin_o_supervisor else 0,
    }
    
    return render(request, 'booking/pos_caja.html', context)


@staff_member_required
@require_POST
def abrir_caja(request):
    """Abrir caja para el día actual con monto inicial"""
    try:
        fecha_hoy = timezone.now().date()
        
        # Verificar si ya hay caja abierta
        if CashRegister.objects.filter(user=request.user, opening_date__date=fecha_hoy, status='open').exists():
            return JsonResponse({'success': False, 'error': 'Ya tienes una caja abierta hoy'})
        
        # Obtener el monto inicial del request
        try:
            data = json.loads(request.body)
            opening_balance = Decimal(str(data.get('opening_balance', 0)))
        except (json.JSONDecodeError, KeyError, ValueError):
            # Si no viene monto o hay error, usar 0 por defecto
            opening_balance = Decimal('0')
        
        # Validar que el monto sea positivo
        if opening_balance < 0:
            return JsonResponse({'success': False, 'error': 'El monto inicial no puede ser negativo'})
        
        # Crear nueva caja con monto inicial
        caja = CashRegister.objects.create(
            user=request.user,
            opening_balance=opening_balance
        )
        
        message = 'Caja abierta exitosamente'
        if opening_balance > 0:
            message = f'Caja abierta exitosamente con saldo inicial: ${opening_balance}'
        
        return JsonResponse({'success': True, 'message': message})
    
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})


@staff_member_required
@require_POST
def cerrar_caja(request):
    """Cerrar caja actual"""
    try:
        fecha_hoy = timezone.now().date()
        
        # Obtener caja abierta
        caja = CashRegister.objects.get(
            user=request.user, 
            opening_date__date=fecha_hoy, 
            status='open'
        )
        
        # Calcular ventas del día
        ventas_hoy = Ticket.objects.filter(
            created_at__date=fecha_hoy,
            created_by=request.user
        )
        total_ventas = ventas_hoy.aggregate(total=Sum('price'))['total'] or 0
        total_boletos = ventas_hoy.count()
        
        # Actualizar caja
        caja.closing_date = timezone.now()
        caja.total_sales = total_ventas
        caja.total_tickets = total_boletos
        caja.closing_balance = total_ventas  # Por ahora, saldo final = ventas
        caja.status = 'closed'
        caja.save()
        
        # Actualizar o crear reporte diario
        reporte, created = DailyReport.objects.get_or_create(date=fecha_hoy)
        reporte.total_tickets += total_boletos
        reporte.total_revenue += total_ventas
        reporte.total_cash_registers = CashRegister.objects.filter(
            opening_date__date=fecha_hoy, 
            status='closed'
        ).count()
        reporte.save()
        
        return JsonResponse({
            'success': True, 
            'message': f'Caja cerrada. Ventas: ${total_ventas}, Boletos: {total_boletos}'
        })
    
    except CashRegister.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'No tienes caja abierta'})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})


# =========================
# Reportes
# =========================
@staff_member_required
def pos_reportes(request):
    """Vista de reportes detallados"""
    # Filtros por fecha
    fecha_inicio_str = request.GET.get('fecha_inicio', '')
    fecha_fin_str = request.GET.get('fecha_fin', '')
    usuario_id = request.GET.get('usuario', '')  # ✅ NUEVO: Filtro por usuario
    
    # Fechas por defecto (últimos 7 días)
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
    
    # ✅ NUEVO: Lógica multi-usuario
    # Para administradores y supervisores: ver todas las ventas
    # Para vendedores y cajeros: ver solo sus ventas
    es_admin_o_supervisor = (
        request.user.is_superuser or 
        (hasattr(request.user, 'profile') and 
         request.user.profile.role in ['admin', 'supervisor'])
    )
    
    # ✅ MANTENIDO: Base query con soporte multi-usuario
    if es_admin_o_supervisor:
        ventas = Ticket.objects.filter(
            created_at__date__range=[fecha_inicio, fecha_fin]
        ).select_related('trip__route__origin', 'trip__route__destination', 'created_by')
        
        # ✅ NUEVO: Filtro por usuario específico si se selecciona
        if usuario_id and usuario_id != 'todos':
            ventas = ventas.filter(created_by_id=usuario_id)
    else:
        # ✅ MANTENIDO: Comportamiento original para usuarios normales
        ventas = Ticket.objects.filter(
            created_at__date__range=[fecha_inicio, fecha_fin],
            created_by=request.user
        ).select_related('trip__route__origin', 'trip__route__destination')
    
    # ✅ MANTENIDO: Estadísticas generales en una sola consulta
    stats = ventas.aggregate(
        total_ventas=Sum('price'),
        total_boletos=Count('id'),
        ticket_promedio=Avg('price')
    )
    total_ventas = stats['total_ventas'] or 0
    total_boletos = stats['total_boletos'] or 0
    ticket_promedio_general = stats['ticket_promedio'] or 0
    
    dias_rango = (fecha_fin - fecha_inicio).days + 1
    
    # CALCULAR PROMEDIO DIARIO
    promedio_diario = total_ventas / dias_rango if dias_rango > 0 else 0
    
    # Ventas por día (para gráfico)
    ventas_por_dia = ventas.extra(
        {'fecha': "DATE(created_at)"}
    ).values('fecha').annotate(
        total=Sum('price'),
        cantidad=Count('id')
    ).order_by('fecha')
    
    # CALCULAR PROMEDIOS PARA VENTAS POR DÍA
    ventas_por_dia_procesadas = []
    for venta in ventas_por_dia:
        promedio_dia = venta['total'] / venta['cantidad'] if venta['cantidad'] > 0 else 0
        ventas_por_dia_procesadas.append({
            'fecha': venta['fecha'],
            'total': venta['total'],
            'cantidad': venta['cantidad'],
            'promedio': promedio_dia
        })
    
    # Rutas más vendidas
    rutas_populares = ventas.values(
        'trip__route__origin__name', 
        'trip__route__destination__name'
    ).annotate(
        total=Sum('price'),
        cantidad=Count('id'),
        # ✅ MANTENIDO: Calcular promedio directamente en la consulta
        promedio=Avg('price')
    ).order_by('-total')[:10]
    
    # CALCULAR PROMEDIOS PARA RUTAS POPULARES (mantenemos por compatibilidad con el template)
    rutas_populares_procesadas = []
    for ruta in rutas_populares:
        # Usamos el promedio calculado en la consulta o calculamos como fallback
        promedio_ruta = ruta['promedio'] if ruta['promedio'] is not None else (ruta['total'] / ruta['cantidad'] if ruta['cantidad'] > 0 else 0)
        rutas_populares_procesadas.append({
            'trip__route__origin__name': ruta['trip__route__origin__name'],
            'trip__route__destination__name': ruta['trip__route__destination__name'],
            'total': ruta['total'],
            'cantidad': ruta['cantidad'],
            'promedio': promedio_ruta
        })
    
    # Horarios más populares
    ventas_por_hora = ventas.extra(
        {'hora': "EXTRACT(HOUR FROM created_at)"}
    ).values('hora').annotate(
        total=Sum('price'),
        cantidad=Count('id'),
        # ✅ MANTENIDO: Calcular promedio por hora
        promedio=Avg('price')
    ).order_by('hora')
    
    # ✅ NUEVO: Ventas por vendedor (solo para admin/supervisor)
    ventas_por_vendedor = []
    if es_admin_o_supervisor:
        ventas_por_vendedor = Ticket.objects.filter(
            created_at__date__range=[fecha_inicio, fecha_fin]
        ).values(
            'created_by__id',
            'created_by__username',
            'created_by__first_name',
            'created_by__last_name'
        ).annotate(
            total=Sum('price'),
            cantidad=Count('id'),
            promedio=Avg('price')
        ).order_by('-total')
    
    # ✅ NUEVO: Información del usuario actual
    user_profile = None
    user_role = "Vendedor"
    if hasattr(request.user, 'profile'):
        user_profile = request.user.profile
        user_role = user_profile.get_role_display()
    
    # ✅ CORREGIDO: Obtener lista de usuarios para el filtro (solo para admin/supervisor)
    usuarios_lista = []
    if es_admin_o_supervisor:
        usuarios_lista = User.objects.filter(
            is_staff=True,
            tickets_sold__created_at__date__range=[fecha_inicio, fecha_fin]  # ✅ CAMBIO: ticket → tickets_sold
        ).distinct().annotate(
            ventas_count=Count('tickets_sold')  # ✅ CAMBIO: ticket → tickets_sold
        ).order_by('username')
    
    context = {
        'title': 'POS — Reportes Detallados',
        'fecha_inicio': fecha_inicio.strftime('%Y-%m-%d'),
        'fecha_fin': fecha_fin.strftime('%Y-%m-%d'),
        'total_ventas': total_ventas,
        'total_boletos': total_boletos,
        'ticket_promedio_general': ticket_promedio_general,
        'promedio_diario': promedio_diario,
        'ventas_por_dia': ventas_por_dia_procesadas,
        'rutas_populares': rutas_populares_procesadas,
        'ventas_por_hora': list(ventas_por_hora),
        'dias_rango': dias_rango,
        
        # ✅ NUEVO: Datos para multi-usuario
        'es_admin_o_supervisor': es_admin_o_supervisor,
        'ventas_por_vendedor': ventas_por_vendedor,
        'usuarios_lista': usuarios_lista,
        'usuario_seleccionado': usuario_id,
        'user_profile': user_profile,
        'user_role': user_role,
        
        # ✅ NUEVO: Información del filtro aplicado
        'filtro_usuario_aplicado': usuario_id if usuario_id else None,
        'usuario_filtrado': User.objects.get(id=usuario_id).get_full_name() if usuario_id and usuario_id != 'todos' else None,
    }
    
    return render(request, 'booking/pos_reportes.html', context)

# =========================
# Gestión de Usuarios
# =========================

def gestion_usuarios(request):
    qs = User.objects.select_related('profile').order_by('username')

    context = {
        "title": "Gestión de Usuarios",
        "usuarios": qs,

        # métricas para las tarjetas
        "total_count": qs.count(),
        "admins_count": qs.filter(profile__role="admin").count(),
        "supervisores_count": qs.filter(profile__role="supervisor").count(),
        "vendedores_count": qs.filter(profile__role="vendedor").count(),
        "activos_count": qs.filter(profile__is_active=True).count(),
        "inactivos_count": qs.filter(profile__is_active=False).count(),

        # ✅ ahora desde el helper
        "roles": _get_role_choices(),
        "terminales": Terminal.objects.all(),
    }
    return render(request, "booking/gestion_usuarios.html", context)
@staff_member_required
@role_required(['admin', 'supervisor'])
def editar_usuario(request, user_id):
    """Editar perfil de usuario."""
    usuario = get_object_or_404(User, id=user_id)

    if request.method == 'POST':
        try:
            with transaction.atomic():
                # Actualizar información básica del usuario
                usuario.first_name = request.POST.get('first_name', '')
                usuario.last_name = request.POST.get('last_name', '')
                usuario.email = request.POST.get('email', '')
                usuario.save()

                # ✅ Obtener o crear el perfil (manejar caso donde no existe)
                profile, created = UserProfile.objects.get_or_create(
                    user=usuario,
                    defaults={
                        'role': 'vendedor',
                        'is_active': True
                    }
                )
                
                # Actualizar el perfil
                profile.role = request.POST.get('role', 'vendedor')
                profile.terminal_id = request.POST.get('terminal') or None
                
                try:
                    profile.commission_rate = Decimal(request.POST.get('commission_rate', 0) or 0)
                except (ValueError, TypeError):
                    profile.commission_rate = Decimal('0')
                
                try:
                    profile.max_discount = Decimal(request.POST.get('max_discount', 0) or 0)
                except (ValueError, TypeError):
                    profile.max_discount = Decimal('0')
                    
                profile.is_active = 'is_active' in request.POST
                profile.save()

            messages.success(request, f"Usuario {usuario.username} actualizado correctamente.")
            return redirect('gestion_usuarios')

        except Exception as e:
            messages.error(request, f"Error al actualizar usuario: {str(e)}")

    context = {
        'title': f'Editar Usuario - {usuario.username}',
        'usuario': usuario,
        'terminales': Terminal.objects.all(),
        'roles': _get_role_choices(),
    }
    return render(request, 'booking/editar_usuario.html', context)
# =========================
# Crear Usuario
# =========================

def crear_usuario(request):
    roles = _get_role_choices()
    terminales = Terminal.objects.all()

    if request.method == "POST":
        g = request.POST.get
        username = (g("username") or "").strip().lower()
        password = g("password") or ""
        confirm = g("confirm_password") or ""

        if not username:
            messages.error(request, "El nombre de usuario es obligatorio.")
        elif User.objects.filter(username__iexact=username).exists():
            messages.error(request, "Ese nombre de usuario ya existe.")
        elif password != confirm:
            messages.error(request, "Las contraseñas no coinciden.")
        else:
            try:
                with transaction.atomic():
                    # 1. Crear el usuario
                    user = User.objects.create_user(
                        username=username,
                        email=(g("email") or "").strip(),
                        password=password,
                        first_name=(g("first_name") or "").strip(),
                        last_name=(g("last_name") or "").strip(),
                    )
                    
                    # 2. Verificar si el perfil YA EXISTE (por si las señales no están completamente desactivadas)
                    if hasattr(user, 'profile'):
                        # Si ya existe, actualizar
                        profile = user.profile
                        profile.role = g("role") or "vendedor"
                        profile.terminal_id = g("terminal") or None
                        profile.commission_rate = Decimal(g("commission_rate") or 0)
                        profile.max_discount = Decimal(g("max_discount") or 0)
                        profile.is_active = True
                        profile.save()
                    else:
                        # Si no existe, crear
                        UserProfile.objects.create(
                            user=user,
                            role=g("role") or "vendedor",
                            terminal_id=g("terminal") or None,
                            commission_rate=Decimal(g("commission_rate") or 0),
                            max_discount=Decimal(g("max_discount") or 0),
                            is_active=True
                        )

                messages.success(request, "Usuario creado correctamente.")
                return redirect("gestion_usuarios")

            except IntegrityError as e:
                # Si hay error de integridad, mostrar información de depuración
                messages.error(request, f"Error de integridad: {e}")
                # Depuración adicional
                import traceback
                print("Traceback completo:")
                print(traceback.format_exc())
                
            except Exception as e:
                messages.error(request, f"Error inesperado: {e}")
                import traceback
                print("Traceback completo:")
                print(traceback.format_exc())

    context = {
        "title": "Crear Usuario",
        "roles": roles,
        "terminales": terminales,
    }
    return render(request, "booking/crear_usuario.html", context)
# =========================
# Helpers
# =========================

def _get_role_choices():
    """
    Lee los choices del campo 'role' en UserProfile (si existen).
    Devuelve tuplas (value, label). Fallback incluido.
    """
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

def debug_signals(request):
    """Página temporal para depurar señales"""
    from django.db.models.signals import post_save
    from django.contrib.auth import get_user_model
    
    User = get_user_model()
    
    # Verificar qué señales están registradas para User
    signals_info = []
    for receiver in post_save.receivers:
        if hasattr(receiver, '__self__'):
            receiver_str = str(receiver)
            if 'User' in receiver_str:
                signals_info.append(receiver_str)
    
    context = {
        'signals': signals_info,
        'total_signals': len(signals_info)
    }
    return render(request, 'booking/debug_signals.html', context)




def search_customer(request):
    """API para buscar cliente por RUT o nombre"""
    query = request.GET.get('q', '').strip()
    if not query or len(query) < 2:
        return JsonResponse({'customers': []})

    # Buscar por RUT o nombre
    customers = Customer.objects.filter(
        Q(national_id__icontains=query) | 
        Q(full_name__icontains=query)
    )[:10]

    results = []
    for customer in customers:
        results.append({
            'id': customer.id,
            'national_id': customer.national_id,
            'full_name': customer.full_name,
            'phone': customer.phone,
            'email': customer.email
        })

    return JsonResponse({'customers': results})

def create_customer(request):
    """API para crear nuevo cliente"""
    if request.method == 'POST':
        national_id = request.POST.get('national_id', '').strip()
        full_name = request.POST.get('full_name', '').strip()
        
        if not national_id or not full_name:
            return JsonResponse({'success': False, 'error': 'RUT y nombre son obligatorios'})

        try:
            customer, created = Customer.objects.get_or_create(
                national_id=national_id,
                defaults={
                    'full_name': full_name,
                    'phone': request.POST.get('phone', ''),
                    'email': request.POST.get('email', '')
                }
            )
            
            return JsonResponse({
                'success': True,
                'created': created,
                'customer': {
                    'id': customer.id,
                    'national_id': customer.national_id,
                    'full_name': customer.full_name,
                    'phone': customer.phone,
                    'email': customer.email
                }
            })
            
        except Exception as e:
            return JsonResponse({'success': False, 'error': str(e)})

    return JsonResponse({'success': False, 'error': 'Método no permitido'})




# ✅ Vista de prueba
def test_view(request):
    return JsonResponse({"message": "✅ Las URLs de customer están funcionando!", "status": "ok"})

# ✅ Buscar cliente
def search_customer(request):
    """API para buscar cliente por RUT o nombre (normaliza RUT para evitar mismatches)."""
    try:
        query = (request.GET.get("q") or "").strip()

        # Mantener comportamiento actual (mínimo 2 chars)
        if not query or len(query) < 2:
            return JsonResponse({"customers": []})

        # 1) Normalización tipo RUT (quita puntos/guion/espacios, upper-case)
        #    Esto hace que "12.345.678-9" encuentre "123456789" guardado.
        query_clean = Customer._clean_rut(query)

        # 2) Armar filtro:
        #    - Para RUT: buscamos por query_clean (y también por query por compatibilidad)
        #    - Para nombre: usamos el query original
        rut_q = Q(national_id__icontains=query_clean)
        if query_clean != query:
            rut_q = rut_q | Q(national_id__icontains=query)

        customers = (
            Customer.objects
            .filter(rut_q | Q(full_name__icontains=query))
            .order_by("full_name")[:10]
        )

        results = [
            {
                "id": c.id,
                "national_id": c.national_id,  # se mantiene igual para no romper tu front
                "full_name": c.full_name,
                "phone": c.phone or "",
                "email": c.email or "",
            }
            for c in customers
        ]

        return JsonResponse({"customers": results})

    except Exception as e:
        return JsonResponse({"customers": [], "error": str(e)})
    
    

# ✅ Crear cliente - VERSIÓN SIMPLIFICADA
@csrf_exempt
def create_customer(request):
    """API para crear nuevo cliente - CAMPOS NO OBLIGATORIOS"""
    print("📝 Recibiendo solicitud para crear cliente...")  # Debug
    
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Método no permitido'})

    try:
        # Obtener datos del POST
        national_id = request.POST.get('national_id', '').strip()
        full_name = request.POST.get('full_name', '').strip()
        phone = request.POST.get('phone', '').strip()
        email = request.POST.get('email', '').strip()
        
        print(f"📋 Datos recibidos - RUT: {national_id}, Nombre: {full_name}")  # Debug
        
        if not national_id:
            return JsonResponse({'success': False, 'error': 'RUT es obligatorio'})

        # Si no viene nombre, usar valor por defecto
        if not full_name:
            full_name = "Cliente"

        # Verificar si ya existe
        existing_customer = Customer.objects.filter(national_id=national_id).first()
        if existing_customer:
            print(f"ℹ️ Cliente ya existe: {existing_customer}")  # Debug
            return JsonResponse({
                'success': True,
                'created': False,
                'message': 'Cliente ya existe',
                'customer': {
                    'id': existing_customer.id,
                    'national_id': existing_customer.national_id,
                    'full_name': existing_customer.full_name,
                    'phone': existing_customer.phone or '',
                    'email': existing_customer.email or ''
                }
            })

        # Crear nuevo cliente - TODOS LOS CAMPOS OPCIONALES excepto RUT
        customer = Customer.objects.create(
            national_id=national_id,
            full_name=full_name,
            phone=phone,  # Puede estar vacío
            email=email   # Puede estar vacío
        )
        
        print(f"✅ Cliente creado: {customer}")  # Debug
        
        return JsonResponse({
            'success': True,
            'created': True,
            'message': 'Cliente creado exitosamente',
            'customer': {
                'id': customer.id,
                'national_id': customer.national_id,
                'full_name': customer.full_name,
                'phone': customer.phone or '',
                'email': customer.email or ''
            }
        })
        
    except Exception as e:
        print(f"❌ Error creando cliente: {e}")  # Debug
        return JsonResponse({'success': False, 'error': str(e)})
    


@csrf_exempt
def save_layout_template(request):
    if request.method != "POST":
        return JsonResponse({"error": "Método no permitido"}, status=400)

    try:
        data = json.loads(request.body)

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