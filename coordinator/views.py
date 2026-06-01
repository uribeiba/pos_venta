import json
from datetime import datetime, timedelta
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import ProtectedError, Q
from django.core.paginator import Paginator
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST, require_http_methods

from booking.models import (
    Assistant, Bus, BusDocument, City, Company, Driver, DriverDocument,
    Route, Seat, SeatHold, Terminal, Ticket, Trip, Agency
)
from booking.forms import (
    TerminalForm, CityForm, RouteForm, RouteStopFormSet, 
    AssistantForm, DriverForm, AgencyForm, BusFullForm, TripForm
)
from django.db import IntegrityError


# ---------------------------
# Permisos y Control de Acceso
# ---------------------------
def is_coordinator(user):
    if user.is_superuser:
        return True
    if hasattr(user, 'profile') and user.profile.role in ['admin', 'coordinator']:
        return True
    return False


# ---------------------------
# Helpers Utilitarios
# ---------------------------
def assign_missing_numbers(numbers, layout, prefix, start_from=1):
    """
    Asigna números correlativos a las celdas tipo 'L' que no tengan número.
    Retorna (numbers_actualizados, último_contador_usado)
    """
    counter = start_from
    for i in range(len(numbers)):
        if layout[i] == 'L' and (i >= len(numbers) or not numbers[i]):
            numbers[i] = f"{prefix}{counter}"
            counter += 1
    return numbers, counter


def make_aware_datetime(dt_str, field_name="Fecha"):
    """Convierte string de datetime-local a datetime timezone-aware."""
    if not dt_str:
        return None
    try:
        naive = datetime.strptime(dt_str, '%Y-%m-%dT%H:%M')
        return timezone.make_aware(naive)
    except ValueError:
        raise ValidationError(f"{field_name}: formato inválido. Use YYYY-MM-DDTHH:MM")


# ---------------------------
# Vistas de Buses (Clásicas y Dashboard)
# ---------------------------
@login_required
@user_passes_test(is_coordinator, login_url='pos_home')
def bus_list(request):
    buses = Bus.objects.select_related('company').all().order_by('company__name', 'plate')
    return render(request, 'coordinator/bus_list.html', {'buses': buses})


@login_required
@user_passes_test(is_coordinator)
def bus_editor(request, bus_id=None):
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

                # Solo regenerar asientos si cambió la estructura (filas/columnas/pisos)
                if not old_bus or (
                    old_bus.rows_lower != bus.rows_lower or
                    old_bus.rows_upper != bus.rows_upper or
                    old_bus.cols != bus.cols or
                    old_bus.floors != bus.floors
                ):
                    # Verificar si hay viajes futuros para evitar pérdida de datos
                    future_trips = Trip.objects.filter(bus=bus, departure__gt=timezone.now()).exists()
                    if future_trips:
                        messages.warning(request, "No se regeneraron los asientos porque el bus tiene viajes futuros. Para aplicar cambios estructurales, primero reasigna los viajes.")
                    else:
                        bus.regenerate_seats()
                        messages.success(request, f'Estructura de asientos regenerada para {bus.plate}.')

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
@user_passes_test(is_coordinator)
def buses_dashboard(request):
    """ Tablero unificado comercial para buses (Edición rápida de metadatos) """
    bus_to_edit = None
    edit_id = request.GET.get('edit')
    if edit_id:
        bus_to_edit = get_object_or_404(Bus, pk=edit_id)

    if request.method == 'POST':
        form = BusFullForm(request.POST, instance=bus_to_edit) if bus_to_edit else BusFullForm(request.POST)
        if form.is_valid():
            bus = form.save()
            bus.ensure_layouts()
            # ¡SEGURIDAD! No llamamos ciegamente a regenerate_seats() aquí para proteger pasajes activos.
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
@user_passes_test(is_coordinator)
def bus_duplicate(request, bus_id):
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
@user_passes_test(is_coordinator)
@require_POST
def bus_delete(request, bus_id):
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
@user_passes_test(is_coordinator)
@require_POST
def bus_delete_massive(request):
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
@user_passes_test(is_coordinator)
def api_bus_data(request, bus_id):
    bus = get_object_or_404(Bus, pk=bus_id)
    bus.ensure_layouts()
    return JsonResponse({
        'id': bus.id, 'floors': bus.floors, 'rows_lower': bus.rows_lower, 'rows_upper': bus.rows_upper, 'cols': bus.cols,
        'layout_lower': bus.layout_lower, 'layout_upper': bus.layout_upper,
        'numbers_lower': bus.numbers_lower, 'numbers_upper': bus.numbers_upper,
        'services_lower': bus.services_lower, 'services_upper': bus.services_upper,
        'prefix_lower': bus.prefix_lower, 'prefix_upper': bus.prefix_upper,
    })


