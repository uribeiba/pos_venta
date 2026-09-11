# booking/models.py
from __future__ import annotations

from datetime import timedelta
from typing import Optional
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models, transaction, connection
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
            base = slugify(self.name or "")
            slug = base
            counter = 1
            while City.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                slug = f"{base}-{counter}"
                counter += 1
            self.slug = slug
        super().save(*args, **kwargs)


class Terminal(models.Model):
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
# FASE 2.18.3-A1 — Propietarios / socios de la flota
# =========================================================
class FleetOwner(models.Model):
    OWNER_TYPE_CHOICES = (
        ("person", "Persona natural"),
        ("company", "Empresa / sociedad"),
    )

    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="fleet_owners",
        verbose_name="Empresa operadora",
    )
    owner_type = models.CharField(
        "Tipo de propietario",
        max_length=20,
        choices=OWNER_TYPE_CHOICES,
        default="person",
    )
    first_name = models.CharField(
        "Nombres / razón social",
        max_length=140,
    )
    last_name = models.CharField(
        "Apellidos",
        max_length=140,
        blank=True,
        default="",
    )
    rut = models.CharField(
        "RUT",
        max_length=20,
        blank=True,
        default="",
        db_index=True,
    )
    email = models.EmailField(
        "Correo electrónico",
        blank=True,
        default="",
    )
    phone = models.CharField(
        "Teléfono",
        max_length=30,
        blank=True,
        default="",
    )
    notes = models.TextField(
        "Observaciones",
        blank=True,
        default="",
    )
    is_active = models.BooleanField(
        "Activo",
        default=True,
        db_index=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Propietario de flota"
        verbose_name_plural = "Propietarios de flota"
        ordering = ("company__name", "first_name", "last_name")
        constraints = [
            models.UniqueConstraint(
                fields=("company", "rut"),
                condition=~models.Q(rut=""),
                name="uniq_fleet_owner_company_rut",
            )
        ]
        indexes = [
            models.Index(fields=("company", "is_active")),
            models.Index(fields=("rut",)),
        ]

    @property
    def display_name(self):
        if self.owner_type == "company":
            return self.first_name.strip()

        full_name = f"{self.first_name} {self.last_name}".strip()
        return full_name or self.first_name

    def __str__(self):
        return f"{self.display_name} — {self.company.name}"



# =========================================================
# Chofer (Conductor)
# =========================================================
class Driver(models.Model):
    company = models.ForeignKey(
        "Company",
        verbose_name="Empresa",
        on_delete=models.PROTECT,
        related_name="drivers",
        
    )

    full_name = models.CharField("Nombre completo", max_length=140)
    rut = models.CharField("RUT", max_length=20, unique=True, db_index=True)
    email = models.EmailField("Correo electrónico", blank=True, default="")
    phone = models.CharField("Teléfono", max_length=20, blank=True, default="")
    license_number = models.CharField("N° Licencia", max_length=30, blank=True)
    license_expiry = models.DateField("Vencimiento licencia", null=True, blank=True)
    is_active = models.BooleanField("Activo", default=True)

    photo = models.ImageField(
        "Foto",
        upload_to="drivers/photos/",
        null=True,
        blank=True,
    )

    medical_cert_expiry = models.DateField(
        "Vencimiento certificado médico",
        null=True,
        blank=True,
    )

    background_check_expiry = models.DateField(
        "Vencimiento antecedentes",
        null=True,
        blank=True,
    )

    notes = models.TextField(
        "Observaciones",
        blank=True,
    )

    class Meta:
        verbose_name = "Chofer"
        verbose_name_plural = "Choferes"
        ordering = ("full_name",)

    def __str__(self):
        return f"{self.full_name} ({self.rut})"


# =========================================================
# Auxiliar
# =========================================================
class Assistant(models.Model):
    company = models.ForeignKey(
        "Company",
        verbose_name="Empresa",
        on_delete=models.PROTECT,
        related_name="assistants",
        
    )

    full_name = models.CharField("Nombre completo", max_length=140)
    rut = models.CharField("RUT", max_length=20, unique=True, db_index=True)
    email = models.EmailField("Correo electrónico", blank=True, default="")
    phone = models.CharField("Teléfono", max_length=20, blank=True, default="")
    is_active = models.BooleanField("Activo", default=True)

    photo = models.ImageField(
        "Foto",
        upload_to="assistants/photos/",
        null=True,
        blank=True,
    )

    notes = models.TextField(
        "Observaciones",
        blank=True,
    )

    class Meta:
        verbose_name = "Auxiliar"
        verbose_name_plural = "Auxiliares"
        ordering = ("full_name",)

    def __str__(self):
        return f"{self.full_name} ({self.rut})"



# =========================================================
# Plantilla visual / estructural de diseño de bus
# =========================================================
class BusLayout(models.Model):
    """
    Plantilla reutilizable para el editor de buses.

    Define:
    - configuración base de pisos / filas / columnas
    - layout inicial opcional
    - imagen visual de fondo por piso
    - zonas estructurales y configuración del editor

    El Bus conserva su layout concreto y sus Seat físicos.
    Esta clase funciona como plantilla/preset.
    """

    name = models.CharField(
        "Nombre de la plantilla",
        max_length=120,
        unique=True,
    )

    slug = models.SlugField(
        max_length=140,
        unique=True,
        blank=True,
    )

    floors = models.PositiveSmallIntegerField(
        "Pisos",
        default=1,
    )

    rows_lower = models.PositiveSmallIntegerField(
        "Filas piso inferior",
        default=0,
    )

    rows_upper = models.PositiveSmallIntegerField(
        "Filas piso superior",
        default=0,
    )

    cols = models.PositiveSmallIntegerField(
        "Columnas",
        default=4,
    )

    layout_lower = models.JSONField(
        default=list,
        blank=True,
    )

    layout_upper = models.JSONField(
        default=list,
        blank=True,
    )

    numbers_lower = models.JSONField(
        default=list,
        blank=True,
    )

    numbers_upper = models.JSONField(
        default=list,
        blank=True,
    )

    prefix_lower = models.CharField(
        max_length=10,
        blank=True,
        default="",
    )

    prefix_upper = models.CharField(
        max_length=10,
        blank=True,
        default="",
    )

    # Fondo visual del bus
    # Ejemplo: img/bus-generico.png
    background_lower = models.CharField(
        "Fondo piso inferior",
        max_length=255,
        blank=True,
        default="",
        help_text="Ruta relativa dentro de static. Ejemplo: img/bus-generico.png",
    )

    background_upper = models.CharField(
        "Fondo piso superior",
        max_length=255,
        blank=True,
        default="",
        help_text="Ruta relativa dentro de static. Ejemplo: img/bus-piso2.png",
    )

    # Configuración visual del editor
    editor_config = models.JSONField(
        "Configuración visual del editor",
        default=dict,
        blank=True,
    )

    # Zonas estructurales / bloqueadas
    structure_config = models.JSONField(
        "Configuración estructural",
        default=dict,
        blank=True,
    )

    is_active = models.BooleanField(
        "Activa",
        default=True,
    )

    is_system = models.BooleanField(
        "Plantilla del sistema",
        default=False,
        help_text="Identifica plantillas base provistas por el sistema.",
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    class Meta:
        verbose_name = "Plantilla de bus"
        verbose_name_plural = "Plantillas de buses"
        ordering = ("name",)

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.name)[:130]
            slug = base
            i = 1

            while (
                BusLayout.objects
                .filter(slug=slug)
                .exclude(pk=self.pk)
                .exists()
            ):
                slug = f"{base}-{i}"
                i += 1

            self.slug = slug

        super().save(*args, **kwargs)

# =========================================================
# Bus con diseño de asientos (layout)
# =========================================================
class Bus(models.Model):
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

    services_lower = models.JSONField("Servicios inferiores", default=list, blank=True)
    services_upper = models.JSONField("Servicios superiores", default=list, blank=True)

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

    # =====================================================
    # FASE 2.18.3-A1 — Propietario real de la máquina
    # =====================================================
    # owner es la relación estructurada que usaremos para dashboards,
    # permisos, estadísticas y futuras liquidaciones.
    #
    # Los campos owner_first_name / owner_last_name se conservan
    # temporalmente por compatibilidad con los buses ya existentes.
    owner = models.ForeignKey(
        "FleetOwner",
        on_delete=models.PROTECT,
        related_name="buses",
        verbose_name="Propietario / socio",
        null=True,
        blank=True,
    )

    # Datos históricos del propietario (compatibilidad)
    owner_first_name = models.CharField("Nombres del propietario", max_length=100, blank=True)
    owner_last_name = models.CharField("Apellidos del propietario", max_length=100, blank=True)

    # Documentos y registro
    circulation_card = models.CharField("Tarjeta de circulación", max_length=50, blank=True, unique=True, null=True)
    vehicle_class = models.CharField(
        "Clase", max_length=50, blank=True,
        choices=[
            ('BUS RURAL', 'BUS RURAL'),
            ('BUS URBANO', 'BUS URBANO'),
            ('OMNIBUS', 'OMNIBUS'),
            ('N3-CAMION', 'N3-CAMION'),
            ('OTRO', 'OTRO'),
        ],
        default='BUS RURAL'
    )
    brand = models.CharField("Marca", max_length=80, blank=True)
    manufacturing_year = models.PositiveIntegerField("Año fabricación", null=True, blank=True)
    fuel_type = models.CharField(
        "Tipo combustible", max_length=30, blank=True,
        choices=[
            ('Gasolina', 'Gasolina'),
            ('Diesel', 'Diesel'),
            ('Gas Licuado de Petróleo', 'GLP'),
            ('Eléctrico', 'Eléctrico'),
        ],
        default='Diesel'
    )
    bodywork = models.CharField("Carrocería", max_length=50, blank=True)
    axles = models.PositiveSmallIntegerField("Ejes", default=2)
    color = models.CharField("Color", max_length=100, blank=True)
    engine_number = models.CharField("N° Motor", max_length=50, blank=True)
    cylinders = models.PositiveSmallIntegerField("Cantidad de cilindros", null=True, blank=True)
    serial_number = models.CharField("N° Serie (VIN)", max_length=50, blank=True, unique=True, null=True)
    wheels_count = models.PositiveSmallIntegerField("Cantidad de ruedas", default=6)
    dry_weight = models.DecimalField("Peso seco (kg)", max_digits=10, decimal_places=2, null=True, blank=True)
    gross_weight = models.DecimalField("Peso bruto (kg)", max_digits=10, decimal_places=2, null=True, blank=True)
    length = models.DecimalField("Longitud (m)", max_digits=6, decimal_places=2, null=True, blank=True)
    height = models.DecimalField("Altura (m)", max_digits=6, decimal_places=2, null=True, blank=True)
    width = models.DecimalField("Ancho (m)", max_digits=6, decimal_places=2, null=True, blank=True)
    total_passengers = models.PositiveSmallIntegerField("Total pasajeros", default=0)
    total_seats = models.PositiveSmallIntegerField("Total asientos", default=0)

    # Mantenimiento y operación
    current_mileage = models.PositiveIntegerField("Kilometraje actual", default=0)
    last_maintenance_mileage = models.PositiveIntegerField("Kilometraje último mantenimiento", default=0)
    next_maintenance_mileage = models.PositiveIntegerField("Próximo mantenimiento (km)", default=0)
    fuel_consumption = models.DecimalField("Consumo (L/100km)", max_digits=5, decimal_places=2, null=True, blank=True)
    last_fuel_refill = models.DateField("Última carga combustible", null=True, blank=True)
    last_fuel_mileage = models.PositiveIntegerField("Kilometraje última carga", default=0)
    service_type = models.CharField(
        "Tipo servicio", max_length=30, blank=True,
        choices=[
            ('semi_cama', 'Semi Cama'),
            ('cama', 'Cama'),
            ('ejecutivo', 'Ejecutivo'),
            ('rural', 'Rural'),
        ],
        default='semi_cama'
    )

    # Fechas de documentos
    technical_review_expiry = models.DateField("Vencimiento revisión técnica", null=True, blank=True)
    insurance_expiry = models.DateField("Vencimiento seguro", null=True, blank=True)
    permit_expiry = models.DateField("Vencimiento permiso circulación", null=True, blank=True)
    last_maintenance = models.DateField("Último mantenimiento", null=True, blank=True)

    is_active = models.BooleanField("Activo", default=True)

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

    def effective_floors(self) -> int:
        return int(self.floors or 1)

    def effective_cols(self) -> int:
        return int(self.cols or 4)

    def effective_rows_lower(self) -> int:
        return int(self.rows_lower or 0)

    def effective_rows_upper(self) -> int:
        return int(self.rows_upper or 0)

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

    def ensure_layouts(self):
        """Asegura que layout, numbers y services tengan el tamaño correcto."""
        gl_lower = self.grid_len_lower()

        # PISO INFERIOR
        if not self.layout_lower or len(self.layout_lower) == 0:
            self.layout_lower = ["L"] * gl_lower
        elif len(self.layout_lower) < gl_lower:
            self.layout_lower.extend(["L"] * (gl_lower - len(self.layout_lower)))
        else:
            self.layout_lower = self.layout_lower[:gl_lower]

        if not self.numbers_lower or len(self.numbers_lower) == 0:
            self.numbers_lower = [""] * gl_lower
        elif len(self.numbers_lower) < gl_lower:
            self.numbers_lower.extend([""] * (gl_lower - len(self.numbers_lower)))
        else:
            self.numbers_lower = self.numbers_lower[:gl_lower]

        if not self.services_lower or len(self.services_lower) == 0:
            self.services_lower = ["semi_cama"] * gl_lower
        elif len(self.services_lower) < gl_lower:
            self.services_lower.extend(["semi_cama"] * (gl_lower - len(self.services_lower)))
        else:
            self.services_lower = self.services_lower[:gl_lower]

        counter = 1
        for i, typ in enumerate(self.layout_lower):
            if typ == "L" and (i >= len(self.numbers_lower) or not self.numbers_lower[i]):
                prefix = getattr(self, 'prefix_lower', '')
                self.numbers_lower[i] = f"{prefix}{counter}"
                counter += 1

        # PISO SUPERIOR
        if self.effective_floors() >= 2:
            gl_upper = self.grid_len_upper()
            if not self.layout_upper or len(self.layout_upper) == 0:
                self.layout_upper = ["L"] * gl_upper
            elif len(self.layout_upper) < gl_upper:
                self.layout_upper.extend(["L"] * (gl_upper - len(self.layout_upper)))
            else:
                self.layout_upper = self.layout_upper[:gl_upper]

            if not self.numbers_upper or len(self.numbers_upper) == 0:
                self.numbers_upper = [""] * gl_upper
            elif len(self.numbers_upper) < gl_upper:
                self.numbers_upper.extend([""] * (gl_upper - len(self.numbers_upper)))
            else:
                self.numbers_upper = self.numbers_upper[:gl_upper]

            if not self.services_upper or len(self.services_upper) == 0:
                self.services_upper = ["semi_cama"] * gl_upper
            elif len(self.services_upper) < gl_upper:
                self.services_upper.extend(["semi_cama"] * (gl_upper - len(self.services_upper)))
            else:
                self.services_upper = self.services_upper[:gl_upper]

            counter = 1
            for i, typ in enumerate(self.layout_upper):
                if typ == "L" and (i >= len(self.numbers_upper) or not self.numbers_upper[i]):
                    prefix = getattr(self, 'prefix_upper', '')
                    self.numbers_upper[i] = f"{prefix}{counter}"
                    counter += 1
        else:
            self.layout_upper = []
            self.numbers_upper = []
            self.services_upper = []

    def regenerate_seats(self) -> int:
        SeatModel = apps.get_model(self._meta.app_label, "Seat")
        TripModel = apps.get_model(self._meta.app_label, "Trip")

        with transaction.atomic():
            SeatModel.objects.filter(bus=self).delete()
            created = 0
            POSITIONS = ["A", "B", "C", "D", "E", "F"]

            def _create_for(deck_num, rows, layout, numbers, services, prefix):
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
                        service = "semi_cama"
                        if idx < len(services) and services[idx] in ['semi_cama', 'cama', 'ejecutivo']:
                            service = services[idx]

                        SeatModel.objects.create(
                            bus=self,
                            deck=deck_num,
                            row=r + 1,
                            position=position,
                            number=number,
                            is_occupied=False,
                            seat_service=service
                        )
                        created += 1

            _create_for(
                1,
                self.effective_rows_lower(),
                self.layout_lower,
                self.numbers_lower,
                self.services_lower,
                self.prefix_lower
            )
            if self.effective_floors() >= 2:
                _create_for(
                    2,
                    self.effective_rows_upper(),
                    self.layout_upper,
                    self.numbers_upper,
                    self.services_upper,
                    self.prefix_upper
                )

            total = SeatModel.objects.filter(bus=self).count()
            TripModel.objects.filter(bus=self).update(seats_total=total)

        return created

    def save(self, *args, **kwargs):
        if not self.pk:
            self.ensure_layouts()
        super().save(*args, **kwargs)


# =========================================================
# Documentos de Chofer
# =========================================================
class DriverDocument(models.Model):
    DOC_TYPES = (
        ('license', 'Licencia de conducir'),
        ('medical', 'Certificado médico'),
        ('background', 'Antecedentes'),
        ('other', 'Otro'),
    )
    driver = models.ForeignKey(Driver, on_delete=models.CASCADE, related_name='documents')
    doc_type = models.CharField("Tipo", max_length=20, choices=DOC_TYPES)
    document_number = models.CharField("Número documento", max_length=50, blank=True)
    issue_date = models.DateField("Fecha emisión", null=True, blank=True)
    expiry_date = models.DateField("Fecha vencimiento")
    notes = models.TextField("Observaciones", blank=True)
    file = models.FileField("Archivo", upload_to='driver_docs/', blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Documento de chofer"
        verbose_name_plural = "Documentos de choferes"
        ordering = ('expiry_date',)

    def __str__(self):
        return f"{self.driver.full_name} - {self.get_doc_type_display()}"


# =========================================================
# Documentos de Vehículo (Bus)
# =========================================================
class BusDocument(models.Model):
    DOC_TYPES = (
        ('technical', 'Revisión técnica'),
        ('insurance', 'Seguro'),
        ('permit', 'Permiso de circulación'),
        ('other', 'Otro'),
    )
    bus = models.ForeignKey(Bus, on_delete=models.CASCADE, related_name='documents')
    doc_type = models.CharField("Tipo", max_length=20, choices=DOC_TYPES)
    document_number = models.CharField("Número documento", max_length=50, blank=True)
    issue_date = models.DateField("Fecha emisión", null=True, blank=True)
    expiry_date = models.DateField("Fecha vencimiento")
    notes = models.TextField("Observaciones", blank=True)
    file = models.FileField("Archivo", upload_to='bus_docs/', blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Documento de bus"
        verbose_name_plural = "Documentos de buses"
        ordering = ('expiry_date',)

    def __str__(self):
        return f"{self.bus.plate} - {self.get_doc_type_display()}"


# =========================================================
# Rutas y viajes
# =========================================================
class Route(models.Model):
    company = models.ForeignKey(
        "Company",
        verbose_name="Empresa",
        on_delete=models.PROTECT,
        related_name="routes"
    )

    origin = models.ForeignKey(
        City,
        verbose_name="Origen",
        on_delete=models.PROTECT,
        related_name="routes_from"
    )

    destination = models.ForeignKey(
        City,
        verbose_name="Destino",
        on_delete=models.PROTECT,
        related_name="routes_to"
    )

    origin_terminal = models.ForeignKey(
        Terminal,
        verbose_name="Terminal origen",
        on_delete=models.PROTECT,
        related_name="routes_from",
        null=True,
        blank=True
    )

    destination_terminal = models.ForeignKey(
        Terminal,
        verbose_name="Terminal destino",
        on_delete=models.PROTECT,
        related_name="routes_to",
        null=True,
        blank=True
    )

    duration_minutes = models.PositiveIntegerField(
        "Duración (min)",
        default=120
    )

    base_price = models.DecimalField(
        "Precio base",
        max_digits=10,
        decimal_places=2
    )

    is_active = models.BooleanField(
        "Activa",
        default=True
    )

    class Meta:
        unique_together = (
            "company",
            "origin",
            "destination",
            "origin_terminal",
            "destination_terminal",
        )
        verbose_name = "Ruta"
        verbose_name_plural = "Rutas"
        ordering = (
            "company__name",
            "origin__name",
            "destination__name",
        )

    def __str__(self) -> str:
        base = f"{self.origin} → {self.destination}"

        if self.origin_terminal or self.destination_terminal:
            t1 = (
                f" ({self.origin_terminal.name})"
                if self.origin_terminal
                else ""
            )

            t2 = (
                f" ({self.destination_terminal.name})"
                if self.destination_terminal
                else ""
            )

            base += f"{t1} →{t2}"

        return base
    
    
class Trip(models.Model):
    route = models.ForeignKey(Route, on_delete=models.CASCADE, related_name="trips")
    bus = models.ForeignKey(Bus, on_delete=models.PROTECT, related_name="trips")
    departure = models.DateTimeField("Salida")
    arrival = models.DateTimeField("Llegada")
    seats_total = models.PositiveIntegerField("Asientos totales", default=0)

        # =========================================================
    # FASE 2.18 - ESTADO OPERACIONAL / DESPACHO REAL
    # =========================================================
    STATUS_SCHEDULED = "scheduled"
    STATUS_IN_PROGRESS = "in_progress"
    STATUS_COMPLETED = "completed"

    STATUS_CHOICES = [
        (STATUS_SCHEDULED, "Programado"),
        (STATUS_IN_PROGRESS, "En viaje"),
        (STATUS_COMPLETED, "Finalizado"),
    ]

    status = models.CharField(
        "Estado operacional",
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_SCHEDULED,
        db_index=True,
    )

    actual_departure = models.DateTimeField(
        "Salida real",
        null=True,
        blank=True,
    )

    actual_arrival = models.DateTimeField(
        "Llegada real",
        null=True,
        blank=True,
    )

    dispatched_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="dispatched_trips",
        verbose_name="Despachado por",
    )

    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="completed_trips",
        verbose_name="Finalizado por",
    )

    cutoff_minutes = models.PositiveSmallIntegerField(
        "Corte de ventas web (min)",
        default=10,
        help_text="Minutos antes de la salida en que se bloquea la compra en la web. 0 = sin restricción."
    )

    driver1 = models.ForeignKey(
        Driver, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="trips_as_driver1", verbose_name="Chofer principal"
    )
    driver2 = models.ForeignKey(
        Driver, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="trips_as_driver2", verbose_name="Segundo chofer"
    )
    assistant = models.ForeignKey(
        Assistant, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="trips", verbose_name="Auxiliar"
    )

    class Meta:
        verbose_name = "Viaje"
        verbose_name_plural = "Viajes"
        ordering = ("-departure",)
        indexes = [
            models.Index(fields=['departure']),
            models.Index(fields=['bus']),
        ]

    def __str__(self) -> str:
        return f"{self.route} {self.departure:%Y-%m-%d %H:%M}"


# =========================================================
# Tipos de servicio de asiento
# =========================================================
SEAT_SERVICE_CHOICES = [
    ('semi_cama', 'Semi Cama'),
    ('cama', 'Cama'),
    ('ejecutivo', 'Ejecutivo'),
]


# =========================================================
# Asientos
# =========================================================
class Seat(models.Model):
    bus = models.ForeignKey(Bus, on_delete=models.CASCADE, related_name="seats")
    deck = models.PositiveSmallIntegerField("Piso", default=1)
    row = models.PositiveSmallIntegerField("Fila")
    position = models.CharField(
        "Posición", max_length=1,
        choices=[("A", "A"), ("B", "B"), ("C", "C"), ("D", "D"), ("E", "E"), ("F", "F")]
    )
    number = models.CharField("Número", max_length=10)
    seat_service = models.CharField(
        "Tipo de asiento",
        max_length=20,
        choices=SEAT_SERVICE_CHOICES,
        default='semi_cama'
    )
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


# =========================================================
# Cliente
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


# =========================================================
# Bloqueos temporales (SeatHold)
# =========================================================
class SeatHold(models.Model):
    """
    Bloqueo temporal de un asiento para un viaje.

    Puede pertenecer a:
    - un usuario autenticado (POS / vendedor / cliente registrado)
    - una sesión web anónima mediante session_key

    La concurrencia debe controlarse siempre utilizando
    transaction.atomic() + select_for_update() sobre Seat.
    """

    trip = models.ForeignKey(
        Trip,
        on_delete=models.CASCADE,
        related_name="holds",
    )

    seat = models.ForeignKey(
        Seat,
        on_delete=models.CASCADE,
        related_name="holds",
    )

    # IMPORTANTE:
    # En venta web el cliente puede no estar autenticado.
    # Por eso user debe aceptar NULL.
    user = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name="seat_holds",
        null=True,
        blank=True,
    )

    expires_at = models.DateTimeField()

    active = models.BooleanField(
        default=True,
        db_index=True,
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    # Identifica clientes web, incluso si no están autenticados.
    session_key = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        db_index=True,
    )

    class Meta:
        indexes = [
            models.Index(fields=["trip", "seat"]),
            models.Index(fields=["active"]),
            models.Index(fields=["expires_at"]),
            models.Index(fields=["trip"]),
            models.Index(fields=["user"]),
            models.Index(fields=["session_key"]),

            # Ayuda a las consultas frecuentes de disponibilidad.
            models.Index(
                fields=["trip", "seat", "active"],
                name="hold_trip_seat_active_idx",
            ),
        ]

        verbose_name = "Bloqueo temporal"
        verbose_name_plural = "Bloqueos temporales"

    def __str__(self):
        owner = (
            self.user.username
            if self.user_id
            else self.session_key or "anónimo"
        )

        return (
            f"{self.trip} — "
            f"{self.seat.number} — "
            f"{owner} "
            f"({'activo' if self.active else 'inactivo'})"
        )

    @property
    def is_expired(self):
        """
        Indica si el bloqueo ya expiró.
        """
        return self.expires_at <= timezone.now()

    @classmethod
    def cleanup(cls):
        """
        Desactiva reservas expiradas.

        No elimina registros para conservar trazabilidad.
        """
        now = timezone.now()

        return cls.objects.filter(
            active=True,
            expires_at__lte=now,
        ).update(active=False)

    @classmethod
    def hold(
        cls,
        trip,
        seat,
        user=None,
        session_key=None,
        minutes=10,
    ):
        """
        Crea o renueva una reserva temporal de asiento.

        SEGURIDAD DE CONCURRENCIA:
        El asiento físico se bloquea con SELECT FOR UPDATE para impedir
        que dos procesos reserven simultáneamente la misma butaca.

        Puede utilizarse desde:
        - POS mediante user
        - Web mediante session_key
        """

        if user is None and not session_key:
            raise ValueError(
                "Debe existir un usuario o una sesión para reservar el asiento."
            )

        now = timezone.now()
        new_expire = now + timedelta(minutes=minutes)

        with transaction.atomic():

            # -------------------------------------------------
            # 1. BLOQUEAR ASIENTO EN POSTGRESQL
            # -------------------------------------------------
            locked_seat = Seat.objects.select_for_update().get(
                pk=seat.pk
            )

            # -------------------------------------------------
            # 2. LIMPIAR HOLDS EXPIRADOS DE ESTE ASIENTO
            # -------------------------------------------------
            cls.objects.filter(
                trip=trip,
                seat=locked_seat,
                active=True,
                expires_at__lte=now,
            ).update(active=False)

            # -------------------------------------------------
            # 3. VERIFICAR QUE EL ASIENTO NO ESTÉ VENDIDO
            # -------------------------------------------------
            TicketModel = apps.get_model(
                "booking",
                "Ticket",
            )

            if TicketModel.objects.filter(
                trip=trip,
                seat=locked_seat,
            ).exists():
                raise ValueError(
                    "El asiento ya fue vendido."
                )

            # -------------------------------------------------
            # 4. BUSCAR RESERVA ACTIVA
            # -------------------------------------------------
            existing_hold = (
                cls.objects
                .select_for_update()
                .filter(
                    trip=trip,
                    seat=locked_seat,
                    active=True,
                    expires_at__gt=now,
                )
                .order_by("-expires_at")
                .first()
            )

            if existing_hold:

                # ---------------------------------------------
                # Determinar si el hold pertenece al solicitante
                # ---------------------------------------------
                same_owner = False

                # Usuario autenticado
                if user is not None and existing_hold.user_id:
                    same_owner = (
                        existing_hold.user_id == user.id
                    )

                # Sesión web
                if (
                    session_key
                    and existing_hold.session_key
                    and existing_hold.session_key == session_key
                ):
                    same_owner = True

                if not same_owner:
                    raise ValueError(
                        "El asiento está temporalmente reservado "
                        "por otro usuario."
                    )

                # ---------------------------------------------
                # Renovar reserva existente
                # ---------------------------------------------
                existing_hold.expires_at = new_expire
                existing_hold.active = True

                # Si ahora tenemos datos que antes no existían,
                # los asociamos.
                if user is not None:
                    existing_hold.user = user

                if session_key:
                    existing_hold.session_key = session_key

                existing_hold.save(
                    update_fields=[
                        "expires_at",
                        "active",
                        "user",
                        "session_key",
                    ]
                )

                return existing_hold

            # -------------------------------------------------
            # 5. CREAR NUEVA RESERVA
            # -------------------------------------------------
            return cls.objects.create(
                trip=trip,
                seat=locked_seat,
                user=user,
                session_key=session_key,
                expires_at=new_expire,
                active=True,
            )

    @classmethod
    def release(
        cls,
        trip,
        seat,
        user=None,
        session_key=None,
    ):
        """
        Libera una reserva únicamente si pertenece
        al usuario o sesión solicitante.
        """

        if user is None and not session_key:
            return 0

        filters = {
            "trip": trip,
            "seat": seat,
            "active": True,
        }

        if user is not None:
            filters["user"] = user

        if session_key:
            filters["session_key"] = session_key

        with transaction.atomic():
            Seat.objects.select_for_update().get(
                pk=seat.pk
            )

            return cls.objects.filter(
                **filters
            ).update(active=False)


