from __future__ import annotations
from django.db.models import Count, Q
from django import forms
from django.contrib import admin, messages
from django.db import transaction
from django.shortcuts import get_object_or_404, render
from django.urls import path, reverse
from django.utils.html import format_html
from django.views.decorators.clickjacking import xframe_options_exempt
from datetime import date, datetime
from .models import (
    City, Company, Bus, Route, Trip, Seat, SeatHold, Ticket,
    Terminal, BusLayout, CashRegister, DailyReport)
from .models import UserProfile
from django.contrib.auth import get_user_model
# ---- Form opcional del asistente (si existe) ----
try:
    from .forms import BusWizardForm  # si no existe, seguimos sin él
except Exception:
    BusWizardForm = None
    

User = get_user_model()

@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'role', 'terminal', 'is_active', 'commission_rate', 'max_discount', 'created_at')
    list_filter  = ('role', 'is_active', 'terminal')
    search_fields = ('user__username', 'user__first_name', 'user__last_name', 'user__email')    
    
    

# =========================
# Helpers
# =========================
def _build_grid(bus: Bus, deck: int):
    """
    Convierte el layout lineal en matriz para el template del mapa (modo Bus).
    Cada celda: {"type": "L/P/X/E/D/B", "label": "<número>"}
    """
    cols = int(bus.cols or 0)
    if cols <= 0:
        return [], 0

    if deck == 1:
        flat = bus.layout_lower or []
        labels = bus.numbers_lower or []
        rows = int(bus.rows_lower or 0)
    else:
        flat = bus.layout_upper or []
        labels = bus.numbers_upper or []
        rows = int(bus.rows_upper or 0)

    grid, idx = [], 0
    for _r in range(rows):
        row = []
        for _c in range(cols):
            typ = (flat[idx] if idx < len(flat) else "L") or "L"
            lab = (labels[idx] if idx < len(labels) else "").strip() if typ == "L" else ""
            row.append({"type": typ, "label": lab})
            idx += 1
        grid.append(row)
    return grid, cols


def _letters_for_cols(cols: int):
    return ["A", "B", "C", "D"][: max(1, cols)]


def _build_trip_grid(trip: Trip):
    """
    Construye matrices para piso 1 y 2 del bus del viaje, marcando estado:
      {"type": 'L/P/X/E/D/B', "label": str, "status": 'free|occupied|hold', "hold_user": str}

    Notas de robustez añadidas:
    - Soporta discrepancias entre tamaño de layout y filas*columnas (rellena con 'L').
    - Tolera posiciones de asiento no mapeables (fallback a columna 0).
    - Usa el número del Seat (real) como etiqueta; si no existe, intenta con numbers_* del bus.
    - Devuelve también claves extra no invasivas: "seat_id", "is_window", "deck", "row", "col".
      (Tu template puede ignorarlas sin problema.)
    """
    bus = trip.bus
    cols = int(bus.cols or 0)
    if cols <= 0:
        return [], [], 0

    letters = _letters_for_cols(cols)
    pos2col = {ch: i for i, ch in enumerate(letters)}

    # ---------- indexación Seat -> índice lineal por piso (0-index) ----------
    index_by_deck = {1: {}, 2: {}}
    seats_qs = Seat.objects.filter(bus=bus).only("id", "deck", "row", "position", "number", "is_occupied")
    for s in seats_qs:
        col = pos2col.get((s.position or "").strip(), 0)   # tolerante a posiciones desconocidas
        row0 = max(int(s.row or 1) - 1, 0)
        idx = row0 * cols + col
        index_by_deck.setdefault(int(s.deck or 1), {})[idx] = s

    # ---------- estados (tickets y holds activos) ----------
    ticketed_ids = set(Ticket.objects.filter(trip=trip).values_list("seat_id", flat=True))
    holds = list(SeatHold.objects.filter(trip=trip, active=True))
    hold_by_seat = {h.seat_id: h for h in holds}

    # ---------- util para tomar etiqueta fallback desde numbers_* ----------
    def fallback_label(numbers: list, idx: int) -> str:
        try:
            v = numbers[idx]
            return (v or "").strip()
        except Exception:
            return ""

    # ---------- genera grid para un piso ----------
    def grid_for(deck: int, rows: int, flat_layout: list, numbers: list):
        out = []
        total_cells = max(int(rows or 0), 0) * cols

        # Normaliza largos de layout/numbers para evitar IndexError
        def _norm(seq: list, filler):
            seq = list(seq or [])
            if len(seq) < total_cells:
                seq = seq + [filler] * (total_cells - len(seq))
            else:
                seq = seq[:total_cells]
            return seq

        flat_layout = _norm(flat_layout, "L")
        numbers = _norm(numbers, "")

        i = 0
        for r in range(int(rows or 0)):
            row_cells = []
            for c in range(cols):
                t = (flat_layout[i] or "L")
                label = ""
                status = "free"
                hold_user = ""
                seat_id = None

                # Solo celdas tipo asiento tienen estado y número
                if t == "L":
                    s = index_by_deck.get(deck, {}).get(i)
                    if s:
                        seat_id = s.id
                        # Etiqueta: prioriza número real del Seat; si vacío, intenta numbers del bus
                        label = (s.number or "").strip() or fallback_label(numbers, i)
                        if s.id in ticketed_ids or s.is_occupied:
                            status = "occupied"
                        elif s.id in hold_by_seat:
                            status = "hold"
                            hold_user = getattr(hold_by_seat[s.id].user, "username", "")
                    else:
                        # Si no existe Seat mapeado pero el layout marca asiento,
                        # aún mostramos la etiqueta fallback si la hay.
                        label = fallback_label(numbers, i)

                is_window = (c == 0) or (c == cols - 1)
                row_cells.append({
                    "type": t,
                    "label": label,
                    "status": status,
                    "hold_user": hold_user,
                    # extras no invasivos
                    "seat_id": seat_id,
                    "is_window": is_window,
                    "deck": deck,
                    "row": r + 1,
                    "col": c + 1,
                })
                i += 1
            out.append(row_cells)
        return out

    lower = grid_for(1, int(bus.rows_lower or 0), bus.layout_lower or [], bus.numbers_lower or [])
    upper = []
    if int(bus.floors or 1) == 2 and int(bus.rows_upper or 0) > 0:
        upper = grid_for(2, int(bus.rows_upper or 0), bus.layout_upper or [], bus.numbers_upper or [])

    return lower, upper, cols


