# booking/forms.py
from __future__ import annotations
from django import forms
from .models import Bus

from django.contrib.auth import get_user_model
from .models import UserProfile

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