# =========================================================
# Boletos (Ticket) - CORREGIDO COMPLETO
# =========================================================
class Ticket(models.Model):
    trip = models.ForeignKey(Trip, on_delete=models.PROTECT, related_name="tickets")
    seat = models.ForeignKey(Seat, on_delete=models.PROTECT, related_name="tickets")
    number = models.CharField("N° ticket", max_length=20, unique=True)
    buyer_name = models.CharField("Nombre pasajero", max_length=140)
    national_id = models.CharField("Documento", max_length=40, blank=True, default="")
    checked_in = models.BooleanField("Embarcado", default=False)
    checked_in_at = models.DateTimeField("Hora embarque", null=True, blank=True)
    customer = models.ForeignKey(
        Customer,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        verbose_name="Cliente",
        related_name="tickets"
    )

    PAYMENT_METHOD_CHOICES = [
        ("cash", "Efectivo"),
        ("card", "Tarjeta"),
        ("credit", "Crédito Convenio"),
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

    # =========================================================
    # FASE 2.18.3-A2.3.2 — Propiedad económica congelada
    # =========================================================
    #
    # revenue_bus:
    #   Bus que originó económicamente la venta en el momento
    #   exacto en que se emitió el boleto.
    #
    # revenue_owner:
    #   Propietario / socio al que corresponde económicamente
    #   la venta. NO debe cambiar si posteriormente el pasajero
    #   es trasladado operacionalmente a otra máquina.
    #
    # Ambos admiten NULL para mantener compatibilidad con los
    # tickets históricos hasta ejecutar el backfill.
    # =========================================================
    revenue_bus = models.ForeignKey(
        Bus,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="revenue_tickets",
        verbose_name="Bus origen de la venta",
        editable=False,
    )

    revenue_owner = models.ForeignKey(
        FleetOwner,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="revenue_tickets",
        verbose_name="Propietario económico",
        editable=False,
    )

    contract = models.ForeignKey(
        'CompanyContract',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='tickets',
        verbose_name="Contrato de convenio"
    )
    is_credit = models.BooleanField(
        default=False,
        verbose_name="¿Compra a crédito?"
    )

    class Meta:
        indexes = [
            models.Index(fields=["trip"]),
            models.Index(fields=["created_at"]),
            models.Index(fields=["payment_method"]),
            models.Index(fields=["created_by"]),
            models.Index(fields=["customer"]),
            models.Index(fields=["contract"]),
            models.Index(fields=["is_credit"]),
        ]
        verbose_name = "Boleto"
        verbose_name_plural = "Boletos"
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(fields=["trip", "seat"], name="uniq_ticket_trip_seat"),
        ]

    def __str__(self):
        return f"{self.number} — {self.seat.number} ({self.trip})"

    def save(self, *args, **kwargs):
        """
        FASE 2.18.3-A2.3.2

        Defensa de integridad para emisiones nuevas.

        Aunque una vista antigua use Ticket.objects.create() en lugar de
        create_for_sale(), al crear el Ticket se congela automáticamente:

            revenue_bus   = trip.bus
            revenue_owner = trip.bus.owner

        En actualizaciones posteriores NO recalculamos estos campos.
        De esta forma un cambio operacional de bus no mueve el ingreso
        histórico de un socio a otro.
        """
        if self._state.adding:
            if not self.revenue_bus_id and self.trip_id:
                # Si trip ya viene cargado evitamos una consulta innecesaria.
                trip = self.trip
                self.revenue_bus = trip.bus

            if not self.revenue_owner_id and self.revenue_bus_id:
                revenue_bus = self.revenue_bus
                self.revenue_owner = getattr(revenue_bus, "owner", None)

        super().save(*args, **kwargs)

    @staticmethod
    def _next_number() -> str:
        """
        Genera el siguiente número de ticket.
        Esta versión NUNCA genera duplicados porque verifica la existencia.
        """
        from django.db import connection
        import time

        # ===== INTENTAR CON LA SECUENCIA =====
        try:
            with connection.cursor() as cursor:
                # Primero, asegurar que la secuencia esté en el valor correcto
                # Obtener el último número usado
                cursor.execute("SELECT COALESCE(MAX(CAST(SUBSTRING(number FROM 3) AS INTEGER)), 0) FROM booking_ticket")
                max_num = cursor.fetchone()[0]

                # Reiniciar la secuencia si es necesario
                cursor.execute(f"SELECT setval('ticket_number_seq', {max_num}, true)")

                # Obtener el siguiente valor
                cursor.execute("SELECT nextval('ticket_number_seq')")
                next_id = cursor.fetchone()[0]

                # Verificar que el número no exista
                new_number = f"T-{next_id:06d}"
                if Ticket.objects.filter(number=new_number).exists():
                    # Si existe, buscar el siguiente disponible
                    while Ticket.objects.filter(number=f"T-{next_id:06d}").exists():
                        next_id += 1
                    return f"T-{next_id:06d}"
                return new_number
        except Exception as e:
            print(f"⚠️ Error con secuencia: {e}")

        # ===== MÉTODO ALTERNATIVO: Buscar el último número y sumar 1 =====
        try:
            # Obtener todos los números de tickets
            numbers = Ticket.objects.values_list('number', flat=True)
            max_num = 0
            for num in numbers:
                if num.startswith('T-'):
                    try:
                        current = int(num[2:])
                        if current > max_num:
                            max_num = current
                    except:
                        pass

            next_id = max_num + 1

            # Verificar que no exista
            new_number = f"T-{next_id:06d}"
            if Ticket.objects.filter(number=new_number).exists():
                # Si existe, buscar el siguiente disponible
                while Ticket.objects.filter(number=f"T-{next_id:06d}").exists():
                    next_id += 1
                return f"T-{next_id:06d}"
            return new_number
        except Exception as e:
            print(f"⚠️ Error en método alternativo: {e}")

        # ===== ÚLTIMO RECURSO: timestamp + random =====
        import random
        timestamp = int(time.time() * 1000) % 1000000
        new_number = f"T-{timestamp:06d}"
        counter = 1
        while Ticket.objects.filter(number=new_number).exists():
            new_number = f"T-{timestamp:06d}-{counter}"
            counter += 1
        return new_number

    @classmethod
    def create_for_sale(
        cls,
        *,
        trip,  # Trip
        seat,  # Seat
        buyer_name: str,
        national_id: str,
        price,
        created_by,  # User
        number: Optional[str] = None,
        customer=None,
        **kwargs
    ):
        """
        Crea un ticket para la venta con validaciones atómicas.
        """
        from django.core.exceptions import ValidationError
        from django.apps import apps

        if not created_by:
            raise ValidationError("created_by es obligatorio para emitir un boleto.")

        # Extraer campos de convenio si están presentes
        contract = kwargs.pop('contract', None)
        is_credit = kwargs.pop('is_credit', False)
        payment_method = kwargs.pop('payment_method', 'cash')

        # Validar contrato si está presente
        if contract:
            if not contract.is_active:
                raise ValidationError("El contrato no está activo.")

            today = timezone.now().date()
            if contract.valid_from and contract.valid_from > today:
                raise ValidationError("El contrato aún no está vigente.")
            if contract.valid_to and contract.valid_to < today:
                raise ValidationError("El contrato ha expirado.")

            if not contract.can_purchase(price):
                raise ValidationError(
                    f"Crédito insuficiente. Disponible: ${contract.available_credit:,.0f}"
                )

        with transaction.atomic():
            s = Seat.objects.select_for_update().get(pk=seat.pk)

            if cls.objects.filter(trip=trip, seat=s).exists():
                raise ValidationError("El asiento ya está ocupado para este viaje.")

            SeatHold.objects.filter(
                trip=trip, seat=s, user=created_by, active=True
            ).update(active=False)

            number = number or cls._next_number()

            if not customer and national_id:
                try:
                    CustomerModel = apps.get_model(cls._meta.app_label, "Customer")
                    customer_obj, _ = CustomerModel.objects.get_or_create(
                        national_id=national_id,
                        defaults={'full_name': buyer_name}
                    )
                    customer = customer_obj
                except Exception:
                    customer = None

            # Si es compra a crédito, asegurar que el método de pago sea 'credit'
            if is_credit:
                payment_method = 'credit'

            # =====================================================
            # FASE 2.18.3-A2.3.2
            # Congelar propiedad económica al momento de emitir.
            # =====================================================
            #
            # Se ignora cualquier intento accidental de pasar estos
            # campos vía **kwargs: la fuente de verdad en una emisión
            # normal es el bus asignado al viaje en este instante.
            # =====================================================
            kwargs.pop('revenue_bus', None)
            kwargs.pop('revenue_owner', None)

            revenue_bus = trip.bus
            revenue_owner = getattr(revenue_bus, "owner", None)

            t = cls.objects.create(
                trip=trip,
                seat=s,
                number=number,
                buyer_name=(buyer_name or "").strip() or "Pasajero",
                national_id=(national_id or "").strip(),
                price=price,
                created_by=created_by,
                customer=customer,
                contract=contract,
                is_credit=is_credit,
                payment_method=payment_method,
                revenue_bus=revenue_bus,
                revenue_owner=revenue_owner,
                **kwargs,
            )

            # Actualizar crédito del contrato
            if contract and is_credit:
                contract.used_credit += price
                contract.save(update_fields=['used_credit'])

            if hasattr(s, "is_occupied"):
                s.is_occupied = True
                s.save(update_fields=["is_occupied"])

            return t

    @classmethod
    def purchase(cls, trip: Trip, seat_ids, buyer_name, national_id, user, customer=None, payment_method="cash"):
        from django.apps import apps

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
                    CustomerModel = apps.get_model(cls._meta.app_label, "Customer")
                    customer_obj, created = CustomerModel.objects.get_or_create(
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
        from django.apps import apps

        if self.customer:
            return self.customer
        if self.national_id:
            try:
                CustomerModel = apps.get_model(self._meta.app_label, "Customer")
                customer, created = CustomerModel.objects.get_or_create(
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
# FASE 2.18.3-A3.3 — LIQUIDACIONES DE PROPIETARIOS
# =========================================================

class OwnerSettlement(models.Model):
    STATUS_PENDING = "pending"
    STATUS_REVIEW = "review"
    STATUS_PAID = "paid"
    STATUS_CANCELLED = "cancelled"

    STATUS_CHOICES = (
        (STATUS_PENDING, "Pendiente"),
        (STATUS_REVIEW, "En revisión"),
        (STATUS_PAID, "Pagada"),
        (STATUS_CANCELLED, "Anulada"),
    )

    company = models.ForeignKey(
        Company,
        on_delete=models.PROTECT,
        related_name="owner_settlements",
        verbose_name="Empresa",
    )

    owner = models.ForeignKey(
        FleetOwner,
        on_delete=models.PROTECT,
        related_name="settlements",
        verbose_name="Propietario / socio",
    )

    date_from = models.DateField(
        "Desde",
    )

    date_to = models.DateField(
        "Hasta",
    )

    gross_amount = models.DecimalField(
        "Ventas brutas",
        max_digits=14,
        decimal_places=2,
        default=Decimal("0"),
    )

    commission_amount = models.DecimalField(
        "Comisión",
        max_digits=14,
        decimal_places=2,
        default=Decimal("0"),
    )

    net_amount = models.DecimalField(
        "Monto líquido",
        max_digits=14,
        decimal_places=2,
        default=Decimal("0"),
    )

    status = models.CharField(
        "Estado",
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING,
        db_index=True,
    )

    notes = models.TextField(
        "Observaciones",
        blank=True,
        default="",
    )

    created_by = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name="owner_settlements_created",
        verbose_name="Creada por",
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    paid_at = models.DateTimeField(
        "Fecha de pago",
        null=True,
        blank=True,
    )
    
    cancelled_at = models.DateTimeField(
    null=True,
    blank=True,
    )

    cancelled_by = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="cancelled_owner_settlements",
    )

    cancellation_reason = models.TextField(
        blank=True,
        default="",
    )

    cancelled_ticket_snapshot = models.JSONField(
        default=list,
        blank=True,
    )
    
    

    class Meta:
        verbose_name = "Liquidación de propietario"
        verbose_name_plural = "Liquidaciones de propietarios"
        ordering = ("-date_to", "-created_at")
        indexes = [
            models.Index(fields=("company", "owner")),
            models.Index(fields=("status",)),
            models.Index(fields=("date_from", "date_to")),
        ]

    def __str__(self):
        return (
            f"Liquidación #{self.pk or 'NUEVA'} — "
            f"{self.owner.display_name} — "
            f"{self.date_from} a {self.date_to}"
        )


class OwnerSettlementTicket(models.Model):
    settlement = models.ForeignKey(
        OwnerSettlement,
        on_delete=models.CASCADE,
        related_name="items",
        verbose_name="Liquidación",
    )

    ticket = models.OneToOneField(
        Ticket,
        on_delete=models.PROTECT,
        related_name="owner_settlement_item",
        verbose_name="Ticket",
    )

    amount = models.DecimalField(
        "Monto del ticket",
        max_digits=14,
        decimal_places=2,
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    class Meta:
        verbose_name = "Ticket liquidado"
        verbose_name_plural = "Tickets liquidados"
        ordering = ("ticket__created_at",)

    def __str__(self):
        return f"{self.ticket.number} — Liquidación #{self.settlement_id}"
    


class OwnerSettlementHistory(models.Model):
    settlement = models.ForeignKey(
        OwnerSettlement,
        on_delete=models.CASCADE,
        related_name="history",
    )

    previous_status = models.CharField(
        max_length=20,
        choices=OwnerSettlement.STATUS_CHOICES,
    )

    new_status = models.CharField(
        max_length=20,
        choices=OwnerSettlement.STATUS_CHOICES,
    )

    changed_by = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name="owner_settlement_status_changes",
    )

    note = models.TextField(
        blank=True,
        default="",
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    class Meta:
        verbose_name = "Historial de liquidación"
        verbose_name_plural = "Historial de liquidaciones"
        ordering = [
            "created_at",
            "id",
        ]

        indexes = [
            models.Index(
                fields=[
                    "settlement",
                    "created_at",
                ]
            ),
            models.Index(
                fields=[
                    "new_status",
                    "created_at",
                ]
            ),
        ]

    def __str__(self):
        return (
            f"Liquidación #{self.settlement_id}: "
            f"{self.previous_status} → {self.new_status}"
        )
# =========================================================
# Perfil de Usuario
# =========================================================
class UserProfile(models.Model):
    ROLE_CHOICES = (
        ('admin', 'Administrador'),
        ('supervisor', 'Supervisor'),
        ('coordinator', 'Coordinador'),
        ('vendedor', 'Vendedor'),
        ('cajero', 'Cajero'),
        ('convenio', 'Gestor de Convenios'),
        ('owner', 'Propietario / Socio'),
        ('executive', 'Dueño / Gerencia'),
        ('secretary', 'Secretaria'),
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
        null=True,
        blank=True,
        verbose_name="Terminal asignada",
        db_index=True,
    )

    # =========================================================
    # FASE 2.18.3-A2.1 — Alcance empresarial del usuario
    # =========================================================
    company = models.ForeignKey(
        Company,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='user_profiles',
        verbose_name='Empresa operadora',
        db_index=True,
    )

    fleet_owner = models.ForeignKey(
        FleetOwner,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='user_profiles',
        verbose_name='Propietario / socio asociado',
        db_index=True,
    )

    is_active = models.BooleanField(
        "Activo",
        default=True,
        db_index=True,
    )

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
            models.Index(fields=['company']),
            models.Index(fields=['fleet_owner']),
        ]

    def __str__(self) -> str:
        try:
            role_display = self.get_role_display()
        except Exception:
            role_display = self.role

        return (
            f"{getattr(self.user, 'username', 'user')} - "
            f"{role_display}"
        )


# =========================================================
# Paradas intermedias de rutas
# =========================================================
class RouteStop(models.Model):
    route = models.ForeignKey(Route, on_delete=models.CASCADE, related_name='stops')
    city = models.ForeignKey(City, on_delete=models.PROTECT, verbose_name="Ciudad")
    terminal = models.ForeignKey(Terminal, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="Terminal (opcional)")
    order = models.PositiveSmallIntegerField("Orden", help_text="Orden de la parada en la ruta (1,2,3...)")
    extra_price = models.DecimalField("Precio adicional", max_digits=10, decimal_places=2, default=0)
    is_mandatory = models.BooleanField("Parada obligatoria", default=True)
    notes = models.CharField("Notas", max_length=200, blank=True)

    class Meta:
        ordering = ['order']
        unique_together = [['route', 'order']]
        verbose_name = "Parada intermedia"
        verbose_name_plural = "Paradas intermedias"

    def __str__(self):
        return f"{self.route} - {self.order}: {self.city}"


# =========================================================
# Agencias
# =========================================================
class Agency(models.Model):
    company = models.ForeignKey(
        Company,
        verbose_name="Empresa",
        on_delete=models.PROTECT,
        related_name="agencies",
        db_index=True,
    )

    name = models.CharField(
        "Nombre de la agencia",
        max_length=120,

    )

    city = models.ForeignKey(
        City,
        on_delete=models.PROTECT,
        verbose_name="Ciudad",
    )

    address = models.CharField(
        "Dirección",
        max_length=200,
        blank=True,
    )

    phone = models.CharField(
        "Teléfono",
        max_length=20,
        blank=True,
    )

    email = models.EmailField(
        "Correo electrónico",
        blank=True,
    )

    is_active = models.BooleanField(
        "Activa",
        default=True,
    )

    created_at = models.DateTimeField(
        auto_now_add=True
    )

    updated_at = models.DateTimeField(
        auto_now=True
    )

    class Meta:
        verbose_name = "Agencia"
        verbose_name_plural = "Agencias"
        ordering = ("name",)

        constraints = [
            models.UniqueConstraint(
                fields=["company", "name"],
                name="unique_agency_name_per_company",
            ),
        ]

    def __str__(self):
        return self.name

# =========================================================
# Tarifas dinámicas y promociones (unificado)
# =========================================================
class Season(models.Model):
    name = models.CharField("Nombre", max_length=100)
    start_date = models.DateField("Fecha inicio")
    end_date = models.DateField("Fecha fin")
    multiplier = models.DecimalField("Multiplicador", max_digits=4, decimal_places=2, default=1.0,
                                     help_text="Ej: 1.2 = +20%, 0.9 = -10%")
    is_active = models.BooleanField("Activo", default=True)

    class Meta:
        verbose_name = "Temporada"
        verbose_name_plural = "Temporadas"
        ordering = ['start_date']

    def __str__(self):
        return f"{self.name} ({self.start_date} → {self.end_date})"


class Promotion(models.Model):
    DISCOUNT_TYPES = (
        ('percentage', 'Porcentaje'),
        ('fixed', 'Monto fijo'),
    )

    name = models.CharField("Nombre", max_length=100)
    code = models.CharField("Código", max_length=50, unique=True, db_index=True)
    discount_type = models.CharField("Tipo descuento", max_length=20, choices=DISCOUNT_TYPES, default='percentage')
    discount_value = models.DecimalField("Valor descuento", max_digits=10, decimal_places=2)

    min_purchase_amount = models.DecimalField("Monto mínimo de compra", max_digits=10, decimal_places=2, default=0)
    max_discount_amount = models.DecimalField("Descuento máximo", max_digits=10, decimal_places=2, null=True, blank=True)

    valid_from = models.DateField("Válido desde", null=True, blank=True)
    valid_to = models.DateField("Válido hasta", null=True, blank=True)

    max_uses = models.IntegerField("Usos máximos", default=0, help_text="0 = ilimitado")
    used_count = models.IntegerField("Usos realizados", default=0)

    is_active = models.BooleanField("Activo", default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Promoción"
        verbose_name_plural = "Promociones"
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.code} ({self.get_discount_type_display()}: {self.discount_value})"

    def is_valid(self, total_amount=0):
        now = timezone.now().date()
        if not self.is_active:
            return False, "No activo"
        if self.valid_from and self.valid_from > now:
            return False, f"Válido desde {self.valid_from}"
        if self.valid_to and self.valid_to < now:
            return False, f"Expiró el {self.valid_to}"
        if self.max_uses > 0 and self.used_count >= self.max_uses:
            return False, "Límite de usos alcanzado"
        if total_amount < self.min_purchase_amount:
            return False, f"Monto mínimo ${self.min_purchase_amount}"
        return True, "Válido"

    def calculate_discount(self, total_amount):
        if self.discount_type == 'percentage':
            discount = total_amount * (self.discount_value / 100)
        else:
            discount = self.discount_value
        if self.max_discount_amount and discount > self.max_discount_amount:
            discount = self.max_discount_amount
        return max(0, discount)


# =========================================================
# Módulo de Encomiendas (Paquetes)
# =========================================================
class Parcel(models.Model):
    STATUS_CHOICES = (
        ('pending', 'Pendiente'),
        ('delivered', 'Entregada'),
        ('cancelled', 'Cancelada'),
    )
    trip = models.ForeignKey(Trip, on_delete=models.PROTECT, related_name='parcels')
    tracking_number = models.CharField("Nº seguimiento", max_length=20, unique=True, editable=False)
    sender_name = models.CharField("Remitente", max_length=140)
    sender_phone = models.CharField("Teléfono remitente", max_length=20)
    recipient_name = models.CharField("Destinatario", max_length=140)
    recipient_phone = models.CharField("Teléfono destinatario", max_length=20)
    recipient_rut = models.CharField("RUT destinatario", max_length=20, blank=True, default="")
    description = models.TextField("Descripción", blank=True)
    weight = models.DecimalField("Peso (kg)", max_digits=6, decimal_places=2, default=1.0)
    price = models.DecimalField("Tarifa", max_digits=10, decimal_places=2)
    payment_method = models.CharField("Método pago", max_length=10, choices=Ticket.PAYMENT_METHOD_CHOICES, default='cash')
    status = models.CharField("Estado", max_length=20, choices=STATUS_CHOICES, default='pending')
    delivered_at = models.DateTimeField("Fecha entrega", null=True, blank=True)
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name='parcels_created')
    created_at = models.DateTimeField(auto_now_add=True)
    notes = models.TextField("Notas internas", blank=True)

    class Meta:
        verbose_name = "Encomienda"
        verbose_name_plural = "Encomiendas"
        ordering = ('-created_at',)

    def __str__(self):
        return f"{self.tracking_number} - {self.sender_name} → {self.recipient_name}"

    def save(self, *args, **kwargs):
        if not self.tracking_number:
            today = timezone.now().strftime('%Y%m%d')
            last = Parcel.objects.filter(tracking_number__startswith=f'PAR-{today}').count()
            self.tracking_number = f"PAR-{today}-{last+1:05d}"
        super().save(*args, **kwargs)

    def deliver(self):
        if self.status == 'pending':
            self.status = 'delivered'
            self.delivered_at = timezone.now()
            self.save()


