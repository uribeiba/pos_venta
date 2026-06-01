from django.urls import path
from . import views

# app_name = 'booking'   # COMENTADO

urlpatterns = [
    path("pos/", views.pos_home, name="pos_home"),
    path("pos/trip/<int:trip_id>/", views.pos_trip, name="pos_trip"),
    path("pos/trip/<int:trip_id>/modal/", views.pos_trip_modal, name="pos_trip_modal"),
    path("pos/checkout/<int:trip_id>/", views.pos_checkout, name="pos_checkout"),

    path("api/hold/<int:trip_id>/", views.api_hold, name="api_hold"),
    path("api/release/<int:trip_id>/", views.api_release, name="api_release"),
    path("pos/api/trip/<int:trip_id>/purchase/", views.api_purchase, name="pos_api_purchase"),

    path("search-customer/", views.search_customer, name="search_customer"),
    path("create-customer/", views.create_customer, name="create_customer"),

    path("buses/<int:bus_id>/seatmap/", views.bus_seatmap, name="bus_seatmap"),
    
    path("pos/caja/", views.pos_caja, name="pos_caja"),
    path("pos/caja/abrir/", views.abrir_caja, name="abrir_caja"),
    path("pos/caja/cerrar/", views.cerrar_caja, name="cerrar_caja"),
    path("pos/reportes/", views.pos_reportes, name="pos_reportes"),
    
    path('gestion-usuarios/', views.gestion_usuarios, name='gestion_usuarios'),
    path('gestion-usuarios/crear/', views.crear_usuario, name='crear_usuario'),
    path('gestion-usuarios/editar/<int:user_id>/', views.editar_usuario, name='editar_usuario'),

    path("api/cities/", views.api_cities, name="api_cities"),
    path("api/search-trips/", views.api_search_trips, name="api_search_trips"),
    path("api/trips/<int:trip_id>/seats/", views.trip_seats, name="api_trip_seats"),
    
    path("booking/api/save-layout-template/", views.save_layout_template, name="save_layout_template"),
]