# ---------------------------
# Gestión de Viajes
# ---------------------------
@login_required
@user_passes_test(is_coordinator)
def trip_list(request):
    trips = Trip.objects.select_related('route__origin', 'route__destination', 'bus').all().order_by('-departure')
    return render(request, 'coordinator/trip_list.html', {'trips': trips})


@login_required
@user_passes_test(is_coordinator)
def trip_create_edit(request, trip_id=None):
    trip = get_object_or_404(Trip, pk=trip_id) if trip_id else Trip()
    if request.method == 'POST':
        try:
            trip.route_id = request.POST.get('route')
            trip.bus_id = request.POST.get('bus')
            # Conversión con zona horaria
            trip.departure = make_aware_datetime(request.POST.get('departure'), "Salida")
            if request.POST.get('arrival'):
                trip.arrival = make_aware_datetime(request.POST.get('arrival'), "Llegada")
            trip.seats_total = int(request.POST.get('seats_total', 0))
            trip.driver1_id = request.POST.get('driver1') or None
            trip.driver2_id = request.POST.get('driver2') or None
            trip.assistant_id = request.POST.get('assistant') or None
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
        'trip': trip, 'routes': routes, 'buses': buses, 'drivers': drivers, 'assistants': assistants,
    })


@login_required
@user_passes_test(is_coordinator)
def trips_dashboard(request):
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
        'form': form, 'trips': trips_page, 'query': query,
        'edit_mode': bool(trip_to_edit), 'trip_edit_id': trip_to_edit.id if trip_to_edit else None,
    }
    return render(request, 'viajes/viajes.html', context)


@login_required
@user_passes_test(is_coordinator)
def trip_change_bus(request, trip_id):
    from booking.views import _build_trip_grid
    trip = get_object_or_404(Trip.objects.select_related('bus', 'route'), pk=trip_id)

    if request.method == 'POST':
        new_bus_id = request.POST.get('new_bus')
        if not new_bus_id:
            messages.error(request, "Debe seleccionar un bus nuevo.")
            return redirect('coordinator:trip_change_bus', trip_id=trip.id)

        new_bus = get_object_or_404(Bus, pk=new_bus_id)
        reassign_map = {k.split('_')[1]: v for k, v in request.POST.items() if k.startswith('seat_')}

        try:
            with transaction.atomic():
                tickets = Ticket.objects.filter(trip=trip).select_related('seat')
                if not tickets:
                    trip.bus = new_bus
                    trip.seats_total = Seat.objects.filter(bus=new_bus).count()
                    trip.save()
                    messages.success(request, f"Bus cambiado a {new_bus.plate} (sin pasajeros).")
                    return redirect('coordinator:trip_list')

                missing = [ticket.seat.number for ticket in tickets if str(ticket.seat.id) not in reassign_map]
                if missing:
                    raise ValidationError(f"Asientos sin reasignar: {', '.join(missing)}")

                for old_seat_id, new_seat_id in reassign_map.items():
                    new_seat = Seat.objects.select_for_update().get(pk=new_seat_id, bus=new_bus)
                    if Ticket.objects.filter(trip=trip, seat=new_seat).exists():
                        raise ValidationError(f"El asiento {new_seat.number} ya está ocupado.")

                for ticket in tickets:
                    new_seat_id = reassign_map[str(ticket.seat.id)]
                    new_seat = Seat.objects.get(pk=new_seat_id)
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
        'trip': trip, 'buses': buses, 'current_lower': current_lower, 'current_upper': current_upper, 'cols': cols,
    }
    return render(request, 'coordinator/trip_change_bus.html', context)


