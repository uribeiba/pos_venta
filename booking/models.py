from __future__ import annotations

from datetime import timedelta
from typing import Optional

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models, transaction, connection
from django.dispatch import receiver
from django.db.models.signals import post_save
from django.utils import timezone
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _
from django.conf import settings

User = get_user_model()


# =========================================================
# Catálogos básicos
# =========================================================
class City(models.Model):
    name = models.CharField("Nombre", max_length=120, unique=True)
    slug = models.SlugField("Slug", max_length=140, unique=True, blank=True)

    class Meta:
        verbose_name = "Ciudad"
        verbose_name_plural = "Ciudades"
        ordering = ("name",)

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name or "")
        super().save(*args, **kwargs)


class Terminal(models.Model):
    """
    Terminal/Estación de buses de una ciudad.
    Es opcional para las rutas/viajes (puedes usar solo City).
    """
    nombre = models.CharField("Nombre", max_length=120, default="Terminal Principal")
    city = models.ForeignKey(City, on_delete=models.PROTECT, related_name="terminals", verbose_name="Ciudad")
    name = models.CharField("Nombre", max_length=160)
    address = models.CharField("Dirección", max_length=200, blank=True, default="")
    is_active = models.BooleanField("Activo", default=True)

    class Meta:
        verbose_name = "Terminal"
        verbose_name_plural = "Terminales"
        unique_together = (("city", "name"),)
        ordering = ("city__name", "name")

    def __str__(self):
        return self.name


class Company(models.Model):
    name = models.CharField("Nombre", max_length=140, unique=True)
    logo = models.URLField("Logo (URL)", blank=True)

    class Meta:
        verbose_name = "Empresa"
        verbose_name_plural = "Empresas"
        ordering = ("name",)

    def __str__(self) -> str:
        return self.name