# ---------- POS: Home con buscador y lista de viajes ----------
def pos_home(request):
    """
    Dashboard POS con buscador (fecha, origen, destino) y listado de viajes.
    Cada viaje muestra libres/vendidos/total y un botón para abrir el mapa (modal).
    """
    # filtros GET
    try:
        sel_date = request.GET.get("date") or ""
        if sel_date:
            query_date = datetime.strptime(sel_date, "%Y-%m-%d").date()
        else:
            query_date = date.today()
            sel_date = query_date.strftime("%Y-%m-%d")
    except Exception:
        query_date = date.today()
        sel_date = query_date.strftime("%Y-%m-%d")

    origin_id = request.GET.get("origin_id") or ""
    dest_id   = request.GET.get("dest_id") or ""

    trips = Trip.objects.select_related("route", "bus", "route__origin", "route__destination") \
                        .filter(departure__date=query_date) \
                        .order_by("departure")

    if origin_id:
        trips = trips.filter(route__origin_id=origin_id)
    if dest_id:
        trips = trips.filter(route__destination_id=dest_id)

    # Conteos rápidos
    sold_map = dict(
        Ticket.objects.filter(trip__in=trips).values("trip").annotate(c=Count("id")).values_list("trip", "c")
    )
    holds_qs = SeatHold.objects.filter(trip__in=trips, active=True).values("trip").annotate(c=Count("id"))
    hold_map = dict(holds_qs.values_list("trip", "c"))
    seats_map = dict(trips.values_list("id", "seats_total"))

    # anota atributos dinámicos solo para mostrar
    for t in trips:
        t.sold  = int(sold_map.get(t.id, 0))
        t.hold  = int(hold_map.get(t.id, 0))
        t.total = int(seats_map.get(t.id, 0))
        t.free  = max(t.total - t.sold - t.hold, 0)

    ctx = {
        "title": "POS — Salidas",
        "date": sel_date,
        "cities": City.objects.all().order_by("name"),
        "origin_id": origin_id,
        "dest_id": dest_id,
        "trips": trips,
    }
    return render(request, "booking/pos_home.html", ctx)

