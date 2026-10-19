# coordinator/workshop_views.py
"""
Vistas del módulo Taller Mecánico.
"""
from datetime import datetime, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Sum, Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.decorators import role_required

from booking.models import (
    Bus,
    Mechanic,
    MaintenanceOrder,
    MaintenanceOrderItem,
    Workshop,
)
from booking.access import get_user_scope


coordinator_required = role_required(['admin', 'coordinator'])


# ============================================================
# HELPER: filtrar queryset por empresa del usuario
# ============================================================
def _filter_by_company(request, queryset, company_field="company"):
    """Filtra un queryset según la empresa del usuario."""
    scope = get_user_scope(request.user)
    company = scope.get("company")
    if company:
        return queryset.filter(**{company_field: company})
    return queryset


# ============================================================
# DASHBOARD
# ============================================================
@login_required
@coordinator_required
def workshop_dashboard(request):
    """Panel principal del módulo taller."""
    scope = get_user_scope(request.user)
    company = scope.get("company")

    orders = _filter_by_company(request, MaintenanceOrder.objects.all())
    mechanics = _filter_by_company(request, Mechanic.objects.all())
    workshops = _filter_by_company(request, Workshop.objects.all())

    now = timezone.now()

    # KPIs
    open_orders = orders.filter(status__in=["open", "in_progress"]).count()
    done_this_month = orders.filter(
        status="done",
        exit_date__month=now.month,
        exit_date__year=now.year,
    ).count()
    active_mechanics = mechanics.filter(is_active=True).count()

    month_cost = orders.filter(
        status="done",
        exit_date__month=now.month,
        exit_date__year=now.year,
    ).aggregate(total=Sum("total_cost"))["total"] or 0

    recent_orders = orders.select_related(
        "bus", "workshop", "mechanic"
    ).order_by("-entry_date")[:10]

    context = {
        "company": company,
        "open_orders": open_orders,
        "done_this_month": done_this_month,
        "active_mechanics": active_mechanics,
        "month_cost": month_cost,
        "recent_orders": recent_orders,
        "total_workshops": workshops.count(),
        "total_mechanics": mechanics.count(),
    }
    return render(request, "coordinator/workshop/dashboard.html", context)


# ============================================================
# ÓRDENES
# ============================================================
@login_required
@coordinator_required
def workshop_order_list(request):
    """Lista de órdenes de taller con filtros."""
    orders = _filter_by_company(request, MaintenanceOrder.objects.all())

    # Filtros
    status = request.GET.get("status", "")
    workshop_id = request.GET.get("workshop", "")
    bus_id = request.GET.get("bus", "")
    q = request.GET.get("q", "").strip()

    if status:
        orders = orders.filter(status=status)
    if workshop_id:
        orders = orders.filter(workshop_id=workshop_id)
    if bus_id:
        orders = orders.filter(bus_id=bus_id)
    if q:
        orders = orders.filter(
            Q(bus__plate__icontains=q) |
            Q(description__icontains=q) |
            Q(notes__icontains=q)
        )

    orders = orders.select_related(
        "bus", "workshop", "mechanic", "chief", "electrician"
    ).order_by("-entry_date")

    # Para los filtros
    workshops = _filter_by_company(request, Workshop.objects.all())
    buses = _filter_by_company(request, Bus.objects.all()).order_by("plate")

    context = {
        "orders": orders,
        "workshops": workshops,
        "buses": buses,
        "current_status": status,
        "current_workshop": workshop_id,
        "current_bus": bus_id,
        "q": q,
        "status_choices": MaintenanceOrder.STATUS_CHOICES,
    }
    return render(request, "coordinator/workshop/order_list.html", context)