# =========================================================
# Mantenimiento de flota
# =========================================================
class Maintenance(models.Model):
    MAINTENANCE_TYPES = (
        ('preventive', 'Mantenimiento preventivo'),
        ('corrective', 'Mantenimiento correctivo'),
    )
    bus = models.ForeignKey(Bus, on_delete=models.CASCADE, related_name='maintenances')
    maintenance_type = models.CharField("Tipo", max_length=20, choices=MAINTENANCE_TYPES)
    date = models.DateField("Fecha mantenimiento")
    mileage = models.PositiveIntegerField("Kilometraje al momento")
    description = models.TextField("Descripción del trabajo")
    cost = models.DecimalField("Costo", max_digits=10, decimal_places=2, default=0)
    workshop = models.CharField("Taller", max_length=200, blank=True)
    next_maintenance_km = models.PositiveIntegerField("Próximo mantenimiento (km)", default=0,
                                                      help_text="Kilometraje sugerido para próximo mantenimiento")
    technician = models.CharField("Técnico responsable", max_length=100, blank=True)
    notes = models.TextField("Notas adicionales", blank=True)
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name='maintenances_created')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Mantenimiento"
        verbose_name_plural = "Mantenimientos"
        ordering = ('-date',)

    def __str__(self):
        return f"{self.bus.plate} - {self.date} - {self.get_maintenance_type_display()}"


