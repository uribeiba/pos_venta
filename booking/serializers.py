from rest_framework import serializers
from .models import City, Route, Trip, Seat, Ticket, Company, Bus

# ---------- City ----------
class CitySerializer(serializers.ModelSerializer):
    class Meta:
        model = City
        fields = ["id", "name", "slug"]
        read_only_fields = fields


# ---------- Company ----------
class CompanySerializer(serializers.ModelSerializer):
    logo_url = serializers.SerializerMethodField()

    class Meta:
        model = Company
        fields = ["id", "name", "logo", "logo_url"]
        read_only_fields = fields

    def get_logo_url(self, obj):
        # Si usas ImageField/FileField y quieres URL absoluta
        request = self.context.get("request")
        if hasattr(obj, "logo") and obj.logo:
            url = getattr(obj.logo, "url", None)
            if url and request:
                return request.build_absolute_uri(url)
            return url
        return None


# ---------- Bus ----------
class BusSerializer(serializers.ModelSerializer):
    company = CompanySerializer(read_only=True)

    class Meta:
        model = Bus
        fields = ["id", "company", "plate", "model", "year", "decks"]
        read_only_fields = fields


# ---------- Route ----------
class RouteSerializer(serializers.ModelSerializer):
    origin = CitySerializer(read_only=True)
    destination = CitySerializer(read_only=True)

    class Meta:
        model = Route
        fields = ["id", "origin", "destination", "duration_minutes", "base_price"]
        read_only_fields = fields


# ---------- Trip ----------
class TripSerializer(serializers.ModelSerializer):
    route = RouteSerializer(read_only=True)
    bus = BusSerializer(read_only=True)
    seats_sold = serializers.SerializerMethodField()

    # (Opcional) formatos ISO claros; si ya usas DRF global DATETIME_FORMAT, puedes quitarlo
    departure = serializers.DateTimeField(format="%Y-%m-%dT%H:%M:%S%z")
    arrival = serializers.DateTimeField(format="%Y-%m-%dT%H:%M:%S%z")

    class Meta:
        model = Trip
        fields = ["id", "route", "bus", "departure", "arrival", "seats_total", "seats_sold"]
        read_only_fields = fields

    def get_seats_sold(self, obj):
        """
        Soporta tanto related_name='tickets' como el default 'ticket_set'.
        Si tu view ya anota el conteo, úsalo (obj.seats_sold_annotated).
        """
        # 1) Si fue anotado en queryset (recomendado)
        annotated = getattr(obj, "seats_sold_annotated", None)
        if annotated is not None:
            return annotated

        # 2) Related name flexible
        rel = getattr(obj, "tickets", None) or getattr(obj, "ticket_set", None)
        if rel is not None:
            return rel.count()
        # 3) Fallback seguro
        return Ticket.objects.filter(trip=obj).count()


# ---------- Seat ----------
class SeatSerializer(serializers.ModelSerializer):
    class Meta:
        model = Seat
        fields = ["id", "number", "deck", "row", "position", "is_window"]
        read_only_fields = fields