@login_required
@coordinator_required
def workshop_order_create(request):
    """Crear nueva orden de taller."""
    scope = get_user_scope(request.user)
    user_company = scope.get("company")

    if request.method == "POST":
        try:
            with transaction.atomic():
                # Tomar la empresa del bus seleccionado
                bus = get_object_or_404(
                    Bus,
                    pk=request.POST.get("bus"),
                )
                company = bus.company

                # Si el usuario tiene empresa, validar que el bus sea de su empresa
                if user_company and bus.company_id != user_company.id:
                    messages.error(
                        request,
                        "El bus seleccionado no pertenece a tu empresa.",
                    )
                    return redirect("coordinator:workshop_order_new")

                order = MaintenanceOrder.objects.create(
                    company=company,
                    workshop_id=request.POST.get("workshop"),
                    bus_id=request.POST.get("bus"),
                    entry_date=request.POST.get("entry_date"),
                    exit_date=request.POST.get("exit_date") or None,
                    odometer_in=request.POST.get("odometer_in") or None,
                    odometer_out=request.POST.get("odometer_out") or None,
                    reason=request.POST.get("reason"),
                    description=request.POST.get("description", ""),
                    chief_id=request.POST.get("chief") or None,
                    mechanic_id=request.POST.get("mechanic") or None,
                    electrician_id=request.POST.get("electrician") or None,
                    status=request.POST.get("status", "open"),
                    next_maintenance_date=request.POST.get("next_maintenance_date") or None,
                    next_maintenance_km=request.POST.get("next_maintenance_km") or None,
                    notes=request.POST.get("notes", ""),
                    created_by=request.user,
                )

                # Procesar items
                descriptions = request.POST.getlist("item_description")
                quantities = request.POST.getlist("item_quantity")
                prices = request.POST.getlist("item_price")
                is_labor_list = request.POST.getlist("item_is_labor")

                from decimal import Decimal, InvalidOperation

                for i, desc in enumerate(descriptions):
                    if not desc.strip():
                        continue

                    # Convertir cantidad a Decimal
                    try:
                        qty = Decimal(str(quantities[i]).strip() or "1")
                    except (InvalidOperation, IndexError):
                        qty = Decimal("1")

                    # Convertir precio a Decimal
                    try:
                        price = Decimal(str(prices[i]).strip() or "0")
                    except (InvalidOperation, IndexError):
                        price = Decimal("0")

                    MaintenanceOrderItem.objects.create(
                        order=order,
                        description=desc,
                        quantity=qty,
                        unit_price=price,
                        is_labor=str(i) in is_labor_list,
                    )

                messages.success(request, f"Orden OT-{order.id:05d} creada.")
                return redirect("coordinator:workshop_order_detail", order_id=order.id)

        except Exception as e:
            messages.error(request, f"Error al crear la orden: {e}")

    # Datos para el formulario
    workshops = _filter_by_company(request, Workshop.objects.filter(is_active=True))
    buses = _filter_by_company(request, Bus.objects.filter(is_active=True)).order_by("plate")
    mechanics = _filter_by_company(request, Mechanic.objects.filter(is_active=True))

    context = {
        "workshops": workshops,
        "buses": buses,
        "mechanics": mechanics,
        "reason_choices": MaintenanceOrder.REASON_CHOICES,
        "status_choices": MaintenanceOrder.STATUS_CHOICES,
        "is_new": True,
    }
    return render(request, "coordinator/workshop/order_form.html", context)


@login_required
@coordinator_required
def workshop_order_detail(request, order_id):
    """Detalle de una orden."""
    order = get_object_or_404(
        _filter_by_company(request, MaintenanceOrder.objects.all()),
        pk=order_id,
    )
    context = {"order": order}
    return render(request, "coordinator/workshop/order_detail.html", context)


@login_required
@coordinator_required
def workshop_order_edit(request, order_id):
    """Editar orden."""
    order = get_object_or_404(
        _filter_by_company(request, MaintenanceOrder.objects.all()),
        pk=order_id,
    )

    if request.method == "POST":
        try:
            with transaction.atomic():
                order.workshop_id = request.POST.get("workshop")
                order.bus_id = request.POST.get("bus")
                order.entry_date = request.POST.get("entry_date")
                order.exit_date = request.POST.get("exit_date") or None
                order.odometer_in = request.POST.get("odometer_in") or None
                order.odometer_out = request.POST.get("odometer_out") or None
                order.reason = request.POST.get("reason")
                order.description = request.POST.get("description", "")
                order.chief_id = request.POST.get("chief") or None
                order.mechanic_id = request.POST.get("mechanic") or None
                order.electrician_id = request.POST.get("electrician") or None
                order.status = request.POST.get("status")
                order.next_maintenance_date = request.POST.get("next_maintenance_date") or None
                order.next_maintenance_km = request.POST.get("next_maintenance_km") or None
                order.notes = request.POST.get("notes", "")
                order.save()

                # Borrar items anteriores y recrear
                order.items.all().delete()

                descriptions = request.POST.getlist("item_description")
                quantities = request.POST.getlist("item_quantity")
                prices = request.POST.getlist("item_price")
                is_labor_list = request.POST.getlist("item_is_labor")

                from decimal import Decimal, InvalidOperation

                for i, desc in enumerate(descriptions):
                    if not desc.strip():
                        continue

                    try:
                        qty = Decimal(str(quantities[i]).strip() or "1")
                    except (InvalidOperation, IndexError):
                        qty = Decimal("1")

                    try:
                        price = Decimal(str(prices[i]).strip() or "0")
                    except (InvalidOperation, IndexError):
                        price = Decimal("0")

                    MaintenanceOrderItem.objects.create(
                        order=order,
                        description=desc,
                        quantity=qty,
                        unit_price=price,
                        is_labor=str(i) in is_labor_list,
                    )

                messages.success(request, f"Orden OT-{order.id:05d} actualizada.")
                return redirect("coordinator:workshop_order_detail", order_id=order.id)

        except Exception as e:
            messages.error(request, f"Error al actualizar: {e}")

    workshops = _filter_by_company(request, Workshop.objects.filter(is_active=True))
    buses = _filter_by_company(request, Bus.objects.all()).order_by("plate")
    mechanics = _filter_by_company(request, Mechanic.objects.filter(is_active=True))

    context = {
        "order": order,
        "workshops": workshops,
        "buses": buses,
        "mechanics": mechanics,
        "reason_choices": MaintenanceOrder.REASON_CHOICES,
        "status_choices": MaintenanceOrder.STATUS_CHOICES,
        "is_new": False,
    }
    return render(request, "coordinator/workshop/order_form.html", context)


