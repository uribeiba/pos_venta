from django.urls import path
from . import views

app_name = 'client_portal'

urlpatterns = [
    # ===== PÁGINAS PRINCIPALES =====
    path('', views.home, name='home'),
    path('search/', views.search_trips, name='search_trips'),
    path('seatmap/<int:trip_id>/', views.seatmap, name='seatmap'),
    path('seatmap/partial/<int:trip_id>/', views.seatmap_partial, name='seatmap_partial'),
     # ===== API DE ASIENTOS (orden IMPORTANTE: primero renew, luego hold) =====
    path('api/hold/renew/<int:trip_id>/', views.renew_hold, name='renew_hold'),  # <-- renovar reserva
    path('api/hold/<int:trip_id>/', views.hold_seat, name='hold_seat'),          # <-- crear/liberar reserva

    # ===== API DE CLIENTES Y CUPONES =====
    path('api/save-customer/', views.save_customer_from_checkout, name='save_customer_from_checkout'),
    path('api/validate-coupon/', views.validate_coupon, name='validate_coupon'),

    # ===== PROCESO DE COMPRA =====
    path('checkout/<int:trip_id>/', views.checkout, name='checkout'),
    path('confirmation/<int:trip_id>/', views.confirmation, name='confirmation'),

    path(
        'payment-result/<str:order_code>/',
        views.payment_result,
        name='payment_result'
    ),

    path('terminos-condiciones/', views.terms, name='terms'),

    # ===== DASHBOARD PARA JEFES =====
    path('admin/dashboard/', views.admin_dashboard, name='admin_dashboard'),
    path('admin/dashboard/stats/', views.dashboard_stats, name='dashboard_stats'),
    path('admin/dashboard/export/pdf/', views.export_dashboard_pdf, name='export_dashboard_pdf'),
    path('admin/dashboard/export/excel/', views.export_dashboard_excel, name='export_dashboard_excel'),
    path('admin/dashboard/notifications/', views.check_new_sales, name='check_new_sales'),

    # ===== MIS RESERVAS (USUARIO AUTENTICADO) =====
    path('my-reservations/', views.my_reservations, name='my_reservations'),
    path('reservation/<int:ticket_id>/', views.reservation_detail, name='reservation_detail'),
    path('reservation/<int:ticket_id>/cancel/', views.cancel_reservation, name='cancel_reservation'),
    path('reservation/<int:ticket_id>/download/', views.download_ticket_pdf, name='download_ticket_pdf'),

    # ===== PANTALLA DEL CLIENTE (MODO EXHIBICIÓN) =====
    path('client-display/<int:trip_id>/', views.client_display, name='client_display'),
    path('client-display/', views.client_display, name='client_display_no_trip'),
    path('client-display/launch/<int:trip_id>/', views.client_display_launcher, name='client_display_launcher'),
    path('client-sync/', views.client_sync, name='client_sync'),

   # ===== WEBPAY PLUS =====

    path(
        'webpay/start/<str:order_code>/',
        views.webpay_start,
        name='webpay_start'
    ),

    path(
        'webpay/retry/<str:order_code>/',
        views.webpay_retry,
        name='webpay_retry'
    ),

    path(
        'webpay/return/',
        views.webpay_return,
        name='webpay_return'
    ),

    path(
    'purchase/<str:order_code>/download/',
    views.download_purchase_pdf,
    name='download_purchase_pdf'
    ),


    path(
    'mercadopago/start/<str:order_code>/',
    views.mercadopago_start,
    name='mercadopago_start'
        ),

    path(
        'mercadopago/success/',
        views.mercadopago_success,
        name='mercadopago_success'
        ),

    path(
        'mercadopago/failure/',
        views.mercadopago_failure,
        name='mercadopago_failure'
        ),

    path(
        'mercadopago/pending/',
        views.mercadopago_pending,
        name='mercadopago_pending'
        ),

    path(
        'mercadopago/webhook/',
        views.mercadopago_webhook,
        name='mercadopago_webhook'
         ),
]
