# booking/management/commands/list_pos_info.py
from django.core.management.base import BaseCommand
from django.utils import timezone
from booking.models import Trip, Seat, Ticket, SeatHold

class Command(BaseCommand):
    help = "Lista viajes próximos y muestra asientos disponibles por piso (primeros 20)."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=5, help="Número de trips a listar")
        parser.add_argument("--route", type=int, help="Filtrar por route_id (opcional)")

    def handle(self, *args, **opts):
        SeatHold.cleanup()
        qs = Trip.objects.order_by("departure")
        if opts.get("route"):
            qs = qs.filter(route_id=opts["route"])
        now = timezone.now()
        qs = qs.filter(departure__gte=now).order_by("departure")[: opts["limit"]]

        if not qs:
            self.stdout.write("No hay trips próximos.")
            return

        for t in qs:
            self.stdout.write(self.style.NOTICE(f"\nTrip {t.id} | Ruta: {t.route} | Sale: {t.departure:%Y-%m-%d %H:%M} | Bus {t.bus_id}"))
            taken = set(Ticket.objects.filter(trip=t).values_list("seat_id", flat=True))
            holds = {h.seat_id: h for h in SeatHold.objects.filter(trip=t, active=True)}
            for deck in (1, 2):
                seats = list(Seat.objects.filter(bus=t.bus, deck=deck).order_by("number").values("id","number"))
                if not seats:
                    continue
                libres, reservados, ocupados = [], [], []
                for s in seats:
                    sid = s["id"]
                    if sid in taken:
                        ocupados.append(s["number"])
                    elif sid in holds:
                        reservados.append(s["number"])
                    else:
                        libres.append(s["number"])
                self.stdout.write(
                    f"  Piso {deck}: libres({len(libres)}): {libres[:20]}  |  reservados({len(reservados)}): {reservados[:10]}  |  ocupados({len(ocupados)}): {len(ocupados)}"
                )