@login_required
@coordinator_required
@require_POST
def workshop_order_close(request, order_id):
    """Cerrar una orden (cambiar a done)."""
    order = get_object_or_404(
        _filter_by_company(request, MaintenanceOrder.objects.all()),
        pk=order_id,
    )
    order.status = "done"
    order.exit_date = order.exit_date or timezone.now()
    order.save()
    messages.success(request, f"Orden OT-{order.id:05d} cerrada.")
    return redirect("coordinator:workshop_order_detail", order_id=order.id)


# ============================================================
# MECÁNICOS
# ============================================================
@login_required
@coordinator_required
def workshop_mechanic_list(request):
    """Lista de mecánicos."""
    mechanics = _filter_by_company(request, Mechanic.objects.all()).select_related(
        "workshop", "company"
    ).order_by("last_name", "first_name")

    context = {
        "mechanics": mechanics,
        "role_choices": Mechanic.ROLE_CHOICES,
    }
    return render(request, "coordinator/workshop/mechanic_list.html", context)


@login_required
@coordinator_required
def workshop_mechanic_create(request):
    """Crear mecánico."""
    scope = get_user_scope(request.user)
    user_company = scope.get("company")

    if request.method == "POST":
        try:
            # Tomar la empresa del taller seleccionado
            workshop_id = request.POST.get("workshop")
            if workshop_id:
                workshop = get_object_or_404(Workshop, pk=workshop_id)
                company = workshop.company
            else:
                company = user_company

            if not company:
                messages.error(request, "No se pudo determinar la empresa.")
                return redirect("coordinator:workshop_mechanic_list")

            Mechanic.objects.create(
                company=company,
                workshop_id=request.POST.get("workshop") or None,
                first_name=request.POST.get("first_name"),
                last_name=request.POST.get("last_name"),
                rut=request.POST.get("rut", ""),
                phone=request.POST.get("phone", ""),
                email=request.POST.get("email", ""),
                role=request.POST.get("role", "mechanic"),
                is_active=request.POST.get("is_active") == "on",
            )
            messages.success(request, "Mecánico creado.")
            return redirect("coordinator:workshop_mechanic_list")
        except Exception as e:
            messages.error(request, f"Error: {e}")

    workshops = _filter_by_company(request, Workshop.objects.filter(is_active=True))
    context = {
        "workshops": workshops,
        "role_choices": Mechanic.ROLE_CHOICES,
        "is_new": True,
    }
    return render(request, "coordinator/workshop/mechanic_form.html", context)


@login_required
@coordinator_required
def workshop_mechanic_edit(request, mechanic_id):
    """Editar mecánico."""
    mechanic = get_object_or_404(
        _filter_by_company(request, Mechanic.objects.all()),
        pk=mechanic_id,
    )

    if request.method == "POST":
        try:
            mechanic.workshop_id = request.POST.get("workshop") or None
            mechanic.first_name = request.POST.get("first_name")
            mechanic.last_name = request.POST.get("last_name")
            mechanic.rut = request.POST.get("rut", "")
            mechanic.phone = request.POST.get("phone", "")
            mechanic.email = request.POST.get("email", "")
            mechanic.role = request.POST.get("role")
            mechanic.is_active = request.POST.get("is_active") == "on"
            mechanic.save()
            messages.success(request, "Mecánico actualizado.")
            return redirect("coordinator:workshop_mechanic_list")
        except Exception as e:
            messages.error(request, f"Error: {e}")

    workshops = _filter_by_company(request, Workshop.objects.filter(is_active=True))
    context = {
        "mechanic": mechanic,
        "workshops": workshops,
        "role_choices": Mechanic.ROLE_CHOICES,
        "is_new": False,
    }
    return render(request, "coordinator/workshop/mechanic_form.html", context)


# ============================================================
# TALLERES
# ============================================================
@login_required
@coordinator_required
def workshop_list(request):
    """Lista de talleres."""
    workshops = _filter_by_company(request, Workshop.objects.all()).order_by("name")
    context = {"workshops": workshops}
    return render(request, "coordinator/workshop/workshop_list.html", context)


