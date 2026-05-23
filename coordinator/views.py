# coordinator/views.py
import json
from datetime import datetime, timedelta
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import ProtectedError
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from booking.models import (
    Assistant, Bus, BusDocument, City, Company, Driver, DriverDocument,
    Route, Seat, SeatHold, Terminal, Ticket, Trip, Terminal, Agency
)
from django.core.paginator import Paginator
from django.db.models import Q
from booking.forms import TerminalForm, CityForm, RouteForm, RouteStopFormSet, AssistantForm, DriverForm, AgencyForm, BusFullForm
from booking.forms import TripForm
from booking.models import Trip


# ---------------------------
# Permisos
# ---------------------------
def is_coordinator(user):
    if user.is_superuser:
        return True
    if hasattr(user, 'profile') and user.profile.role in ['admin', 'coordinator']:
        return True
    return False


# ---------------------------
# Helper: asignar números a asientos tipo 'L'
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


# ---------------------------
# Vistas de Buses
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

                # Datos básicos
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
                    bus.layout_upper = []
                    bus.numbers_upper = []
                    bus.services_upper = []

                # Asignar números faltantes
                last_counter = 1
                bus.numbers_lower, last_counter = assign_missing_numbers(
                    bus.numbers_lower, bus.layout_lower, bus.prefix_lower, last_counter
                )
                if bus.floors == 2:
                    bus.numbers_upper, _ = assign_missing_numbers(
                        bus.numbers_upper, bus.layout_upper, bus.prefix_upper, last_counter
                    )

                bus.save()

                if not old_bus or (
                    old_bus.rows_lower != bus.rows_lower or
                    old_bus.rows_upper != bus.rows_upper or
                    old_bus.cols != bus.cols or
                    old_bus.floors != bus.floors
                ):
                    bus.regenerate_seats()

                messages.success(request, f'Bus {bus.plate} guardado.')
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
def bus_duplicate(request, bus_id):
    original = get_object_or_404(Bus, pk=bus_id)
    new_bus = Bus()
    for field in ['company', 'model', 'year', 'floors', 'rows_lower', 'rows_upper', 'cols',
                  'layout_lower', 'layout_upper', 'numbers_lower', 'numbers_upper',
                  'services_lower', 'services_upper', 'prefix_lower', 'prefix_upper']:
        setattr(new_bus, field, getattr(original, field))
    new_bus.plate = f"{original.plate} (copia)"
    new_bus.save()
    new_bus.ensure_layouts()
    new_bus.regenerate_seats()
    messages.success(request, f'Bus duplicado: {new_bus.plate}')
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
            return JsonResponse({
                'success': False,
                'partial': True,
                'deleted': deleted_count,
                'errors': errors
            })
        return JsonResponse({'success': True, 'deleted': deleted_count})

    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})


@login_required
@user_passes_test(is_coordinator)
def api_bus_data(request, bus_id):
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


# ---------------------------
# Vistas de Viajes
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
            trip.departure = datetime.strptime(request.POST.get('departure'), '%Y-%m-%dT%H:%M')
            if request.POST.get('arrival'):
                trip.arrival = datetime.strptime(request.POST.get('arrival'), '%Y-%m-%dT%H:%M')
            trip.seats_total = int(request.POST.get('seats_total', 0))

            trip.driver1_id = request.POST.get('driver1') or None
            trip.driver2_id = request.POST.get('driver2') or None
            trip.assistant_id = request.POST.get('assistant') or None

            trip.save()
            messages.success(request, 'Viaje guardado correctamente.')
            return redirect('coordinator:trip_list')
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

        reassign_map = {}
        for key, value in request.POST.items():
            if key.startswith('seat_'):
                old_seat_id = key.split('_')[1]
                new_seat_id = value
                reassign_map[old_seat_id] = new_seat_id

        try:
            with transaction.atomic():
                tickets = Ticket.objects.filter(trip=trip).select_related('seat')
                if not tickets:
                    trip.bus = new_bus
                    trip.seats_total = Seat.objects.filter(bus=new_bus).count()
                    trip.save()
                    messages.success(request, f"Bus cambiado a {new_bus.plate} (sin pasajeros).")
                    return redirect('coordinator:trip_list')

                missing = []
                for ticket in tickets:
                    if str(ticket.seat.id) not in reassign_map:
                        missing.append(ticket.seat.number)
                if missing:
                    raise ValidationError(f"Asientos sin reasignar: {', '.join(missing)}")

                for old_seat_id, new_seat_id in reassign_map.items():
                    new_seat = Seat.objects.select_for_update().get(pk=new_seat_id, bus=new_bus)
                    if Ticket.objects.filter(trip=trip, seat=new_seat).exists():
                        raise ValidationError(f"El asiento {new_seat.number} ya está ocupado en este viaje.")

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

                messages.success(request, f"Viaje reasignado a bus {new_bus.plate}. Pasajeros reubicados.")
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




