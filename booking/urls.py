from django.urls import path
from . import views

# app_name = 'booking'   # COMENTADO (para que las URLs sean globales)

urlpatterns = [
    # ===== RUTA RAÍZ DEL POS =====
    path("", views.pos_home, name="pos_home"),  # ← VOLVER A POS_HOME (VENTAS)

    # ===== RUTAS PRINCIPALES DEL POS =====
    path("trip/<int:trip_id>/", views.pos_trip, name="pos_trip"),
    path("trip/<int:trip_id>/modal/", views.pos_trip_modal, name="pos_trip_modal"),
    path("checkout/<int:trip_id>/", views.pos_checkout, name="pos_checkout"),

    # ===== NUEVA RUTA DE CONFIRMACIÓN =====
    path("confirmation/<int:trip_id>/", views.pos_confirmation, name="pos_confirmation"),

    # ===== API =====
    path("api/hold/<int:trip_id>/", views.api_hold, name="api_hold"),
    path("api/release/<int:trip_id>/", views.api_release, name="api_release"),
    path("api/trip/<int:trip_id>/purchase/", views.api_purchase, name="pos_api_purchase"),

    # ===== CLIENTES =====
    path("search-customer/", views.search_customer, name="search_customer"),
    path("create-customer/", views.create_customer, name="create_customer"),

    # ===== BUS =====
    path("buses/<int:bus_id>/seatmap/", views.bus_seatmap, name="bus_seatmap"),

    # ===== CAJA (solo para cajeros/administradores) =====
    path("caja/", views.pos_caja, name="pos_caja"),          # ← /pos/caja/
    path("caja/abrir/", views.abrir_caja, name="abrir_caja"),
    path("caja/cerrar/", views.cerrar_caja, name="cerrar_caja"),
    path("reportes/", views.pos_reportes, name="pos_reportes"),

    # ===== GESTIÓN DE USUARIOS =====
    path('gestion-usuarios/', views.gestion_usuarios, name='gestion_usuarios'),
    path('gestion-usuarios/crear/', views.crear_usuario, name='crear_usuario'),
    path('gestion-usuarios/editar/<int:user_id>/', views.editar_usuario, name='editar_usuario'),

    # ===== APIS EXTERNAS =====
    path("api/cities/", views.api_cities, name="api_cities"),
    path("api/search-trips/", views.api_search_trips, name="api_search_trips"),
    path("api/trips/<int:trip_id>/seats/", views.trip_seats, name="api_trip_seats"),
    path("api/save-layout-template/", views.save_layout_template, name="save_layout_template"),
    path("api/cities-search/", views.api_cities_search, name="api_cities_search"),
    path("api/validate-discount/", views.validate_discount, name="validate_discount"),
    path("api/clean-all-holds/", views.api_clean_all_holds, name="api_clean_all_holds"),

    # ===== ENCOMIENDAS =====
    path("api/parcel/create/<int:trip_id>/", views.api_create_parcel, name="api_create_parcel"),
    path("parcel/receipt/<int:parcel_id>/", views.parcel_receipt, name="parcel_receipt"),

    # ===== CIERRE DE CAJA Y LOGOUT =====
    path('cerrar-caja-salir/', views.cerrar_caja_y_logout, name='cerrar_caja_y_logout'),
    path('logout/', views.logout_view, name='logout'),

    # ===== PORTAL PROPIETARIO / SOCIO =====
    path(
    'owner/',
    views.owner_dashboard,
    name='owner_dashboard',
    ),

    path(
    'owner/bus/<int:bus_id>/',
    views.owner_bus_detail,
    name='owner_bus_detail',
    ),
    
    path(
    'owner/settlements/',
    views.owner_settlements,
    name='owner_settlements',
    ),

    path(
    'owner/settlements/<int:settlement_id>/',
    views.owner_settlement_detail,
    name='owner_settlement_detail',
    ),
    
    # ===== ADMINISTRACIÓN DE LIQUIDACIONES DE PROPIETARIOS =====

    path(
        'liquidaciones/',
        views.owner_settlement_admin_list,
        name='owner_settlement_admin_list',
    ),
    
    path(
        'liquidaciones/<int:settlement_id>/',
        views.owner_settlement_admin_detail,
        name='owner_settlement_admin_detail',
    ),
    
    path(
    'liquidaciones/generar/',
    views.owner_settlement_generate,
    name='owner_settlement_generate',
    ),

    path(
        'liquidaciones/<int:settlement_id>/estado/',
        views.owner_settlement_change_status,
        name='owner_settlement_change_status',
    ),
    
    path(
        'liquidaciones/<int:settlement_id>/anular/',
        views.owner_settlement_cancel,
        name='owner_settlement_cancel',
    ),

    # ===== CONVENIOS =====
    path('convenio/dashboard/', views.contract_dashboard, name='contract_dashboard'),
    path('convenio/empleados/<int:contract_id>/', views.contract_employees, name='contract_employees'),
    path('convenio/empleado/toggle/<int:employee_id>/', views.contract_employee_toggle, name='contract_employee_toggle'),
    path('api/contract-employees/', views.api_contract_employees, name='api_contract_employees'),
    path('pos/trip/<int:trip_id>/convenio/', views.pos_trip_convenio, name='pos_trip_convenio'),
    path('pos/checkout/convenio/<int:trip_id>/', views.pos_checkout_convenio, name='pos_checkout_convenio'),
    path('convenio/empleados/<int:contract_id>/', views.contract_employees, name='contract_employees'),
]