@login_required
@coordinator_required
def workshop_create(request):
    """Crear taller."""
    scope = get_user_scope(request.user)
    company = scope.get("company")

    # Si el usuario no tiene empresa (superuser), permitir elegir
    # (por ahora usamos la primera empresa activa)
    if not company:
        from booking.models import Company
        company = Company.objects.first()

    if request.method == "POST":
        try:
            Workshop.objects.create(
                company=company,
                name=request.POST.get("name"),
                code=request.POST.get("code", ""),
                address=request.POST.get("address", ""),
                phone=request.POST.get("phone", ""),
                email=request.POST.get("email", ""),
                is_internal=request.POST.get("is_internal") == "on",
                is_active=request.POST.get("is_active") == "on",
            )
            messages.success(request, "Taller creado.")
            return redirect("coordinator:workshop_list")
        except Exception as e:
            messages.error(request, f"Error: {e}")

    context = {"is_new": True}
    return render(request, "coordinator/workshop/workshop_form.html", context)


@login_required
@coordinator_required
def workshop_edit(request, workshop_id):
    """Editar taller."""
    workshop = get_object_or_404(
        _filter_by_company(request, Workshop.objects.all()),
        pk=workshop_id,
    )

    if request.method == "POST":
        try:
            workshop.name = request.POST.get("name")
            workshop.code = request.POST.get("code", "")
            workshop.address = request.POST.get("address", "")
            workshop.phone = request.POST.get("phone", "")
            workshop.email = request.POST.get("email", "")
            workshop.is_internal = request.POST.get("is_internal") == "on"
            workshop.is_active = request.POST.get("is_active") == "on"
            workshop.save()
            messages.success(request, "Taller actualizado.")
            return redirect("coordinator:workshop_list")
        except Exception as e:
            messages.error(request, f"Error: {e}")

    context = {"workshop": workshop, "is_new": False}
    return render(request, "coordinator/workshop/workshop_form.html", context)


@login_required
@coordinator_required
def workshop_reports(request):
    """Reportes del taller."""
    scope = get_user_scope(request.user)
    company = scope.get("company")

    # Filtros base
    orders = MaintenanceOrder.objects.all()
    if company:
        orders = orders.filter(company=company)

    # ============================================================
    # 1. Top 10 buses que más gastan
    # ============================================================
    top_buses_by_cost = (
        orders.filter(status="done")
        .values("bus__plate", "bus__id")
        .annotate(total_cost_sum=Sum("total_cost"), orders_count=Count("id"))
        .order_by("-total_cost_sum")[:10]
    )

    # ============================================================
    # 2. Top 10 buses que más entran al taller
    # ============================================================
    top_buses_by_orders = (
        orders.values("bus__plate", "bus__id")
        .annotate(orders_count=Count("id"), total_cost_sum=Sum("total_cost"))
        .order_by("-orders_count")[:10]
    )

    # ============================================================
    # 3. Costos por mes (últimos 6 meses)
    # ============================================================
    from django.db.models.functions import TruncMonth
    from datetime import timedelta

    six_months_ago = timezone.now() - timedelta(days=180)
    costs_by_month = (
        orders.filter(entry_date__gte=six_months_ago)
        .annotate(month=TruncMonth("entry_date"))
        .values("month")
        .annotate(total=Sum("total_cost"), count=Count("id"))
        .order_by("-month")[:6]
    )

    # ============================================================
    # 4. Ranking de mecánicos
    # ============================================================
    top_mechanics = (
        Mechanic.objects
        .filter(company=company) if company else Mechanic.objects.all()
    )
    top_mechanics = top_mechanics.annotate(
        orders_as_mechanic_count=Count(
            "orders_as_mechanic",
            filter=Q(orders_as_mechanic__status="done"),
        ),
        orders_as_chief_count=Count(
            "orders_as_chief",
            filter=Q(orders_as_chief__status="done"),
        ),
    ).order_by("-orders_as_mechanic_count")[:10]

    # ============================================================
    # 5. Órdenes por estado
    # ============================================================
    orders_by_status = {
        "open": orders.filter(status="open").count(),
        "in_progress": orders.filter(status="in_progress").count(),
        "done": orders.filter(status="done").count(),
        "cancelled": orders.filter(status="cancelled").count(),
    }

    context = {
        "company": company,
        "top_buses_by_cost": top_buses_by_cost,
        "top_buses_by_orders": top_buses_by_orders,
        "costs_by_month": costs_by_month,
        "top_mechanics": top_mechanics,
        "orders_by_status": orders_by_status,
        "total_orders": orders.count(),
        "total_cost": orders.aggregate(t=Sum("total_cost"))["t"] or 0,
    }
    return render(request, "coordinator/workshop/reports.html", context)