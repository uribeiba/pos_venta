# booking/forms.py
from __future__ import annotations
from django import forms
from .models import Bus, BusLayout
from django.contrib.auth import get_user_model
from .models import UserProfile
import json
from .models import Driver  # asegúrate de importar Driver
from .models import Assistant
from .models import Route, RouteStop, City, Terminal, Agency
from .models import Trip



class BusWizardForm(forms.ModelForm):
    """
    Asistente para crear/editar un Bus sin tocar JSON a mano.
    - Plantillas rápidas (2+2, 2+1, 1+1) generan el layout automáticamente.
    - Bloqueos por coordenadas: marca celdas como 'X' (hueco) usando fila:col (1-indexado).
      Ejemplo: "3:3;4:3" bloquea las celdas (fila 3, col 3) y (fila 4, col 3).
    """

    TEMPLATE_CHOICES = [
        ("", "— Elegir plantilla (opcional) —"),
        ("2+2", "2 + Pasillo + 2 (5 columnas)"),
        ("2+1", "2 + Pasillo + 1 (4 columnas)"),
        ("1+1", "1 + Pasillo + 1 (3 columnas)"),
    ]

    # Asistente (no persisten en el modelo, solo influyen en clean())
    template = forms.ChoiceField(
        label="Plantilla rápida",
        required=False,
        choices=TEMPLATE_CHOICES,
        help_text="Si la eliges, el layout se generará automáticamente."
    )
    rows_lower_w = forms.IntegerField(
        label="Filas piso inferior (asistente)", required=False, min_value=0
    )
    rows_upper_w = forms.IntegerField(
        label="Filas piso superior (asistente)", required=False, min_value=0
    )

    # Bloqueos por coordenada (se convierten a 'X' en el layout)
    blocks_lower = forms.CharField(
        label="Bloqueos piso inferior (fila:col;…)",
        required=False,
        help_text="Ej: 3:3;4:3. Fila/col empiezan en 1."
    )
    blocks_upper = forms.CharField(
        label="Bloqueos piso superior (fila:col;…)",
        required=False,
        help_text="Ej: 1:2;2:2. Solo aplica si hay 2 pisos."
    )

    class Meta:
        model = Bus
        fields = "__all__"
        widgets = {
            "layout_lower": forms.Textarea(attrs={"rows": 3}),
            "layout_upper": forms.Textarea(attrs={"rows": 3}),
            "numbers_lower": forms.Textarea(attrs={"rows": 3}),
            "numbers_upper": forms.Textarea(attrs={"rows": 3}),
        }

    # -------------------- helpers del asistente --------------------
    @staticmethod
    def _row_for_template(template: str) -> list[str]:
        """
        Devuelve una fila base según la plantilla elegida.
        'L' = asiento; 'P' = pasillo.
        """
        if template == "2+2":  # 5 columnas
            return ["L", "L", "P", "L", "L"]
        if template == "2+1":  # 4 columnas
            return ["L", "L", "P", "L"]
        if template == "1+1":  # 3 columnas
            return ["L", "P", "L"]
        return []

    @staticmethod
    def _make_layout(rows: int, row_pattern: list[str]) -> list[str]:
        """Crea el arreglo lineal filas*columnas repitiendo la fila patrón."""
        if rows is None or rows <= 0 or not row_pattern:
            return []
        return row_pattern * rows

    @staticmethod
    def _parse_blocks(value: str) -> list[tuple[int, int]]:
        """'1:3;4:2' -> [(1,3), (4,2)] en base 1."""
        out: list[tuple[int, int]] = []
        for part in (value or "").replace(",", ";").split(";"):
            part = part.strip()
            if not part:
                continue
            try:
                r, c = part.split(":")
                out.append((int(r), int(c)))
            except Exception:
                # Silencioso: si hay un valor inválido, lo ignoramos.
                continue
        return out

    @staticmethod
    def _apply_blocks(layout: list[str], rows: int, cols: int, blocks: list[tuple[int, int]]):
        """Marca como 'X' las celdas indicadas (1-indexadas)."""
        if not layout or rows <= 0 or cols <= 0:
            return
        for r, c in blocks:
            if 1 <= r <= rows and 1 <= c <= cols:
                idx = (r - 1) * cols + (c - 1)
                if 0 <= idx < len(layout):
                    layout[idx] = "X"

    # ----------------------------------------------------------------

    def clean(self):
        cleaned = super().clean()

        # 1) Plantilla rápida (si la hay) -> genera layouts
        tpl = cleaned.get("template") or ""
        if tpl:
            row_pattern = self._row_for_template(tpl)
            if row_pattern:
                cleaned["cols"] = len(row_pattern)
                floors = int(cleaned.get("floors") or 1)

                rows_lower = self.cleaned_data.get("rows_lower_w")
                rows_upper = self.cleaned_data.get("rows_upper_w")
                if rows_lower is None:
                    rows_lower = int(cleaned.get("rows_lower") or 0)
                if rows_upper is None:
                    rows_upper = int(cleaned.get("rows_upper") or 0)

                cleaned["layout_lower"] = self._make_layout(rows_lower, row_pattern)
                cleaned["numbers_lower"] = [""] * len(cleaned["layout_lower"])

                if floors == 2:
                    cleaned["layout_upper"] = self._make_layout(rows_upper, row_pattern)
                    cleaned["numbers_upper"] = [""] * len(cleaned["layout_upper"])
                else:
                    cleaned["layout_upper"] = []
                    cleaned["numbers_upper"] = []

        # 2) Aplicar bloqueos por coordenada (convierte a 'X')
        cols = int(cleaned.get("cols") or 0)
        rows_lower = len(cleaned.get("layout_lower") or []) // cols if cols else 0
        rows_upper = len(cleaned.get("layout_upper") or []) // cols if cols else 0

        bl_lower = self._parse_blocks(self.cleaned_data.get("blocks_lower"))
        bl_upper = self._parse_blocks(self.cleaned_data.get("blocks_upper"))

        self._apply_blocks(cleaned.get("layout_lower") or [], rows_lower, cols, bl_lower)
        if int(cleaned.get("floors") or 1) == 2:
            self._apply_blocks(cleaned.get("layout_upper") or [], rows_upper, cols, bl_upper)

        # Prefijos: default cadena vacía
        cleaned["prefix_lower"] = cleaned.get("prefix_lower") or ""
        cleaned["prefix_upper"] = cleaned.get("prefix_upper") or ""
        return cleaned

    class Media:
        # JS opcional para mostrar/ocultar campos del asistente
        js = ("booking/bus_wizard.js",)



