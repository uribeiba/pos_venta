# booking/admin.py
import logging
from django.contrib import admin
from django.db.models import Count
from django.urls import path, reverse
from django.utils.html import format_html
from django.views.decorators.clickjacking import xframe_options_exempt
from django.shortcuts import render, get_object_or_404
from django import forms
from django.contrib import messages
from django.db import transaction
from django.http import JsonResponse
import json
from datetime import date, datetime

# Configurar logger
logger = logging.getLogger(__name__)

# ===== IMPORTACIONES DE MODELOS (TODOS) =====
from .models import (
    City,
    Company,
    Bus,
    BusLayout,
    Route,
    Seat,
    SeatHold,
    Ticket,
    Trip,
    Terminal,
    CashRegister,
    DailyReport,
    UserProfile,
    Driver,
    Assistant,
    DriverDocument,
    BusDocument,
    Season,
    Promotion,
    Parcel,
    Maintenance,
    FuelRecord,
    Customer,
    CompanyContract,
    ContractEmployee,
)

# ============================================================
# HELPERS PARA MAPAS DE ASIENTOS
# ============================================================
def _build_grid(bus: Bus, deck: int):
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
    bus = trip.bus
    cols = int(bus.cols or 0)
    if cols <= 0:
        return [], [], 0

    letters = _letters_for_cols(cols)
    pos2col = {ch: i for i, ch in enumerate(letters)}

    index_by_deck = {1: {}, 2: {}}
    seats_qs = Seat.objects.filter(bus=bus).only("id", "deck", "row", "position", "number", "is_occupied")
    for s in seats_qs:
        col = pos2col.get((s.position or "").strip(), 0)
        row0 = max(int(s.row or 1) - 1, 0)
        idx = row0 * cols + col
        index_by_deck.setdefault(int(s.deck or 1), {})[idx] = s

    ticketed_ids = set(Ticket.objects.filter(trip=trip).values_list("seat_id", flat=True))
    holds = list(SeatHold.objects.filter(trip=trip, active=True))
    hold_by_seat = {h.seat_id: h for h in holds}

    def fallback_label(numbers: list, idx: int) -> str:
        try:
            v = numbers[idx]
            return (v or "").strip()
        except Exception:
            return ""

    def grid_for(deck: int, rows: int, flat_layout: list, numbers: list):
        out = []
        total_cells = max(int(rows or 0), 0) * cols

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

                if t == "L":
                    s = index_by_deck.get(deck, {}).get(i)
                    if s:
                        seat_id = s.id
                        label = (s.number or "").strip() or fallback_label(numbers, i)
                        if s.id in ticketed_ids or s.is_occupied:
                            status = "occupied"
                        elif s.id in hold_by_seat:
                            status = "hold"
                            hold_user = getattr(hold_by_seat[s.id].user, "username", "")
                    else:
                        label = fallback_label(numbers, i)

                is_window = (c == 0) or (c == cols - 1)
                row_cells.append({
                    "type": t,
                    "label": label,
                    "status": status,
                    "hold_user": hold_user,
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


# ---------- VISTAS POS (para el admin) ----------
def pos_home(request):
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
    dest_id = request.GET.get("dest_id") or ""

    trips = Trip.objects.select_related("route", "bus", "route__origin", "route__destination") \
        .filter(departure__date=query_date) \
        .order_by("departure")

    if origin_id:
        trips = trips.filter(route__origin_id=origin_id)
    if dest_id:
        trips = trips.filter(route__destination_id=dest_id)

    sold_map = dict(
        Ticket.objects.filter(trip__in=trips).values("trip").annotate(c=Count("id")).values_list("trip", "c")
    )
    holds_qs = SeatHold.objects.filter(trip__in=trips, active=True).values("trip").annotate(c=Count("id"))
    hold_map = dict(holds_qs.values_list("trip", "c"))
    seats_map = dict(trips.values_list("id", "seats_total"))

    for t in trips:
        t.sold = int(sold_map.get(t.id, 0))
        t.hold = int(hold_map.get(t.id, 0))
        t.total = int(seats_map.get(t.id, 0))
        t.free = max(t.total - t.sold - t.hold, 0)

    ctx = {
        "title": "POS — Salidas",
        "date": sel_date,
        "cities": City.objects.all().order_by("name"),
        "origin_id": origin_id,
        "dest_id": dest_id,
        "trips": trips,
    }
    return render(request, "booking/pos_home.html", ctx)


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
        "change_url": "",
    }
    return render(request, "booking/seatmap.html", ctx)


# ============================================================
# ACCIÓN MASIVA PARA BUS
# ============================================================
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


# ============================================================
# ADMINS BÁSICOS (REGISTRO TEMPRANO)
# ============================================================
@admin.register(City)
class CityAdmin(admin.ModelAdmin):
    list_display = ("name", "slug")
    search_fields = ("name", "slug")


@admin.register(Company)
class CompanyAdmin(admin.ModelAdmin):
    list_display = ("name", "logo")
    search_fields = ("name",)


@admin.register(Terminal)
class TerminalAdmin(admin.ModelAdmin):
    list_display = ("name", "city", "address")
    search_fields = ("name", "city__name", "address")
    list_filter = ("city",)


@admin.register(Route)
class RouteAdmin(admin.ModelAdmin):
    list_display = ["origin", "destination", "duration_minutes", "base_price"]
    list_filter = ["origin", "destination"]
    search_fields = ("origin__name", "destination__name")


@admin.register(Seat)
class SeatAdmin(admin.ModelAdmin):
    list_display = ("bus", "deck", "row", "position", "number", "is_window", "is_occupied")
    search_fields = ("bus__plate", "number")
    list_filter = ("bus", "deck", "is_window", "is_occupied")


@admin.register(SeatHold)
class SeatHoldAdmin(admin.ModelAdmin):
    list_display = ("trip", "seat", "user", "active", "expires_at", "created_at")
    list_filter = ("active",)
    search_fields = (
        "trip__route__origin__name",
        "trip__route__destination__name",
        "user__username",
        "seat__number",
    )


@admin.register(Driver)
class DriverAdmin(admin.ModelAdmin):
    list_display = ("full_name", "rut", "phone", "email", "is_active")
    search_fields = ("full_name", "rut")
    list_filter = ("is_active",)


@admin.register(Assistant)
class AssistantAdmin(admin.ModelAdmin):
    list_display = ("full_name", "rut", "phone", "email", "is_active")
    search_fields = ("full_name", "rut")
    list_filter = ("is_active",)


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ('full_name', 'national_id', 'phone', 'email', 'created_at')
    search_fields = ('full_name', 'national_id', 'phone')
    ordering = ('full_name',)


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'role', 'terminal', 'is_active', 'commission_rate', 'max_discount', 'created_at')
    list_filter = ('role', 'is_active', 'terminal')
    search_fields = ('user__username', 'user__first_name', 'user__last_name', 'user__email')