# =========================================================
# Registro de Combustible
# =========================================================
class FuelRecord(models.Model):
    bus = models.ForeignKey('Bus', on_delete=models.PROTECT, related_name='fuel_records', verbose_name="Bus")
    date = models.DateField("Fecha de carga", help_text="Fecha en que se realizó la carga de combustible")
    liters = models.DecimalField("Litros cargados", max_digits=10, decimal_places=2,
                                 validators=[MinValueValidator(0.01)], help_text="Cantidad de combustible en litros")
    cost = models.DecimalField("Costo total", max_digits=10, decimal_places=2,
                               validators=[MinValueValidator(0)], help_text="Monto total pagado por la carga")
    mileage = models.PositiveIntegerField("Kilometraje al momento", help_text="Kilometraje del bus en el momento de la carga")
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name='fuel_records_created', verbose_name="Registrado por")
    created_at = models.DateTimeField("Fecha de registro", auto_now_add=True)
    notes = models.TextField("Observaciones", blank=True, help_text="Notas adicionales sobre la carga")
    invoice_number = models.CharField("N° Factura", max_length=50, blank=True, help_text="Número de factura o comprobante")
    gas_station = models.CharField("Estación de servicio", max_length=200, blank=True, help_text="Nombre de la estación donde se realizó la carga")
    fuel_type = models.CharField(
        "Tipo de combustible", max_length=30, blank=True,
        choices=[
            ('Diesel', 'Diesel'),
            ('Gasolina 93', 'Gasolina 93'),
            ('Gasolina 95', 'Gasolina 95'),
            ('Gasolina 97', 'Gasolina 97'),
            ('GLP', 'GLP'),
            ('GNV', 'GNV'),
            ('Otro', 'Otro'),
        ],
        default='Diesel'
    )

    class Meta:
        verbose_name = "Carga de combustible"
        verbose_name_plural = "Cargas de combustible"
        ordering = ('-date', '-created_at')
        indexes = [
            models.Index(fields=['bus', 'date']),
            models.Index(fields=['date']),
            models.Index(fields=['bus']),
        ]
        get_latest_by = 'date'

    def __str__(self):
        return f"{self.bus.plate} - {self.date} - {self.liters}L - ${self.cost:,.0f}"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if self.mileage > self.bus.current_mileage:
            self.bus.current_mileage = self.mileage
            self.bus.save(update_fields=['current_mileage'])

    @property
    def price_per_liter(self) -> float:
        if self.liters > 0:
            return float(self.cost / self.liters)
        return 0.0

    @classmethod
    def get_consumption_stats(cls, bus, start_date=None, end_date=None):
        queryset = cls.objects.filter(bus=bus)
        if start_date:
            queryset = queryset.filter(date__gte=start_date)
        if end_date:
            queryset = queryset.filter(date__lte=end_date)

        stats = queryset.aggregate(
            total_liters=models.Sum('liters'),
            total_cost=models.Sum('cost'),
            count=models.Count('id')
        )

        result = {
            'total_liters': stats['total_liters'] or 0,
            'total_cost': stats['total_cost'] or 0,
            'total_loads': stats['count'] or 0,
            'avg_price_per_liter': 0.0,
            'consumption_per_100km': 0.0,
        }

        if result['total_liters'] > 0:
            result['avg_price_per_liter'] = float(result['total_cost'] / result['total_liters'])

        if queryset.count() >= 2:
            first = queryset.first()
            last = queryset.last()
            if first and last and first.mileage < last.mileage:
                km_traveled = last.mileage - first.mileage
                if km_traveled > 0:
                    result['consumption_per_100km'] = float((result['total_liters'] / km_traveled) * 100)
                    result['km_traveled'] = km_traveled

        return result