User = get_user_model()

class UsuarioCreateForm(forms.ModelForm):
    password = forms.CharField(widget=forms.PasswordInput, required=True)
    role = forms.ChoiceField(choices=UserProfile.ROLE_CHOICES)
    terminal = forms.ModelChoiceField(queryset=None, required=False)
    commission_rate = forms.DecimalField(max_digits=5, decimal_places=2, required=False, initial=0)
    max_discount = forms.DecimalField(max_digits=7, decimal_places=2, required=False, initial=0)

    class Meta:
        model = User
        fields = ['username', 'first_name', 'last_name', 'email', 'password', 'is_staff', 'is_active']

    def __init__(self, *args, **kwargs):
        from .models import Terminal
        super().__init__(*args, **kwargs)
        self.fields['terminal'].queryset = Terminal.objects.all()

class UsuarioEditForm(forms.ModelForm):
    role = forms.ChoiceField(choices=UserProfile.ROLE_CHOICES)
    terminal = forms.ModelChoiceField(queryset=None, required=False)
    commission_rate = forms.DecimalField(max_digits=5, decimal_places=2, required=False)
    max_discount = forms.DecimalField(max_digits=7, decimal_places=2, required=False)

    class Meta:
        model = User
        fields = ['first_name', 'last_name', 'email', 'is_staff', 'is_active']

    def __init__(self, *args, **kwargs):
        from .models import Terminal
        user = kwargs.pop('user_instance')
        super().__init__(*args, **kwargs)
        self.fields['terminal'].queryset = Terminal.objects.all()
        profile = getattr(user, 'profile', None)
        if profile:
            self.fields['role'].initial = profile.role
            self.fields['terminal'].initial = profile.terminal
            self.fields['commission_rate'].initial = profile.commission_rate
            self.fields['max_discount'].initial = profile.max_discount



class BusAdminForm(forms.ModelForm):
    class Meta:
        model = Bus
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        field = self.fields.get("layout_template")
        if field:
            field.widget.attrs["class"] = "js-layout-template"

            # 🔥 Inyectar data en cada opción
            choices = []
            for layout in BusLayout.objects.all():
                choices.append((
                    layout.id,
                    layout.name,
                    {
                        "data-layout-lower": json.dumps(layout.layout_lower),
                        "data-layout-upper": json.dumps(layout.layout_upper),
                        "data-cols": layout.cols,
                        "data-floors": layout.floors,
                        "data-rows-lower": layout.rows_lower,
                        "data-rows-upper": layout.rows_upper,
                    }
                ))
            field.choices = choices