# ---------- POS: Seatmap del viaje (usado por el modal) ----------
def pos_trip(request, trip_id: int):
    trip = get_object_or_404(Trip.objects.select_related("route", "bus"), pk=trip_id)
    grid_lower, grid_upper, cols = _build_trip_grid(trip)

    ctx = {
        "title": f"Mapa del viaje — {trip.route} {trip.departure:%Y-%m-%d %H:%M}",
        "trip": trip,
        "bus": trip.bus,
        "cols": cols,
        "grid_lower": grid_lower,
        "grid_upper": grid_upper,
        "change_url": "",  # en POS no usamos link de edición
    }
    # reutilizamos el template único y lindo
    return render(request, "booking/seatmap.html", ctx)

def _safe_register(model, admin_class):
    """Evita AlreadyRegistered si ya fue registrado en otro módulo."""
    try:
        admin.site.unregister(model)
    except Exception:
        pass
    admin.site.register(model, admin_class)


# =========================
# Admins básicos
# =========================
class CityAdmin(admin.ModelAdmin):
    list_display = ("name", "slug")
    search_fields = ("name", "slug")


class CompanyAdmin(admin.ModelAdmin):
    list_display = ("name", "logo")
    search_fields = ("name",)


class TerminalAdmin(admin.ModelAdmin):
    list_display = ("name", "city", "address")
    search_fields = ("name", "city__name", "address")
    list_filter = ("city",)


class RouteAdmin(admin.ModelAdmin):
    """
    Admin de Rutas. No asume que exista 'service'.
    """
    base_list_display = ["origin", "destination", "duration_minutes", "base_price"]
    base_list_filter = ["origin", "destination"]
    search_fields = ("origin__name", "destination__name")

    def __init__(self, model, admin_site):
        super().__init__(model, admin_site)
        if hasattr(model, "service"):
            self.list_display = tuple(self.base_list_display + ["service"])
            self.list_filter = tuple(self.base_list_filter + ["service"])
        else:
            self.list_display = tuple(self.base_list_display)
            self.list_filter = tuple(self.base_list_filter)


class SeatAdmin(admin.ModelAdmin):
    list_display = ("bus", "deck", "row", "position", "number", "is_window", "is_occupied")
    search_fields = ("bus__plate", "number")
    list_filter = ("bus", "deck", "is_window", "is_occupied")


class SeatHoldAdmin(admin.ModelAdmin):
    list_display = ("trip", "seat", "user", "active", "expires_at", "created_at")
    list_filter = ("active",)
    search_fields = (
        "trip__route__origin__name",
        "trip__route__destination__name",
        "user__username",
        "seat__number",
    )


# =========================
# TicketAdmin compatible
# =========================
TICKET_FIELDS = {f.name for f in Ticket._meta.get_fields()}
HAS_BUYER_NAME = "buyer_name" in TICKET_FIELDS
HAS_PASSENGER_NAME = "passenger_name" in TICKET_FIELDS
HAS_PAID = "paid" in TICKET_FIELDS


class TicketAdmin(admin.ModelAdmin):
    def display_name(self, obj: Ticket):
        return getattr(obj, "buyer_name", None) or getattr(obj, "passenger_name", "")
    display_name.short_description = "Pasajero"

    def display_paid(self, obj: Ticket):
        return getattr(obj, "paid", False)
    display_paid.boolean = True
    display_paid.short_description = "Pagado"

    base_columns = ["number", "trip", "seat", "display_name", "price", "created_by", "created_at"]
    if not hasattr(Ticket, "number"):
        base_columns = ["id", "trip", "seat", "display_name", "price", "created_at"]
    if HAS_PAID:
        if "created_at" in base_columns:
            base_columns.insert(base_columns.index("created_at"), "display_paid")
        else:
            base_columns.append("display_paid")

    list_display = tuple(base_columns)
    base_filters = ["trip__route__origin", "trip__route__destination"]
    if HAS_PAID:
        base_filters.append("paid")
    list_filter = tuple(base_filters)

    search_fields = ("seat__number", "number", "created_by__username")
    if HAS_BUYER_NAME:
        search_fields += ("buyer_name",)
    if HAS_PASSENGER_NAME:
        search_fields += ("passenger_name",)


