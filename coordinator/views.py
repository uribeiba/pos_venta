# coordinator/views.py
from django.contrib.auth.decorators import login_required, user_passes_test
from django.shortcuts import render, get_object_or_404, redirect
from django.http import JsonResponse
from django.contrib import messages
from django.db import transaction
from django.urls import reverse
from datetime import datetime
import json

from booking.models import Bus, Route, Trip, Company, Ticket, SeatHold, Seat
from django.views.decorators.http import require_POST
from django.views.decorators.csrf import csrf_exempt
from django.db.models import ProtectedError

# ---------------------------
# Permisos: solo coordinador o admin
# ---------------------------
def is_coordinator(user):
    if user.is_superuser:
        return True
    if hasattr(user, 'profile') and user.profile.role in ['admin', 'coordinator']:
        return True
    return False

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
            except:
                return []

        try:
            with transaction.atomic():

                # 🔹 Guardamos estado anterior (ANTES de modificar)
                old_bus = Bus.objects.filter(pk=bus.pk).first() if bus.pk else None

                # -------- DATOS BÁSICOS --------
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

                # -------- TAMAÑOS --------
                s_low = bus.rows_lower * bus.cols
                s_upp = bus.rows_upper * bus.cols

                # -------- JSON DEL FRONT --------
                l_low = safe_json_loads(request.POST.get('layout_lower'))
                l_upp = safe_json_loads(request.POST.get('layout_upper'))
                n_low = safe_json_loads(request.POST.get('numbers_lower'))
                n_upp = safe_json_loads(request.POST.get('numbers_upper'))

                # -------- NORMALIZACIÓN --------
                bus.layout_lower = (l_low + ['L'] * s_low)[:s_low]
                bus.numbers_lower = (n_low + [''] * s_low)[:s_low]

                if bus.floors == 2:
                    bus.layout_upper = (l_upp + ['L'] * s_upp)[:s_upp]
                    bus.numbers_upper = (n_upp + [''] * s_upp)[:s_upp]
                else:
                    bus.layout_upper = []
                    bus.numbers_upper = []

                # -------- GUARDAR BUS --------
                bus.save()

                # 🔥 SOLO regenerar si cambió la estructura (NO layout visual)
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

    # -------- SALIDA AL TEMPLATE --------
    companies = Company.objects.all()

    context = {
        'bus': bus,
        'companies': companies,

        # 🔥 ESTO ARREGLA EL ERROR JSON DEL FRONT
        'layout_lower_json': json.dumps(bus.layout_lower) if bus.layout_lower else "[]",
        'layout_upper_json': json.dumps(bus.layout_upper) if bus.layout_upper else "[]",
        'numbers_lower_json': json.dumps(bus.numbers_lower) if bus.numbers_lower else "[]",
        'numbers_upper_json': json.dumps(bus.numbers_upper) if bus.numbers_upper else "[]",
    }

    return render(request, 'coordinator/bus_editor.html', context)


@login_required
@user_passes_test(is_coordinator)
def bus_duplicate(request, bus_id):
    original = get_object_or_404(Bus, pk=bus_id)
    new_bus = Bus()
    for field in ['company', 'model', 'year', 'floors', 'rows_lower', 'rows_upper', 'cols',
                  'layout_lower', 'layout_upper', 'numbers_lower', 'numbers_upper', 'prefix_lower', 'prefix_upper']:
        setattr(new_bus, field, getattr(original, field))
    new_bus.plate = f"{original.plate} (copia)"
    new_bus.save()
    new_bus.ensure_layouts()
    new_bus.regenerate_seats()
    messages.success(request, f'Bus duplicado: {new_bus.plate}')
    return redirect('coordinator:bus_editor', bus_id=new_bus.id)


# ----- FUNCIONES DE ELIMINACIÓN ACTUALIZADAS -----
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
                # Eliminar tickets y holds de esos viajes
                Ticket.objects.filter(trip__in=trips).delete()
                SeatHold.objects.filter(trip__in=trips).delete()
                # Eliminar los viajes
                trips.delete()
                # Eliminar asientos (si no se eliminan automáticamente por CASCADE)
                Seat.objects.filter(bus=bus).delete()
                # Finalmente el bus
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
            trip.save()
            messages.success(request, 'Viaje guardado correctamente.')
            return redirect('coordinator:trip_list')
        except Exception as e:
            messages.error(request, f'Error: {str(e)}')
    routes = Route.objects.select_related('origin', 'destination').all()
    buses = Bus.objects.all()
    return render(request, 'coordinator/trip_form.html', {'trip': trip, 'routes': routes, 'buses': buses})