@login_required
@user_passes_test(is_coordinator)
def trip_delete(request, trip_id):
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


# ---------------------------
# Gestión de Ciudades
# ---------------------------
@login_required
@user_passes_test(is_coordinator)
def city_list(request):
    cities = City.objects.all().order_by("name")
    return render(request, "coordinator/city_list.html", {"cities": cities})


@login_required
@user_passes_test(is_coordinator)
def city_create_edit(request, city_id=None):
    city = get_object_or_404(City, pk=city_id) if city_id else City()
    if request.method == "POST":
        city.name = request.POST.get("name")
        city.save()
        messages.success(request, f"Ciudad '{city.name}' guardada.")
        return redirect("coordinator:city_list")
    return render(request, "coordinator/city_form.html", {"city": city})


@login_required
@user_passes_test(is_coordinator)
def city_delete(request, city_id):
    city = get_object_or_404(City, pk=city_id)
    city.delete()
    messages.success(request, "Ciudad eliminada.")
    return redirect("coordinator:city_list")


@login_required
@user_passes_test(is_coordinator)
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
        'form': form, 'cities': cities_page, 'query': query,
        'edit_mode': bool(city_to_edit), 'city_edit_id': city_to_edit.id if city_to_edit else None,
    }
    return render(request, 'ciudades/ciudades.html', context)


# ---------------------------
# Gestión de Terminales
# ---------------------------
@login_required
@user_passes_test(is_coordinator)
def terminal_list(request):
    terminals = Terminal.objects.select_related("city").all().order_by("city__name", "name")
    return render(request, "coordinator/terminal_list.html", {"terminals": terminals})


@login_required
@user_passes_test(is_coordinator)
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
@user_passes_test(is_coordinator)
def terminal_delete(request, terminal_id):
    terminal = get_object_or_404(Terminal, pk=terminal_id)
    terminal.delete()
    messages.success(request, "Terminal eliminada.")
    return redirect("coordinator:terminal_list")


@login_required
@user_passes_test(is_coordinator)
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
        'form': form, 'terminals': terminals_page, 'query': query,
        'edit_mode': bool(terminal_to_edit), 'terminal_edit_id': terminal_to_edit.id if terminal_to_edit else None,
    }
    return render(request, 'terminales/terminales.html', context)


# ---------------------------
# Gestión de Rutas
# ---------------------------
@login_required
@user_passes_test(is_coordinator)
def route_list(request):
    routes = Route.objects.select_related("origin", "destination", "origin_terminal", "destination_terminal").all()
    return render(request, "coordinator/route_list.html", {"routes": routes})


@login_required
@user_passes_test(is_coordinator)
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
        "route": route, "cities": cities, "terminals": terminals
    })


@login_required
@user_passes_test(is_coordinator)
def route_delete(request, route_id):
    route = get_object_or_404(Route, pk=route_id)
    route.delete()
    messages.success(request, "Ruta eliminada.")
    return redirect("coordinator:route_list")


@login_required
@user_passes_test(is_coordinator)
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
        'form': form, 'formset': formset, 'routes': routes_page, 'query': query,
        'edit_mode': bool(route_to_edit), 'route_edit_id': route_to_edit.id if route_to_edit else None,
        'cities': City.objects.all(), 'terminals': Terminal.objects.all(),
    }
    return render(request, 'rutas/rutas.html', context)


# ---------------------------
# Gestión de Choferes
# ---------------------------
@login_required
@user_passes_test(is_coordinator)
def driver_list(request):
    drivers = Driver.objects.all().order_by("full_name")
    return render(request, "coordinator/driver_list.html", {"drivers": drivers})