# =========================
# TripAdmin con “Mapa”
# =========================
class TripAdmin(admin.ModelAdmin):
    list_display = ("route", "bus", "departure", "arrival", "seats_total", "seatmap_link")
    search_fields = ("route__origin__name", "route__destination__name", "bus__plate")
    list_filter = ("route__origin", "route__destination", "bus")
    autocomplete_fields = ("bus",)

    def seatmap_link(self, obj):
        url = reverse("admin:booking_trip_seatmap", args=[obj.pk])
        return format_html('<a class="button" href="{}" target="_blank">Mapa</a>', url)
    seatmap_link.short_description = "Mapa"

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "<int:trip_id>/seatmap/",
                self.admin_site.admin_view(self.seatmap_view),
                name="booking_trip_seatmap",
            ),
        ]
        return custom + urls

    def seatmap_view(self, request, trip_id: int):
        trip = get_object_or_404(Trip, pk=trip_id)
        grid_lower, grid_upper, cols = _build_trip_grid(trip)
        change_url = reverse("admin:booking_trip_change", args=[trip.pk])
        ctx = dict(
            self.admin_site.each_context(request),
            title=f"Mapa del viaje — {trip.route} {trip.departure:%Y-%m-%d %H:%M}",
            trip=trip,
            cols=cols,
            grid_lower=grid_lower,
            grid_upper=grid_upper,
            change_url=change_url,
        )
        return render(request, "booking/seatmap.html", ctx)


# =========================
# Acción masiva para Bus
# =========================
@admin.action(description="Regenerar asientos desde layout")
def regenerate_seats_action(modeladmin, request, queryset):
    total = 0
    errors = 0
    for bus in queryset:
        try:
            bus.ensure_layouts()
            total += bus.regenerate_seats()
        except Exception as e:
            errors += 1
            modeladmin.message_user(request, f"{bus}: {e}", level=messages.ERROR)
    modeladmin.message_user(
        request,
        f"Se recrearon {total} asientos en {queryset.count()} bus(es).",
        level=messages.SUCCESS if errors == 0 else messages.WARNING,
    )

