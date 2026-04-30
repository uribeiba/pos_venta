# Bus Tickets Backend (Cejer Edition)

- Admin en español chileno (es-cl), theme **verde/rojo** Cejer.
- API DRF para ciudades, búsqueda de viajes, asientos y compra.
- CORS: http://127.0.0.1:7000

## Setup
```
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py createsuperuser
python manage.py loaddata booking/fixtures/seed.json
python manage.py runserver 127.0.0.1:8000
```

## Endpoints
- GET /api/cities/
- GET /api/search-trips/?from=santiago&to=valparaiso&date=YYYY-MM-DD
- GET /api/trips/<id>/seats/
- POST /api/trips/<id>/purchase/
```
{ "seat_id": 1, "passenger_name": "Juan", "passenger_id": "11.111.111-1" }
```