class DriverForm(forms.ModelForm):
    class Meta:
        model = Driver
        fields = [
            'full_name', 'rut', 'phone', 'email', 'license_number',
            'medical_cert_expiry', 'background_check_expiry', 'photo', 'notes', 'is_active'
        ]
        widgets = {
            'full_name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Nombre completo'}),
            'rut': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '12.345.678-9'}),
            'phone': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '+56 9 1234 5678'}),
            'email': forms.EmailInput(attrs={'class': 'form-control', 'placeholder': 'conductor@empresa.cl'}),
            'license_number': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'N° licencia'}),
            'medical_cert_expiry': forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}),
            'background_check_expiry': forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}),
            'notes': forms.Textarea(attrs={'rows': 3, 'class': 'form-control', 'placeholder': 'Observaciones (opcional)'}),
            'photo': forms.ClearableFileInput(attrs={'class': 'form-control-file'}),
            'is_active': forms.CheckboxInput(attrs={'class': 'form-check-input', 'role': 'switch'}),
        }
        labels = {
            'full_name': 'NOMBRE COMPLETO',
            'rut': 'RUT',
            'phone': 'TELÉFONO',
            'email': 'CORREO ELECTRÓNICO',
            'license_number': 'Nº LICENCIA',
            'medical_cert_expiry': 'VENCIMIENTO CERT. MÉDICO',
            'background_check_expiry': 'VENCIMIENTO ANTECEDENTES',
            'photo': 'FOTO',
            'notes': 'OBSERVACIONES',
            'is_active': 'ACTIVO',
        }
        


class AssistantForm(forms.ModelForm):
    class Meta:
        model = Assistant
        fields = ['full_name', 'rut', 'phone', 'email', 'photo', 'notes', 'is_active']
        widgets = {
            'full_name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Nombre completo'}),
            'rut': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '12.345.678-9'}),
            'phone': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '+56 9 1234 5678'}),
            'email': forms.EmailInput(attrs={'class': 'form-control', 'placeholder': 'auxiliar@empresa.cl'}),
            'photo': forms.ClearableFileInput(attrs={'class': 'form-control-file'}),
            'notes': forms.Textarea(attrs={'rows': 3, 'class': 'form-control', 'placeholder': 'Observaciones (opcional)'}),
            'is_active': forms.CheckboxInput(attrs={'class': 'form-check-input', 'role': 'switch'}),
        }
        labels = {
            'full_name': 'NOMBRE COMPLETO',
            'rut': 'RUT',
            'phone': 'TELÉFONO',
            'email': 'CORREO ELECTRÓNICO',
            'photo': 'FOTO',
            'notes': 'OBSERVACIONES',
            'is_active': 'ACTIVO',
        }


class TerminalForm(forms.ModelForm):
    class Meta:
        model = Terminal
        fields = ['name', 'city', 'address', 'is_active']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Nombre de la terminal'}),
            'city': forms.Select(attrs={'class': 'form-select'}),
            'address': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Dirección'}),
            'is_active': forms.CheckboxInput(attrs={'class': 'form-check-input', 'role': 'switch'}),
        }
        labels = {
            'name': 'NOMBRE',
            'city': 'CIUDAD',
            'address': 'DIRECCIÓN',
            'is_active': 'ACTIVO',
        }
        
  
class RouteForm(forms.ModelForm):
    class Meta:
        model = Route
        fields = ['origin', 'destination', 'origin_terminal', 'destination_terminal', 'duration_minutes', 'base_price', 'is_active']
        widgets = {
            'origin': forms.Select(attrs={'class': 'form-select'}),
            'destination': forms.Select(attrs={'class': 'form-select'}),
            'origin_terminal': forms.Select(attrs={'class': 'form-select'}),
            'destination_terminal': forms.Select(attrs={'class': 'form-select'}),
            'duration_minutes': forms.NumberInput(attrs={'class': 'form-control', 'min': 0}),
            'base_price': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01', 'min': 0}),
            'is_active': forms.CheckboxInput(attrs={'class': 'form-check-input', 'role': 'switch'}),
        }
        labels = {
            'origin': 'ORIGEN',
            'destination': 'DESTINO',
            'origin_terminal': 'TERMINAL ORIGEN (opcional)',
            'destination_terminal': 'TERMINAL DESTINO (opcional)',
            'duration_minutes': 'DURACIÓN (min)',
            'base_price': 'PRECIO BASE ($)',
            'is_active': 'ACTIVO',
        }