# =========================
# BusAdmin con “Mapa” (modal) + Guardar como plantilla + Auto-completado
# =========================
class BusAdmin(admin.ModelAdmin):
    """
    - Usa BusWizardForm si está disponible.
    - Botón "Mapa" con modal.
    - Botón "Guardar como plantilla" que crea un BusLayout desde el bus.
    - Regenera asientos al guardar (on_commit).
    - Auto-completa campos cuando se selecciona plantilla.
    """
    if BusWizardForm:
        form = BusWizardForm

    list_display = (
        "plate", "company", "model", "year", "floors",
        "rows_lower", "rows_upper", "cols",
        "seatmap_link", "save_as_layout_link"
    )
    search_fields = ("plate", "model", "company__name")
    list_filter = ("company", "floors")

    fieldsets = (
        ("Datos del bus", {"fields": ("company", "plate", "model", "year")}),
        ("Estructura / Dimensiones", {"fields": ("floors", "rows_lower", "rows_upper", "cols")}),
        ("Mapa reutilizable (opcional)", {"fields": ("layout_template",)}),
        ("Numeración automática (opcional)", {"fields": ("prefix_lower", "prefix_upper")}),
        ("Asistente (opcional)", {
            "fields": tuple(
                f for f in ("template", "rows_lower_w", "rows_upper_w", "blocks_lower", "blocks_upper")
                if not BusWizardForm or f in getattr(BusWizardForm().fields, "keys", lambda: [])()
            ),
            "description": "Elige plantilla y/o bloquea celdas por coordenada (fila:col;fila:col).",
        }),
        ("Layout (avanzado)", {
            "fields": ("layout_lower", "layout_upper", "numbers_lower", "numbers_upper"),
            "classes": ("collapse",),
        }),
    )

    actions = ("regenerate_seats_action", "force_regenerate_seats")

    class Media:
        css = {"all": ("booking/admin-seatmap.css",)}
        js = (
            "admin/js/jquery.init.js",
            "booking/bus_wizard.js",
            "booking/seatmap-modal.js",
        )

    # -------- Botón "Mapa" (abre modal) --------
    def seatmap_link(self, obj: Bus):
        if not obj.pk:
            return "-"
        url = reverse("admin:booking_bus_seatmap", args=[obj.pk])
        return format_html('<a class="button js-seatmap" href="{}" data-seatmap-url="{}">Mapa</a>', url, url)
    seatmap_link.short_description = "Mapa"

    # -------- Botón "Guardar como plantilla" --------
    def save_as_layout_link(self, obj: Bus):
        if not obj.pk:
            return "-"
        url = reverse("admin:booking_bus_save_as_layout", args=[obj.pk])
        return format_html('<a class="button" href="{}">Guardar como plantilla</a>', url)
    save_as_layout_link.short_description = "Guardar como plantilla"

    # -------- CORREGIDO: Guardado: regenerar asientos al confirmar --------
    def save_model(self, request, obj: Bus, form, change):
        # CORRECCIÓN: Si hay plantilla, copiar TODOS los datos, no solo dimensiones
        if obj.layout_template:
            template = obj.layout_template
            # Copiar todos los datos de layout de la plantilla
            obj.floors = template.floors
            obj.rows_lower = template.rows_lower
            obj.rows_upper = template.rows_upper
            obj.cols = template.cols
            obj.layout_lower = list(template.layout_lower or [])
            obj.layout_upper = list(template.layout_upper or [])
            obj.numbers_lower = list(template.numbers_lower or [])
            obj.numbers_upper = list(template.numbers_upper or [])
            obj.prefix_lower = template.prefix_lower or ""
            obj.prefix_upper = template.prefix_upper or ""
        
        super().save_model(request, obj, form, change)

        def _regen():
            try:
                # CORRECCIÓN: Asegurar layouts ANTES de regenerar asientos
                obj.ensure_layouts()
                created = obj.regenerate_seats()
                self.message_user(request, f"Se generaron {created} asientos para {obj}.", messages.SUCCESS)
            except Exception as e:
                self.message_user(request, f"Error generando asientos: {e}", messages.ERROR)

        transaction.on_commit(_regen)

    # -------- MEJORADO: Auto-completar campos desde plantilla --------
    def _auto_fill_from_template(self, bus: Bus):
        """Auto-completa dimensiones desde la plantilla seleccionada"""
        if bus.layout_template:
            template = bus.layout_template
            bus.floors = template.floors
            bus.rows_lower = template.rows_lower
            bus.rows_upper = template.rows_upper
            bus.cols = template.cols
            # CORRECCIÓN: También copiar los layouts y números
            bus.layout_lower = list(template.layout_lower or [])
            bus.layout_upper = list(template.layout_upper or [])
            bus.numbers_lower = list(template.numbers_lower or [])
            bus.numbers_upper = list(template.numbers_upper or [])
            bus.prefix_lower = template.prefix_lower or ""
            bus.prefix_upper = template.prefix_upper or ""

    # -------- CORREGIDO: Formfield para agregar data-attributes --------
    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "layout_template":
            from django import forms
            
            class TemplateSelect(forms.Select):
                def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
                    option = super().create_option(name, value, label, selected, index, subindex, attrs)
                    # CORRECCIÓN: Obtener el valor real del ID
                    if value and hasattr(value, 'value'):
                        # value es un ModelChoiceIteratorValue, obtener el valor real
                        real_value = value.value
                    else:
                        real_value = value
                    
                    if real_value:
                        try:
                            from .models import BusLayout
                            layout = BusLayout.objects.get(id=real_value)
                            # Agregar data-attributes para auto-completado
                            option['attrs']['data-floors'] = layout.floors
                            option['attrs']['data-rows-lower'] = layout.rows_lower
                            option['attrs']['data-rows-upper'] = layout.rows_upper
                            option['attrs']['data-cols'] = layout.cols
                            # CORRECCIÓN: Agregar más datos para mejor auto-completado
                            option['attrs']['data-prefix-lower'] = layout.prefix_lower or ""
                            option['attrs']['data-prefix-upper'] = layout.prefix_upper or ""
                        except (BusLayout.DoesNotExist, ValueError):
                            pass
                    return option
            
            kwargs['widget'] = TemplateSelect
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    # -------- URLs personalizadas --------
    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "<int:bus_id>/seatmap/",
                self.admin_site.admin_view(self.seatmap_view),
                name="booking_bus_seatmap",
            ),
            path(
                "<int:bus_id>/save-as-layout/",
                self.admin_site.admin_view(self.save_as_layout_view),
                name="booking_bus_save_as_layout",
            ),
            path(
                "<int:bus_id>/template-data/",
                self.admin_site.admin_view(self.template_data_view),
                name="booking_bus_template_data",
            ),
        ]
        return custom + urls

    # -------- Vista del mapa (en iframe para el modal) --------
    @xframe_options_exempt
    def seatmap_view(self, request, bus_id: int):
        bus = get_object_or_404(Bus, pk=bus_id)
        grid_lower, cols = _build_grid(bus, 1)
        grid_upper = []
        if int(bus.floors or 1) == 2 and int(bus.rows_upper or 0) > 0:
            grid_upper, _ = _build_grid(bus, 2)

        change_url = reverse("admin:booking_bus_change", args=[bus.pk])
        ctx = dict(
            self.admin_site.each_context(request),
            title=f"Mapa de asientos — {bus.plate}",
            bus=bus,
            cols=cols,
            grid_lower=grid_lower,
            grid_upper=grid_upper,
            change_url=change_url,
        )
        return render(request, "booking/seatmap.html", ctx)

    # -------- Crear BusLayout desde Bus --------
    def save_as_layout_view(self, request, bus_id: int):
        from django.shortcuts import redirect
        bus = get_object_or_404(Bus, pk=bus_id)

        # Asegura arrays antes de copiar
        try:
            bus.ensure_layouts()
        except Exception:
            pass

        # Nombre único sugerido
        base_name = f"Mapa — {bus.plate}"
        name = base_name
        i = 2
        while BusLayout.objects.filter(name=name).exists():
            name = f"{base_name} ({i})"
            i += 1

        # Crear plantilla
        new_layout = BusLayout.objects.create(
            name=name,
            floors=int(bus.floors or 1),
            rows_lower=int(bus.rows_lower or 0),
            rows_upper=int(bus.rows_upper or 0),
            cols=int(bus.cols or 0),
            layout_lower=list(bus.layout_lower or []),
            layout_upper=list(bus.layout_upper or []),
            numbers_lower=list(bus.numbers_lower or []),
            numbers_upper=list(bus.numbers_upper or []),
            prefix_lower=bus.prefix_lower or "",
            prefix_upper=bus.prefix_upper or "",
        )

        self.message_user(request, f"Plantilla creada desde {bus.plate}: «{new_layout.name}».", messages.SUCCESS)
        # Redirigir a editar la plantilla
        return redirect(reverse("admin:booking_buslayout_change", args=[new_layout.pk]))

    # -------- API para datos de plantilla (JSON) --------
    def template_data_view(self, request, bus_id: int):
        """Endpoint que devuelve datos de plantilla en JSON"""
        import json
        from django.http import JsonResponse
        
        bus = get_object_or_404(Bus, pk=bus_id)
        if not bus.layout_template:
            return JsonResponse({'error': 'No template selected'}, status=400)
        
        template = bus.layout_template
        data = {
            'floors': template.floors,
            'rows_lower': template.rows_lower,
            'rows_upper': template.rows_upper,
            'cols': template.cols,
            'layout_lower': template.layout_lower,
            'layout_upper': template.layout_upper,
            'numbers_lower': template.numbers_lower,
            'numbers_upper': template.numbers_upper,
            'prefix_lower': template.prefix_lower,
            'prefix_upper': template.prefix_upper,
        }
        return JsonResponse(data)

    # -------- Acción masiva de regenerar seats --------
    def regenerate_seats_action(self, request, queryset):
        return globals()["regenerate_seats_action"](self, request, queryset)
    regenerate_seats_action.short_description = "Regenerar asientos desde layout"

    # -------- NUEVA ACCIÓN: Forzar regeneración completa --------
    @admin.action(description="Forzar regeneración completa de asientos")
    def force_regenerate_seats(self, request, queryset):
        total = 0
        errors = 0
        for bus in queryset:
            try:
                # Forzar la sincronización con la plantilla si existe
                if bus.layout_template:
                    template = bus.layout_template
                    bus.floors = template.floors
                    bus.rows_lower = template.rows_lower
                    bus.rows_upper = template.rows_upper
                    bus.cols = template.cols
                    bus.layout_lower = list(template.layout_lower or [])
                    bus.layout_upper = list(template.layout_upper or [])
                    bus.numbers_lower = list(template.numbers_lower or [])
                    bus.numbers_upper = list(template.numbers_upper or [])
                    bus.save()
                
                bus.ensure_layouts()
                created = bus.regenerate_seats()
                total += created
                self.message_user(
                    request, 
                    f"{bus.plate}: {created} asientos regenerados", 
                    messages.SUCCESS
                )
            except Exception as e:
                errors += 1
                self.message_user(
                    request, 
                    f"{bus.plate}: Error - {e}", 
                    messages.ERROR
                )
        
        if errors == 0:
            self.message_user(
                request, 
                f"Se regeneraron {total} asientos en {queryset.count()} bus(es).", 
                messages.SUCCESS
            )
        else:
            self.message_user(
                request, 
                f"Se regeneraron {total} asientos con {errors} error(es).", 
                messages.WARNING
            )