class Sale(models.Model):
    STATUS = (
        ('draft', 'Borrador'),
        ('pending_payment', 'Pendiente Pago'),
        ('paid', 'Pagado'),
        ('cancelled', 'Cancelado'),
        ('reserved', 'Reservado'),
    )

    trip = models.ForeignKey(Trip, on_delete=models.PROTECT)
    customer = models.ForeignKey(
        Customer,
        on_delete=models.SET_NULL,
        null=True
    )
    total = models.DecimalField(
        max_digits=10,
        decimal_places=2
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS,
        default='draft'
    )
    created_by = models.ForeignKey(
        User,
        on_delete=models.PROTECT
    )
    created_at = models.DateTimeField(
        auto_now_add=True
    )


# =========================================================
# CONVENIOS
# =========================================================
class CompanyContract(models.Model):
    company = models.ForeignKey('Company', on_delete=models.PROTECT, related_name='contracts')
    contract_number = models.CharField("N° Contrato", max_length=50, unique=True)
    credit_limit = models.DecimalField("Límite de crédito", max_digits=12, decimal_places=2, default=0)
    used_credit = models.DecimalField("Crédito utilizado", max_digits=12, decimal_places=2, default=0)
    discount_percentage = models.DecimalField("Descuento (%)", max_digits=5, decimal_places=2, default=0)
    valid_from = models.DateField("Válido desde")
    valid_to = models.DateField("Válido hasta")
    is_active = models.BooleanField("Activo", default=True)

    contact_name = models.CharField("Nombre de contacto", max_length=140, blank=True)
    contact_phone = models.CharField("Teléfono de contacto", max_length=20, blank=True)
    contact_email = models.EmailField("Email de contacto", blank=True)

    notes = models.TextField("Observaciones", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Contrato de Convenio"
        verbose_name_plural = "Contratos de Convenio"
        ordering = ['company__name', 'contract_number']

    def __str__(self):
        return f"{self.company.name} - {self.contract_number}"

    @property
    def available_credit(self):
        return self.credit_limit - self.used_credit

    def can_purchase(self, amount):
        return self.available_credit >= amount


class ContractEmployee(models.Model):
    contract = models.ForeignKey(CompanyContract, on_delete=models.CASCADE, related_name='employees')
    customer = models.ForeignKey('Customer', on_delete=models.PROTECT, related_name='contracts_employee')
    employee_id = models.CharField("ID Interno (empresa)", max_length=50, blank=True, db_index=True)
    is_active = models.BooleanField("Activo", default=True)
    notes = models.TextField("Observaciones", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Empleado Convenio"
        verbose_name_plural = "Empleados Convenio"
        unique_together = (('contract', 'customer'),)
        ordering = ['contract__company__name', 'customer__full_name']

    def __str__(self):
        return f"{self.customer.full_name} ({self.contract.company.name})"


class AuditLog(models.Model):
    ACTION_CHOICES = (
        ('login', 'Inicio de sesión'),
        ('logout', 'Cierre de sesión'),
        ('create', 'Creación'),
        ('update', 'Actualización'),
        ('delete', 'Eliminación'),
        ('view', 'Visualización'),
        ('export', 'Exportación'),
        ('login_failed', 'Intento de login fallido'),
        ('download_backup', 'Descarga de respaldo'),
        ('backup_created', 'Respaldo creado'),
    )
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='audit_logs')
    action = models.CharField(max_length=20, choices=ACTION_CHOICES, db_index=True)
    model_name = models.CharField(max_length=100, blank=True, db_index=True)
    object_id = models.CharField(max_length=50, blank=True, null=True, db_index=True)
    object_repr = models.CharField(max_length=200, blank=True)
    changes = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)
    session_key = models.CharField(max_length=40, blank=True, null=True, db_index=True)

    class Meta:
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['timestamp', 'user']),
            models.Index(fields=['action', 'timestamp']),
        ]
        verbose_name = "Registro de auditoría"
        verbose_name_plural = "Registros de auditoría"

    def __str__(self):
        return f"{self.get_action_display()} - {self.user} - {self.timestamp}"



    # ============================================================
