from django.urls import path
from . import views

app_name = 'coordinator'

urlpatterns = [
    # ===== RUTA RAÍZ DEL COORDINADOR =====
    path('', views.dashboard, name='dashboard'),  # ← CORREGIDO: antes tenía "pos_home"

 
     # ----- Buses -----
    path('buses/', views.bus_list, name='bus_list'),
    path('buses/nuevo/', views.bus_editor, name='bus_new'),
    path('buses/editar/<int:bus_id>/', views.bus_editor, name='bus_editor'),
    path('buses/duplicar/<int:bus_id>/', views.bus_duplicate, name='bus_duplicate'),
    path('buses/eliminar/<int:bus_id>/', views.bus_delete, name='bus_delete'),
    path('buses/eliminar-masivo/', views.bus_delete_massive, name='bus_delete_massive'),
    path('buses/api/<int:bus_id>/', views.api_bus_data, name='api_bus_data'),

    # ----- Viajes -----
    path('viajes/', views.trip_list, name='trip_list'),
    path('viajes/nuevo/', views.trip_create_edit, name='trip_new'),
    path('viajes/editar/<int:trip_id>/', views.trip_create_edit, name='trip_edit'),
    path('viaje/<int:trip_id>/cambiar-bus/', views.trip_change_bus, name='trip_change_bus'),

    # ----- Ciudades -----
    path('ciudades/', views.city_list, name='city_list'),
    path('ciudades/nueva/', views.city_create_edit, name='city_new'),
    path('ciudades/editar/<int:city_id>/', views.city_create_edit, name='city_edit'),
    path('ciudades/eliminar/<int:city_id>/', views.city_delete, name='city_delete'),

    # ----- Terminales -----
    path('terminales/', views.terminal_list, name='terminal_list'),
    path('terminales/nueva/', views.terminal_create_edit, name='terminal_new'),
    path('terminales/editar/<int:terminal_id>/', views.terminal_create_edit, name='terminal_edit'),
    path('terminales/eliminar/<int:terminal_id>/', views.terminal_delete, name='terminal_delete'),

    # ----- Rutas -----
    path('rutas/', views.route_list, name='route_list'),
    path('rutas/nueva/', views.route_create_edit, name='route_new'),
    path('rutas/editar/<int:route_id>/', views.route_create_edit, name='route_edit'),
    path('rutas/eliminar/<int:route_id>/', views.route_delete, name='route_delete'),

    # ----- Choferes -----
    path('choferes/', views.driver_list, name='driver_list'),
    path('choferes/nuevo/', views.driver_create_edit, name='driver_new'),
    path('choferes/editar/<int:driver_id>/', views.driver_create_edit, name='driver_edit'),
    path('choferes/eliminar/<int:driver_id>/', views.driver_delete, name='driver_delete'),

    # ----- Documentos de choferes -----
    path('choferes/<int:driver_id>/documentos/', views.driver_documents, name='driver_documents'),
    path('choferes/<int:driver_id>/documentos/nuevo/', views.driver_document_create, name='driver_document_new'),
    path('documentos/chofer/editar/<int:doc_id>/', views.driver_document_edit, name='driver_document_edit'),
    path('documentos/chofer/eliminar/<int:doc_id>/', views.driver_document_delete, name='driver_document_delete'),

    # ----- Auxiliares -----
    path('auxiliares/', views.assistant_list, name='assistant_list'),
    path('auxiliares/nuevo/', views.assistant_create_edit, name='assistant_new'),
    path('auxiliares/editar/<int:assistant_id>/', views.assistant_create_edit, name='assistant_edit'),
    path('auxiliares/eliminar/<int:assistant_id>/', views.assistant_delete, name='assistant_delete'),

    # ----- Documentos de buses -----
    path('buses/<int:bus_id>/documentos/', views.bus_documents, name='bus_documents'),
    path('buses/<int:bus_id>/documentos/nuevo/', views.bus_document_create, name='bus_document_new'),
    path('documentos/bus/editar/<int:doc_id>/', views.bus_document_edit, name='bus_document_edit'),
    path('documentos/bus/eliminar/<int:doc_id>/', views.bus_document_delete, name='bus_document_delete'),

    # ----- Documentos por vencer -----
    path('documentos-por-vencer/', views.expiring_documents, name='expiring_documents'),

    # ----- Interfaz unificada (dashboards) -----
    path('choferes/nueva-interface/', views.drivers_dashboard, name='drivers_dashboard'),
    path('auxiliares/dashboard/', views.assistants_dashboard, name='assistants_dashboard'),
    path('terminales/dashboard/', views.terminals_dashboard, name='terminals_dashboard'),
    path('rutas/dashboard/', views.routes_dashboard, name='routes_dashboard'),
    path('ciudades/dashboard/', views.cities_dashboard, name='cities_dashboard'),
    path('buses/dashboard/', views.buses_dashboard, name='buses_dashboard'),
    path('agencias/dashboard/', views.agencies_dashboard, name='agencies_dashboard'),
    path('agencias/eliminar/<int:agency_id>/', views.agency_delete, name='agency_delete'),
    path('viajes/dashboard/', views.trips_dashboard, name='trips_dashboard'),
    path('viajes/eliminar/<int:trip_id>/', views.trip_delete, name='trip_delete'),

    # ----- Generación recurrente de viajes -----
    path('generate-trips/', views.generate_trips, name='generate_trips'),
    path('api/trips-calendar/', views.api_trips_calendar, name='api_trips_calendar'),
    path('delete-trip-by-date/', views.delete_trip_by_date, name='delete_trip_by_date'),

    # ----- Detalles y reportes -----
    path('viajes/detalle/<int:trip_id>/', views.trip_detail, name='trip_detail'),
    path('buses/detalle/<int:bus_id>/', views.bus_detail, name='bus_detail'),
    path('reportes/ocupacion/', views.occupancy_report, name='occupancy_report'),
    path('checkin/scan/',views.checkin_qr_scan,name='checkin_qr_scan'),
    path('checkin/<str:ticket_number>/', views.checkin_ticket, name='checkin'),

    # ----- Encomiendas -----
    path('encomiendas/', views.parcel_list, name='parcel_list'),
    path('encomiendas/entregar/<int:parcel_id>/', views.parcel_deliver, name='parcel_deliver'),

    # ----- Mantenimiento y combustible -----
    path('mantenimiento/', views.maintenance_list, name='maintenance_list'),
    path('mantenimiento/nuevo/', views.maintenance_create, name='maintenance_create'),
    path('mantenimiento/nuevo/<int:bus_id>/', views.maintenance_create, name='maintenance_create_for_bus'),
    path('mantenimiento/editar/<int:pk>/', views.maintenance_edit, name='maintenance_edit'),
    path('mantenimiento/eliminar/<int:pk>/', views.maintenance_delete, name='maintenance_delete'),
    path('combustible/', views.fuel_list, name='fuel_list'),
    path('combustible/nuevo/', views.fuel_create, name='fuel_create'),
    path('combustible/eliminar/<int:pk>/', views.fuel_delete, name='fuel_delete'),
    
    # ===== SEGURIDAD - LEY 21.719 =====
    path('seguridad/', views.seguridad_dashboard, name='seguridad_dashboard'),  # <--- Panel principal
    path('seguridad/auditoria/', views.seguridad_auditoria, name='seguridad_auditoria'),
    path('seguridad/respaldos/', views.seguridad_respaldos, name='seguridad_respaldos'),
    path('seguridad/incidentes/', views.seguridad_incidentes, name='seguridad_incidentes'),
    path('seguridad/respaldos/crear/', views.seguridad_crear_respaldo, name='seguridad_crear_respaldo'),
    path('seguridad/respaldos/descargar/<str:filename>/', views.seguridad_descargar_respaldo, name='seguridad_descargar_respaldo'),
    path('seguridad/respaldos/eliminar/<str:filename>/', views.seguridad_eliminar_respaldo, name='seguridad_eliminar_respaldo'),
    path('seguridad/respaldos/programacion/', views.seguridad_guardar_programacion, name='seguridad_guardar_programacion'),
    path('seguridad/incidentes/resolver/<int:incident_id>/', views.seguridad_resolver_incidente, name='seguridad_resolver_incidente'),
    
]