# =========================
# BusLayout (mapas) — modal + labels ES
# =========================
class BusLayoutAdminForm(forms.ModelForm):
    class Meta:
        model = BusLayout
        fields = "__all__"
        labels = {
            "layout_lower":  "Piso inferior (layout)",
            "layout_upper":  "Piso superior (layout)",
            "numbers_lower": "Números inferiores",
            "numbers_upper": "Números superiores",
            "rows_lower":    "Filas piso inferior",
            "rows_upper":    "Filas piso superior",
            "cols":          "Asientos por fila",
            "floors":        "Pisos",
            "prefix_lower":  "Prefijo piso inferior",
            "prefix_upper":  "Prefijo piso superior",
        }
        help_texts = {
            "layout_lower":  "Lista lineal de celdas (L/P/X/E/D/B) con tamaño filas×columnas.",
            "layout_upper":  "Lista lineal de celdas (L/P/X/E/D/B) con tamaño filas×columnas.",
            "numbers_lower": "Lista lineal con numeración (o vacío) del piso inferior.",
            "numbers_upper": "Lista lineal con numeración (o vacío) del piso superior.",
        }
        widgets = {
            "layout_lower":  forms.Textarea(attrs={"rows": 7, "cols": 80}),
            "layout_upper":  forms.Textarea(attrs={"rows": 7, "cols": 80}),
            "numbers_lower": forms.Textarea(attrs={"rows": 5, "cols": 80}),
            "numbers_upper": forms.Textarea(attrs={"rows": 5, "cols": 80}),
        }