# Formset para paradas
RouteStopFormSet = forms.inlineformset_factory(
    Route, RouteStop,
    fields=['city', 'terminal', 'order', 'extra_price', 'is_mandatory', 'notes'],
    extra=1,
    can_delete=True,
    widgets={
        'city': forms.Select(attrs={'class': 'form-select'}),
        'terminal': forms.Select(attrs={'class': 'form-select'}),
        'order': forms.NumberInput(attrs={'class': 'form-control', 'style': 'width:80px'}),
        'extra_price': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
        'is_mandatory': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        'notes': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Opcional'}),
    },
    labels={
        'city': 'Ciudad',
        'terminal': 'Terminal (opcional)',
        'order': 'Orden',
        'extra_price': 'Precio adicional',
        'is_mandatory': 'Obligatoria',
        'notes': 'Notas',
    }
)

class CityForm(forms.ModelForm):
    class Meta:
        model = City
        fields = ['name']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Ej: Santiago'}),
        }
        labels = {
            'name': 'NOMBRE DE LA CIUDAD',
        }

class BusFullForm(forms.ModelForm):
    class Meta:
        model = Bus
        fields = [
            # Empresa y datos básicos (ya existentes)
            'company', 'plate', 'model', 'year',
            'floors', 'rows_lower', 'rows_upper', 'cols',
            'prefix_lower', 'prefix_upper',
            # Nuevos campos
            'owner_first_name', 'owner_last_name',
            'circulation_card', 'vehicle_class', 'brand', 'manufacturing_year',
            'fuel_type', 'bodywork', 'axles', 'color', 'engine_number',
            'cylinders', 'serial_number', 'wheels_count', 'dry_weight',
            'gross_weight', 'length', 'height', 'width', 'total_passengers',
            'total_seats', 'service_type',
            # Fechas de documentos
            'technical_review_expiry', 'insurance_expiry', 'permit_expiry', 'last_maintenance',
            'is_active',  # si lo tienes, sino agregar
        ]
        widgets = {
            'company': forms.Select(attrs={'class': 'form-select'}),
            'plate': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'ABC-123'}),
            'model': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Omnibus GX'}),
            'year': forms.NumberInput(attrs={'class': 'form-control', 'min': 1980, 'max': 2030}),
            'floors': forms.NumberInput(attrs={'class': 'form-control'}),
            'rows_lower': forms.NumberInput(attrs={'class': 'form-control'}),
            'rows_upper': forms.NumberInput(attrs={'class': 'form-control'}),
            'cols': forms.NumberInput(attrs={'class': 'form-control'}),
            'prefix_lower': forms.TextInput(attrs={'class': 'form-control'}),
            'prefix_upper': forms.TextInput(attrs={'class': 'form-control'}),
            'owner_first_name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Nombres'}),
            'owner_last_name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Apellidos'}),
            'circulation_card': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'N° tarjeta'}),
            'vehicle_class': forms.Select(attrs={'class': 'form-select'}),
            'brand': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Marca'}),
            'manufacturing_year': forms.NumberInput(attrs={'class': 'form-control'}),
            'fuel_type': forms.Select(attrs={'class': 'form-select'}),
            'bodywork': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Tipo carrocería'}),
            'axles': forms.NumberInput(attrs={'class': 'form-control', 'min': 1}),
            'color': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Color principal'}),
            'engine_number': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'N° motor'}),
            'cylinders': forms.NumberInput(attrs={'class': 'form-control', 'min': 0}),
            'serial_number': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'VIN'}),
            'wheels_count': forms.NumberInput(attrs={'class': 'form-control', 'min': 2}),
            'dry_weight': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
            'gross_weight': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
            'length': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
            'height': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
            'width': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
            'total_passengers': forms.NumberInput(attrs={'class': 'form-control'}),
            'total_seats': forms.NumberInput(attrs={'class': 'form-control'}),
            'service_type': forms.Select(attrs={'class': 'form-select'}),
            'technical_review_expiry': forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}),
            'insurance_expiry': forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}),
            'permit_expiry': forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}),
            'last_maintenance': forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}),
        }
        labels = {
            'company': 'EMPRESA',
            'plate': 'PATENTE',
            'model': 'MODELO',
            'year': 'AÑO',
            'floors': 'PISOS',
            'rows_lower': 'FILAS INFERIOR',
            'rows_upper': 'FILAS SUPERIOR',
            'cols': 'ASIENTOS POR FILA',
            'prefix_lower': 'PREFIJO INFERIOR',
            'prefix_upper': 'PREFIJO SUPERIOR',
            'owner_first_name': 'NOMBRES DEL PROPIETARIO',
            'owner_last_name': 'APELLIDOS DEL PROPIETARIO',
            'circulation_card': 'TARJETA DE CIRCULACIÓN',
            'vehicle_class': 'CLASE',
            'brand': 'MARCA',
            'manufacturing_year': 'AÑO FABRICACIÓN',
            'fuel_type': 'TIPO COMBUSTIBLE',
            'bodywork': 'CARROCERÍA',
            'axles': 'EJES',
            'color': 'COLOR',
            'engine_number': 'N° MOTOR',
            'cylinders': 'CILINDROS',
            'serial_number': 'N° SERIE',
            'wheels_count': 'CANT. RUEDAS',
            'dry_weight': 'PESO SECO (kg)',
            'gross_weight': 'PESO BRUTO (kg)',
            'length': 'LONGITUD (m)',
            'height': 'ALTURA (m)',
            'width': 'ANCHO (m)',
            'total_passengers': 'TOTAL PASAJEROS',
            'total_seats': 'TOTAL ASIENTOS',
            'service_type': 'TIPO SERVICIO',
            'technical_review_expiry': 'VENC. REVISIÓN TÉCNICA',
            'insurance_expiry': 'VENC. SEGURO',
            'permit_expiry': 'VENC. PERMISO CIRCULACIÓN',
            'last_maintenance': 'ÚLT. MANTENIMIENTO',
        }
        
        



