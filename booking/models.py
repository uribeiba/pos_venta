from __future__ import annotations

from datetime import timedelta

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
        return self.nombre

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
    Bus con layout por pisos. El diseñador del admin escribe/lee:
      - layout_lower/layout_upper: listas lineales filas*columnas (L/P/X/E/D/B)
      - numbers_lower/numbers_upper: listas lineales con numeración manual (o "")
    Tipos:
      L = asiento (seat)
      P = pasillo (se deja vacío, sin borde)
      X = bloqueo (celda no usada)
      E = escalera
      D = puerta
      B = baño
    """
    company = models.ForeignKey(Company, verbose_name="Empresa", on_delete=models.PROTECT)

    plate = models.CharField("Patente", max_length=30, unique=True)
    model = models.CharField("Modelo", max_length=80, blank=True)
    year = models.PositiveIntegerField("Año", default=2024)

    floors = models.PositiveSmallIntegerField(
        "Pisos", default=1, validators=[MinValueValidator(1), MaxValueValidator(2)]
    )
    rows_upper = models.PositiveSmallIntegerField("Filas piso superior", default=0)   # si floors==2
    rows_lower = models.PositiveSmallIntegerField("Filas piso inferior", default=10)
    cols = models.PositiveSmallIntegerField("Asientos por fila", default=4, validators=[MinValueValidator(1)])

    # Prefijos opcionales para numeración autogenerada
    prefix_upper = models.CharField("Prefijo piso superior", max_length=5, blank=True, default="")
    prefix_lower = models.CharField("Prefijo piso inferior", max_length=5, blank=True, default="")

    # Layout y numeración (listas lineales)
    layout_upper = models.JSONField("Layout superior", default=list, blank=True)
    layout_lower = models.JSONField("Layout inferior", default=list, blank=True)
    numbers_upper = models.JSONField("Números superiores", default=list, blank=True)
    numbers_lower = models.JSONField("Números inferiores", default=list, blank=True)

    # -----------------------------------------------------
    # NUEVO: plantilla reutilizable (opcional)
    # -----------------------------------------------------
    layout_template = models.ForeignKey(
        "BusLayout",  # modelo nuevo reutilizable
        null=True, blank=True,
        on_delete=models.SET_NULL,
        verbose_name="Mapa (plantilla)"
    )

    class Meta:
        verbose_name = "Bus"
        verbose_name_plural = "Buses"
        ordering = ("plate",)

    # <<< NUEVO: representación amigable para selects del admin >>>
    def __str__(self):
        parts = []
        if getattr(self, "company", None):
            parts.append(self.company.name)
        if self.plate:
            parts.append(self.plate)
        if self.model:
            parts.append(self.model)
        return " — ".join(parts) or f"Bus #{self.pk}"

    # ======= Helpers "efectivos" (plantilla > datos propios) =======
    def effective_floors(self) -> int:
        return int(self.layout_template.floors) if self.layout_template else int(self.floors or 1)

    def effective_cols(self) -> int:
        return int(self.layout_template.cols) if self.layout_template else int(self.cols or 0)

    def effective_rows_lower(self) -> int:
        return int(self.layout_template.rows_lower) if self.layout_template else int(self.rows_lower or 0)

    def effective_rows_upper(self) -> int:
        return int(self.layout_template.rows_upper) if self.layout_template else int(self.rows_upper or 0)

    def effective_layout_lower(self) -> list:
        return list(self.layout_template.layout_lower) if self.layout_template else list(self.layout_lower or [])

    def effective_layout_upper(self) -> list:
        return list(self.layout_template.layout_upper) if self.layout_template else list(self.layout_upper or [])

    def effective_numbers_lower(self) -> list:
        return list(self.layout_template.numbers_lower) if self.layout_template else list(self.numbers_lower or [])

    def effective_numbers_upper(self) -> list:
        return list(self.layout_template.numbers_upper) if self.layout_template else list(self.numbers_upper or [])

    # -------- Helpers de dimensiones --------
    def grid_len_lower(self) -> int:
        # usa dimensiones efectivas (soporta plantilla)
        return max(int(self.effective_rows_lower() or 0), 0) * int(self.effective_cols() or 0)

    def grid_len_upper(self) -> int:
        # usa dimensiones efectivas (soporta plantilla)
        if int(self.effective_floors() or 1) < 2:
            return 0
        return max(int(self.effective_rows_upper() or 0), 0) * int(self.effective_cols() or 0)

    def _idx(self, row: int, col: int) -> int:
        return (row * int(self.effective_cols() or 0)) + col

    def _iter_cells(self, rows: int):
        cols = int(self.effective_cols() or 0)
        for r in range(rows):
            for c in range(cols):
                yield r, c, self._idx(r, c)

    # -------- Normalización que preserva --------
    def ensure_layouts(self, fill_with: str = "L") -> None:
        """
        Ajusta/crea layouts y arrays de numeración al tamaño correcto,
        **preservando** el contenido existente.

        Si el bus usa plantilla (layout_template), COPIA los datos de la plantilla.
        """
        if self.layout_template:
            # COPIAR datos de la plantilla al bus
            template = self.layout_template
            self.floors = template.floors
            self.rows_lower = template.rows_lower
            self.rows_upper = template.rows_upper
            self.cols = template.cols
            self.layout_lower = list(template.layout_lower or [])
            self.layout_upper = list(template.layout_upper or [])
            self.numbers_lower = list(template.numbers_lower or [])
            self.numbers_upper = list(template.numbers_upper or [])
            self.prefix_lower = template.prefix_lower or ""
            self.prefix_upper = template.prefix_upper or ""
            return  # No necesitamos redimensionar, la plantilla ya tiene tamaños correctos

        def _resize_preserve(seq: list, new_len: int, filler):
            if not isinstance(seq, list):
                seq = []
            out = list(seq[:new_len])  # recorta si sobra
            while len(out) < new_len:  # rellena si falta
                out.append(filler)
            return out

        gl_lower = self.grid_len_lower()
        gl_upper = self.grid_len_upper()

        # números (strings)
        self.numbers_lower = _resize_preserve(self.numbers_lower, gl_lower, "")
        self.numbers_upper = _resize_preserve(self.numbers_upper, gl_upper, "")

        # layout (L/P/X/E/D/B)
        self.layout_lower = _resize_preserve(self.layout_lower, gl_lower, fill_with)
        self.layout_upper = _resize_preserve(self.layout_upper, gl_upper, fill_with)

    # -------- Generación de seats desde layout --------
    def regenerate_seats(self) -> int:
        """
        Recrea los asientos del bus a partir de los layouts y numeraciones.
        - Borra los asientos previos del bus.
        - Crea asiento por cada celda que NO sea 'P' ni 'X' ni 'E'/'D'/'B' (elementos estructurales).
        - Calcula ventana por extremos de la fila.
        - Respeta números manuales; si vacío → correlativo + prefijo por piso.
        Devuelve la cantidad creada.
        """
        SeatModel = apps.get_model(self._meta.app_label, "Seat")
        TripModel = apps.get_model(self._meta.app_label, "Trip")

        # Asegurar que los layouts estén sincronizados
        self.ensure_layouts()

        # datos efectivos (plantilla > propios)
        rows_lower = self.effective_rows_lower()
        rows_upper = self.effective_rows_upper()
        cols = self.effective_cols()
        floors = self.effective_floors()

        layout_lower = self.effective_layout_lower()
        layout_upper = self.effective_layout_upper()
        numbers_lower = self.effective_numbers_lower()
        numbers_upper = self.effective_numbers_upper()

        # Borrar asientos existentes
        SeatModel.objects.filter(bus=self).delete()

        created = 0
        letters = ["A", "B", "C", "D", "E", "F"][:max(int(cols or 0), 1)]

        def _create_for(rows: int, layout: list, numbers: list, deck: int, prefix: str):
            nonlocal created
            n = 1
            for r, c, i in self._iter_cells(int(rows or 0)):
                if i >= len(layout):
                    continue
                    
                typ = (layout[i] if i < len(layout) else "L") or "L"
                # saltar pasillos/bloqueos/escaleras/puertas/baño
                if typ in ("P", "X", "E", "D", "B"):
                    continue
                    
                # Usar número de la plantilla o generar uno automático
                label = (numbers[i] if i < len(numbers) else "").strip()
                if label:
                    number = label
                else:
                    number = f"{(prefix or '')}{n:02d}"
                    n += 1
                    
                is_window = (c == 0) or (c == int(cols or 0) - 1)
                pos_letter = letters[c] if 0 <= c < len(letters) else letters[-1]

                SeatModel.objects.create(
                    bus=self, 
                    deck=deck, 
                    row=r + 1, 
                    position=pos_letter,
                    number=number, 
                    is_window=is_window
                )
                created += 1

        # Piso 1
        _create_for(rows_lower, layout_lower, numbers_lower, deck=1, prefix=self.prefix_lower)

        # Piso 2
        if int(floors or 1) == 2 and int(rows_upper or 0) > 0:
            _create_for(rows_upper, layout_upper, numbers_upper, deck=2, prefix=self.prefix_upper)

        # Actualizar seats_total en viajes de este bus (si existe)
        if hasattr(TripModel, "seats_total"):
            total = SeatModel.objects.filter(bus=self).count()
            TripModel.objects.filter(bus=self).update(seats_total=total)

        return created


# -------------------------
# Compatibilidad (fallback)
# -------------------------
def generate_seats_for_bus(bus: Bus):
    """
    Si el bus tiene layout definido, usa bus.regenerate_seats().
    Si no, genera asientos por cuadrícula 2D simple (compatibilidad).
    """
    try:
        if bus.layout_lower or bus.layout_upper or bus.layout_template:
            return bus.regenerate_seats()
    except Exception:
        pass

    SeatModel = apps.get_model(bus._meta.app_label, "Seat")
    TripModel = apps.get_model(bus._meta.app_label, "Trip")

    SeatModel.objects.filter(bus=bus).delete()
    letters = ["A", "B", "C", "D"][:max(int(bus.cols or 0), 1)]
    created = 0

    # Piso 1
    for r in range(1, int(bus.rows_lower or 0) + 1):
        for ci, pos in enumerate(letters, start=1):
            num = f"{(bus.prefix_lower or '')}{((r - 1) * len(letters) + ci):02d}"
            SeatModel.objects.create(
                bus=bus, deck=1, row=r, position=pos,
                number=num, is_window=(ci == 1 or ci == len(letters))
            )
            created += 1

    # Piso 2
    if int(bus.floors or 1) == 2 and int(bus.rows_upper or 0) > 0:
        for r in range(1, int(bus.rows_upper or 0) + 1):
            for ci, pos in enumerate(letters, start=1):
                num = f"{(bus.prefix_upper or '')}{((r - 1) * len(letters) + ci):02d}"
                SeatModel.objects.create(
                    bus=bus, deck=2, row=r, position=pos,
                    number=num, is_window=(ci == 1 or ci == len(letters))
                )
                created += 1

    total = SeatModel.objects.filter(bus=bus).count()
    TripModel.objects.filter(bus=bus).update(seats_total=total)
    return created


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

    # Terminales (opcionales)
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
    route = models.ForeignKey(
        Route, verbose_name="Ruta",
        on_delete=models.CASCADE, related_name="trips"
    )
    bus = models.ForeignKey(
        Bus, verbose_name="Bus",
        on_delete=models.PROTECT, related_name="trips"
    )
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
    bus = models.ForeignKey(Bus, verbose_name="Bus", on_delete=models.CASCADE, related_name="seats")
    deck = models.PositiveSmallIntegerField("Piso", default=1)
    row = models.PositiveSmallIntegerField("Fila")
    position = models.CharField("Posición", max_length=1, choices=[("A","A"),("B","B"),("C","C"),("D","D"),("E","E"),("F","F")])
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

    # Limpieza automática de holds vencidos
    @classmethod
    def cleanup(cls):
        now = timezone.now()
        cls.objects.filter(active=True, expires_at__lte=now).update(active=False)

    # ===== NUEVO: crear/renovar hold seguro =====
    @classmethod
    def hold(cls, trip, seat, user, minutes: int = 10):
        """
        Crea o RENUEVA un hold para (trip, seat) del usuario.
        - Falla si el asiento ya está ocupado (tiene Ticket) o tiene hold activo de otro usuario.
        - Si el hold activo es del mismo usuario, extiende el vencimiento.
        Retorna la instancia SeatHold creada/renovada.
        Lanza ValueError con mensaje claro si no se puede bloquear.
        """
        from datetime import timedelta
        from django.db import transaction
        # Import interno para evitar ciclos
        from .models import Ticket, Seat

        now = timezone.now()
        new_expire = now + timedelta(minutes=minutes)

        with transaction.atomic():
            # Limpieza previa de vencidos
            cls.cleanup()

            # Bloqueo de la fila del asiento para carrera
            s = Seat.objects.select_for_update().get(pk=seat.pk)

            # Si ya existe Ticket para ese viaje/asiento -> ocupado
            if Ticket.objects.filter(trip=trip, seat=s).exists() or getattr(s, "is_occupied", False):
                raise ValueError("Asiento ocupado.")

            # ¿Hay hold activo?
            active_qs = cls.objects.select_for_update().filter(
                trip=trip, seat=s, active=True, expires_at__gt=now
            ).order_by("-expires_at")

            if active_qs.exists():
                h = active_qs.first()
                # Si el hold es de otro usuario, no permitir
                if h.user_id != user.id:
                    raise ValueError("Asiento temporalmente bloqueado por otro vendedor.")
                # Si es del mismo usuario, renovar
                h.expires_at = new_expire
                h.active = True
                h.save(update_fields=["expires_at", "active"])
                return h

            # Crear hold nuevo
            return cls.objects.create(
                trip=trip,
                seat=s,
                user=user,
                expires_at=new_expire,
                active=True,
            )

    # ===== NUEVO: liberar hold del usuario =====
    @classmethod
    def release(cls, trip, seat, user) -> int:
        """
        Desactiva el hold ACTIVO del usuario sobre (trip, seat).
        Retorna la cantidad de filas actualizadas (0 o 1).
        """
        from django.db import transaction
        with transaction.atomic():
            return cls.objects.filter(
                trip=trip, seat=seat, user=user, active=True
            ).update(active=False)



class Ticket(models.Model):
    """Ticket definitivo (compra)."""
    trip = models.ForeignKey(Trip, on_delete=models.PROTECT, related_name="tickets")
    seat = models.ForeignKey(Seat, on_delete=models.PROTECT, related_name="tickets")
    number = models.CharField("N° ticket", max_length=20, unique=True)
    buyer_name = models.CharField("Nombre pasajero", max_length=140)
    national_id = models.CharField("Documento", max_length=40, blank=True, default="")

    # ✅ NUEVO: Relación con cliente (manteniendo compatibilidad con datos existentes)
    customer = models.ForeignKey(
        'Customer',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name="Cliente",
        related_name="tickets"
    )

    # ✅ NUEVO: Método de pago
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
            # ✅ OPCIONAL pero útil: filtrar por método de pago rápido
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
        """Genera correlativo simple tipo T-000001."""
        with transaction.atomic():
            cur = connection.cursor()
            cur.execute("SELECT number FROM booking_ticket ORDER BY id DESC LIMIT 1")
            row = cur.fetchone()
            last = 0
            if row and row[0]:
                digits = "".join(ch for ch in row[0] if ch.isdigit())
                last = int(digits) if digits else 0
            return f"T-{last+1:06d}"

    # ✅ NUEVO: creador seguro (útil si emites uno por uno en la vista)
    @classmethod
    def create_for_sale(
        cls, *, trip: Trip, seat: Seat, buyer_name: str,
        national_id: str, price, created_by: User, number: str | None = None,
        customer=None, **extra
    ):
        """
        Emite un ticket de forma segura:
        - exige created_by;
        - verifica que no exista ticket previo para (trip, seat);
        - libera el hold del propio usuario si existe;
        - marca seat.is_occupied = True.
        - ✅ soporta relación con Customer
        - ✅ soporta payment_method vía **extra sin romper llamadas existentes
        """
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
                    customer_obj, created = Customer.objects.get_or_create(
                        national_id=national_id,
                        defaults={'full_name': buyer_name}
                    )
                    customer = customer_obj
                except Exception:
                    customer = None

            # ✅ Blindaje: si llega payment_method vacío, que no rompa ni guarde vacío
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
                **extra,  # ✅ aquí entra payment_method si se envía desde la vista
            )

            if hasattr(s, "is_occupied"):
                s.is_occupied = True
                s.save(update_fields=["is_occupied"])

            return t

    @classmethod
    def purchase(cls, trip: Trip, seat_ids, buyer_name, national_id, user, customer=None, payment_method="cash"):
        """
        Crea tickets para la selección, validando disponibilidad y holds.
        Marca Seat.is_occupied=True.
        ✅ soporta relación con Customer
        ✅ guarda método de pago (sin romper llamadas viejas: tiene default)
        """
        SeatHold.cleanup()
        seat_ids = list(seat_ids or [])
        if not seat_ids:
            raise ValueError("No hay asientos seleccionados.")

        # ✅ Blindaje por si llega "" desde el form
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
                    payment_method=payment_method,  # ✅ NUEVO
                )
                tickets.append(t)

                if hasattr(s, "is_occupied"):
                    s.is_occupied = True
                    s.save(update_fields=["is_occupied"])

                SeatHold.objects.filter(trip=trip, seat=s, active=True).update(active=False)

            return tickets

    def get_or_create_customer(self):
        """Obtiene o crea un cliente basado en los datos del ticket"""
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
    """
    Plantilla reutilizable de asientos (mapa).
    La idea es que varios buses físicos (patentes) puedan apuntar a un mismo layout.
    """
    name = models.CharField("Nombre del mapa", max_length=120, unique=True)
    slug = models.SlugField(max_length=140, unique=True, blank=True)

    # Dimensiones
    floors = models.PositiveSmallIntegerField(default=1)
    rows_lower = models.PositiveSmallIntegerField(default=0)
    rows_upper = models.PositiveSmallIntegerField(default=0)
    cols       = models.PositiveSmallIntegerField(default=4)

    # Layouts (tipos por celda) y numeración
    layout_lower  = models.JSONField(default=list, blank=True)
    layout_upper  = models.JSONField(default=list, blank=True)
    numbers_lower = models.JSONField(default=list, blank=True)
    numbers_upper = models.JSONField(default=list, blank=True)

    # Opcional: prefijos automáticos por piso
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
            self.slug = slugify(self.name)[:140]
        super().save(*args, **kwargs)


# =========================================================
# Modelos de Caja y Reportes
# =========================================================
class CashRegister(models.Model):
    """Control de caja diario por vendedor"""
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
    """Reporte diario de ventas"""
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


    # --- Roles disponibles ---
    ROLE_CHOICES = (
        ('admin', 'Administrador'),
        ('supervisor', 'Supervisor'),
        ('vendedor', 'Vendedor'),
        ('cajero', 'Cajero'),
    )

    # Relación 1-1 con el usuario
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='profile',
        verbose_name='Usuario',
    )

    # Rol de operación
    role = models.CharField(
        "Rol",
        max_length=20,
        choices=ROLE_CHOICES,
        default='vendedor',
        db_index=True,
    )

    # Terminal asignada (opcional)
    terminal = models.ForeignKey(
        Terminal,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name="Terminal asignada",
        db_index=True,
    )

    # Estado del perfil
    is_active = models.BooleanField("Activo", default=True, db_index=True)

    # Porcentaje de comisión del vendedor/supervisor (%)
    commission_rate = models.DecimalField(
        "Porcentaje de comisión",
        max_digits=5,
        decimal_places=2,
        default=0.00,
        help_text="Porcentaje (%) — ej.: 2.50",
    )

    # Descuento máximo permitido en CLP (monto)
    # Usamos 7 dígitos para permitir montos altos en CLP (hasta 999,999.99)
    max_discount = models.DecimalField(
        "Descuento máximo permitido",
        max_digits=7,
        decimal_places=2,
        default=0.00,
        help_text="Monto en CLP — ej.: 1500.00",
    )

    # Timestamps
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
        # Muestra etiqueta legible del rol si existe, sino el código
        try:
            role_display = self.get_role_display()
        except Exception:
            role_display = self.role
        return f"{getattr(self.user, 'username', 'user')} - {role_display}"
    
    
    
# =========================================================
# Señales: creación/actualización automática del perfil
# =========================================================

# COMENTAR TODO - INICIO
# @receiver(post_save, sender=settings.AUTH_USER_MODEL)
# def create_or_update_user_profile(sender, instance, created, **kwargs):
#     """
#     - Al crear un usuario, se crea el UserProfile.
#     - En actualizaciones, garantiza que el perfil exista (get_or_create)
#       y se guarda para disparar cualquier lógica dependiente.
#     """
#     if created:
#         UserProfile.objects.create(user=instance)
#     else:
#         # Garantiza que el perfil exista (si fue borrado manualmente o por una migración)
#         UserProfile.objects.get_or_create(user=instance)
#         # Si el perfil existe, forzamos un save para actualizar `updated_at` si aplica.
#         if hasattr(instance, 'profile'):
#             instance.profile.save()


# # Señal para crear perfil automáticamente
# @receiver(post_save, sender=User)
# def create_user_profile(sender, instance, created, **kwargs):
#     if created:
#         UserProfile.objects.create(user=instance)


# @receiver(post_save, sender=User)
# def save_user_profile(sender, instance, **kwargs):
#     if hasattr(instance, 'profile'):
#         instance.profile.save()
# COMENTAR TODO - FIN
    


# =========================================================
# Modelo de Cliente para ventas recurrentes
# =========================================================

class Customer(models.Model):
    """
    Cliente recurrente para agilizar ventas.
    """
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
        # Limpiar y formatear RUT
        if self.national_id:
            self.national_id = self._clean_rut(self.national_id)
        super().save(*args, **kwargs)

    @staticmethod
    def _clean_rut(rut: str) -> str:
        """Limpia y formatea RUT chileno"""
        rut = rut.upper().replace(".", "").replace("-", "").strip()
        if rut and rut[-1] in "0123456789K":
            return rut
        return rut