@admin.register(BusLayout)
class BusLayoutAdmin(admin.ModelAdmin):
    form = BusLayoutAdminForm
    # ⬇️ añadimos duplicate_link a las columnas
    list_display = ("name", "floors", "rows_lower", "rows_upper", "cols", "seatmap_link", "duplicate_link")
    search_fields = ("name", "slug")
    fieldsets = (
        ("Identificación", {"fields": ("name",)}),
        ("Dimensiones", {"fields": ("floors", "rows_lower", "rows_upper", "cols")}),
        ("Layout (avanzado)", {"fields": ("layout_lower", "layout_upper", "numbers_lower", "numbers_upper")}),
        ("Numeración automática (opcional)", {"fields": ("prefix_lower", "prefix_upper")}),
    )

    # ⬇️ acción masiva para duplicar
    actions = ("duplicate_selected_layouts",)

    class Media:
        css = {"all": ("booking/admin-seatmap.css",)}
        js = ("booking/seatmap-modal.js",)

    # -------- Botones por fila --------
    def seatmap_link(self, obj):
        url = reverse("admin:booking_layout_seatmap", args=[obj.pk])
        return format_html('<a class="button js-seatmap" href="{}" data-seatmap-url="{}">Mapa</a>', url, url)
    seatmap_link.short_description = "Mapa"

    def duplicate_link(self, obj):
        url = reverse("admin:booking_layout_duplicate", args=[obj.pk])
        return format_html('<a class="button" href="{}">Duplicar</a>', url)
    duplicate_link.short_description = "Duplicar"

    # -------- URLs personalizadas --------
    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "<int:layout_id>/seatmap/",
                self.admin_site.admin_view(self.seatmap_view),
                name="booking_layout_seatmap",
            ),
            path(
                "<int:layout_id>/duplicate/",
                self.admin_site.admin_view(self.duplicate_view),
                name="booking_layout_duplicate",
            ),
        ]
        return custom + urls

    # -------- Vista: mapa (en iframe para modal) --------
    @xframe_options_exempt
    def seatmap_view(self, request, layout_id: int):
        layout = get_object_or_404(BusLayout, pk=layout_id)

        class _FakeBus:
            """Wrapper para reutilizar seatmap.html tal cual."""
            company = type("C", (), {"name": "Plantilla"})
            plate = layout.name
            floors = layout.floors
            rows_lower = layout.rows_lower
            rows_upper = layout.rows_upper
            cols = layout.cols
            layout_lower = layout.layout_lower
            layout_upper = layout.layout_upper
            numbers_lower = layout.numbers_lower or []
            numbers_upper = layout.numbers_upper or []

        fake = _FakeBus()
        ctx = dict(
            self.admin_site.each_context(request),
            title=f"Mapa — {layout.name}",
            bus=fake,
        )
        return render(request, "booking/seatmap.html", ctx)

    # -------- Vista: duplicar un layout --------
    def duplicate_view(self, request, layout_id: int):
        layout = get_object_or_404(BusLayout, pk=layout_id)

        base_name = f"{layout.name} (copia)"
        name = base_name
        i = 2
        while BusLayout.objects.filter(name=name).exists():
            name = f"{base_name} {i}"
            i += 1

        new_obj = BusLayout.objects.create(
            name=name,
            floors=layout.floors,
            rows_lower=layout.rows_lower,
            rows_upper=layout.rows_upper,
            cols=layout.cols,
            layout_lower=list(layout.layout_lower or []),
            layout_upper=list(layout.layout_upper or []),
            numbers_lower=list(layout.numbers_lower or []),
            numbers_upper=list(layout.numbers_upper or []),
            prefix_lower=layout.prefix_lower,
            prefix_upper=layout.prefix_upper,
        )

        self.message_user(request, f"Mapa duplicado como “{new_obj.name}”.")
        # redirigimos al change del nuevo objeto sin tocar imports globales
        from django.shortcuts import redirect
        change_url = reverse(
            f"admin:{new_obj._meta.app_label}_{new_obj._meta.model_name}_change",
            args=[new_obj.pk],
        )
        return redirect(change_url)

    # -------- Acción masiva: duplicar seleccionados --------
    @admin.action(description="Duplicar mapas seleccionados")
    def duplicate_selected_layouts(self, request, queryset):
        created = 0
        for layout in queryset:
            base_name = f"{layout.name} (copia)"
            name = base_name
            i = 2
            while BusLayout.objects.filter(name=name).exists():
                name = f"{base_name} {i}"
                i += 1

            BusLayout.objects.create(
                name=name,
                floors=layout.floors,
                rows_lower=layout.rows_lower,
                rows_upper=layout.rows_upper,
                cols=layout.cols,
                layout_lower=list(layout.layout_lower or []),
                layout_upper=list(layout.layout_upper or []),
                numbers_lower=list(layout.numbers_lower or []),
                numbers_upper=list(layout.numbers_upper or []),
                prefix_lower=layout.prefix_lower,
                prefix_upper=layout.prefix_upper,
            )
            created += 1

        self.message_user(request, f"Se duplicaron {created} mapa(s).")