# RESERVAS WEB / ÓRDENES DE COMPRA
# ============================================================

class BookingOrder(models.Model):
    """
    Reserva/orden de compra previa al pago.

    IMPORTANTE:
    Una BookingOrder NO es todavía un boleto vendido.
    El Ticket se debe crear únicamente después de confirmar
    correctamente el pago con el proveedor (ej. Transbank).
    """

    STATUS_PENDING = "pending"
    STATUS_PAYMENT_STARTED = "payment_started"
    STATUS_PAID = "paid"
    STATUS_FAILED = "failed"
    STATUS_CANCELLED = "cancelled"
    STATUS_EXPIRED = "expired"

    STATUS_CHOICES = [
        (STATUS_PENDING, "Pendiente"),
        (STATUS_PAYMENT_STARTED, "Pago iniciado"),
        (STATUS_PAID, "Pagada"),
        (STATUS_FAILED, "Pago fallido"),
        (STATUS_CANCELLED, "Cancelada"),
        (STATUS_EXPIRED, "Expirada"),
    ]

    code = models.CharField(
        max_length=40,
        unique=True,
        db_index=True,
    )

    trip = models.ForeignKey(
        Trip,
        on_delete=models.PROTECT,
        related_name="booking_orders",
    )

    user = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="booking_orders",
    )

    session_key = models.CharField(
        max_length=100,
        db_index=True,
    )

    customer = models.ForeignKey(
        Customer,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="booking_orders",
    )

    subtotal = models.DecimalField(
        max_digits=12,
        decimal_places=2,
    )

    discount_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
    )

    total_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
    )

    status = models.CharField(
        max_length=30,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING,
        db_index=True,
    )


    buyer_name = models.CharField(
        max_length=150,
        blank=True,
        default="",
    )

    buyer_email = models.EmailField(
        max_length=254,
        blank=True,
        default="",
    )

    buyer_phone = models.CharField(
        max_length=30,
        blank=True,
        default="",
    )

    buyer_address = models.CharField(
        max_length=255,
        blank=True,
        default="",
    )

    discount_code = models.CharField(
        max_length=50,
        blank=True,
        default="",
    )


    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    expires_at = models.DateTimeField(
        db_index=True,
    )

    class Meta:
        ordering = ["-created_at"]

        indexes = [
            models.Index(
                fields=["status", "expires_at"],
                name="order_status_exp_idx",
            ),
            models.Index(
                fields=["trip", "status"],
                name="order_trip_status_idx",
            ),
            models.Index(
                fields=["session_key", "status"],
                name="order_session_status_idx",
            ),
        ]

        verbose_name = "Reserva web"
        verbose_name_plural = "Reservas web"

    def __str__(self):
        return f"Reserva {self.code} - {self.status}"


