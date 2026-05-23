from django.urls import path
from . import views

app_name = 'coordinator'

urlpatterns = [
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

    # ----- Documentos de choferes (nuevo) -----
    path('choferes/<int:driver_id>/documentos/', views.driver_documents, name='driver_documents'),
    path('choferes/<int:driver_id>/documentos/nuevo/', views.driver_document_create, name='driver_document_new'),
    path('documentos/chofer/editar/<int:doc_id>/', views.driver_document_edit, name='driver_document_edit'),
    path('documentos/chofer/eliminar/<int:doc_id>/', views.driver_document_delete, name='driver_document_delete'),

    # ----- Auxiliares -----
    path('auxiliares/', views.assistant_list, name='assistant_list'),
    path('auxiliares/nuevo/', views.assistant_create_edit, name='assistant_new'),
    path('auxiliares/editar/<int:assistant_id>/', views.assistant_create_edit, name='assistant_edit'),
    path('auxiliares/eliminar/<int:assistant_id>/', views.assistant_delete, name='assistant_delete'),

    # ----- Documentos de buses (nuevo) -----
    path('buses/<int:bus_id>/documentos/', views.bus_documents, name='bus_documents'),
    path('buses/<int:bus_id>/documentos/nuevo/', views.bus_document_create, name='bus_document_new'),
    path('documentos/bus/editar/<int:doc_id>/', views.bus_document_edit, name='bus_document_edit'),
    path('documentos/bus/eliminar/<int:doc_id>/', views.bus_document_delete, name='bus_document_delete'),

    # ----- Documentos por vencer (ya existente, se conserva) -----
    path('documentos-por-vencer/', views.expiring_documents, name='expiring_documents'),
    
    # Nueva interfaz unificada de choferes
    path('choferes/nueva-interface/', views.drivers_dashboard, name='drivers_dashboard'),
  
    # Nueva interfaz unificada de auxiliares
    path('auxiliares/dashboard/', views.assistants_dashboard, name='assistants_dashboard'),
    path('terminales/dashboard/', views.terminals_dashboard, name='terminals_dashboard'),
    path('rutas/dashboard/', views.routes_dashboard, name='routes_dashboard'),
    path('ciudades/dashboard/', views.cities_dashboard, name='cities_dashboard'),
    path('buses/dashboard/', views.buses_dashboard, name='buses_dashboard'),
    path('agencias/dashboard/', views.agencies_dashboard, name='agencies_dashboard'),
    path('agencias/eliminar/<int:agency_id>/', views.agency_delete, name='agency_delete'),
    path('viajes/dashboard/', views.trips_dashboard, name='trips_dashboard'),
    path('viajes/eliminar/<int:trip_id>/', views.trip_delete, name='trip_delete'),
    
    
]