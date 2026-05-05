from django.urls import path
from . import views

app_name = 'coordinator'

urlpatterns = [
    # Buses
    path('buses/', views.bus_list, name='bus_list'),
    path('buses/nuevo/', views.bus_editor, name='bus_new'),
    path('buses/editar/<int:bus_id>/', views.bus_editor, name='bus_editor'),
    path('buses/duplicar/<int:bus_id>/', views.bus_duplicate, name='bus_duplicate'),
    path('buses/api/<int:bus_id>/', views.api_bus_data, name='api_bus_data'),

    # Viajes
    path('viajes/', views.trip_list, name='trip_list'),
    path('viajes/nuevo/', views.trip_create_edit, name='trip_new'),
    path('viajes/editar/<int:trip_id>/', views.trip_create_edit, name='trip_edit'),
    
    path('buses/eliminar/<int:bus_id>/', views.bus_delete, name='bus_delete'),
    path('buses/eliminar-masivo/', views.bus_delete_massive, name='bus_delete_massive'),
]