@admin.register(DriverDocument)
class DriverDocumentAdmin(admin.ModelAdmin):
    list_display = ('driver', 'doc_type', 'expiry_date', 'document_number')
    list_filter = ('doc_type', 'expiry_date')
    search_fields = ('driver__full_name', 'driver__rut')


@admin.register(BusDocument)
class BusDocumentAdmin(admin.ModelAdmin):
    list_display = ('bus', 'doc_type', 'expiry_date', 'document_number')
    list_filter = ('doc_type', 'expiry_date')
    search_fields = ('bus__plate',)


@admin.register(Season)
class SeasonAdmin(admin.ModelAdmin):
    list_display = ('name', 'start_date', 'end_date', 'multiplier', 'is_active')
    list_filter = ('is_active',)
    search_fields = ('name',)


@admin.register(Promotion)
class PromotionAdmin(admin.ModelAdmin):
    list_display = [
        'code', 'name', 'discount_display', 'validity_display',
        'usage_display', 'is_active'
    ]
    list_filter = ['discount_type', 'is_active', 'valid_from', 'valid_to']
    search_fields = ['code', 'name']
    readonly_fields = ['used_count', 'created_at', 'updated_at']

    fieldsets = (
        ('Información Básica', {
            'fields': ('code', 'name', 'is_active')
        }),
        ('Descuento', {
            'fields': ('discount_type', 'discount_value', 'max_discount_amount', 'min_purchase_amount')
        }),
        ('Vigencia', {
            'fields': ('valid_from', 'valid_to')
        }),
        ('Límites de Uso', {
            'fields': ('max_uses', 'used_count')
        }),
        ('Metadatos', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        })
    )

    def discount_display(self, obj):
        if obj.discount_type == 'percentage':
            value = f"{obj.discount_value}%"
            if obj.max_discount_amount:
                value += f" (máx ${obj.max_discount_amount:,.0f})"
        else:
            value = f"${obj.discount_value:,.0f}"

        if obj.min_purchase_amount > 0:
            value += f" (min ${obj.min_purchase_amount:,.0f})"

        return value
    discount_display.short_description = 'Descuento'

    def validity_display(self, obj):
        from django.utils import timezone
        now = timezone.now().date()

        if not obj.is_active:
            return format_html('<span style="color: #64748b;">❌ Inactivo</span>')

        if obj.valid_from and obj.valid_from > now:
            return format_html(
                '<span style="color: #f59e0b;">⏳ Inicia: {}</span>',
                obj.valid_from.strftime('%d/%m/%Y')
            )

        if obj.valid_to and obj.valid_to < now:
            return format_html(
                '<span style="color: #e30613;">⚠️ Expirado: {}</span>',
                obj.valid_to.strftime('%d/%m/%Y')
            )

        return format_html(
            '<span style="color: #10b981;">✅ Vigente hasta: {}</span>',
            obj.valid_to.strftime('%d/%m/%Y') if obj.valid_to else 'Indefinido'
        )
    validity_display.short_description = 'Vigencia'

    def usage_display(self, obj):
        if obj.max_uses > 0:
            remaining = obj.max_uses - obj.used_count
            color = '#e30613' if remaining <= 0 else '#10b981'
            return format_html(
                '<span style="color: {};">{} / {} usos</span>',
                color, obj.used_count, obj.max_uses
            )
        return f"{obj.used_count} usos (ilimitado)"
    usage_display.short_description = 'Uso'

    actions = ['activate_promotions', 'deactivate_promotions']

    @admin.action(description="Activar promociones seleccionadas")
    def activate_promotions(self, request, queryset):
        updated = queryset.update(is_active=True)
        self.message_user(request, f"{updated} promoción(es) activada(s).")

    @admin.action(description="Desactivar promociones seleccionadas")
    def deactivate_promotions(self, request, queryset):
        updated = queryset.update(is_active=False)
        self.message_user(request, f"{updated} promoción(es) desactivada(s).")