# coordinator/views.py (agregar imports si no están)

@login_required
def terminals_dashboard(request):
    """
    Vista unificada para Terminales: formulario + listado + búsqueda + paginación.
    """
    terminal_to_edit = None
    edit_id = request.GET.get('edit')
    if edit_id:
        terminal_to_edit = get_object_or_404(Terminal, pk=edit_id)

    if request.method == 'POST':
        if terminal_to_edit:
            form = TerminalForm(request.POST, instance=terminal_to_edit)
        else:
            form = TerminalForm(request.POST)
        if form.is_valid():
            terminal = form.save()
            messages.success(request, f'Terminal {terminal.name} guardada correctamente.')
            return redirect('coordinator:terminals_dashboard')
        else:
            messages.error(request, 'Por favor corrige los errores del formulario.')
    else:
        form = TerminalForm(instance=terminal_to_edit) if terminal_to_edit else TerminalForm()

    # Búsqueda
    query = request.GET.get('q', '').strip()
    terminals_list = Terminal.objects.select_related('city').all().order_by('city__name', 'name')
    if query:
        terminals_list = terminals_list.filter(
            Q(name__icontains=query) | Q(city__name__icontains=query) | Q(address__icontains=query)
        )

    # Paginación
    paginator = Paginator(terminals_list, 10)
    page_number = request.GET.get('page')
    terminals_page = paginator.get_page(page_number)

    context = {
        'form': form,
        'terminals': terminals_page,
        'query': query,
        'edit_mode': bool(terminal_to_edit),
        'terminal_edit_id': terminal_to_edit.id if terminal_to_edit else None,
    }
    return render(request, 'terminales/terminales.html', context)


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
        "route": route,
        "cities": cities,
        "terminals": terminals
    })


@login_required
@user_passes_test(is_coordinator)
def route_delete(request, route_id):
    route = get_object_or_404(Route, pk=route_id)
    route.delete()
    messages.success(request, "Ruta eliminada.")
    return redirect("coordinator:route_list")


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
        driver.license_expiry = request.POST.get('license_expiry') or None
        driver.medical_cert_expiry = request.POST.get('medical_cert_expiry') or None
        driver.background_check_expiry = request.POST.get('background_check_expiry') or None
        driver.notes = request.POST.get('notes', '')
        driver.full_name = request.POST.get("full_name")
        driver.rut = request.POST.get("rut")
        driver.email = request.POST.get("email", "")
        driver.phone = request.POST.get("phone", "")
        driver.license_number = request.POST.get("license_number", "")
        driver.is_active = "is_active" in request.POST
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


# ---------------------------
# Documentos de Choferes
# ---------------------------
@login_required
@user_passes_test(is_coordinator)
def driver_documents(request, driver_id):
    driver = get_object_or_404(Driver, pk=driver_id)
    documents = DriverDocument.objects.filter(driver=driver).order_by('expiry_date')
    return render(request, 'coordinator/driver_documents.html', {
        'driver': driver,
        'documents': documents,
    })