@login_required
@user_passes_test(is_coordinator)
def driver_create_edit(request, driver_id=None):
    driver = get_object_or_404(Driver, pk=driver_id) if driver_id else Driver()
    if request.method == "POST":
        # CORREGIDO: Eliminada referencia a 'license_expiry' que no existe en el modelo
        driver.medical_cert_expiry = request.POST.get('medical_cert_expiry') or None
        driver.background_check_expiry = request.POST.get('background_check_expiry') or None
        driver.notes = request.POST.get('notes', '')
        driver.full_name = request.POST.get("full_name")
        driver.rut = request.POST.get("rut")
        driver.email = request.POST.get("email", "")
        driver.phone = request.POST.get("phone", "")
        driver.license_number = request.POST.get("license_number", "")
        driver.is_active = "is_active" in request.POST
        # Manejo de foto (si se sube)
        if 'photo' in request.FILES:
            driver.photo = request.FILES['photo']
        driver.save()
        messages.success(request, f"Chofer {driver.full_name} guardado.")
        return redirect("coordinator:driver_list")
    return render(request, "coordinator/driver_form.html", {"driver": driver})


@login_required
@user_passes_test(is_coordinator)
def driver_delete(request, driver_id):
    driver = get_object_or_404(Driver, pk=driver_id)
    driver.delete()
    messages.success(request, "Chofer eliminado.")
    return redirect("coordinator:driver_list")


@login_required
@user_passes_test(is_coordinator)
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
        'form': form, 'drivers': drivers_page, 'query': query,
        'edit_mode': bool(driver_to_edit), 'driver_edit_id': driver_to_edit.id if driver_to_edit else None,
    }
    return render(request, 'choferes/choferes.html', context)


# ---------------------------
# Gestión de Auxiliares
# ---------------------------
@login_required
@user_passes_test(is_coordinator)
def assistant_list(request):
    assistants = Assistant.objects.all().order_by("full_name")
    return render(request, "coordinator/assistant_list.html", {"assistants": assistants})


@login_required
@user_passes_test(is_coordinator)
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
@user_passes_test(is_coordinator)
def assistant_delete(request, assistant_id):
    assistant = get_object_or_404(Assistant, pk=assistant_id)
    assistant.delete()
    messages.success(request, "Auxiliar eliminado.")
    return redirect("coordinator:assistant_list")


@login_required
@user_passes_test(is_coordinator)
def assistants_dashboard(request):
    assistant_to_edit = None
    edit_id = request.GET.get('edit')
    if edit_id:
        assistant_to_edit = get_object_or_404(Assistant, pk=edit_id)

    if request.method == 'POST':
        form = AssistantForm(request.POST, instance=assistant_to_edit) if assistant_to_edit else AssistantForm(request.POST)
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
        'form': form, 'assistants': assistants_page, 'query': query,
        'edit_mode': bool(assistant_to_edit), 'assistant_edit_id': assistant_to_edit.id if assistant_to_edit else None,
    }
    return render(request, 'auxiliares/assistants.html', context)


# ---------------------------
# Gestión de Agencias
# ---------------------------
@login_required
@user_passes_test(is_coordinator)
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
        'form': form, 'agencies': agencies_page, 'query': query,
        'edit_mode': bool(agency_to_edit), 'agency_edit_id': agency_to_edit.id if agency_to_edit else None,
    }
    return render(request, 'agencias/agencias.html', context)


@login_required
@user_passes_test(is_coordinator)
def agency_delete(request, agency_id):
    agency = get_object_or_404(Agency, pk=agency_id)
    agency.delete()
    messages.success(request, "Agencia eliminada correctamente.")
    return redirect('coordinator:agencies_dashboard')


# ---------------------------
# Documentos de Personal y Flota
# ---------------------------
@login_required
@user_passes_test(is_coordinator)
def driver_documents(request, driver_id):
    driver = get_object_or_404(Driver, pk=driver_id)
    documents = DriverDocument.objects.filter(driver=driver).order_by('expiry_date')
    return render(request, 'coordinator/driver_documents.html', {'driver': driver, 'documents': documents})


@login_required
@user_passes_test(is_coordinator)
def driver_document_create(request, driver_id):
    driver = get_object_or_404(Driver, pk=driver_id)
    if request.method == 'POST':
        DriverDocument.objects.create(
            driver=driver, doc_type=request.POST.get('doc_type'), document_number=request.POST.get('doc_number', ''),
            issue_date=request.POST.get('issue_date') or None, expiry_date=request.POST.get('expiry_date'),
            notes=request.POST.get('notes', '')
        )
        messages.success(request, f'Documento agregado a {driver.full_name}')
        return redirect('coordinator:driver_documents', driver_id=driver.id)
    return render(request, 'coordinator/driver_document_form.html', {'driver': driver})


