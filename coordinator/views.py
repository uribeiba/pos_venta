# coordinator/views.py
import json
import os
import subprocess
import calendar
from datetime import datetime, timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import transaction, IntegrityError
from django.db.models import ProtectedError, Count, Q, Exists, OuterRef, Sum, Avg
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
    Maintenance, Route, Seat, SeatHold, Terminal, Ticket, Trip, Agency,
    Parcel, FuelRecord, BusLayout, AuditLog, User
)
from booking.forms import (
    TerminalForm, CityForm, RouteForm, RouteStopFormSet,
    AssistantForm, DriverForm, AgencyForm, BusFullForm, TripForm
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


def validate_trip_conflicts(route, bus, driver1_id, driver2_id, departure_dt, arrival_dt, exclude_trip=None):
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
    Panel principal del coordinador con métricas y viajes próximos.
    """
    # Limpiar mensajes de la sesión
    storage = messages.get_messages(request)
    storage.used = True
    list(storage)

    now = timezone.now()
    today = now.date()

    # ===== MÉTRICAS DE RESUMEN =====
    # ✅ GUARDAR EL QUERYSET PARA PODER USAR .count() Y .exists()
    viajes_hoy_qs = Trip.objects.filter(departure__date=today)
    viajes_hoy = viajes_hoy_qs.count()  # Esto es un int
    
    proxima_semana = today + timedelta(days=7)
    buses_activos = Trip.objects.filter(
        departure__date__range=[today, proxima_semana]
    ).values('bus').distinct().count()
    
    pasajeros_hoy = Ticket.objects.filter(trip__departure__date=today).count()

    # ✅ USAR EL QUERYSET PARA LA OCUPACIÓN
    trips_hoy = viajes_hoy_qs.select_related(
        'route__origin', 'route__destination', 'bus', 'driver1'
    ).annotate(
        sold_count=Count('tickets')
    )
    
    ocupacion_promedio = 0
    if trips_hoy.exists():  # ✅ .exists() funciona en QuerySet
        total_ocupacion = sum(
            (trip.sold_count / trip.seats_total * 100) if trip.seats_total else 0
            for trip in trips_hoy
        )
        ocupacion_promedio = round(total_ocupacion / trips_hoy.count(), 1)

    # ✅ VIAJES PRÓXIMOS (QuerySet)
    proximas_24h = now + timedelta(hours=24)
    viajes_proximos_qs = Trip.objects.filter(
        departure__gte=now,
        departure__lte=proximas_24h
    ).select_related(
        'route__origin', 'route__destination', 'bus', 'driver1'
    ).annotate(
        sold_count=Count('tickets')
    ).order_by('departure')

    viajes_data = []
    for trip in viajes_proximos_qs:
        libres = trip.seats_total - trip.sold_count
        estado = "Próximo" if trip.departure > now else "En curso"
        viajes_data.append({
            'id': trip.id,
            'hora': trip.departure.strftime('%H:%M'),
            'ruta': f"{trip.route.origin.name} → {trip.route.destination.name}",
            'bus': trip.bus.plate,
            'chofer': trip.driver1.full_name if trip.driver1 else "Sin asignar",
            'asientos_totales': trip.seats_total,
            'asientos_libres': libres,
            'asientos_ocupados': trip.sold_count,
            'estado': estado,
            'estado_color': 'success' if estado == 'Próximo' else 'warning',
        })

    context = {
        'title': 'Dashboard - Coordinador',
        'viajes_hoy': viajes_hoy,  # ✅ Esto es un int
        'buses_activos': buses_activos,  # ✅ Esto es un int
        'pasajeros_hoy': pasajeros_hoy,  # ✅ Esto es un int
        'ocupacion_promedio': ocupacion_promedio,  # ✅ Esto es un float
        'viajes_proximos': viajes_data,  # ✅ Esto es una lista
    }
    return render(request, 'coordinator/dashboard.html', context)

# ============================================================================
# VISTAS DE BUSES
# ============================================================================

@login_required
@coordinator_required
def bus_list(request):
    """Lista todos los buses con su empresa asociada."""
    buses = Bus.objects.select_related('company').all().order_by('company__name', 'plate')
    return render(request, 'coordinator/bus_list.html', {'buses': buses})


@login_required
@coordinator_required
def bus_editor(request, bus_id=None):
    """
    Editor avanzado de buses con configuración de layout de asientos.
    Permite crear o modificar la distribución de pisos, filas, columnas y números.
    """
    if bus_id:
        bus = get_object_or_404(Bus, pk=bus_id)
    else:
        bus = Bus()

    if request.method == 'POST':
        def safe_json_loads(val):
            try:
                return json.loads(val) if val else []
            except Exception:
                return []

        try:
            # Transacción atómica para toda la operación
            with transaction.atomic():
                old_bus = Bus.objects.filter(pk=bus.pk).first() if bus.pk else None

                bus.company_id = request.POST.get('company')
                bus.plate = request.POST.get('plate', '').upper().strip()
                bus.model = request.POST.get('model', '').strip()

                year_val = request.POST.get('year')
                bus.year = int(year_val) if year_val and year_val.isdigit() else 2024

                bus.floors = int(request.POST.get('floors', 1))
                bus.rows_lower = int(request.POST.get('rows_lower', 5))
                bus.rows_upper = int(request.POST.get('rows_upper', 0))
                bus.cols = int(request.POST.get('cols', 4))

                bus.prefix_lower = request.POST.get('prefix_lower', '')
                bus.prefix_upper = request.POST.get('prefix_upper', '')

                bus.technical_review_expiry = request.POST.get('technical_review_expiry') or None
                bus.insurance_expiry = request.POST.get('insurance_expiry') or None
                bus.permit_expiry = request.POST.get('permit_expiry') or None
                bus.last_maintenance = request.POST.get('last_maintenance') or None

                s_low = bus.rows_lower * bus.cols
                s_upp = bus.rows_upper * bus.cols

                l_low = safe_json_loads(request.POST.get('layout_lower'))
                l_upp = safe_json_loads(request.POST.get('layout_upper'))
                n_low = safe_json_loads(request.POST.get('numbers_lower'))
                n_upp = safe_json_loads(request.POST.get('numbers_upper'))
                svc_low = safe_json_loads(request.POST.get('services_lower'))
                svc_upp = safe_json_loads(request.POST.get('services_upper'))

                bus.layout_lower = (l_low + ['L'] * s_low)[:s_low]
                bus.numbers_lower = (n_low + [''] * s_low)[:s_low]
                bus.services_lower = (svc_low + ['semi_cama'] * s_low)[:s_low]

                if bus.floors == 2:
                    bus.layout_upper = (l_upp + ['L'] * s_upp)[:s_upp]
                    bus.numbers_upper = (n_upp + [''] * s_upp)[:s_upp]
                    bus.services_upper = (svc_upp + ['semi_cama'] * s_upp)[:s_upp]
                else:
                    bus.layout_upper, bus.numbers_upper, bus.services_upper = [], [], []

                last_counter = 1
                bus.numbers_lower, last_counter = assign_missing_numbers(
                    bus.numbers_lower, bus.layout_lower, bus.prefix_lower, last_counter
                )
                if bus.floors == 2:
                    bus.numbers_upper, _ = assign_missing_numbers(
                        bus.numbers_upper, bus.layout_upper, bus.prefix_upper, last_counter
                    )

                bus.save()

                # Verificar si hay cambios estructurales
                structure_changed = False
                if old_bus:
                    structure_changed = (
                        old_bus.rows_lower != bus.rows_lower or
                        old_bus.rows_upper != bus.rows_upper or
                        old_bus.cols != bus.cols or
                        old_bus.floors != bus.floors
                    )
                else:
                    structure_changed = True

                if structure_changed:
                    future_trips = Trip.objects.filter(bus=bus, departure__gt=timezone.now()).exists()
                    if future_trips:
                        messages.warning(
                            request,
                            "No se regeneraron los asientos porque el bus tiene viajes futuros. "
                            "Para aplicar cambios estructurales, primero reasigna los viajes."
                        )
                    else:
                        created = bus.regenerate_seats()
                        messages.success(request, f'Estructura de asientos regenerada para {bus.plate}. {created} asientos creados.')

                messages.success(request, f'Bus {bus.plate} guardado correctamente.')
                return redirect('coordinator:bus_list')

        except Exception as e:
            messages.error(request, f'Error: {str(e)}')

    companies = Company.objects.all()
    context = {
        'bus': bus,
        'companies': companies,
        'layout_lower_json': json.dumps(bus.layout_lower or []),
        'layout_upper_json': json.dumps(bus.layout_upper or []),
        'numbers_lower_json': json.dumps(bus.numbers_lower or []),
        'numbers_upper_json': json.dumps(bus.numbers_upper or []),
        'services_lower_json': json.dumps(bus.services_lower or []),
        'services_upper_json': json.dumps(bus.services_upper or []),
    }
    return render(request, 'coordinator/bus_editor.html', context)


@login_required
@coordinator_required
def buses_dashboard(request):
    """Tablero unificado para la gestión rápida de buses."""
    bus_to_edit = None
    edit_id = request.GET.get('edit')
    if edit_id:
        bus_to_edit = get_object_or_404(Bus, pk=edit_id)

    if request.method == 'POST':
        form = BusFullForm(request.POST, instance=bus_to_edit) if bus_to_edit else BusFullForm(request.POST)
        if form.is_valid():
            bus = form.save()
            bus.ensure_layouts()
            messages.success(request, f'Datos del bus {bus.plate} actualizados correctamente.')
            return redirect('coordinator:buses_dashboard')
        else:
            messages.error(request, f'Error al guardar: {form.errors}')
    else:
        form = BusFullForm(instance=bus_to_edit) if bus_to_edit else BusFullForm()

    query = request.GET.get('q', '').strip()
    buses_list = Bus.objects.select_related('company').all().order_by('company__name', 'plate')
    if query:
        buses_list = buses_list.filter(
            Q(plate__icontains=query) | Q(model__icontains=query) |
            Q(owner_first_name__icontains=query) | Q(owner_last_name__icontains=query) |
            Q(brand__icontains=query)
        )

    paginator = Paginator(buses_list, 10)
    buses_page = paginator.get_page(request.GET.get('page'))

    context = {
        'form': form,
        'buses': buses_page,
        'query': query,
        'edit_mode': bool(bus_to_edit),
        'bus_edit_id': bus_to_edit.id if bus_to_edit else None,
    }
    return render(request, 'buses/buses_full.html', context)


@login_required
@coordinator_required
def bus_duplicate(request, bus_id):
    """Duplica un bus existente."""
    original = get_object_or_404(Bus, pk=bus_id)
    new_bus = Bus()
    for field in ['company', 'model', 'year', 'floors', 'rows_lower', 'rows_upper', 'cols',
                  'layout_lower', 'layout_upper', 'numbers_lower', 'numbers_upper',
                  'services_lower', 'services_upper', 'prefix_lower', 'prefix_upper']:
        setattr(new_bus, field, getattr(original, field))
    new_bus.plate = f"{original.plate} (COPIA)"
    new_bus.save()
    new_bus.ensure_layouts()
    new_bus.regenerate_seats()
    messages.success(request, f'Bus duplicado con éxito: {new_bus.plate}')
    return redirect('coordinator:bus_editor', bus_id=new_bus.id)


@login_required
@coordinator_required
@require_POST
def bus_delete(request, bus_id):
    """Elimina un bus. Si cascade=True, también elimina viajes, tickets y asientos asociados."""
    try:
        bus = get_object_or_404(Bus, pk=bus_id)
        cascade = request.POST.get('cascade') == 'true' or request.GET.get('cascade') == 'true'

        if cascade:
            with transaction.atomic():
                trips = Trip.objects.filter(bus=bus)
                Ticket.objects.filter(trip__in=trips).delete()
                SeatHold.objects.filter(trip__in=trips).delete()
                trips.delete()
                Seat.objects.filter(bus=bus).delete()
                bus.delete()
            return JsonResponse({'success': True, 'cascade': True})
        else:
            bus.delete()
            return JsonResponse({'success': True})

    except ProtectedError as e:
        protected_objects = e.protected_objects
        trips = [obj for obj in protected_objects if isinstance(obj, Trip)]
        tickets = [obj for obj in protected_objects if isinstance(obj, Ticket)]
        return JsonResponse({
            'success': False,
            'protected': True,
            'message': f'El bus tiene {len(trips)} viaje(s) y {len(tickets)} ticket(s) asociados.',
            'trips_count': len(trips),
            'tickets_count': len(tickets)
        }, status=409)
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})


@login_required
@coordinator_required
@require_POST
def bus_delete_massive(request):
    """Elimina múltiples buses en lote."""
    try:
        data = json.loads(request.body)
        ids = data.get('ids', [])
        cascade = data.get('cascade', False)

        if not ids:
            return JsonResponse({'success': False, 'error': 'No se seleccionaron buses'})

        buses = Bus.objects.filter(id__in=ids)
        deleted_count = 0
        errors = []

        with transaction.atomic():
            for bus in buses:
                try:
                    if cascade:
                        trips = Trip.objects.filter(bus=bus)
                        Ticket.objects.filter(trip__in=trips).delete()
                        SeatHold.objects.filter(trip__in=trips).delete()
                        trips.delete()
                        Seat.objects.filter(bus=bus).delete()
                        bus.delete()
                    else:
                        bus.delete()
                    deleted_count += 1
                except ProtectedError as e:
                    protected_objects = e.protected_objects
                    trips_count = len([obj for obj in protected_objects if isinstance(obj, Trip)])
                    tickets_count = len([obj for obj in protected_objects if isinstance(obj, Ticket)])
                    errors.append(f"{bus.plate}: tiene {trips_count} viaje(s) y {tickets_count} ticket(s)")
                except Exception as e:
                    errors.append(f"{bus.plate}: {str(e)}")

        if errors:
            return JsonResponse({'success': False, 'partial': True, 'deleted': deleted_count, 'errors': errors})
        return JsonResponse({'success': True, 'deleted': deleted_count})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})


@login_required
@coordinator_required
def api_bus_data(request, bus_id):
    """API que devuelve los datos de layout de un bus en formato JSON."""
    bus = get_object_or_404(Bus, pk=bus_id)
    bus.ensure_layouts()
    return JsonResponse({
        'id': bus.id,
        'floors': bus.floors,
        'rows_lower': bus.rows_lower,
        'rows_upper': bus.rows_upper,
        'cols': bus.cols,
        'layout_lower': bus.layout_lower,
        'layout_upper': bus.layout_upper,
        'numbers_lower': bus.numbers_lower,
        'numbers_upper': bus.numbers_upper,
        'services_lower': bus.services_lower,
        'services_upper': bus.services_upper,
        'prefix_lower': bus.prefix_lower,
        'prefix_upper': bus.prefix_upper,
    })


# ============================================================================
# GESTIÓN DE VIAJES
# ============================================================================

@login_required
@coordinator_required
def trip_list(request):
    """Lista todos los viajes con origen, destino y bus."""
    trips = Trip.objects.select_related('route__origin', 'route__destination', 'bus').all().order_by('-departure')
    return render(request, 'coordinator/trip_list.html', {'trips': trips})


@login_required
@coordinator_required
def trip_create_edit(request, trip_id=None):
    """Crear o editar un viaje individual."""
    trip = get_object_or_404(Trip, pk=trip_id) if trip_id else Trip()

    if request.method == 'POST':
        try:
            route_id = request.POST.get('route')
            bus_id = request.POST.get('bus')
            departure = make_aware_datetime(request.POST.get('departure'), "Salida")
            
            trip.route_id = route_id
            trip.bus_id = bus_id
            trip.departure = departure
            
            if request.POST.get('arrival'):
                trip.arrival = make_aware_datetime(request.POST.get('arrival'), "Llegada")
            
            trip.driver1_id = request.POST.get('driver1') or None
            trip.driver2_id = request.POST.get('driver2') or None
            trip.assistant_id = request.POST.get('assistant') or None

            if trip.bus_id:
                trip.seats_total = Seat.objects.filter(bus_id=trip.bus_id).count()
            else:
                trip.seats_total = 0

            if not trip.arrival and trip.route_id:
                route = Route.objects.get(pk=trip.route_id)
                trip.arrival = trip.departure + timedelta(minutes=route.duration_minutes)

            # Validar conflictos
            route = trip.route
            bus = trip.bus
            conflicts = validate_trip_conflicts(
                route, bus, trip.driver1_id, trip.driver2_id,
                trip.departure, trip.arrival, exclude_trip=trip if trip.pk else None
            )
            if conflicts:
                for conflict in conflicts:
                    messages.error(request, conflict)
                return redirect('coordinator:trip_create_edit', trip_id=trip.pk if trip.pk else None)

            trip.save()
            messages.success(request, 'Viaje guardado correctamente.')
            return redirect('coordinator:trip_list')

        except ValidationError as e:
            messages.error(request, str(e))
        except Exception as e:
            messages.error(request, f'Error: {str(e)}')

    routes = Route.objects.select_related('origin', 'destination').all()
    buses = Bus.objects.all()
    drivers = Driver.objects.filter(is_active=True).order_by('full_name')
    assistants = Assistant.objects.filter(is_active=True).order_by('full_name')

    return render(request, 'coordinator/trip_form.html', {
        'trip': trip,
        'routes': routes,
        'buses': buses,
        'drivers': drivers,
        'assistants': assistants,
    })


@login_required
@coordinator_required
def trips_dashboard(request):
    """Tablero de gestión de viajes con filtros y paginación."""
    # Limpiar mensajes antiguos
    storage = messages.get_messages(request)
    storage.used = True
    list(storage)

    trip_to_edit = None
    edit_id = request.GET.get('edit')
    if edit_id:
        trip_to_edit = get_object_or_404(Trip, pk=edit_id)

    if request.method == 'POST':
        form = TripForm(request.POST, instance=trip_to_edit) if trip_to_edit else TripForm(request.POST)
        if form.is_valid():
            trip = form.save(commit=False)
            if not trip.arrival and trip.route:
                trip.arrival = trip.departure + timedelta(minutes=trip.route.duration_minutes)
            if trip.bus:
                trip.seats_total = Seat.objects.filter(bus=trip.bus).count()
            
            # Validar conflictos
            conflicts = validate_trip_conflicts(
                trip.route, trip.bus, trip.driver1_id, trip.driver2_id,
                trip.departure, trip.arrival, exclude_trip=trip if trip.pk else None
            )
            if conflicts:
                for conflict in conflicts:
                    messages.error(request, conflict)
                return redirect('coordinator:trips_dashboard')
            
            trip.save()
            messages.success(request, f'Viaje {trip} procesado con éxito.')
            return redirect('coordinator:trips_dashboard')
        else:
            messages.error(request, f'Error al guardar: {form.errors}')
    else:
        form = TripForm(instance=trip_to_edit) if trip_to_edit else TripForm()

    query = request.GET.get('q', '').strip()
    trips_list = Trip.objects.select_related(
        'route__origin', 'route__destination', 'bus', 'driver1', 'driver2', 'assistant'
    ).all().order_by('-departure')

    if query:
        trips_list = trips_list.filter(
            Q(route__origin__name__icontains=query) | Q(route__destination__name__icontains=query) |
            Q(bus__plate__icontains=query) | Q(driver1__full_name__icontains=query)
        )

    paginator = Paginator(trips_list, 10)
    trips_page = paginator.get_page(request.GET.get('page'))

    context = {
        'form': form,
        'trips': trips_page,
        'query': query,
        'edit_mode': bool(trip_to_edit),
        'trip_edit_id': trip_to_edit.id if trip_to_edit else None,
    }
    return render(request, 'viajes/viajes.html', context)


@login_required
@coordinator_required
def trip_change_bus(request, trip_id):
    """Reasigna un viaje a un bus diferente."""
    from booking.views import _build_trip_grid
    
    trip = get_object_or_404(
        Trip.objects.select_related('bus', 'route').select_for_update(),
        pk=trip_id
    )

    if request.method == 'POST':
        new_bus_id = request.POST.get('new_bus')
        if not new_bus_id:
            messages.error(request, "Debe seleccionar un bus nuevo.")
            return redirect('coordinator:trip_change_bus', trip_id=trip.id)

        # Bloquear el bus nuevo para evitar race conditions
        new_bus = get_object_or_404(Bus.objects.select_for_update(), pk=new_bus_id)
        reassign_map = {k.split('_')[1]: v for k, v in request.POST.items() if k.startswith('seat_')}

        try:
            with transaction.atomic():
                # Bloquear todos los tickets y asientos involucrados
                tickets = Ticket.objects.filter(trip=trip).select_related('seat').select_for_update()
                
                if not tickets.exists():
                    trip.bus = new_bus
                    trip.seats_total = Seat.objects.filter(bus=new_bus).count()
                    trip.save()
                    messages.success(request, f"Bus cambiado a {new_bus.plate} (sin pasajeros).")
                    return redirect('coordinator:trip_list')

                missing = [ticket.seat.number for ticket in tickets if str(ticket.seat.id) not in reassign_map]
                if missing:
                    raise ValidationError(f"Asientos sin reasignar: {', '.join(missing)}")

                # Bloquear los asientos de destino
                new_seat_ids = list(reassign_map.values())
                new_seats = Seat.objects.filter(pk__in=new_seat_ids, bus=new_bus).select_for_update()
                new_seats_dict = {str(s.id): s for s in new_seats}

                for ticket in tickets:
                    new_seat_id = reassign_map.get(str(ticket.seat.id))
                    if not new_seat_id or new_seat_id not in new_seats_dict:
                        raise ValidationError(f"Asiento destino no encontrado para {ticket.seat.number}")

                    new_seat = new_seats_dict[new_seat_id]
                    
                    # Verificar que el asiento no esté ocupado
                    if Ticket.objects.filter(trip=trip, seat=new_seat).exists():
                        raise ValidationError(f"El asiento {new_seat.number} ya está ocupado.")

                # Ejecutar la reasignación
                for ticket in tickets:
                    new_seat = new_seats_dict[reassign_map[str(ticket.seat.id)]]
                    old_seat = ticket.seat
                    old_seat.is_occupied = False
                    old_seat.save(update_fields=['is_occupied'])

                    ticket.seat = new_seat
                    ticket.save()

                    new_seat.is_occupied = True
                    new_seat.save(update_fields=['is_occupied'])

                trip.bus = new_bus
                trip.seats_total = Seat.objects.filter(bus=new_bus).count()
                trip.save()

                messages.success(request, f"Viaje reasignado a bus {new_bus.plate}.")
                return redirect('coordinator:trip_list')

        except ValidationError as e:
            messages.error(request, str(e))
        except Exception as e:
            messages.error(request, f"Error inesperado: {str(e)}")

        return redirect('coordinator:trip_change_bus', trip_id=trip.id)

    buses = Bus.objects.exclude(pk=trip.bus.pk).order_by('company__name', 'plate')
    current_lower, current_upper, cols = _build_trip_grid(trip)

    context = {
        'trip': trip,
        'buses': buses,
        'current_lower': current_lower,
        'current_upper': current_upper,
        'cols': cols,
    }
    return render(request, 'coordinator/trip_change_bus.html', context)


@login_required
@coordinator_required
def trip_delete(request, trip_id):
    """Elimina un viaje solo si no tiene tickets vendidos."""
    try:
        trip = Trip.objects.get(pk=trip_id)
    except Trip.DoesNotExist:
        messages.warning(request, 'El viaje que intenta eliminar ya no existe.')
        return redirect('coordinator:trips_dashboard')

    if Ticket.objects.filter(trip=trip).exists():
        messages.error(request, 'No se puede eliminar un viaje con tickets vendidos.')
        return redirect('coordinator:trips_dashboard')

    trip.delete()
    messages.success(request, 'Viaje eliminado correctamente.')
    return redirect('coordinator:trips_dashboard')


# ============================================================================
# GENERACIÓN MASIVA DE VIAJES Y CALENDARIO
# ============================================================================

@login_required
@coordinator_required
def generate_trips(request):
    """
    Genera múltiples viajes recurrentes en un rango de fechas.
    Incluye validaciones de conflictos de horario para bus y choferes.
    """
    from django.utils.dateparse import parse_date

    today = timezone.now().date()
    year = int(request.GET.get('year', today.year))
    month = int(request.GET.get('month', today.month))
    if month < 1 or month > 12:
        month = today.month
    if year < 2000 or year > 2100:
        year = today.year

    cal = calendar.monthcalendar(year, month)
    first_day = datetime(year, month, 1).date()
    last_day = (datetime(year, month + 1, 1) - timedelta(days=1)).date() if month < 12 else datetime(year, 12, 31).date()

    existing_trips = Trip.objects.filter(
        departure__date__range=(first_day, last_day)
    ).values_list('departure__date', flat=True).distinct()
    existing_dates = set(existing_trips)

    month_days = []
    for week in cal:
        week_days = []
        for day in week:
            if day == 0:
                week_days.append(None)
            else:
                date_obj = datetime(year, month, day).date()
                has_trip = date_obj in existing_dates
                week_days.append({'day': day, 'date': date_obj, 'has_trip': has_trip})
        month_days.append(week_days)

    if request.method == 'POST':
        is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'

        route_id = request.POST.get('route')
        bus_id = request.POST.get('bus')
        driver1_id = request.POST.get('driver1') or None
        driver2_id = request.POST.get('driver2') or None
        assistant_id = request.POST.get('assistant') or None
        departure_hour = request.POST.get('departure_hour')
        start_date_str = request.POST.get('start_date')
        end_date_str = request.POST.get('end_date')
        weekdays = request.POST.getlist('weekdays')

        errors = []
        
        # Validaciones básicas
        if not route_id:
            errors.append("Debe seleccionar una ruta.")
        if not bus_id:
            errors.append("Debe seleccionar un bus.")
        if not departure_hour:
            errors.append("Debe ingresar la hora de salida.")
        if not start_date_str or not end_date_str:
            errors.append("Debe ingresar fecha inicio y fecha fin.")
        if not weekdays:
            errors.append("Debe seleccionar al menos un día de la semana.")

        # Validaciones avanzadas
        if not errors:
            try:
                start_date = parse_date(start_date_str)
                end_date = parse_date(end_date_str)
                hour, minute = map(int, departure_hour.split(':'))
                selected_weekdays = [int(d) for d in weekdays]
                
                if start_date > end_date:
                    errors.append("La fecha de inicio no puede ser posterior a la fecha fin.")
                
                if hour < 0 or hour > 23 or minute < 0 or minute > 59:
                    errors.append("La hora de salida debe ser válida (00:00 - 23:59).")
                    
            except ValueError:
                errors.append("Formato de fecha u hora inválido.")
            except Exception as e:
                errors.append(f"Error en datos: {str(e)}")

        # Validaciones de negocio
        if not errors:
            try:
                route = Route.objects.select_related('origin', 'destination').get(pk=route_id)
                bus = Bus.objects.get(pk=bus_id)
                driver1 = Driver.objects.filter(pk=driver1_id).first() if driver1_id else None
                driver2 = Driver.objects.filter(pk=driver2_id).first() if driver2_id else None
                
                if not route.is_active:
                    errors.append("La ruta seleccionada no está activa.")
                
                if not bus.is_active:
                    errors.append("El bus seleccionado no está activo.")
                
                if route.origin == route.destination:
                    errors.append("El origen y destino de la ruta no pueden ser iguales.")
                
                if driver1_id and driver1 and not driver1.is_active:
                    errors.append("El chofer principal no está activo.")
                
                if driver2_id and driver2 and not driver2.is_active:
                    errors.append("El chofer secundario no está activo.")
                
                if assistant_id:
                    assistant = Assistant.objects.filter(pk=assistant_id, is_active=True).first()
                    if not assistant:
                        errors.append("El auxiliar seleccionado no está activo.")
                
            except Route.DoesNotExist:
                errors.append("La ruta seleccionada no existe.")
            except Bus.DoesNotExist:
                errors.append("El bus seleccionado no existe.")
            except Exception as e:
                errors.append(f"Error validando datos: {str(e)}")

        if errors:
            error_msg = " | ".join(errors)
            if is_ajax:
                return JsonResponse({'success': False, 'error': error_msg}, status=400)
            for err in errors:
                messages.error(request, err)
            return redirect('coordinator:generate_trips')

        created_count = 0
        skipped_count = 0
        conflict_count = 0
        current_date = start_date
        delta = timedelta(days=1)

        try:
            with transaction.atomic():
                route = Route.objects.select_for_update().get(pk=route_id)
                bus = Bus.objects.select_for_update().get(pk=bus_id)
                total_seats = Seat.objects.filter(bus=bus).count()

                # Pre-cargar viajes existentes para evitar múltiples consultas
                existing_trips_dict = {}
                trips_in_range = Trip.objects.filter(
                    bus_id=bus_id,
                    departure__date__range=(start_date, end_date)
                ).select_related('route', 'bus', 'driver1', 'driver2')
                
                for trip in trips_in_range:
                    date_key = trip.departure.date()
                    if date_key not in existing_trips_dict:
                        existing_trips_dict[date_key] = []
                    existing_trips_dict[date_key].append(trip)

                while current_date <= end_date:
                    day_of_week = current_date.isoweekday()
                    if day_of_week in selected_weekdays:
                        departure_dt = timezone.make_aware(
                            datetime.combine(current_date, datetime.strptime(departure_hour, '%H:%M').time())
                        )
                        arrival_dt = departure_dt + timedelta(minutes=route.duration_minutes)

                        # Verificar si ya existe un viaje en esa fecha y hora
                        existing_on_date = existing_trips_dict.get(current_date, [])
                        existing_trip = None
                        for trip in existing_on_date:
                            trip_time = trip.departure.time()
                            departure_time = departure_dt.time()
                            time_diff_hours = abs(
                                (trip_time.hour * 60 + trip_time.minute) - 
                                (departure_time.hour * 60 + departure_time.minute)
                            ) / 60
                            if time_diff_hours <= 1:
                                existing_trip = trip
                                break

                        if existing_trip:
                            skipped_count += 1
                            current_date += delta
                            continue

                        # Validar conflictos de bus
                        bus_conflict = Trip.objects.filter(
                            bus=bus,
                            departure__lt=arrival_dt,
                            arrival__gt=departure_dt
                        ).exclude(departure__date=current_date).exists()
                        
                        if bus_conflict:
                            conflict_count += 1
                            current_date += delta
                            continue

                        # Validar conflictos de chofer principal
                        driver1_conflict = False
                        if driver1_id:
                            driver1_conflict = Trip.objects.filter(
                                driver1_id=driver1_id,
                                departure__lt=arrival_dt,
                                arrival__gt=departure_dt
                            ).exclude(departure__date=current_date).exists()
                        
                        if driver1_conflict:
                            conflict_count += 1
                            current_date += delta
                            continue

                        # Validar conflictos de chofer secundario
                        driver2_conflict = False
                        if driver2_id:
                            driver2_conflict = Trip.objects.filter(
                                driver2_id=driver2_id,
                                departure__lt=arrival_dt,
                                arrival__gt=departure_dt
                            ).exclude(departure__date=current_date).exists()
                        
                        if driver2_conflict:
                            conflict_count += 1
                            current_date += delta
                            continue

                        # Crear el viaje
                        Trip.objects.create(
                            route=route,
                            bus=bus,
                            driver1_id=driver1_id,
                            driver2_id=driver2_id,
                            assistant_id=assistant_id,
                            departure=departure_dt,
                            arrival=arrival_dt,
                            seats_total=total_seats,
                        )
                        created_count += 1
                        
                    current_date += delta

            # Mensaje de resultado
            message_parts = []
            if created_count > 0:
                message_parts.append(f"✅ {created_count} viajes creados")
            if skipped_count > 0:
                message_parts.append(f"⏭️ {skipped_count} omitidos (ya existían)")
            if conflict_count > 0:
                message_parts.append(f"⚠️ {conflict_count} con conflictos de horario (no creados)")
            
            if not message_parts:
                message_parts.append("No se crearon viajes. Verifica los parámetros.")

            message = " | ".join(message_parts)
            
            if is_ajax:
                return JsonResponse({
                    'success': True, 
                    'message': message,
                    'stats': {
                        'created': created_count,
                        'skipped': skipped_count,
                        'conflicts': conflict_count
                    }
                })
            else:
                if created_count > 0:
                    messages.success(request, message)
                else:
                    messages.warning(request, message)
                return redirect('coordinator:trips_dashboard')

        except Exception as e:
            if is_ajax:
                return JsonResponse({'success': False, 'error': str(e)}, status=500)
            messages.error(request, f"Error al generar viajes: {str(e)}")
            return redirect('coordinator:generate_trips')

    routes = Route.objects.select_related('origin', 'destination').filter(is_active=True)
    buses = Bus.objects.filter(is_active=True)
    drivers = Driver.objects.filter(is_active=True)
    assistants = Assistant.objects.filter(is_active=True)

    context = {
        'routes': routes,
        'buses': buses,
        'drivers': drivers,
        'assistants': assistants,
        'year': year,
        'month': month,
        'month_days': month_days,
        'first_day': first_day,
        'last_day': last_day,
        'today': today,
        'existing_dates': existing_dates,
        'weekday_choices': [
            (1, 'Lunes'), (2, 'Martes'), (3, 'Miércoles'),
            (4, 'Jueves'), (5, 'Viernes'), (6, 'Sábado'), (7, 'Domingo')
        ],
    }
    return render(request, 'coordinator/generate_trips.html', context)


@login_required
@coordinator_required
def api_trips_calendar(request):
    """
    Endpoint para FullCalendar: devuelve viajes en el rango de fechas.
    """
    start_str = request.GET.get('start')
    end_str = request.GET.get('end')
    if not start_str or not end_str:
        return JsonResponse([], safe=False)

    try:
        start = timezone.make_aware(datetime.fromisoformat(start_str.replace('Z', '+00:00')))
        end = timezone.make_aware(datetime.fromisoformat(end_str.replace('Z', '+00:00')))
    except Exception:
        return JsonResponse([], safe=False)

    trips = Trip.objects.filter(departure__range=(start, end)).select_related(
        'route__origin', 'route__destination', 'bus'
    ).annotate(
        has_tickets=Exists(Ticket.objects.filter(trip=OuterRef('pk')))
    )

    events = []
    for trip in trips:
        events.append({
            'id': trip.id,
            'title': f"{trip.route.origin.name} → {trip.route.destination.name}",
            'start': trip.departure.isoformat(),
            'end': trip.arrival.isoformat() if trip.arrival else None,
            'extendedProps': {
                'id': trip.id,
                'route': str(trip.route),
                'bus': trip.bus.plate,
                'departure_date': trip.departure.strftime('%Y-%m-%d'),
                'departure_time': trip.departure.strftime('%H:%M'),
                'has_tickets': trip.has_tickets,
            }
        })
    return JsonResponse(events, safe=False)


@login_required
@coordinator_required
@require_POST
def delete_trip_by_date(request):
    """Elimina un viaje específico (usado desde el calendario)."""
    trip_id = request.POST.get('trip_id')
    if not trip_id:
        return JsonResponse({'success': False, 'error': 'ID de viaje no proporcionado'})
    trip = get_object_or_404(Trip, pk=trip_id)
    if Ticket.objects.filter(trip=trip).exists():
        return JsonResponse({'success': False, 'error': 'El viaje tiene tickets vendidos, no se puede eliminar.'})
    trip.delete()
    return JsonResponse({'success': True, 'message': 'Viaje eliminado correctamente.'})


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

@login_required
@coordinator_required
def route_list(request):
    routes = Route.objects.select_related("origin", "destination", "origin_terminal", "destination_terminal").all()
    return render(request, "coordinator/route_list.html", {"routes": routes})


@login_required
@coordinator_required
def route_create_edit(request, route_id=None):
    route = get_object_or_404(Route, pk=route_id) if route_id else Route()
    if request.method == "POST":
        route.origin_id = request.POST.get("origin")
        route.destination_id = request.POST.get("destination")
        route.origin_terminal_id = request.POST.get("origin_terminal") or None
        route.destination_terminal_id = request.POST.get("destination_terminal") or None
        route.duration_minutes = int(request.POST.get("duration_minutes", 120))
        route.base_price = Decimal(request.POST.get("base_price", 0))
        route.save()
        messages.success(request, f"Ruta {route} guardada.")
        return redirect("coordinator:route_list")
    cities = City.objects.all()
    terminals = Terminal.objects.all()
    return render(request, "coordinator/route_form.html", {
        "route": route,
        "cities": cities,
        "terminals": terminals
    })


@login_required
@coordinator_required
def route_delete(request, route_id):
    route = get_object_or_404(Route, pk=route_id)
    route.delete()
    messages.success(request, "Ruta eliminada.")
    return redirect("coordinator:route_list")


@login_required
@coordinator_required
def routes_dashboard(request):
    route_to_edit = None
    edit_id = request.GET.get('edit')
    if edit_id:
        route_to_edit = get_object_or_404(Route, pk=edit_id)

    if request.method == 'POST':
        if route_to_edit:
            form = RouteForm(request.POST, instance=route_to_edit)
            formset = RouteStopFormSet(request.POST, instance=route_to_edit)
        else:
            form = RouteForm(request.POST)
            formset = RouteStopFormSet(request.POST)

        if form.is_valid() and formset.is_valid():
            route = form.save()
            formset.instance = route
            formset.save()
            messages.success(request, f'Ruta {route} guardada correctamente.')
            return redirect('coordinator:routes_dashboard')
        else:
            messages.error(request, 'Por favor corrige los errores del formulario.')
    else:
        if route_to_edit:
            form = RouteForm(instance=route_to_edit)
            formset = RouteStopFormSet(instance=route_to_edit)
        else:
            form = RouteForm()
            formset = RouteStopFormSet()

    query = request.GET.get('q', '').strip()
    routes_list = Route.objects.select_related('origin', 'destination').all().order_by('origin__name', 'destination__name')
    if query:
        routes_list = routes_list.filter(
            Q(origin__name__icontains=query) | Q(destination__name__icontains=query)
        )

    paginator = Paginator(routes_list, 10)
    routes_page = paginator.get_page(request.GET.get('page'))

    context = {
        'form': form,
        'formset': formset,
        'routes': routes_page,
        'query': query,
        'edit_mode': bool(route_to_edit),
        'route_edit_id': route_to_edit.id if route_to_edit else None,
        'cities': City.objects.all(),
        'terminals': Terminal.objects.all(),
    }
    return render(request, 'rutas/rutas.html', context)


# ============================================================================
# GESTIÓN DE CHOFERES
# ============================================================================

@login_required
@coordinator_required
def driver_list(request):
    drivers = Driver.objects.all().order_by("full_name")
    return render(request, "coordinator/driver_list.html", {"drivers": drivers})


@login_required
@coordinator_required
def driver_create_edit(request, driver_id=None):
    driver = get_object_or_404(Driver, pk=driver_id) if driver_id else Driver()
    if request.method == "POST":
        driver.medical_cert_expiry = request.POST.get('medical_cert_expiry') or None
        driver.background_check_expiry = request.POST.get('background_check_expiry') or None
        driver.notes = request.POST.get('notes', '')
        driver.full_name = request.POST.get("full_name")
        driver.rut = request.POST.get("rut")
        driver.email = request.POST.get("email", "")
        driver.phone = request.POST.get("phone", "")
        driver.license_number = request.POST.get("license_number", "")
        driver.is_active = "is_active" in request.POST
        if 'photo' in request.FILES:
            driver.photo = request.FILES['photo']
        driver.save()
        messages.success(request, f"Chofer {driver.full_name} guardado.")
        return redirect("coordinator:driver_list")
    return render(request, "coordinator/driver_form.html", {"driver": driver})


@login_required
@coordinator_required
def driver_delete(request, driver_id):
    driver = get_object_or_404(Driver, pk=driver_id)
    driver.delete()
    messages.success(request, "Chofer eliminado.")
    return redirect("coordinator:driver_list")


@login_required
@coordinator_required
def drivers_dashboard(request):
    driver_to_edit = None
    edit_id = request.GET.get('edit')
    if edit_id:
        driver_to_edit = get_object_or_404(Driver, pk=edit_id)

    if request.method == 'POST':
        form = DriverForm(request.POST, request.FILES, instance=driver_to_edit) if driver_to_edit else DriverForm(request.POST, request.FILES)
        if form.is_valid():
            driver = form.save()
            messages.success(request, f'Chofer {driver.full_name} guardado.')
            return redirect('coordinator:drivers_dashboard')
        else:
            messages.error(request, 'Por favor corrige los errores del formulario.')
    else:
        form = DriverForm(instance=driver_to_edit) if driver_to_edit else DriverForm()

    query = request.GET.get('q', '').strip()
    drivers_list = Driver.objects.all().order_by('-is_active', 'full_name')
    if query:
        drivers_list = drivers_list.filter(Q(full_name__icontains=query) | Q(rut__icontains=query))

    paginator = Paginator(drivers_list, 10)
    drivers_page = paginator.get_page(request.GET.get('page'))

    context = {
        'form': form,
        'drivers': drivers_page,
        'query': query,
        'edit_mode': bool(driver_to_edit),
        'driver_edit_id': driver_to_edit.id if driver_to_edit else None,
    }
    return render(request, 'choferes/choferes.html', context)


# ============================================================================
# GESTIÓN DE AUXILIARES
# ============================================================================

@login_required
@coordinator_required
def assistant_list(request):
    assistants = Assistant.objects.all().order_by("full_name")
    return render(request, "coordinator/assistant_list.html", {"assistants": assistants})


@login_required
@coordinator_required
def assistant_create_edit(request, assistant_id=None):
    assistant = get_object_or_404(Assistant, pk=assistant_id) if assistant_id else Assistant()
    if request.method == "POST":
        assistant.full_name = request.POST.get("full_name")
        assistant.rut = request.POST.get("rut")
        assistant.email = request.POST.get("email", "")
        assistant.phone = request.POST.get("phone", "")
        assistant.is_active = "is_active" in request.POST
        if 'photo' in request.FILES:
            assistant.photo = request.FILES['photo']
        assistant.save()
        messages.success(request, f"Auxiliar {assistant.full_name} guardado.")
        return redirect("coordinator:assistant_list")
    return render(request, "coordinator/assistant_form.html", {"assistant": assistant})


@login_required
@coordinator_required
def assistant_delete(request, assistant_id):
    assistant = get_object_or_404(Assistant, pk=assistant_id)
    assistant.delete()
    messages.success(request, "Auxiliar eliminado.")
    return redirect("coordinator:assistant_list")


@login_required
@coordinator_required
def assistants_dashboard(request):
    """Tablero de gestión de auxiliares con paginación y búsqueda."""
    assistant_to_edit = None
    edit_id = request.GET.get('edit')
    if edit_id:
        assistant_to_edit = get_object_or_404(Assistant, pk=edit_id)

    if request.method == 'POST':
        form = AssistantForm(request.POST, request.FILES, instance=assistant_to_edit) if assistant_to_edit else AssistantForm(request.POST, request.FILES)
        if form.is_valid():
            assistant = form.save()
            messages.success(request, f'Auxiliar {assistant.full_name} guardado.')
            return redirect('coordinator:assistants_dashboard')
        else:
            messages.error(request, 'Por favor corrige los errores del formulario.')
    else:
        form = AssistantForm(instance=assistant_to_edit) if assistant_to_edit else AssistantForm()

    query = request.GET.get('q', '').strip()
    assistants_list = Assistant.objects.all().order_by('-is_active', 'full_name')
    if query:
        assistants_list = assistants_list.filter(Q(full_name__icontains=query) | Q(rut__icontains=query))

    paginator = Paginator(assistants_list, 10)
    assistants_page = paginator.get_page(request.GET.get('page'))

    context = {
        'form': form,
        'assistants': assistants_page,
        'query': query,
        'edit_mode': bool(assistant_to_edit),
        'assistant_edit_id': assistant_to_edit.id if assistant_to_edit else None,
    }
    return render(request, 'auxiliares/assistants.html', context)


# ============================================================================
# GESTIÓN DE AGENCIAS
# ============================================================================

@login_required
@coordinator_required
def agencies_dashboard(request):
    agency_to_edit = None
    edit_id = request.GET.get('edit')
    if edit_id:
        agency_to_edit = get_object_or_404(Agency, pk=edit_id)

    if request.method == 'POST':
        form = AgencyForm(request.POST, instance=agency_to_edit) if agency_to_edit else AgencyForm(request.POST)
        if form.is_valid():
            agency = form.save()
            messages.success(request, f'Agencia "{agency.name}" guardada.')
            return redirect('coordinator:agencies_dashboard')
        else:
            messages.error(request, 'Por favor corrige los errores del formulario.')
    else:
        form = AgencyForm(instance=agency_to_edit) if agency_to_edit else AgencyForm()

    query = request.GET.get('q', '').strip()
    agencies_list = Agency.objects.select_related('city').all().order_by('name')
    if query:
        agencies_list = agencies_list.filter(
            Q(name__icontains=query) | Q(city__name__icontains=query) | Q(address__icontains=query)
        )

    paginator = Paginator(agencies_list, 10)
    agencies_page = paginator.get_page(request.GET.get('page'))

    context = {
        'form': form,
        'agencies': agencies_page,
        'query': query,
        'edit_mode': bool(agency_to_edit),
        'agency_edit_id': agency_to_edit.id if agency_to_edit else None,
    }
    return render(request, 'agencias/agencias.html', context)


@login_required
@coordinator_required
def agency_delete(request, agency_id):
    agency = get_object_or_404(Agency, pk=agency_id)
    agency.delete()
    messages.success(request, "Agencia eliminada correctamente.")
    return redirect('coordinator:agencies_dashboard')


# ============================================================================
# DOCUMENTOS DE PERSONAL Y FLOTA
# ============================================================================

@login_required
@coordinator_required
def driver_documents(request, driver_id):
    driver = get_object_or_404(Driver, pk=driver_id)
    documents = DriverDocument.objects.filter(driver=driver).order_by('expiry_date')
    return render(request, 'coordinator/driver_documents.html', {'driver': driver, 'documents': documents})


@login_required
@coordinator_required
def driver_document_create(request, driver_id):
    driver = get_object_or_404(Driver, pk=driver_id)
    if request.method == 'POST':
        DriverDocument.objects.create(
            driver=driver,
            doc_type=request.POST.get('doc_type'),
            document_number=request.POST.get('doc_number', ''),
            issue_date=request.POST.get('issue_date') or None,
            expiry_date=request.POST.get('expiry_date'),
            notes=request.POST.get('notes', '')
        )
        messages.success(request, f'Documento agregado a {driver.full_name}')
        return redirect('coordinator:driver_documents', driver_id=driver.id)
    return render(request, 'coordinator/driver_document_form.html', {'driver': driver})


@login_required
@coordinator_required
def driver_document_edit(request, doc_id):
    doc = get_object_or_404(DriverDocument, pk=doc_id)
    if request.method == 'POST':
        doc.doc_type = request.POST.get('doc_type')
        doc.document_number = request.POST.get('doc_number', '')
        doc.issue_date = request.POST.get('issue_date') or None
        doc.expiry_date = request.POST.get('expiry_date')
        doc.notes = request.POST.get('notes', '')
        doc.save()
        messages.success(request, 'Documento actualizado')
        return redirect('coordinator:driver_documents', driver_id=doc.driver.id)
    return render(request, 'coordinator/driver_document_form.html', {'doc': doc, 'driver': doc.driver})


@login_required
@coordinator_required
def driver_document_delete(request, doc_id):
    doc = get_object_or_404(DriverDocument, pk=doc_id)
    driver_id = doc.driver.id
    doc.delete()
    messages.success(request, 'Documento eliminado')
    return redirect('coordinator:driver_documents', driver_id=driver_id)


@login_required
@coordinator_required
def bus_documents(request, bus_id):
    bus = get_object_or_404(Bus, pk=bus_id)
    documents = BusDocument.objects.filter(bus=bus).order_by('expiry_date')
    return render(request, 'coordinator/bus_documents.html', {'bus': bus, 'documents': documents})


@login_required
@coordinator_required
def bus_document_create(request, bus_id):
    bus = get_object_or_404(Bus, pk=bus_id)
    if request.method == 'POST':
        BusDocument.objects.create(
            bus=bus,
            doc_type=request.POST.get('doc_type'),
            document_number=request.POST.get('doc_number', ''),
            issue_date=request.POST.get('issue_date') or None,
            expiry_date=request.POST.get('expiry_date'),
            notes=request.POST.get('notes', '')
        )
        messages.success(request, f'Documento agregado a bus {bus.plate}')
        return redirect('coordinator:bus_documents', bus_id=bus.id)
    return render(request, 'coordinator/bus_document_form.html', {'bus': bus})


@login_required
@coordinator_required
def bus_document_edit(request, doc_id):
    doc = get_object_or_404(BusDocument, pk=doc_id)
    if request.method == 'POST':
        doc.doc_type = request.POST.get('doc_type')
        doc.document_number = request.POST.get('doc_number', '')
        doc.issue_date = request.POST.get('issue_date') or None
        doc.expiry_date = request.POST.get('expiry_date')
        doc.notes = request.POST.get('notes', '')
        doc.save()
        messages.success(request, 'Documento actualizado')
        return redirect('coordinator:bus_documents', bus_id=doc.bus.id)
    return render(request, 'coordinator/bus_document_form.html', {'doc': doc, 'bus': doc.bus})


@login_required
@coordinator_required
def bus_document_delete(request, doc_id):
    doc = get_object_or_404(BusDocument, pk=doc_id)
    bus_id = doc.bus.id
    doc.delete()
    messages.success(request, 'Documento eliminado')
    return redirect('coordinator:bus_documents', bus_id=bus_id)


@login_required
@coordinator_required
def expiring_documents(request):
    today = timezone.now().date()
    warning_days = 30
    expiry_limit = today + timedelta(days=warning_days)

    driver_docs = DriverDocument.objects.filter(
        expiry_date__isnull=False,
        expiry_date__gte=today,
        expiry_date__lte=expiry_limit
    ).select_related('driver').order_by('expiry_date')

    bus_docs = BusDocument.objects.filter(
        expiry_date__isnull=False,
        expiry_date__gte=today,
        expiry_date__lte=expiry_limit
    ).select_related('bus').order_by('expiry_date')

    expired_driver = DriverDocument.objects.filter(
        expiry_date__isnull=False,
        expiry_date__lt=today
    ).select_related('driver').order_by('expiry_date')

    expired_bus = BusDocument.objects.filter(
        expiry_date__isnull=False,
        expiry_date__lt=today
    ).select_related('bus').order_by('expiry_date')

    context = {
        'driver_docs': driver_docs,
        'bus_docs': bus_docs,
        'expired_driver': expired_driver,
        'expired_bus': expired_bus,
        'warning_days': warning_days,
        'today': today,
    }
    return render(request, 'coordinator/expiring_documents.html', context)


# ============================================================================
# DETALLE DE VIAJE Y BUS
# ============================================================================

@login_required
@coordinator_required
def trip_detail(request, trip_id):
    trip = get_object_or_404(
        Trip.objects.select_related(
            'route__origin', 'route__destination', 'bus',
            'driver1', 'driver2', 'assistant'
        ).annotate(
            total_tickets=Count('tickets'),
            lower_tickets=Count('tickets', filter=Q(tickets__seat__deck=1)),
            upper_tickets=Count('tickets', filter=Q(tickets__seat__deck=2)),
        ),
        pk=trip_id
    )

    tickets = Ticket.objects.filter(trip=trip).select_related('seat', 'customer')

    total_tickets = trip.total_tickets
    lower_occupied = trip.lower_tickets
    upper_occupied = trip.upper_tickets if trip.bus.floors == 2 else 0

    lower_seats = Seat.objects.filter(bus=trip.bus, deck=1).count()
    upper_seats = Seat.objects.filter(bus=trip.bus, deck=2).count() if trip.bus.floors == 2 else 0

    occupancy_rate = (total_tickets / trip.seats_total * 100) if trip.seats_total else 0
    lower_occupancy = (lower_occupied / lower_seats * 100) if lower_seats else 0
    upper_occupancy = (upper_occupied / upper_seats * 100) if upper_seats else 0

    context = {
        'trip': trip,
        'tickets': tickets,
        'occupancy_rate': round(occupancy_rate, 1),
        'lower_occupancy': round(lower_occupancy, 1),
        'upper_occupancy': round(upper_occupancy, 1),
    }
    return render(request, 'coordinator/trip_detail.html', context)


@login_required
@coordinator_required
def bus_detail(request, bus_id):
    bus = get_object_or_404(Bus, pk=bus_id)
    trips = Trip.objects.filter(bus=bus).order_by('-departure')[:20]
    total_passengers = Ticket.objects.filter(trip__in=trips).count()

    occupancy_by_route = {}
    for trip in trips:
        key = f"{trip.route.origin.name}→{trip.route.destination.name}"
        occupancy_by_route[key] = occupancy_by_route.get(key, 0) + 1

    context = {
        'bus': bus,
        'trips': trips,
        'total_passengers': total_passengers,
        'occupancy_by_route': occupancy_by_route,
    }
    return render(request, 'coordinator/bus_detail.html', context)


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
    FASE 2E.1 - Check-in seguro de pasajeros.

    GET:
        Muestra y valida el pasaje.
        NO modifica el Ticket.

    POST:
        Confirma embarque.
        Usa SELECT FOR UPDATE para evitar doble check-in concurrente.
    """

    ticket = get_object_or_404(
        Ticket.objects.select_related(
            "trip",
            "trip__route",
            "trip__route__origin",
            "trip__route__destination",
            "seat",
            "customer",
        ),
        number=ticket_number,
    )

    now = timezone.now()

    # ============================================================
    # 1. ESTADO TEMPORAL DEL VIAJE
    # ============================================================

    trip_departure = ticket.trip.departure

    trip_departed = trip_departure < now

    too_early = (
        trip_departure > now + timedelta(minutes=30)
    )

    # ============================================================
    # 2. GET = SOLO MOSTRAR / VALIDAR
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
    # 3. POST = CONFIRMAR EMBARQUE
    # ============================================================

    try:

        with transaction.atomic():

            locked_ticket = (
                Ticket.objects
                .select_for_update()
                .select_related(
                    "trip",
                    "trip__route",
                    "trip__route__origin",
                    "trip__route__destination",
                    "seat",
                )
                .get(
                    pk=ticket.pk
                )
            )

            now = timezone.now()

            # ----------------------------------------------------
            # Viaje ya partió
            # ----------------------------------------------------

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

            # ----------------------------------------------------
            # Ya embarcado
            # ----------------------------------------------------

            if locked_ticket.checked_in:

                messages.warning(
                    request,
                    (
                        f"El pasajero {locked_ticket.buyer_name} "
                        f"ya había sido embarcado."
                    ),
                )

                return redirect(
                    "coordinator:checkin",
                    ticket_number=locked_ticket.number,
                )

            # ----------------------------------------------------
            # Embarcar
            # ----------------------------------------------------

            locked_ticket.checked_in = True
            locked_ticket.checked_in_at = now

            locked_ticket.save(
                update_fields=[
                    "checked_in",
                    "checked_in_at",
                ]
            )

            # ----------------------------------------------------
            # Auditoría
            # ----------------------------------------------------

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
                ip_address=request.META.get(
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
                f"embarcado correctamente."
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
    FASE 2E.2 - Escaneo y validación de QR firmado.

    GET:
        Muestra la cámara / lector.

    POST:
        Recibe el contenido del QR.
        Valida firma Django.
        Verifica ticket_id + ticket_number.
        Redirige al check-in seguro.
    """
    from django.core import signing
    from django.core.signing import BadSignature

    if request.method == "GET":
        return render(
            request,
            "coordinator/checkin_qr_scan.html",
        )

    qr_value = (
        request.POST.get("qr_value", "")
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

    prefix = "CEJER:TICKET:"

    if not qr_value.startswith(prefix):
        messages.error(
            request,
            "El código QR no corresponde a un pasaje Cejer válido.",
        )

        return redirect(
            "coordinator:checkin_qr_scan"
        )

    signed_token = qr_value[len(prefix):]

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

    ticket_id = payload.get("ticket_id")
    ticket_number = payload.get("ticket_number")

    if not ticket_id or not ticket_number:
        messages.error(
            request,
            "El código QR no contiene información válida.",
        )

        return redirect(
            "coordinator:checkin_qr_scan"
        )

    ticket = (
        Ticket.objects
        .filter(
            pk=ticket_id,
            number=ticket_number,
        )
        .first()
    )

    if not ticket:
        messages.error(
            request,
            "El pasaje indicado por el QR no existe.",
        )

        return redirect(
            "coordinator:checkin_qr_scan"
        )

    return redirect(
        "coordinator:checkin",
        ticket_number=ticket.number,
    )


# ============================================================================
# ENCOMIENDAS
# ============================================================================

@login_required
@coordinator_required
def parcel_list(request):
    status_filter = request.GET.get('status', '')
    trip_filter = request.GET.get('trip', '')
    parcels = Parcel.objects.select_related('trip', 'created_by').order_by('-created_at')
    if status_filter:
        parcels = parcels.filter(status=status_filter)
    if trip_filter:
        parcels = parcels.filter(trip_id=trip_filter)
    trips = Trip.objects.all().order_by('-departure')
    context = {
        'parcels': parcels,
        'status_filter': status_filter,
        'trip_filter': trip_filter,
        'trips': trips,
    }
    return render(request, 'coordinator/parcel_list.html', context)


@require_POST
@login_required
@coordinator_required
def parcel_deliver(request, parcel_id):
    parcel = get_object_or_404(Parcel, pk=parcel_id)
    if parcel.status == 'pending':
        parcel.deliver()
        messages.success(request, f'Encomienda {parcel.tracking_number} marcada como entregada.')
    else:
        messages.warning(request, 'La encomienda ya no está pendiente.')
    return redirect('coordinator:parcel_list')


# ============================================================================
# MANTENIMIENTO Y COMBUSTIBLE
# ============================================================================

@login_required
@coordinator_required
def maintenance_list(request):
    bus_id = request.GET.get('bus')
    maint_type = request.GET.get('type')
    maintenances = Maintenance.objects.select_related('bus', 'created_by').order_by('-date')
    if bus_id:
        maintenances = maintenances.filter(bus_id=bus_id)
    if maint_type:
        maintenances = maintenances.filter(maintenance_type=maint_type)
    buses = Bus.objects.filter(is_active=True)
    context = {
        'maintenances': maintenances,
        'buses': buses,
        'bus_filter': bus_id,
        'type_filter': maint_type,
    }
    return render(request, 'coordinator/maintenance_list.html', context)


@login_required
@coordinator_required
def maintenance_create(request, bus_id=None):
    bus = None
    if bus_id:
        bus = get_object_or_404(Bus, pk=bus_id)

    if request.method == 'POST':
        try:
            maintenance = Maintenance.objects.create(
                bus_id=request.POST.get('bus'),
                maintenance_type=request.POST.get('maintenance_type'),
                date=request.POST.get('date'),
                cost=Decimal(request.POST.get('cost', '0')),
                workshop=request.POST.get('workshop'),
                mileage_at_maintenance=request.POST.get('mileage') or None,
                description=request.POST.get('description', ''),
                next_maintenance_due=request.POST.get('next_due') or None,
                invoice_file=request.FILES.get('invoice_file'),
                created_by=request.user,
            )
            messages.success(request, f'Mantenimiento registrado para {maintenance.bus.plate}.')
            return redirect('coordinator:maintenance_list')
        except Exception as e:
            messages.error(request, f'Error: {str(e)}')

    buses = Bus.objects.filter(is_active=True)
    context = {'buses': buses, 'selected_bus': bus}
    return render(request, 'coordinator/maintenance_form.html', context)


@login_required
@coordinator_required
def maintenance_edit(request, pk):
    maintenance = get_object_or_404(Maintenance, pk=pk)
    if request.method == 'POST':
        try:
            maintenance.bus_id = request.POST.get('bus')
            maintenance.maintenance_type = request.POST.get('maintenance_type')
            maintenance.date = request.POST.get('date')
            maintenance.cost = Decimal(request.POST.get('cost', '0'))
            maintenance.workshop = request.POST.get('workshop')
            maintenance.mileage_at_maintenance = request.POST.get('mileage') or None
            maintenance.description = request.POST.get('description', '')
            maintenance.next_maintenance_due = request.POST.get('next_due') or None
            if request.FILES.get('invoice_file'):
                maintenance.invoice_file = request.FILES['invoice_file']
            maintenance.save()
            messages.success(request, 'Mantenimiento actualizado.')
            return redirect('coordinator:maintenance_list')
        except Exception as e:
            messages.error(request, f'Error: {str(e)}')
    buses = Bus.objects.filter(is_active=True)
    context = {'maintenance': maintenance, 'buses': buses}
    return render(request, 'coordinator/maintenance_form.html', context)


@login_required
@coordinator_required
def maintenance_delete(request, pk):
    maintenance = get_object_or_404(Maintenance, pk=pk)
    maintenance.delete()
    messages.success(request, 'Mantenimiento eliminado.')
    return redirect('coordinator:maintenance_list')


@login_required
@coordinator_required
def fuel_list(request):
    bus_id = request.GET.get('bus')
    records = FuelRecord.objects.select_related('bus', 'created_by').order_by('-date')
    if bus_id:
        records = records.filter(bus_id=bus_id)
    buses = Bus.objects.filter(is_active=True)
    context = {
        'records': records,
        'buses': buses,
        'bus_filter': bus_id,
    }
    return render(request, 'coordinator/fuel_list.html', context)


@login_required
@coordinator_required
def fuel_create(request):
    if request.method == 'POST':
        try:
            bus = Bus.objects.get(pk=request.POST.get('bus'))
            record = FuelRecord.objects.create(
                bus=bus,
                date=request.POST.get('date'),
                liters=Decimal(request.POST.get('liters', '0')),
                cost=Decimal(request.POST.get('cost', '0')),
                mileage=int(request.POST.get('mileage', 0)),
                created_by=request.user,
            )
            bus.current_mileage = record.mileage
            bus.save(update_fields=['current_mileage'])
            messages.success(request, f'Carga de combustible registrada para {bus.plate}.')
            return redirect('coordinator:fuel_list')
        except Exception as e:
            messages.error(request, f'Error: {str(e)}')
    buses = Bus.objects.filter(is_active=True)
    return render(request, 'coordinator/fuel_form.html', {'buses': buses})


@login_required
@coordinator_required
def fuel_delete(request, pk):
    record = get_object_or_404(FuelRecord, pk=pk)
    record.delete()
    messages.success(request, 'Registro de combustible eliminado.')
    return redirect('coordinator:fuel_list')


@login_required
@coordinator_required
def bus_maintenance_history(request, bus_id):
    bus = get_object_or_404(Bus, pk=bus_id)
    maintenances = Maintenance.objects.filter(bus=bus).order_by('-date')
    context = {
        'bus': bus,
        'maintenances': maintenances,
    }
    return render(request, 'coordinator/bus_maintenance_history.html', context)


# ============================================================================
# MÓDULO DE SEGURIDAD Y LEY 21.719
# ============================================================================

@login_required
@coordinator_required
def seguridad_dashboard(request):
    """Panel principal de seguridad con estadísticas y resumen."""
    total_logs = AuditLog.objects.count()
    login_failed = AuditLog.objects.filter(action='login_failed').count()
    recent_logs = AuditLog.objects.all()[:10]
    
    today = timezone.now().date()
    usuarios_activos = AuditLog.objects.filter(
        action='login',
        timestamp__date=today
    ).values_list('user_id', flat=True).distinct().count()
    
    context = {
        'title': 'Ley 21.719 - Seguridad y Protección de Datos',
        'total_logs': total_logs,
        'login_failed': login_failed,
        'usuarios_activos': usuarios_activos,
        'recent_logs': recent_logs,
    }
    return render(request, 'coordinator/seguridad/dashboard.html', context)


@login_required
@coordinator_required
def seguridad_auditoria(request):
    """Lista de logs de auditoría con filtros."""
    query = request.GET.get('q', '')
    action_filter = request.GET.get('action', '')
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')
    
    logs = AuditLog.objects.select_related('user').all()
    
    if query:
        logs = logs.filter(
            Q(user__username__icontains=query) |
            Q(object_repr__icontains=query) |
            Q(model_name__icontains=query)
        )
    if action_filter:
        logs = logs.filter(action=action_filter)
    if date_from:
        logs = logs.filter(timestamp__date__gte=date_from)
    if date_to:
        logs = logs.filter(timestamp__date__lte=date_to)
    
    paginator = Paginator(logs, 50)
    page = request.GET.get('page')
    logs_page = paginator.get_page(page)
    
    context = {
        'title': 'Auditoría - Ley 21.719',
        'logs': logs_page,
        'action_choices': AuditLog.ACTION_CHOICES,
        'query': query,
        'action_filter': action_filter,
        'date_from': date_from,
        'date_to': date_to,
    }
    return render(request, 'coordinator/seguridad/auditoria.html', context)


@login_required
@coordinator_required
def seguridad_respaldos(request):
    """Gestión de respaldos de la base de datos."""
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'download_backup':
            backup_file = create_backup()
            if backup_file:
                AuditLog.objects.create(
                    user=request.user,
                    action='download_backup',
                    ip_address=request.META.get('REMOTE_ADDR'),
                    user_agent=request.META.get('HTTP_USER_AGENT', ''),
                )
                return FileResponse(
                    open(backup_file, 'rb'),
                    as_attachment=True,
                    filename=os.path.basename(backup_file)
                )
            else:
                messages.error(request, 'Error al generar el respaldo.')
        elif action == 'create_backup':
            backup_file = create_backup()
            if backup_file:
                AuditLog.objects.create(
                    user=request.user,
                    action='backup_created',
                    ip_address=request.META.get('REMOTE_ADDR'),
                    user_agent=request.META.get('HTTP_USER_AGENT', ''),
                )
                messages.success(request, f'Respaldo creado: {os.path.basename(backup_file)}')
            else:
                messages.error(request, 'Error al crear el respaldo.')
    
    backup_dir = get_backup_dir()
    backups = []
    if os.path.exists(backup_dir):
        for f in sorted(os.listdir(backup_dir), reverse=True):
            if f.endswith('.sql') or f.endswith('.dump'):
                path = os.path.join(backup_dir, f)
                backups.append({
                    'name': f,
                    'size': os.path.getsize(path),
                    'modified': os.path.getmtime(path),
                    'path': path
                })
    
    context = {
        'title': 'Respaldos - Ley 21.719',
        'backups': backups[:10],
    }
    return render(request, 'coordinator/seguridad/respaldos.html', context)


@login_required
@coordinator_required
def seguridad_incidentes(request):
    """Registro de incidentes de seguridad (intentos fallidos, accesos sospechosos)."""
    logs = AuditLog.objects.filter(
        Q(action='login_failed')
    ).select_related('user').order_by('-timestamp')
    
    paginator = Paginator(logs, 50)
    page = request.GET.get('page')
    logs_page = paginator.get_page(page)
    
    context = {
        'title': 'Incidentes de Seguridad',
        'logs': logs_page,
    }
    return render(request, 'coordinator/seguridad/incidentes.html', context)


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
def seguridad_eliminar_respaldo(request, filename):
    """Elimina un archivo de respaldo."""
    try:
        backup_dir = os.path.join(settings.BASE_DIR, 'backups')
        file_path = os.path.join(backup_dir, filename)
        if os.path.exists(file_path) and os.path.isfile(file_path):
            os.remove(file_path)
            return JsonResponse({'success': True})
        return JsonResponse({'error': 'Archivo no encontrado'}, status=404)
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


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
    """Crea un respaldo de la base de datos y devuelve JSON."""
    try:
        backup_file = create_backup()
        if backup_file:
            AuditLog.objects.create(
                user=request.user,
                action='backup_created',
                ip_address=request.META.get('REMOTE_ADDR'),
                user_agent=request.META.get('HTTP_USER_AGENT', ''),
                object_repr=f"Respaldo: {os.path.basename(backup_file)}"
            )
            return JsonResponse({'success': True, 'message': 'Respaldo creado correctamente'})
        else:
            return JsonResponse({'success': False, 'error': 'Error al crear el respaldo'}, status=500)
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)