@admin.register(Parcel)
class ParcelAdmin(admin.ModelAdmin):
    list_display = ('tracking_number', 'trip', 'sender_name', 'recipient_name', 'weight', 'price', 'status', 'created_at')
    list_filter = ('status', 'payment_method', 'trip')
    search_fields = ('tracking_number', 'sender_name', 'recipient_name', 'recipient_rut')
    readonly_fields = ('tracking_number', 'created_at')


@admin.register(FuelRecord)
class FuelRecordAdmin(admin.ModelAdmin):
    list_display = ('bus', 'date', 'liters', 'cost', 'mileage', 'created_by')
    list_filter = ('bus', 'date')
    search_fields = ('bus__plate',)


# ============================================================
# ADMIN DE TICKETS
# ============================================================
TICKET_FIELDS = {f.name for f in Ticket._meta.get_fields()}
HAS_BUYER_NAME = "buyer_name" in TICKET_FIELDS
HAS_PASSENGER_NAME = "passenger_name" in TICKET_FIELDS
HAS_PAID = "paid" in TICKET_FIELDS


@admin.register(Ticket)
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


# ============================================================
# ADMIN DE BUS - DEFINICIÓN (se registra antes que Trip y Maintenance)
# ============================================================
class BusAdmin(admin.ModelAdmin):
    change_form_template = "admin/booking_bus_change_form.html"

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
        ("Layout (avanzado)", {
            "fields": ("layout_lower", "layout_upper", "numbers_lower", "numbers_upper"),
            "classes": ("collapse",),
        }),
    )

    actions = ("regenerate_seats_action", "force_regenerate_seats")

    class Media:
        css = {
            "all": (
                "booking/admin-seatmap.css",
                "booking/layout-catalog.css",
            )
        }
        js = (
            "admin/js/jquery.init.js",
            "booking/bus_wizard.js",
            "booking/seatmap-modal.js",
            "booking/layout-catalog.js",
        )

    def seatmap_link(self, obj: Bus):
        if not obj.pk:
            return "-"
        url = reverse("admin:booking_bus_seatmap", args=[obj.pk])
        return format_html('<a class="button js-seatmap" href="{}" data-seatmap-url="{}">Mapa</a>', url, url)
    seatmap_link.short_description = "Mapa"

    def save_as_layout_link(self, obj: Bus):
        if not obj.pk:
            return "-"
        url = reverse("admin:booking_bus_save_as_layout", args=[obj.pk])
        return format_html('<a class="button" href="{}">Guardar como plantilla</a>', url)
    save_as_layout_link.short_description = "Guardar como plantilla"

    def save_model(self, request, obj: Bus, form, change):
        if obj.layout_template:
            template = obj.layout_template
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
                obj.ensure_layouts()
                created = obj.regenerate_seats()
                self.message_user(
                    request,
                    f"✅ Se generaron {created} asientos para {obj}.",
                    messages.SUCCESS
                )
            except Exception as e:
                error_msg = f"❌ Error generando asientos: {e}"
                self.message_user(request, error_msg, messages.ERROR)
                logger.error(f"Error regenerando asientos para bus {obj.pk}: {e}", exc_info=True)

        if request.method == 'POST' and not request.META.get('HTTP_X_REQUESTED_WITH'):
            transaction.on_commit(_regen)
        else:
            _regen()

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        """Personaliza el campo layout_template con preview."""
        if db_field.name == "layout_template":
            layouts = {l.id: l for l in BusLayout.objects.all()}

            class TemplateSelect(forms.Select):
                def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
                    option = super().create_option(name, value, label, selected, index, subindex, attrs)
                    real_value = getattr(value, "value", value)

                    if real_value and real_value in layouts:
                        layout = layouts[real_value]
                        option['attrs']['data-floors'] = layout.floors
                        option['attrs']['data-rows-lower'] = layout.rows_lower
                        option['attrs']['data-rows-upper'] = layout.rows_upper
                        option['attrs']['data-cols'] = layout.cols
                        option['attrs']['data-prefix-lower'] = layout.prefix_lower or ""
                        option['attrs']['data-prefix-upper'] = layout.prefix_upper or ""
                        option['attrs']['data-layout-lower'] = json.dumps(layout.layout_lower or [])
                        option['attrs']['data-layout-upper'] = json.dumps(layout.layout_upper or [])

                    return option

            kwargs['widget'] = TemplateSelect

        return super().formfield_for_foreignkey(db_field, request, **kwargs)

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
            path(
                "<int:bus_id>/editor/",
                self.admin_site.admin_view(self.editor_view),
                name="booking_bus_editor",
            ),
            path(
                "<int:bus_id>/save-layout/",
                self.admin_site.admin_view(self.save_layout_view),
                name="booking_bus_save_layout",
            ),
            path(
                "layout-catalog-content/",
                self.admin_site.admin_view(self.layout_catalog_content),
                name="booking_layout_catalog_content",
            ),
        ]
        return custom + urls

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

    def editor_view(self, request, bus_id: int):
        bus = get_object_or_404(Bus, pk=bus_id)
        bus.ensure_layouts()
        context = dict(
            self.admin_site.each_context(request),
            title=f"Editor de layout - {bus.plate}",
            bus=bus,
            rows=bus.rows_lower,
            cols=bus.cols,
            layout=json.dumps(bus.layout_lower or []),
            save_url=reverse("admin:booking_bus_save_layout", args=[bus.id]),
        )
        return render(request, "admin/seatmap_editor.html", context)

    def save_layout_view(self, request, bus_id: int):
        if request.method != "POST":
            return JsonResponse({"error": "Método no permitido"}, status=405)

        bus = get_object_or_404(Bus, pk=bus_id)
        try:
            data = json.loads(request.body)

            if 'layout_lower' in data:
                new_layout_lower = data.get('layout_lower', [])
                new_layout_upper = data.get('layout_upper', [])
                bus.layout_lower = new_layout_lower
                bus.layout_upper = new_layout_upper
                if 'rows_lower' in data:
                    bus.rows_lower = data['rows_lower']
                if 'rows_upper' in data:
                    bus.rows_upper = data['rows_upper']
                if 'cols' in data:
                    bus.cols = data['cols']
            else:
                new_layout = data.get('layout', [])
                if not isinstance(new_layout, list):
                    raise ValueError("El layout debe ser una lista")
                bus.layout_lower = new_layout

            bus.save()
            bus.ensure_layouts()
            bus.regenerate_seats()
            return JsonResponse({"status": "ok", "message": "Layout guardado y asientos regenerados"})
        except Exception as e:
            return JsonResponse({"status": "error", "message": str(e)}, status=400)

    def save_as_layout_view(self, request, bus_id: int):
        from django.shortcuts import redirect

        bus = get_object_or_404(Bus, pk=bus_id)
        try:
            bus.ensure_layouts()
        except Exception:
            pass

        base_name = f"Mapa — {bus.plate}"
        name = base_name
        i = 2
        while BusLayout.objects.filter(name=name).exists():
            name = f"{base_name} ({i})"
            i += 1

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
        return redirect(reverse("admin:booking_buslayout_change", args=[new_layout.pk]))

    def template_data_view(self, request, bus_id: int):
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

    def layout_catalog_content(self, request):
        layouts = BusLayout.objects.all()
        for l in layouts:
            preview = []
            flat = (l.layout_lower or [])[:30]
            for cell in flat:
                preview.append(cell if cell in ['L','P','X','E','D','B'] else 'L')
            l.preview_cells = preview
        return render(request, "admin/layout_catalog_modal.html", {"layouts": layouts})

    def regenerate_seats_action(self, request, queryset):
        """Regenera asientos desde layout."""
        return regenerate_seats_action(self, request, queryset)
    regenerate_seats_action.short_description = "Regenerar asientos desde layout"

    @admin.action(description="Forzar regeneración completa de asientos")
    def force_regenerate_seats(self, request, queryset):
        """Fuerza regeneración completa incluyendo layout_template."""
        total = 0
        errors = 0
        for bus in queryset:
            try:
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
                self.message_user(request, f"{bus.plate}: {created} asientos regenerados", messages.SUCCESS)
            except Exception as e:
                errors += 1
                self.message_user(request, f"{bus.plate}: Error - {e}", messages.ERROR)

        if errors == 0:
            self.message_user(request, f"Se regeneraron {total} asientos en {queryset.count()} bus(es).", messages.SUCCESS)
        else:
            self.message_user(request, f"Se regeneraron {total} asientos con {errors} error(es).", messages.WARNING)