@login_required
@user_passes_test(is_coordinator)
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
@user_passes_test(is_coordinator)
def driver_document_delete(request, doc_id):
    doc = get_object_or_404(DriverDocument, pk=doc_id)
    driver_id = doc.driver.id
    doc.delete()
    messages.success(request, 'Documento eliminado')
    return redirect('coordinator:driver_documents', driver_id=driver_id)


@login_required
@user_passes_test(is_coordinator)
def bus_documents(request, bus_id):
    bus = get_object_or_404(Bus, pk=bus_id)
    documents = BusDocument.objects.filter(bus=bus).order_by('expiry_date')
    return render(request, 'coordinator/bus_documents.html', {'bus': bus, 'documents': documents})


@login_required
@user_passes_test(is_coordinator)
def bus_document_create(request, bus_id):
    bus = get_object_or_404(Bus, pk=bus_id)
    if request.method == 'POST':
        BusDocument.objects.create(
            bus=bus, doc_type=request.POST.get('doc_type'), document_number=request.POST.get('doc_number', ''),
            issue_date=request.POST.get('issue_date') or None, expiry_date=request.POST.get('expiry_date'),
            notes=request.POST.get('notes', '')
        )
        messages.success(request, f'Documento agregado a bus {bus.plate}')
        return redirect('coordinator:bus_documents', bus_id=bus.id)
    return render(request, 'coordinator/bus_document_form.html', {'bus': bus})


@login_required
@user_passes_test(is_coordinator)
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
@user_passes_test(is_coordinator)
def bus_document_delete(request, doc_id):
    doc = get_object_or_404(BusDocument, pk=doc_id)
    bus_id = doc.bus.id
    doc.delete()
    messages.success(request, 'Documento eliminado')
    return redirect('coordinator:bus_documents', bus_id=bus_id)


@login_required
@user_passes_test(is_coordinator)
def expiring_documents(request):
    today = timezone.now().date()
    warning_days = 30
    expiry_limit = today + timedelta(days=warning_days)

    driver_docs = DriverDocument.objects.filter(expiry_date__isnull=False, expiry_date__gte=today, expiry_date__lte=expiry_limit).select_related('driver').order_by('expiry_date')
    bus_docs = BusDocument.objects.filter(expiry_date__isnull=False, expiry_date__gte=today, expiry_date__lte=expiry_limit).select_related('bus').order_by('expiry_date')
    expired_driver = DriverDocument.objects.filter(expiry_date__isnull=False, expiry_date__lt=today).select_related('driver').order_by('expiry_date')
    expired_bus = BusDocument.objects.filter(expiry_date__isnull=False, expiry_date__lt=today).select_related('bus').order_by('expiry_date')

    context = {
        'driver_docs': driver_docs, 'bus_docs': bus_docs, 'expired_driver': expired_driver, 'expired_bus': expired_bus,
        'warning_days': warning_days, 'today': today,
    }
    return render(request, 'coordinator/expiring_documents.html', context)


# ============================================================================
# GENERACIÓN MASIVA DE VIAJES (RECURRENTES) Y CALENDARIO
# ============================================================================