# =========================================================
# Bus con diseño de asientos (layout)
# =========================================================
class Bus(models.Model):
    """
    Bus con layout por pisos. 
    Tipos: L (asiento), P (pasillo), X (bloqueo), E (escalera), D (puerta), B (baño)
    """
    company = models.ForeignKey(Company, verbose_name="Empresa", on_delete=models.PROTECT)

    plate = models.CharField("Patente", max_length=30, unique=True)
    model = models.CharField("Modelo", max_length=80, blank=True)
    year = models.PositiveIntegerField("Año", default=2024)

    floors = models.PositiveSmallIntegerField(
        "Pisos", default=1, validators=[MinValueValidator(1), MaxValueValidator(2)]
    )
    rows_upper = models.PositiveSmallIntegerField("Filas piso superior", default=0)
    rows_lower = models.PositiveSmallIntegerField("Filas piso inferior", default=10)
    cols = models.PositiveSmallIntegerField("Asientos por fila", default=4, validators=[MinValueValidator(1)])

    prefix_upper = models.CharField("Prefijo piso superior", max_length=5, blank=True, default="")
    prefix_lower = models.CharField("Prefijo piso inferior", max_length=5, blank=True, default="")

    layout_upper = models.JSONField("Layout superior", default=list, blank=True)
    layout_lower = models.JSONField("Layout inferior", default=list, blank=True)
    numbers_upper = models.JSONField("Números superiores", default=list, blank=True)
    numbers_lower = models.JSONField("Números inferiores", default=list, blank=True)

    layout_template = models.ForeignKey(
        "BusLayout",
        null=True, blank=True,
        on_delete=models.SET_NULL,
        verbose_name="Mapa (plantilla)"
    )

    class Meta:
        verbose_name = "Bus"
        verbose_name_plural = "Buses"
        ordering = ("plate",)

    def __str__(self):
        parts = []
        if getattr(self, "company", None):
            parts.append(self.company.name)
        if self.plate:
            parts.append(self.plate)
        return " — ".join(parts) or f"Bus #{self.pk}"

    # ======= Métodos efectivos =======
    def effective_floors(self) -> int:
        return int(self.floors or 1)

    def effective_cols(self) -> int:
        return int(self.cols or 4)

    def effective_rows_lower(self) -> int:
        return int(self.rows_lower or 0)

    def effective_rows_upper(self) -> int:
        return int(self.rows_upper or 0)

    # -------- Dimensiones --------
    def grid_len_lower(self) -> int:
        return self.effective_rows_lower() * self.effective_cols()

    def grid_len_upper(self) -> int:
        if self.effective_floors() < 2:
            return 0
        return self.effective_rows_upper() * self.effective_cols()

    def _iter_cells(self, rows: int):
        cols = self.effective_cols()
        for r in range(rows):
            for c in range(cols):
                yield r, c, (r * cols + c)

    # -------- Normalización Mejorada (Solo para nuevos o vacíos) --------
    def ensure_layouts(self):
        """
        Asegura que los arrays existan. Si están vacíos, los inicializa como Asientos (L).
        Si ya tienen datos (como tus pasillos 'P'), NO los toca.
        """
        gl_lower = self.grid_len_lower()
        if not self.layout_lower or len(self.layout_lower) == 0:
            self.layout_lower = ["L"] * gl_lower
            self.numbers_lower = [""] * gl_lower

        if self.effective_floors() >= 2:
            gl_upper = self.grid_len_upper()
            if not self.layout_upper or len(self.layout_upper) == 0:
                self.layout_upper = ["L"] * gl_upper
                self.numbers_upper = [""] * gl_upper

    # -------- Regeneración de asientos (REVISADO) --------
    def regenerate_seats(self) -> int:
        SeatModel = apps.get_model(self._meta.app_label, "Seat")
        TripModel = apps.get_model(self._meta.app_label, "Trip")

        with transaction.atomic():
            SeatModel.objects.filter(bus=self).delete()
            created = 0

            POSITIONS = ["A", "B", "C", "D", "E", "F"]

            def _create_for(deck_num: int, rows: int, layout: list, numbers: list, prefix: str):
                nonlocal created
                counter = 1

                for r, c, idx in self._iter_cells(rows):
                    if idx >= len(layout):
                        continue

                    typ = layout[idx]

                    if typ == "L":
                        custom_num = str(numbers[idx]).strip() if idx < len(numbers) else ""
                        number = custom_num if custom_num else f"{prefix}{counter}"
                        if not custom_num:
                            counter += 1

                        position = POSITIONS[c] if c < len(POSITIONS) else "A"

                        SeatModel.objects.create(
                            bus=self,
                            deck=deck_num,
                            row=r + 1,
                            position=position,
                            number=number,
                            is_occupied=False
                        )
                        created += 1

            _create_for(
                1,
                self.effective_rows_lower(),
                self.layout_lower,
                self.numbers_lower,
                self.prefix_lower
            )

            if self.effective_floors() >= 2:
                _create_for(
                    2,
                    self.effective_rows_upper(),
                    self.layout_upper,
                    self.numbers_upper,
                    self.prefix_upper
                )

            total = SeatModel.objects.filter(bus=self).count()
            TripModel.objects.filter(bus=self).update(seats_total=total)

        return created

    def save(self, *args, **kwargs):
        # Aseguramos que existan layouts básicos antes de guardar si es nuevo
        if not self.pk:
            self.ensure_layouts()
        super().save(*args, **kwargs)


# =========================================================
# Rutas y viajes
# =========================================================
class Route(models.Model):
    origin = models.ForeignKey(
        City, verbose_name="Origen",
        on_delete=models.PROTECT, related_name="routes_from"
    )
    destination = models.ForeignKey(
        City, verbose_name="Destino",
        on_delete=models.PROTECT, related_name="routes_to"
    )
    origin_terminal = models.ForeignKey(
        Terminal, verbose_name="Terminal origen",
        on_delete=models.PROTECT, related_name="routes_from", null=True, blank=True
    )
    destination_terminal = models.ForeignKey(
        Terminal, verbose_name="Terminal destino",
        on_delete=models.PROTECT, related_name="routes_to", null=True, blank=True
    )
    duration_minutes = models.PositiveIntegerField("Duración (min)", default=120)
    base_price = models.DecimalField("Precio base", max_digits=10, decimal_places=2)

    class Meta:
        unique_together = ("origin", "destination", "origin_terminal", "destination_terminal")
        verbose_name = "Ruta"
        verbose_name_plural = "Rutas"
        ordering = ("origin__name", "destination__name")

    def __str__(self) -> str:
        base = f"{self.origin} → {self.destination}"
        if self.origin_terminal or self.destination_terminal:
            t1 = f" ({self.origin_terminal.name})" if self.origin_terminal else ""
            t2 = f" ({self.destination_terminal.name})" if self.destination_terminal else ""
            base += f"{t1} →{t2}"
        return base