# ============================================================
# REGISTRO DE BUSAdmin (CRÍTICO - Antes de Trip y Maintenance)
# ============================================================
admin.site.register(Bus, BusAdmin)


# ============================================================
# ADMIN DE VIAJES (Después de BusAdmin)
# ============================================================
@admin.register(Trip)
class TripAdmin(admin.ModelAdmin):
    list_display = ("route", "bus", "departure", "arrival", "driver1", "driver2", "assistant", "seats_total", "cutoff_minutes", "seatmap_link")
    search_fields = ("route__origin__name", "route__destination__name", "bus__plate")
    list_filter = ("route__origin", "route__destination", "bus", "cutoff_minutes")
    autocomplete_fields = ("bus",)

    fieldsets = (
        (None, {
            'fields': ('route', 'bus', 'departure', 'arrival', 'seats_total', 'cutoff_minutes', 'driver1', 'driver2', 'assistant')
        }),
    )

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


# ============================================================
# ADMIN DE MANTENIMIENTO (Después de BusAdmin)
# ============================================================
@admin.register(Maintenance)
class MaintenanceAdmin(admin.ModelAdmin):
    list_display = ('bus', 'date', 'maintenance_type', 'mileage', 'cost', 'next_maintenance_km')
    list_filter = ('maintenance_type', 'date', 'bus')
    search_fields = ('bus__plate', 'description', 'workshop')
    autocomplete_fields = ('bus',)