@login_required
@user_passes_test(is_coordinator)
def driver_document_create(request, driver_id):
    driver = get_object_or_404(Driver, pk=driver_id)
    if request.method == 'POST':
        doc_type = request.POST.get('doc_type')
        doc_number = request.POST.get('doc_number', '')
        issue_date = request.POST.get('issue_date') or None
        expiry_date = request.POST.get('expiry_date')
        notes = request.POST.get('notes', '')
        DriverDocument.objects.create(
            driver=driver,
            doc_type=doc_type,
            doc_number=doc_number,
            issue_date=issue_date,
            expiry_date=expiry_date,
            notes=notes,
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
        doc.doc_number = request.POST.get('doc_number', '')
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


# ---------------------------
# Documentos de Buses
# ---------------------------
@login_required
@user_passes_test(is_coordinator)
def bus_documents(request, bus_id):
    bus = get_object_or_404(Bus, pk=bus_id)
    documents = BusDocument.objects.filter(bus=bus).order_by('expiry_date')
    return render(request, 'coordinator/bus_documents.html', {
        'bus': bus,
        'documents': documents,
    })


@login_required
@user_passes_test(is_coordinator)
def bus_document_create(request, bus_id):
    bus = get_object_or_404(Bus, pk=bus_id)
    if request.method == 'POST':
        doc_type = request.POST.get('doc_type')
        doc_number = request.POST.get('doc_number', '')
        issue_date = request.POST.get('issue_date') or None
        expiry_date = request.POST.get('expiry_date')
        notes = request.POST.get('notes', '')
        BusDocument.objects.create(
            bus=bus,
            doc_type=doc_type,
            doc_number=doc_number,
            issue_date=issue_date,
            expiry_date=expiry_date,
            notes=notes,
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
        doc.doc_number = request.POST.get('doc_number', '')
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


# ---------------------------
# Panel de vencimientos (próximos a vencer y vencidos)
# ---------------------------
@login_required
@user_passes_test(is_coordinator)
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



@login_required
def drivers_dashboard(request):
    """
    Vista unificada: formulario de creación/edición + listado con búsqueda y paginación.
    """
    driver_to_edit = None
    edit_id = request.GET.get('edit')
    if edit_id:
        driver_to_edit = get_object_or_404(Driver, pk=edit_id)

    if request.method == 'POST':
        if driver_to_edit:
            form = DriverForm(request.POST, request.FILES, instance=driver_to_edit)
        else:
            form = DriverForm(request.POST, request.FILES)
        if form.is_valid():
            driver = form.save()
            messages.success(request, f'Chofer {driver.full_name} guardado correctamente.')
            return redirect('coordinator:drivers_dashboard')
        else:
            messages.error(request, 'Por favor corrige los errores del formulario.')
    else:
        form = DriverForm(instance=driver_to_edit) if driver_to_edit else DriverForm()

    # Búsqueda
    query = request.GET.get('q', '').strip()
    drivers_list = Driver.objects.all().order_by('-is_active', 'full_name')
    if query:
        drivers_list = drivers_list.filter(
            Q(full_name__icontains=query) | Q(rut__icontains=query)
        )

    # Paginación
    paginator = Paginator(drivers_list, 10)
    page_number = request.GET.get('page')
    drivers_page = paginator.get_page(page_number)

    context = {
        'form': form,
        'drivers': drivers_page,
        'query': query,
        'edit_mode': bool(driver_to_edit),
        'driver_edit_id': driver_to_edit.id if driver_to_edit else None,
    }
    return render(request, 'choferes/choferes.html', context)


# coordinator/views.py (después de drivers_dashboard)



@login_required
def assistants_dashboard(request):
    """
    Vista unificada para Auxiliares: formulario + listado + búsqueda + paginación.
    """
    assistant_to_edit = None
    edit_id = request.GET.get('edit')
    if edit_id:
        assistant_to_edit = get_object_or_404(Assistant, pk=edit_id)

    if request.method == 'POST':
        if assistant_to_edit:
            form = AssistantForm(request.POST, instance=assistant_to_edit)
        else:
            form = AssistantForm(request.POST)
        if form.is_valid():
            assistant = form.save()
            messages.success(request, f'Auxiliar {assistant.full_name} guardado correctamente.')
            return redirect('coordinator:assistants_dashboard')
        else:
            messages.error(request, 'Por favor corrige los errores del formulario.')
    else:
        form = AssistantForm(instance=assistant_to_edit) if assistant_to_edit else AssistantForm()

    # Búsqueda
    query = request.GET.get('q', '').strip()
    assistants_list = Assistant.objects.all().order_by('-is_active', 'full_name')
    if query:
        assistants_list = assistants_list.filter(
            Q(full_name__icontains=query) | Q(rut__icontains=query)
        )

    # Paginación
    paginator = Paginator(assistants_list, 10)
    page_number = request.GET.get('page')
    assistants_page = paginator.get_page(page_number)

    context = {
        'form': form,
        'assistants': assistants_page,
        'query': query,
        'edit_mode': bool(assistant_to_edit),
        'assistant_edit_id': assistant_to_edit.id if assistant_to_edit else None,
    }
    return render(request, 'auxiliares/assistants.html', context)




@login_required
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

    # Búsqueda
    query = request.GET.get('q', '').strip()
    routes_list = Route.objects.select_related('origin', 'destination').all().order_by('origin__name', 'destination__name')
    if query:
        routes_list = routes_list.filter(
            Q(origin__name__icontains=query) |
            Q(destination__name__icontains=query) |
            Q(origin__name__icontains=query)   # repetido? no importa
        )

    paginator = Paginator(routes_list, 10)
    page_number = request.GET.get('page')
    routes_page = paginator.get_page(page_number)

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




@login_required
def cities_dashboard(request):
    """
    Vista unificada para Ciudades: formulario + listado + búsqueda + paginación.
    """
    city_to_edit = None
    edit_id = request.GET.get('edit')
    if edit_id:
        city_to_edit = get_object_or_404(City, pk=edit_id)

    if request.method == 'POST':
        if city_to_edit:
            form = CityForm(request.POST, instance=city_to_edit)
        else:
            form = CityForm(request.POST)
        if form.is_valid():
            city = form.save()
            messages.success(request, f'Ciudad "{city.name}" guardada correctamente.')
            return redirect('coordinator:cities_dashboard')
        else:
            messages.error(request, 'Por favor corrige los errores del formulario.')
    else:
        form = CityForm(instance=city_to_edit) if city_to_edit else CityForm()

    # Búsqueda
    query = request.GET.get('q', '').strip()
    cities_list = City.objects.all().order_by('name')
    if query:
        cities_list = cities_list.filter(name__icontains=query)

    # Paginación
    paginator = Paginator(cities_list, 10)
    page_number = request.GET.get('page')
    cities_page = paginator.get_page(page_number)

    context = {
        'form': form,
        'cities': cities_page,
        'query': query,
        'edit_mode': bool(city_to_edit),
        'city_edit_id': city_to_edit.id if city_to_edit else None,
    }
    return render(request, 'ciudades/ciudades.html', context)



@login_required
def buses_dashboard(request):
    bus_to_edit = None
    edit_id = request.GET.get('edit')
    if edit_id:
        bus_to_edit = get_object_or_404(Bus, pk=edit_id)

    if request.method == 'POST':
        if bus_to_edit:
            form = BusFullForm(request.POST, instance=bus_to_edit)
        else:
            form = BusFullForm(request.POST)

        if form.is_valid():
            bus = form.save()  # Guarda directamente el bus
            # Regenerar asientos solo si cambió la estructura (filas, columnas, etc.)
            bus.ensure_layouts()
            bus.regenerate_seats()
            messages.success(request, f'Bus {bus.plate} guardado correctamente.')
            return redirect('coordinator:buses_dashboard')
        else:
            # Mostrar errores detallados en consola y en mensajes
            print("Errores del formulario:", form.errors)
            messages.error(request, f'Error al guardar: {form.errors}')
    else:
        form = BusFullForm(instance=bus_to_edit) if bus_to_edit else BusFullForm()

    # Búsqueda
    query = request.GET.get('q', '').strip()
    buses_list = Bus.objects.select_related('company').all().order_by('company__name', 'plate')
    if query:
        buses_list = buses_list.filter(
            Q(plate__icontains=query) | Q(model__icontains=query) |
            Q(owner_first_name__icontains=query) | Q(owner_last_name__icontains=query) |
            Q(brand__icontains=query)
        )

    paginator = Paginator(buses_list, 10)
    page_number = request.GET.get('page')
    buses_page = paginator.get_page(page_number)

    context = {
        'form': form,
        'buses': buses_page,
        'query': query,
        'edit_mode': bool(bus_to_edit),
        'bus_edit_id': bus_to_edit.id if bus_to_edit else None,
    }
    return render(request, 'buses/buses_full.html', context)

@login_required
def agencies_dashboard(request):
    agency_to_edit = None
    edit_id = request.GET.get('edit')
    if edit_id:
        agency_to_edit = get_object_or_404(Agency, pk=edit_id)

    if request.method == 'POST':
        if agency_to_edit:
            form = AgencyForm(request.POST, instance=agency_to_edit)
        else:
            form = AgencyForm(request.POST)
        if form.is_valid():
            agency = form.save()
            messages.success(request, f'Agencia "{agency.name}" guardada correctamente.')
            return redirect('coordinator:agencies_dashboard')
        else:
            messages.error(request, 'Por favor corrige los errores del formulario.')
    else:
        form = AgencyForm(instance=agency_to_edit) if agency_to_edit else AgencyForm()

    # Búsqueda
    query = request.GET.get('q', '').strip()
    agencies_list = Agency.objects.select_related('city').all().order_by('name')
    if query:
        agencies_list = agencies_list.filter(
            Q(name__icontains=query) | Q(city__name__icontains=query) | Q(address__icontains=query)
        )

    paginator = Paginator(agencies_list, 10)
    page_number = request.GET.get('page')
    agencies_page = paginator.get_page(page_number)

    context = {
        'form': form,
        'agencies': agencies_page,
        'query': query,
        'edit_mode': bool(agency_to_edit),
        'agency_edit_id': agency_to_edit.id if agency_to_edit else None,
    }
    return render(request, 'agencias/agencias.html', context)


# coordinator/views.py
@login_required
def agency_delete(request, agency_id):
    agency = get_object_or_404(Agency, pk=agency_id)
    agency.delete()
    messages.success(request, "Agencia eliminada correctamente.")
    return redirect('coordinator:agencies_dashboard')


@login_required
def trips_dashboard(request):
    trip_to_edit = None
    edit_id = request.GET.get('edit')
    if edit_id:
        trip_to_edit = get_object_or_404(Trip, pk=edit_id)

    if request.method == 'POST':
        if trip_to_edit:
            form = TripForm(request.POST, instance=trip_to_edit)
        else:
            form = TripForm(request.POST)
        if form.is_valid():
            trip = form.save(commit=False)
            # Si no se ingresó llegada, calcularla con duración de la ruta
            if not trip.arrival and trip.route:
                trip.arrival = trip.departure + timedelta(minutes=trip.route.duration_minutes)
            # Si el bus tiene asientos, actualizar seats_total
            if trip.bus:
                total_seats = Seat.objects.filter(bus=trip.bus).count()
                trip.seats_total = total_seats
            trip.save()
            messages.success(request, f'Viaje {trip} guardado correctamente.')
            return redirect('coordinator:trips_dashboard')
        else:
            messages.error(request, f'Error al guardar: {form.errors}')
    else:
        form = TripForm(instance=trip_to_edit) if trip_to_edit else TripForm()

    # Búsqueda
    query = request.GET.get('q', '').strip()
    trips_list = Trip.objects.select_related('route__origin', 'route__destination', 'bus', 'driver1', 'driver2', 'assistant').all().order_by('-departure')
    if query:
        trips_list = trips_list.filter(
            Q(route__origin__name__icontains=query) |
            Q(route__destination__name__icontains=query) |
            Q(bus__plate__icontains=query) |
            Q(driver1__full_name__icontains=query)
        )

    paginator = Paginator(trips_list, 10)
    page_number = request.GET.get('page')
    trips_page = paginator.get_page(page_number)

    context = {
        'form': form,
        'trips': trips_page,
        'query': query,
        'edit_mode': bool(trip_to_edit),
        'trip_edit_id': trip_to_edit.id if trip_to_edit else None,
    }
    return render(request, 'viajes/viajes.html', context)

@login_required
@user_passes_test(is_coordinator)
def trip_delete(request, trip_id):
    trip = get_object_or_404(Trip, pk=trip_id)
    # Opcional: verificar si tiene tickets antes de eliminar
    if Ticket.objects.filter(trip=trip).exists():
        messages.error(request, 'No se puede eliminar un viaje que ya tiene tickets vendidos.')
        return redirect('coordinator:trips_dashboard')
    trip.delete()
    messages.success(request, 'Viaje eliminado correctamente.')
    return redirect('coordinator:trips_dashboard')