class Trip(models.Model):
    route = models.ForeignKey(Route, on_delete=models.CASCADE, related_name="trips")
    bus = models.ForeignKey(Bus, on_delete=models.PROTECT, related_name="trips")
    departure = models.DateTimeField("Salida")
    arrival = models.DateTimeField("Llegada")
    seats_total = models.PositiveIntegerField("Asientos totales", default=0)

    class Meta:
        verbose_name = "Viaje"
        verbose_name_plural = "Viajes"
        ordering = ("-departure",)

    def __str__(self) -> str:
        return f"{self.route} {self.departure:%Y-%m-%d %H:%M}"


# =========================================================
# Asientos, Holds y Tickets
# =========================================================
class Seat(models.Model):
    bus = models.ForeignKey(Bus, on_delete=models.CASCADE, related_name="seats")
    deck = models.PositiveSmallIntegerField("Piso", default=1)
    row = models.PositiveSmallIntegerField("Fila")
    position = models.CharField(
        "Posición", max_length=1,
        choices=[("A","A"),("B","B"),("C","C"),("D","D"),("E","E"),("F","F")]
    )
    number = models.CharField("Número", max_length=10)
    is_window = models.BooleanField("Ventana", default=False)
    is_occupied = models.BooleanField("Ocupado", default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["bus", "deck", "number"], name="uniq_seat_per_bus_deck_number")
        ]
        verbose_name = "Asiento"
        verbose_name_plural = "Asientos"
        ordering = ("bus__plate", "deck", "row", "position")

    def __str__(self) -> str:
        return f"{self.bus.plate} D{self.deck} #{self.number}"


class SeatHold(models.Model):
    trip = models.ForeignKey(Trip, on_delete=models.CASCADE, related_name="holds")
    seat = models.ForeignKey("Seat", on_delete=models.CASCADE, related_name="holds")
    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name="seat_holds")
    expires_at = models.DateTimeField()
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["trip", "seat"]),
            models.Index(fields=["active"]),
            models.Index(fields=["expires_at"]),
        ]
        verbose_name = "Bloqueo temporal"
        verbose_name_plural = "Bloqueos temporales"

    def __str__(self) -> str:
        return f"{self.trip} — {self.seat.number} ({'activo' if self.active else 'inactivo'})"

    @classmethod
    def cleanup(cls):
        now = timezone.now()
        cls.objects.filter(active=True, expires_at__lte=now).update(active=False)

    @classmethod
    def hold(cls, trip, seat, user, minutes: int = 10):
        from datetime import timedelta
        from django.db import transaction
        from .models import Ticket

        now = timezone.now()
        new_expire = now + timedelta(minutes=minutes)

        with transaction.atomic():
            cls.cleanup()
            s = Seat.objects.select_for_update().get(pk=seat.pk)

            if Ticket.objects.filter(trip=trip, seat=s).exists() or getattr(s, "is_occupied", False):
                raise ValueError("Asiento ocupado.")

            active_qs = cls.objects.select_for_update().filter(
                trip=trip, seat=s, active=True, expires_at__gt=now
            ).order_by("-expires_at")

            if active_qs.exists():
                h = active_qs.first()
                if h.user_id != user.id:
                    raise ValueError("Asiento temporalmente bloqueado por otro vendedor.")
                h.expires_at = new_expire
                h.active = True
                h.save(update_fields=["expires_at", "active"])
                return h

            return cls.objects.create(
                trip=trip,
                seat=s,
                user=user,
                expires_at=new_expire,
                active=True,
            )

    @classmethod
    def release(cls, trip, seat, user) -> int:
        from django.db import transaction
        with transaction.atomic():
            return cls.objects.filter(
                trip=trip, seat=seat, user=user, active=True
            ).update(active=False)