# ============================================================
# ADMIN DE PLANTILLAS (BusLayout)
# ============================================================
class BusLayoutAdminForm(forms.ModelForm):
    class Meta:
        model = BusLayout
        fields = "__all__"
        labels = {
            "layout_lower": "Piso inferior (layout)",
            "layout_upper": "Piso superior (layout)",
            "numbers_lower": "Números inferiores",
            "numbers_upper": "Números superiores",
            "rows_lower": "Filas piso inferior",
            "rows_upper": "Filas piso superior",
            "cols": "Asientos por fila",
            "floors": "Pisos",
            "prefix_lower": "Prefijo piso inferior",
            "prefix_upper": "Prefijo piso superior",
        }
        help_texts = {
            "layout_lower": "Lista lineal de celdas (L/P/X/E/D/B) con tamaño filas×columnas.",
            "layout_upper": "Lista lineal de celdas (L/P/X/E/D/B) con tamaño filas×columnas.",
            "numbers_lower": "Lista lineal con numeración (o vacío) del piso inferior.",
            "numbers_upper": "Lista lineal con numeración (o vacío) del piso superior.",
        }
        widgets = {
            "layout_lower": forms.Textarea(attrs={"rows": 7, "cols": 80}),
            "layout_upper": forms.Textarea(attrs={"rows": 7, "cols": 80}),
            "numbers_lower": forms.Textarea(attrs={"rows": 5, "cols": 80}),
            "numbers_upper": forms.Textarea(attrs={"rows": 5, "cols": 80}),
        }


