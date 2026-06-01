from django import forms
from django.utils import timezone
from booking.models import Route, Bus, Driver, Assistant

class RecurringTripForm(forms.Form):
    route = forms.ModelChoiceField(queryset=Route.objects.filter(is_active=True), label="Ruta")
    bus = forms.ModelChoiceField(queryset=Bus.objects.filter(is_active=True), label="Bus")
    driver1 = forms.ModelChoiceField(queryset=Driver.objects.filter(is_active=True), label="Chofer principal", required=False)
    driver2 = forms.ModelChoiceField(queryset=Driver.objects.filter(is_active=True), label="Chofer secundario", required=False)
    assistant = forms.ModelChoiceField(queryset=Assistant.objects.filter(is_active=True), label="Auxiliar", required=False)
    
    departure_time = forms.TimeField(label="Hora de salida", widget=forms.TimeInput(attrs={'type': 'time'}))
    start_date = forms.DateField(label="Fecha inicio", widget=forms.DateInput(attrs={'type': 'date'}))
    end_date = forms.DateField(label="Fecha fin", widget=forms.DateInput(attrs={'type': 'date'}))
    
    repeat_weekdays = forms.MultipleChoiceField(
        label="Días de la semana",
        choices=[
            (1, 'Lunes'), (2, 'Martes'), (3, 'Miércoles'), (4, 'Jueves'),
            (5, 'Viernes'), (6, 'Sábado'), (7, 'Domingo')
        ],
        widget=forms.CheckboxSelectMultiple,
        initial=[1,2,3,4,5,6,7]
    )
    
    def clean(self):
        data = super().clean()
        start = data.get('start_date')
        end = data.get('end_date')
        if start and end and start > end:
            raise forms.ValidationError("La fecha fin debe ser posterior a la fecha inicio")
        return data