# ============================================================
# ASIENTOS / PASAJEROS DE LA RESERVA
# ============================================================

class BookingOrderSeat(models.Model):
    """
    Asiento/pasajero asociado a una BookingOrder.

    Permite que una misma reserva incluya varios pasajeros
    y varios asientos.
    """

    order = models.ForeignKey(
        BookingOrder,
        on_delete=models.CASCADE,
        related_name="items",
    )

    seat = models.ForeignKey(
        Seat,
        on_delete=models.PROTECT,
        related_name="booking_order_items",
    )

    passenger_name = models.CharField(
        max_length=150,
    )

    passenger_document = models.CharField(
        max_length=30,
    )

    price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    passenger_lastname = models.CharField(
        max_length=150,
        blank=True,
        default="",
    )

    passenger_nationality = models.CharField(
        max_length=10,
        default="CHI",
    )

    passenger_document_type = models.CharField(
        max_length=20,
        default="RUT",
    )
    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["order", "seat"],
                name="uniq_order_seat",
            )
        ]

        indexes = [
            models.Index(
                fields=["order", "seat"],
                name="order_seat_idx",
            ),
        ]

        verbose_name = "Asiento de reserva"
        verbose_name_plural = "Asientos de reserva"

    def __str__(self):
        return (
            f"{self.order.code} - "
            f"Asiento {self.seat.number}"
        )