class Ticket(models.Model):
    trip = models.ForeignKey(Trip, on_delete=models.PROTECT, related_name="tickets")
    seat = models.ForeignKey(Seat, on_delete=models.PROTECT, related_name="tickets")
    number = models.CharField("N° ticket", max_length=20, unique=True)
    buyer_name = models.CharField("Nombre pasajero", max_length=140)
    national_id = models.CharField("Documento", max_length=40, blank=True, default="")

    customer = models.ForeignKey(
        'Customer',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        verbose_name="Cliente",
        related_name="tickets"
    )

    PAYMENT_METHOD_CHOICES = [
        ("cash", "Efectivo"),
        ("card", "Tarjeta"),
    ]
    payment_method = models.CharField(
        "Método de pago",
        max_length=10,
        choices=PAYMENT_METHOD_CHOICES,
        default="cash",
        db_index=True,
    )

    price = models.DecimalField(max_digits=10, decimal_places=2)
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="tickets_sold")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["trip"]),
            models.Index(fields=["created_at"]),
            models.Index(fields=["payment_method"]),
        ]
        verbose_name = "Boleto"
        verbose_name_plural = "Boletos"
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(fields=["trip", "seat"], name="uniq_ticket_trip_seat"),
        ]

    def __str__(self):
        return f"{self.number} — {self.seat.number} ({self.trip})"

    @staticmethod
    def _next_number() -> str:
        with transaction.atomic():
            cur = connection.cursor()
            cur.execute("SELECT number FROM booking_ticket ORDER BY id DESC LIMIT 1")
            row = cur.fetchone()
            last = 0
            if row and row[0]:
                digits = "".join(ch for ch in row[0] if ch.isdigit())
                last = int(digits) if digits else 0
            return f"T-{last+1:06d}"

    @classmethod
    def create_for_sale(
        cls, *, trip: Trip, seat: Seat, buyer_name: str,
        national_id: str, price, created_by: User, number: Optional[str] = None,
        customer=None, **extra
    ):
        from django.core.exceptions import ValidationError

        if not created_by:
            raise ValidationError("created_by es obligatorio para emitir un boleto.")

        with transaction.atomic():
            s = Seat.objects.select_for_update().get(pk=seat.pk)

            if cls.objects.filter(trip=trip, seat=s).exists():
                raise ValidationError("El asiento ya está ocupado para este viaje.")

            SeatHold.objects.filter(trip=trip, seat=s, user=created_by, active=True).update(active=False)

            number = number or cls._next_number()

            if not customer and national_id:
                try:
                    from .models import Customer
                    customer_obj, _ = Customer.objects.get_or_create(
                        national_id=national_id,
                        defaults={'full_name': buyer_name}
                    )
                    customer = customer_obj
                except Exception:
                    customer = None

            if "payment_method" in extra and not (extra.get("payment_method") or "").strip():
                extra.pop("payment_method", None)

            t = cls.objects.create(
                trip=trip,
                seat=s,
                number=number,
                buyer_name=(buyer_name or "").strip() or "Pasajero",
                national_id=(national_id or "").strip(),
                price=price,
                created_by=created_by,
                customer=customer,
                **extra,
            )

            if hasattr(s, "is_occupied"):
                s.is_occupied = True
                s.save(update_fields=["is_occupied"])

            return t

    @classmethod
    def purchase(cls, trip: Trip, seat_ids, buyer_name, national_id, user, customer=None, payment_method="cash"):
        SeatHold.cleanup()
        seat_ids = list(seat_ids or [])
        if not seat_ids:
            raise ValueError("No hay asientos seleccionados.")

        payment_method = (payment_method or "cash").strip() or "cash"

        with transaction.atomic():
            seats = list(Seat.objects.select_for_update().filter(id__in=seat_ids))
            if len(seats) != len(seat_ids):
                raise ValueError("Alguno de los asientos ya no existe.")

            for s in seats:
                if s.bus_id != trip.bus_id:
                    raise ValueError("Algún asiento no pertenece a este viaje.")
                if getattr(s, "is_occupied", False):
                    raise ValueError(f"El asiento {s.number} ya está ocupado.")
                hold_other = SeatHold.objects.filter(trip=trip, seat=s, active=True).exclude(user=user).exists()
                if hold_other:
                    raise ValueError(f"El asiento {s.number} está bloqueado por otro vendedor.")

            if not customer and national_id:
                try:
                    from .models import Customer
                    customer_obj, created = Customer.objects.get_or_create(
                        national_id=national_id,
                        defaults={'full_name': buyer_name}
                    )
                    customer = customer_obj
                except Exception:
                    customer = None

            tickets = []
            unit_price = getattr(trip.route, "base_price", 0)
            for s in seats:
                t = cls.objects.create(
                    trip=trip,
                    seat=s,
                    number=cls._next_number(),
                    buyer_name=(buyer_name or "").strip() or "Pasajero",
                    national_id=(national_id or "").strip(),
                    price=unit_price,
                    created_by=user,
                    customer=customer,
                    payment_method=payment_method,
                )
                tickets.append(t)

                if hasattr(s, "is_occupied"):
                    s.is_occupied = True
                    s.save(update_fields=["is_occupied"])

                SeatHold.objects.filter(trip=trip, seat=s, active=True).update(active=False)

            return tickets

    def get_or_create_customer(self):
        if self.customer:
            return self.customer
        if self.national_id:
            try:
                from .models import Customer
                customer, created = Customer.objects.get_or_create(
                    national_id=self.national_id,
                    defaults={'full_name': self.buyer_name}
                )
                self.customer = customer
                self.save(update_fields=['customer'])
                return customer
            except Exception as e:
                print(f"Error creando cliente: {e}")
        return None

    @property
    def customer_name(self):
        if self.customer:
            return self.customer.full_name
        return self.buyer_name

    @property
    def customer_rut(self):
        if self.customer:
            return self.customer.national_id
        return self.national_id