# =========================
# Modelos de Caja y Reportes
# =========================

class CashRegisterAdmin(admin.ModelAdmin):
    list_display = ("user", "opening_date", "closing_date", "total_sales", "total_tickets", "status")
    list_filter = ("status", "opening_date", "user")
    search_fields = ("user__username",)
    readonly_fields = ("opening_date", "closing_date")
    
    def get_queryset(self, request):
        return super().get_queryset(request).select_related('user')

class DailyReportAdmin(admin.ModelAdmin):
    list_display = ("date", "total_tickets", "total_revenue", "total_cash_registers", "created_at")
    list_filter = ("date",)
    readonly_fields = ("created_at",)
    search_fields = ("date",)
    
    def has_add_permission(self, request):
        return False  # Los reportes se generan automáticamente

# =========================
# Registro seguro de los nuevos modelos
# =========================

# AGREGAR ESTAS LÍNEAS ANTES de los registros existentes:

# Registrar modelos de caja de forma segura
try:
    admin.site.register(CashRegister, CashRegisterAdmin)
except admin.sites.AlreadyRegistered:
    pass

try:
    admin.site.register(DailyReport, DailyReportAdmin)
except admin.sites.AlreadyRegistered:
    pass

# Luego mantén todos tus registros existentes:
_safe_register(City, CityAdmin)
_safe_register(Company, CompanyAdmin)
_safe_register(Route, RouteAdmin)
_safe_register(Seat, SeatAdmin)
_safe_register(SeatHold, SeatHoldAdmin)
_safe_register(Ticket, TicketAdmin)
_safe_register(Trip, TripAdmin)
_safe_register(Bus, BusAdmin)
_safe_register(Terminal, TerminalAdmin)