# ============================================================
# TRANSACCIONES DE PAGO
# ============================================================

class PaymentTransaction(models.Model):
    """
    Registro de cada intento de pago asociado a una BookingOrder.

    Una orden puede tener más de un intento de pago.
    """

    STATUS_CREATED = "created"
    STATUS_AUTHORIZED = "authorized"
    STATUS_REJECTED = "rejected"
    STATUS_ABORTED = "aborted"
    STATUS_ERROR = "error"

    STATUS_CHOICES = [
        (STATUS_CREATED, "Creada"),
        (STATUS_AUTHORIZED, "Autorizada"),
        (STATUS_REJECTED, "Rechazada"),
        (STATUS_ABORTED, "Abortada"),
        (STATUS_ERROR, "Error"),
    ]

    order = models.ForeignKey(
        BookingOrder,
        on_delete=models.PROTECT,
        related_name="payments",
    )

    provider = models.CharField(
        max_length=30,
        default="transbank",
    )

    token = models.CharField(
        max_length=255,
        blank=True,
        db_index=True,
    )

    buy_order = models.CharField(
        max_length=50,
        unique=True,
        db_index=True,
    )

    session_id = models.CharField(
        max_length=100,
    )

    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
    )

    status = models.CharField(
        max_length=30,
        choices=STATUS_CHOICES,
        default=STATUS_CREATED,
        db_index=True,
    )

    authorization_code = models.CharField(
        max_length=100,
        blank=True,
    )

    response_code = models.IntegerField(
        null=True,
        blank=True,
    )

    raw_response = models.JSONField(
        default=dict,
        blank=True,
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    class Meta:
        ordering = ["-created_at"]

        indexes = [
            models.Index(
                fields=["order", "status"],
                name="payment_order_status_idx",
            ),
            models.Index(
                fields=["provider", "status"],
                name="payment_provider_status_idx",
            ),
        ]

        verbose_name = "Transacción de pago"
        verbose_name_plural = "Transacciones de pago"

    def __str__(self):
        return (
            f"{self.provider} - "
            f"{self.buy_order} - "
            f"{self.status}"
        )