class BusLayout(models.Model):
    name = models.CharField("Nombre del mapa", max_length=120, unique=True)
    slug = models.SlugField(max_length=140, unique=True, blank=True)

    floors = models.PositiveSmallIntegerField(default=1)
    rows_lower = models.PositiveSmallIntegerField(default=0)
    rows_upper = models.PositiveSmallIntegerField(default=0)
    cols = models.PositiveSmallIntegerField(default=4)

    layout_lower  = models.JSONField(default=list, blank=True)
    layout_upper  = models.JSONField(default=list, blank=True)
    numbers_lower = models.JSONField(default=list, blank=True)
    numbers_upper = models.JSONField(default=list, blank=True)

    prefix_lower = models.CharField(max_length=10, blank=True, default="")
    prefix_upper = models.CharField(max_length=10, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Mapa de bus"
        verbose_name_plural = "Mapas de bus"
        ordering = ("name",)

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.name)[:130]
            slug = base
            i = 1
            while BusLayout.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                slug = f"{base}-{i}"
                i += 1
            self.slug = slug
        super().save(*args, **kwargs)


# =========================================================
# Modelos de Caja y Reportes
# =========================================================
class CashRegister(models.Model):
    user = models.ForeignKey(User, on_delete=models.PROTECT, verbose_name="Vendedor")
    opening_date = models.DateTimeField("Fecha apertura", auto_now_add=True)
    closing_date = models.DateTimeField("Fecha cierre", null=True, blank=True)
    opening_balance = models.DecimalField("Saldo inicial", max_digits=10, decimal_places=2, default=0)
    closing_balance = models.DecimalField("Saldo final", max_digits=10, decimal_places=2, null=True, blank=True)
    total_sales = models.DecimalField("Total ventas", max_digits=10, decimal_places=2, default=0)
    total_tickets = models.PositiveIntegerField("Total boletos", default=0)
    status = models.CharField("Estado", max_length=20, choices=[
        ('open', 'Abierta'),
        ('closed', 'Cerrada')
    ], default='open')

    class Meta:
        verbose_name = "Caja"
        verbose_name_plural = "Cajas"
        ordering = ['-opening_date']

    def __str__(self):
        return f"Caja {self.user.username} - {self.opening_date.date()}"