@admin.register(BusLayout)
class BusLayoutAdmin(admin.ModelAdmin):
    form = BusLayoutAdminForm
    list_display = ("name", "floors", "rows_lower", "rows_upper", "cols", "seatmap_link", "duplicate_link")
    search_fields = ("name", "slug")
    fieldsets = (
        ("Identificación", {"fields": ("name",)}),
        ("Dimensiones", {"fields": ("floors", "rows_lower", "rows_upper", "cols")}),
        ("Layout (avanzado)", {"fields": ("layout_lower", "layout_upper", "numbers_lower", "numbers_upper")}),
        ("Numeración automática (opcional)", {"fields": ("prefix_lower", "prefix_upper")}),
    )

    actions = ("duplicate_selected_layouts",)

    class Media:
        css = {"all": ("booking/admin-seatmap.css",)}
        js = ("booking/seatmap-modal.js",)

    def seatmap_link(self, obj):
        url = reverse("admin:booking_layout_seatmap", args=[obj.pk])
        return format_html('<a class="button js-seatmap" href="{}" data-seatmap-url="{}">Mapa</a>', url, url)
    seatmap_link.short_description = "Mapa"

    def duplicate_link(self, obj):
        url = reverse("admin:booking_layout_duplicate", args=[obj.pk])
        return format_html('<a class="button" href="{}">Duplicar</a>', url)
    duplicate_link.short_description = "Duplicar"

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

    @xframe_options_exempt
    def seatmap_view(self, request, layout_id: int):
        layout = get_object_or_404(BusLayout, pk=layout_id)

        class _FakeBus:
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
        from django.shortcuts import redirect
        change_url = reverse(
            f"admin:{new_obj._meta.app_label}_{new_obj._meta.model_name}_change",
            args=[new_obj.pk],
        )
        return redirect(change_url)

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


# ============================================================
# ADMIN DE CAJA Y REPORTES
# ============================================================
@admin.register(CashRegister)
class CashRegisterAdmin(admin.ModelAdmin):
    list_display = ("user", "opening_date", "closing_date", "total_sales", "total_tickets", "status")
    list_filter = ("status", "opening_date", "user")
    search_fields = ("user__username",)
    readonly_fields = ("opening_date", "closing_date")

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('user')


@admin.register(DailyReport)
class DailyReportAdmin(admin.ModelAdmin):
    list_display = ("date", "total_tickets", "total_revenue", "total_cash_registers", "created_at")
    list_filter = ("date",)
    readonly_fields = ("created_at",)
    search_fields = ("date",)

    def has_add_permission(self, request):
        return False


# ============================================================
# ADMIN DE CONVENIOS
# ============================================================
@admin.register(CompanyContract)
class CompanyContractAdmin(admin.ModelAdmin):
    list_display = ('company', 'contract_number', 'credit_limit', 'used_credit', 'available_credit', 'is_active')
    list_filter = ('is_active', 'company')
    search_fields = ('company__name', 'contract_number')
    readonly_fields = ('used_credit', 'created_at', 'updated_at')
    fieldsets = (
        ('Datos del Contrato', {
            'fields': ('company', 'contract_number', 'is_active')
        }),
        ('Crédito', {
            'fields': ('credit_limit', 'used_credit', 'discount_percentage')
        }),
        ('Vigencia', {
            'fields': ('valid_from', 'valid_to')
        }),
        ('Contacto', {
            'fields': ('contact_name', 'contact_phone', 'contact_email')
        }),
        ('Observaciones', {
            'fields': ('notes',)
        }),
        ('Metadatos', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        })
    )

    def available_credit(self, obj):
        return obj.available_credit
    available_credit.short_description = 'Crédito disponible'


@admin.register(ContractEmployee)
class ContractEmployeeAdmin(admin.ModelAdmin):
    list_display = ('customer', 'contract', 'employee_id', 'is_active')
    list_filter = ('is_active', 'contract')
    search_fields = ('customer__full_name', 'customer__national_id', 'employee_id')
    autocomplete_fields = ('customer',)
    raw_id_fields = ('customer',)