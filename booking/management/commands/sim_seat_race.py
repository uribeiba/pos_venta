# booking/management/commands/sim_seat_race.py
from __future__ import annotations

import threading
import time
from typing import Optional

from django.core.management.base import BaseCommand, CommandError
from django.contrib.auth import get_user_model
from django.db import transaction, IntegrityError
from django.utils import timezone

from booking.models import Trip, Seat, SeatHold, Ticket

User = get_user_model()


def _get_or_create_user(username: str) -> User:
    user, _ = User.objects.get_or_create(
        username=username,
        defaults={"is_active": True, "is_staff": False, "is_superuser": False},
    )
    # Asegura contraseña por si quieres iniciar sesión con ellos luego.
    if not user.has_usable_password():
        user.set_password("pass1234")
        user.save(update_fields=["password"])
    return user


def _find_seat(trip: Trip, deck: int, number: str) -> Seat:
    return Seat.objects.get(bus=trip.bus, deck=deck, number=number)


def _reset_hold_for_test(trip: Trip, seat: Seat):
    # Limpia holds vencidos y libera holds activos para la prueba (solo del asiento target).
    SeatHold.cleanup()
    SeatHold.objects.filter(trip=trip, seat=seat, active=True).update(active=False)


class Command(BaseCommand):
    help = (
        "Simula una carrera entre dos vendedores por el mismo asiento.\n"
        "Ejemplos:\n"
        "  python manage.py sim_seat_race --trip 12 --deck 1 --number 10 --mode hold\n"
        "  python manage.py sim_seat_race --trip 12 --deck 1 --number 10 --mode purchase\n"
    )

    def add_arguments(self, parser):
        parser.add_argument("--trip", type=int, required=True, help="ID del Trip")
        parser.add_argument("--deck", type=int, required=True, help="Piso del asiento (1 o 2)")
        parser.add_argument("--number", type=str, required=True, help="Número del asiento (ej: '12')")
        parser.add_argument(
            "--mode",
            type=str,
            choices=["hold", "purchase"],
            default="hold",
            help="Modo de prueba: hold (bloqueo) o purchase (emisión)",
        )
        parser.add_argument(
            "--delay",
            type=float,
            default=0.05,
            help="Pequeño delay entre hilos para aumentar chances de carrera (segundos).",
        )

    def handle(self, *args, **opts):
        trip_id: int = opts["trip"]
        deck: int = opts["deck"]
        number: str = opts["number"]
        mode: str = opts["mode"]
        delay: float = float(opts["delay"])

        try:
            trip = Trip.objects.get(pk=trip_id)
        except Trip.DoesNotExist:
            raise CommandError(f"Trip {trip_id} no existe.")

        try:
            seat = _find_seat(trip, deck, number)
        except Seat.DoesNotExist:
            raise CommandError(f"Asiento no encontrado: deck={deck} number={number} para el bus del trip {trip_id}.")

        # Reset de estado SOLO para la prueba de hold.
        if mode == "hold":
            _reset_hold_for_test(trip, seat)

        # Para purchase, no vamos a "desocupar" si ya está ocupado: debe fallar si corresponde.
        if mode == "purchase" and Ticket.objects.filter(trip=trip, seat=seat).exists():
            self.stdout.write(self.style.WARNING("El asiento ya tiene ticket; la simulación de compra mostrará conflictos."))

        u1 = _get_or_create_user("vendedor_test_1")
        u2 = _get_or_create_user("vendedor_test_2")

        results = []

        def worker_hold(user: User, label: str):
            try:
                with transaction.atomic():
                    # select_for_update en los métodos calzará cuando corresponda
                    SeatHold.hold(trip=trip, seat=seat, user=user, minutes=1)
                results.append((label, "OK", "hold"))
            except Exception as e:
                results.append((label, "FAIL", f"{type(e).__name__}: {e}"))

        def worker_purchase(user: User, label: str):
            try:
                with transaction.atomic():
                    # Usa el helper seguro que creaste
                    Ticket.create_for_sale(
                        trip=trip,
                        seat=Seat.objects.select_for_update().get(pk=seat.pk),
                        buyer_name=f"Cliente {label}",
                        national_id=f"ID-{label}",
                        price=getattr(getattr(trip, "route", None), "base_price", 0) or 0,
                        created_by=user,
                    )
                results.append((label, "OK", "ticket"))
            except IntegrityError as e:
                # Suele ocurrir si dos hilos intentan a la vez -> UniqueConstraint salva
                results.append((label, "FAIL", f"IntegrityError: {e}"))
            except Exception as e:
                results.append((label, "FAIL", f"{type(e).__name__}: {e}"))

        # Prepara los hilos
        if mode == "hold":
            t1 = threading.Thread(target=worker_hold, args=(u1, "V1"), daemon=True)
            t2 = threading.Thread(target=worker_hold, args=(u2, "V2"), daemon=True)
        else:
            t1 = threading.Thread(target=worker_purchase, args=(u1, "V1"), daemon=True)
            t2 = threading.Thread(target=worker_purchase, args=(u2, "V2"), daemon=True)

        self.stdout.write(self.style.NOTICE(f"=== Simulando carrera ({mode.upper()}) sobre asiento {number} piso {deck} del Trip {trip_id} ==="))
        self.stdout.write(self.style.NOTICE("Lanzando V1..."))
        t1.start()
        time.sleep(delay)  # pequeño desfase para aumentar probabilidad de colisión
        self.stdout.write(self.style.NOTICE("Lanzando V2..."))
        t2.start()

        t1.join()
        t2.join()

        self.stdout.write(self.style.SUCCESS("=== Resultados ==="))
        for who, status, detail in results:
            if status == "OK":
                self.stdout.write(self.style.SUCCESS(f"{who}: {status} -> {detail}"))
            else:
                self.stdout.write(self.style.ERROR(f"{who}: {status} -> {detail}"))

        # Estado final
        holds = list(SeatHold.objects.filter(trip=trip, seat=seat, active=True))
        ticket_exists = Ticket.objects.filter(trip=trip, seat=seat).exists()
        self.stdout.write(self.style.NOTICE("--- Estado final ---"))
        self.stdout.write(f"Holds activos: {len(holds)}  |  Ticket existe: {ticket_exists}")
        if holds:
            users = ", ".join([f"{h.user.username} (expira {h.expires_at:%H:%M:%S})" for h in holds])
            self.stdout.write(f"Usuarios con hold: {users}")