class AgencyForm(forms.ModelForm):
    class Meta:
        model = Agency
        fields = ['name', 'city', 'address', 'phone', 'email', 'is_active']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Ej: Agencia Central'}),
            'city': forms.Select(attrs={'class': 'form-select'}),
            'address': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Calle, número, etc.'}),
            'phone': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '+56 9 1234 5678'}),
            'email': forms.EmailInput(attrs={'class': 'form-control', 'placeholder': 'agencia@ejemplo.cl'}),
            'is_active': forms.CheckboxInput(attrs={'class': 'form-check-input', 'role': 'switch'}),
        }
        labels = {
            'name': 'NOMBRE DE LA AGENCIA',
            'city': 'CIUDAD',
            'address': 'DIRECCIÓN',
            'phone': 'TELÉFONO',
            'email': 'CORREO ELECTRÓNICO',
            'is_active': 'ACTIVA',
        }        
        

class TripForm(forms.ModelForm):
    class Meta:
        model = Trip
        fields = ['route', 'bus', 'driver1', 'driver2', 'assistant', 'departure', 'arrival', 'seats_total']
        widgets = {
            'route': forms.Select(attrs={'class': 'form-select'}),
            'bus': forms.Select(attrs={'class': 'form-select', 'id': 'id_bus'}),
            'driver1': forms.Select(attrs={'class': 'form-select'}),
            'driver2': forms.Select(attrs={'class': 'form-select'}),
            'assistant': forms.Select(attrs={'class': 'form-select'}),
            'departure': forms.DateTimeInput(attrs={'type': 'datetime-local', 'class': 'form-control'}),
            'arrival': forms.DateTimeInput(attrs={'type': 'datetime-local', 'class': 'form-control'}),
            'seats_total': forms.NumberInput(attrs={'class': 'form-control', 'readonly': 'readonly'}),
        }
        labels = {
            'route': 'RUTA',
            'bus': 'BUS',
            'driver1': 'CHOFER PRINCIPAL',
            'driver2': 'SEGUNDO CHOFER (opcional)',
            'assistant': 'AUXILIAR (opcional)',
            'departure': 'FECHA Y HORA DE SALIDA',
            'arrival': 'FECHA Y HORA DE LLEGADA (opcional)',
            'seats_total': 'TOTAL DE ASIENTOS',
        }
        help_texts = {
            'arrival': 'Opcional. Si no se ingresa, se calculará sumando la duración de la ruta.',
            'seats_total': 'Se calcula automáticamente según el bus seleccionado.',
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Filtrar choferes y auxiliares activos
        self.fields['driver1'].queryset = Driver.objects.filter(is_active=True)
        self.fields['driver2'].queryset = Driver.objects.filter(is_active=True)
        self.fields['assistant'].queryset = Assistant.objects.filter(is_active=True)
        # Añadir opción vacía para campos opcionales
        self.fields['driver2'].empty_label = "-- Sin segundo chofer --"
        self.fields['assistant'].empty_label = "-- Sin auxiliar --"