class DailyReport(models.Model):
    date = models.DateField("Fecha", unique=True)
    total_tickets = models.PositiveIntegerField("Total boletos", default=0)
    total_revenue = models.DecimalField("Ingresos totales", max_digits=12, decimal_places=2, default=0)
    total_cash_registers = models.PositiveIntegerField("Cajas abiertas", default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Reporte Diario"
        verbose_name_plural = "Reportes Diarios"
        ordering = ['-date']

    def __str__(self):
        return f"Reporte {self.date}"


# =========================================================
# Perfil de Usuario (UNIFICADO)
# =========================================================
class UserProfile(models.Model):
    ROLE_CHOICES = (
        ('admin', 'Administrador'),
        ('supervisor', 'Supervisor'),
        ('coordinator', 'Coordinador'), 
        ('vendedor', 'Vendedor'),
        ('cajero', 'Cajero'),
    )

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='profile',
        verbose_name='Usuario',
    )
    role = models.CharField(
        "Rol",
        max_length=20,
        choices=ROLE_CHOICES,
        default='vendedor',
        db_index=True,
    )
    terminal = models.ForeignKey(
        Terminal,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        verbose_name="Terminal asignada",
        db_index=True,
    )
    is_active = models.BooleanField("Activo", default=True, db_index=True)
    commission_rate = models.DecimalField(
        "Porcentaje de comisión",
        max_digits=5,
        decimal_places=2,
        default=0.00,
        help_text="Porcentaje (%) — ej.: 2.50",
    )
    max_discount = models.DecimalField(
        "Descuento máximo permitido",
        max_digits=7,
        decimal_places=2,
        default=0.00,
        help_text="Monto en CLP — ej.: 1500.00",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Perfil de Usuario"
        verbose_name_plural = "Perfiles de Usuario"
        indexes = [
            models.Index(fields=['role']),
            models.Index(fields=['is_active']),
            models.Index(fields=['terminal']),
        ]

    def __str__(self) -> str:
        try:
            role_display = self.get_role_display()
        except Exception:
            role_display = self.role
        return f"{getattr(self.user, 'username', 'user')} - {role_display}"


# =========================================================
# Señales: creación/actualización automática del perfil
# =========================================================
# (Comentadas – se pueden activar si se desea crear perfil automáticamente)
# @receiver(post_save, sender=settings.AUTH_USER_MODEL)
# def create_or_update_user_profile(sender, instance, created, **kwargs):
#     if created:
#         UserProfile.objects.create(user=instance)
#     else:
#         UserProfile.objects.get_or_create(user=instance)
#         if hasattr(instance, 'profile'):
#             instance.profile.save()


# =========================================================
# Modelo de Cliente para ventas recurrentes
# =========================================================
class Customer(models.Model):
    national_id = models.CharField("RUT/Documento", max_length=40, unique=True, db_index=True)
    full_name = models.CharField("Nombre completo", max_length=140)
    phone = models.CharField("Teléfono", max_length=20, blank=True, default="")
    email = models.EmailField("Email", blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Cliente"
        verbose_name_plural = "Clientes"
        ordering = ("full_name",)

    def __str__(self):
        return f"{self.full_name} ({self.national_id})"

    def save(self, *args, **kwargs):
        if self.national_id:
            self.national_id = self._clean_rut(self.national_id)
        super().save(*args, **kwargs)

    @staticmethod
    def _clean_rut(rut: str) -> str:
        rut = rut.upper().replace(".", "").replace("-", "").strip()
        if rut and rut[-1] in "0123456789K":
            return rut
        return rut