@login_required
@user_passes_test(is_coordinator)
def generate_trips(request):
    """
    Vista para crear múltiples viajes recurrentes en un rango de fechas,
    seleccionando días de la semana y una misma hora de salida.
    Soporta peticiones normales (redirect) y AJAX (JSON).
    """
    from django.utils.dateparse import parse_date
    import calendar

    today = timezone.now().date()
    # Parámetros para el calendario (mes actual por defecto)
    year = int(request.GET.get('year', today.year))
    month = int(request.GET.get('month', today.month))
    if month < 1 or month > 12:
        month = today.month
    if year < 2000 or year > 2100:
        year = today.year

    # Obtener días del mes para mostrar calendario
    cal = calendar.monthcalendar(year, month)
    first_day = datetime(year, month, 1).date()
    last_day = (datetime(year, month + 1, 1) - timedelta(days=1)).date() if month < 12 else datetime(year, 12, 31).date()

    # Obtener viajes existentes en ese rango para marcar en calendario
    existing_trips = Trip.objects.filter(
        departure__date__range=(first_day, last_day)
    ).values_list('departure__date', flat=True).distinct()
    existing_dates = set(existing_trips)

    # Preparar lista de días del mes con clases para CSS
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
        # Detectar si es una petición AJAX (fetch)
        is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'

        route_id = request.POST.get('route')
        bus_id = request.POST.get('bus')
        driver1_id = request.POST.get('driver1') or None
        driver2_id = request.POST.get('driver2') or None
        assistant_id = request.POST.get('assistant') or None
        departure_hour = request.POST.get('departure_hour')
        start_date_str = request.POST.get('start_date')
        end_date_str = request.POST.get('end_date')
        weekdays = request.POST.getlist('weekdays')  # lista de strings "1".."7"

        errors = []
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

        if not errors:
            try:
                start_date = parse_date(start_date_str)
                end_date = parse_date(end_date_str)
                hour, minute = map(int, departure_hour.split(':'))
                selected_weekdays = [int(d) for d in weekdays]
                if start_date > end_date:
                    errors.append("La fecha de inicio no puede ser posterior a la fecha fin.")
            except Exception:
                errors.append("Formato de fecha u hora inválido.")

        if errors:
            error_msg = " | ".join(errors)
            if is_ajax:
                return JsonResponse({'success': False, 'error': error_msg}, status=400)
            for err in errors:
                messages.error(request, err)
            return redirect('coordinator:generate_trips')

        # Crear viajes
        created_count = 0
        skipped_count = 0
        current_date = start_date
        delta = timedelta(days=1)

        try:
            with transaction.atomic():
                route = Route.objects.get(pk=route_id)
                bus = Bus.objects.get(pk=bus_id)
                total_seats = Seat.objects.filter(bus=bus).count()

                while current_date <= end_date:
                    day_of_week = current_date.isoweekday()  # lunes=1, domingo=7
                    if day_of_week in selected_weekdays:
                        departure_dt = timezone.make_aware(
                            datetime.combine(current_date, datetime.strptime(departure_hour, '%H:%M').time())
                        )
                        arrival_dt = departure_dt + timedelta(minutes=route.duration_minutes)

                        # Evitar duplicados: mismo bus, misma fecha y hora cercana (±1 hora)
                        existing = Trip.objects.filter(
                            bus_id=bus_id,
                            departure__date=current_date,
                            departure__time__range=(
                                (departure_dt - timedelta(hours=1)).time(),
                                (departure_dt + timedelta(hours=1)).time()
                            )
                        ).exists()

                        if not existing:
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
                        else:
                            skipped_count += 1
                    current_date += delta

            message = f"Se crearon {created_count} viajes. Omitidos (ya existían): {skipped_count}."
            if is_ajax:
                return JsonResponse({'success': True, 'message': message})
            else:
                messages.success(request, message)
                return redirect('coordinator:trips_dashboard')

        except Exception as e:
            if is_ajax:
                return JsonResponse({'success': False, 'error': str(e)}, status=500)
            messages.error(request, f"Error al generar viajes: {str(e)}")
            return redirect('coordinator:generate_trips')

    # GET: preparar contextos para el formulario
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
@user_passes_test(is_coordinator)
def api_trips_calendar(request):
    """Endpoint para FullCalendar: devuelve viajes en el rango de fechas."""
    start_str = request.GET.get('start')
    end_str = request.GET.get('end')
    if not start_str or not end_str:
        return JsonResponse([], safe=False)

    try:
        start = timezone.make_aware(datetime.fromisoformat(start_str.replace('Z', '+00:00')))
        end = timezone.make_aware(datetime.fromisoformat(end_str.replace('Z', '+00:00')))
    except Exception:
        return JsonResponse([], safe=False)

    trips = Trip.objects.filter(departure__range=(start, end)).select_related('route__origin', 'route__destination', 'bus')
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
                'has_tickets': Ticket.objects.filter(trip=trip).exists()
            }
        })
    return JsonResponse(events, safe=False)


@login_required
@user_passes_test(is_coordinator)
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