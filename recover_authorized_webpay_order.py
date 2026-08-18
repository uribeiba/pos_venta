#!/usr/bin/env python3
import os, sys
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "bus_tickets.settings")

import django
django.setup()

from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import F
from booking.models import BookingOrder, PaymentTransaction, Promotion, Seat, SeatHold, Ticket

def recover_order(order_code):
    created_ticket_ids = []

    with transaction.atomic():
        order = BookingOrder.objects.select_for_update().get(code=order_code)
        payment = (
            PaymentTransaction.objects
            .select_for_update()
            .filter(order=order)
            .order_by("-created_at")
            .first()
        )

        if payment is None:
            raise RuntimeError("La orden no tiene PaymentTransaction.")

        if order.status == BookingOrder.STATUS_PAID:
            existing = list(
                Ticket.objects.filter(
                    trip=order.trip,
                    seat_id__in=order.items.values_list("seat_id", flat=True),
                ).values_list("id", flat=True)
            )
            print("La orden ya está PAID.")
            print("Tickets existentes:", existing)
            return

        if payment.status != PaymentTransaction.STATUS_AUTHORIZED:
            raise RuntimeError(f"Pago NO autorizado: {payment.status}")

        if payment.response_code != 0:
            raise RuntimeError(f"response_code inválido: {payment.response_code}")

        commit_data = (payment.raw_response or {}).get("commit", {})
        if commit_data.get("status") != "AUTHORIZED":
            raise RuntimeError("raw_response no indica AUTHORIZED.")

        if commit_data.get("buy_order") and commit_data.get("buy_order") != payment.buy_order:
            raise RuntimeError("buy_order de Transbank no coincide.")

        if commit_data.get("amount") is not None and int(commit_data.get("amount")) != int(order.total_amount):
            raise RuntimeError("Monto autorizado no coincide con la orden.")

        items = list(order.items.select_related("seat").order_by("seat_id"))
        if not items:
            raise RuntimeError("La orden no contiene asientos.")

        seat_ids = [item.seat_id for item in items]
        locked_seats = {
            s.id: s
            for s in Seat.objects.select_for_update().filter(pk__in=seat_ids).order_by("pk")
        }

        if len(locked_seats) != len(seat_ids):
            raise RuntimeError("No fue posible bloquear todos los asientos.")

        if Ticket.objects.filter(trip=order.trip, seat_id__in=seat_ids).exists():
            raise RuntimeError("DETENIDO: alguno de los asientos ya tiene Ticket.")

        system_user = User.objects.filter(username="ventas_web").first()
        if system_user is None:
            system_user = User.objects.filter(is_superuser=True).order_by("id").first()
        if system_user is None:
            raise RuntimeError("No existe usuario para registrar la venta web.")

        for item in items:
            seat = locked_seats[item.seat_id]
            passenger_full_name = f"{item.passenger_name} {item.passenger_lastname}".strip()

            ticket = Ticket.create_for_sale(
                trip=order.trip,
                seat=seat,
                buyer_name=passenger_full_name,
                national_id=item.passenger_document,
                price=item.price,
                created_by=system_user,
                payment_method="card",
                customer=order.customer,
            )
            created_ticket_ids.append(ticket.id)

        released = SeatHold.objects.filter(
            trip=order.trip,
            session_key=order.session_key,
            seat_id__in=seat_ids,
            active=True,
        ).update(active=False)

        if order.discount_code:
            promotion = (
                Promotion.objects.select_for_update()
                .filter(code__iexact=order.discount_code)
                .first()
            )
            if promotion:
                Promotion.objects.filter(pk=promotion.pk).update(
                    used_count=F("used_count") + 1
                )

        order.status = BookingOrder.STATUS_PAID
        order.save(update_fields=["status", "updated_at"])

    print("RECUPERACIÓN COMPLETADA")
    print("ORDER:", order.status)
    print("PAYMENT:", payment.status)
    print("TICKETS CREADOS:", created_ticket_ids)
    print("HOLDS DESACTIVADOS:", released)

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Uso: python recover_authorized_webpay_order.py RES-CODIGO")
        raise SystemExit(1)
    recover_order(sys.argv[1])