# booking/customer_views.py
from django.http import JsonResponse
from django.db.models import Q
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from .models import Customer
from booking.utils import validate_chilean_rut, validate_email, validate_phone
import re
import logging

logger = logging.getLogger(__name__)

# ✅ Vista de prueba simple
def test_view(request):
    return JsonResponse({
        "message": "✅ Las URLs de customer están funcionando!", 
        "status": "ok",
        "endpoints": {
            "search": "/booking/customer/search/",
            "create": "/booking/customer/create/"
        }
    })

# ✅ Buscar cliente
def search_customer(request):
    """API para buscar cliente por RUT o nombre"""
    try:
        query = request.GET.get('q', '').strip()
        print(f"🔍 Búsqueda de cliente: {query}")
        
        if not query or len(query) < 2:
            return JsonResponse({'customers': []})

        # Buscar por RUT o nombre
        customers = Customer.objects.filter(
            Q(national_id__icontains=query) | 
            Q(full_name__icontains=query)
        )[:10]

        results = []
        for customer in customers:
            results.append({
                'id': customer.id,
                'national_id': customer.national_id,
                'full_name': customer.full_name,
                'phone': customer.phone or '',
                'email': customer.email or ''
            })

        print(f"✅ Encontrados {len(results)} clientes")
        return JsonResponse({'customers': results})
    
    except Exception as e:
        print(f"❌ Error en search_customer: {e}")
        return JsonResponse({'customers': [], 'error': str(e)})

# ✅ Crear cliente - VERSIÓN SIMPLIFICADA
@csrf_exempt
@require_POST
def create_customer(request):
    """
    API para crear nuevo cliente con validación completa.
    """
    print("📝 Recibiendo solicitud para crear cliente...")
    
    try:
        # Obtener y limpiar datos
        national_id = request.POST.get('national_id', '').strip().upper()
        full_name = request.POST.get('full_name', '').strip()
        phone = request.POST.get('phone', '').strip()
        email = request.POST.get('email', '').strip()
        
        print(f"📋 Datos recibidos - RUT: {national_id}, Nombre: {full_name}")
        
        # ✅ VALIDACIÓN DE RUT
        if not national_id:
            return JsonResponse({
                'success': False,
                'error': 'El RUT es obligatorio.',
                'field': 'national_id'
            }, status=400)
        
        if not validate_chilean_rut(national_id):
            return JsonResponse({
                'success': False,
                'error': 'RUT inválido. Formato: 12345678-9 o 123456789.',
                'field': 'national_id'
            }, status=400)
        
        # ✅ VALIDACIÓN DE NOMBRE
        if not full_name:
            full_name = "Cliente"
        elif len(full_name) < 3:
            return JsonResponse({
                'success': False,
                'error': 'El nombre debe tener al menos 3 caracteres.',
                'field': 'full_name'
            }, status=400)
        elif len(full_name) > 140:
            return JsonResponse({
                'success': False,
                'error': 'El nombre no puede exceder los 140 caracteres.',
                'field': 'full_name'
            }, status=400)
        
        # ✅ VALIDACIÓN DE EMAIL (opcional)
        if email and not validate_email(email):
            return JsonResponse({
                'success': False,
                'error': 'Formato de email inválido.',
                'field': 'email'
            }, status=400)
        
        # ✅ VALIDACIÓN DE TELÉFONO (opcional)
        if phone and not validate_phone(phone):
            return JsonResponse({
                'success': False,
                'error': 'Formato de teléfono inválido.',
                'field': 'phone'
            }, status=400)
        
        # ✅ VERIFICAR SI YA EXISTE
        existing_customer = Customer.objects.filter(national_id=national_id).first()
        if existing_customer:
            logger.info(f"ℹ️ Cliente ya existe: {existing_customer}")
            return JsonResponse({
                'success': True,
                'created': False,
                'message': 'Cliente ya existe.',
                'customer': {
                    'id': existing_customer.id,
                    'national_id': existing_customer.national_id,
                    'full_name': existing_customer.full_name,
                    'phone': existing_customer.phone or '',
                    'email': existing_customer.email or ''
                }
            })

        # ✅ CREAR CLIENTE
        customer = Customer.objects.create(
            national_id=national_id,
            full_name=full_name,
            phone=phone or '',
            email=email or ''
        )
        
        logger.info(f"✅ Cliente creado exitosamente: {customer}")
        
        return JsonResponse({
            'success': True,
            'created': True,
            'message': 'Cliente creado exitosamente.',
            'customer': {
                'id': customer.id,
                'national_id': customer.national_id,
                'full_name': customer.full_name,
                'phone': customer.phone or '',
                'email': customer.email or ''
            }
        })
        
    except Exception as e:
        logger.error(f"❌ Error creando cliente: {e}", exc_info=True)
        return JsonResponse({
            'success': False,
            'error': 'Error interno del servidor.'
        }, status=500)
