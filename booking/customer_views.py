# booking/customer_views.py
from django.http import JsonResponse
from django.db.models import Q
from django.views.decorators.csrf import csrf_exempt
from .models import Customer

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
def create_customer(request):
    """API para crear nuevo cliente - CAMPOS NO OBLIGATORIOS"""
    print("📝 Recibiendo solicitud para crear cliente...")
    
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Método no permitido'})

    try:
        # Obtener datos del POST
        national_id = request.POST.get('national_id', '').strip()
        full_name = request.POST.get('full_name', '').strip()
        phone = request.POST.get('phone', '').strip()
        email = request.POST.get('email', '').strip()
        
        print(f"📋 Datos recibidos - RUT: {national_id}, Nombre: {full_name}")
        
        if not national_id:
            return JsonResponse({'success': False, 'error': 'RUT es obligatorio'})

        # Si no viene nombre, usar valor por defecto
        if not full_name:
            full_name = "Cliente"

        # Verificar si ya existe
        existing_customer = Customer.objects.filter(national_id=national_id).first()
        if existing_customer:
            print(f"ℹ️ Cliente ya existe: {existing_customer}")
            return JsonResponse({
                'success': True,
                'created': False,
                'message': 'Cliente ya existe',
                'customer': {
                    'id': existing_customer.id,
                    'national_id': existing_customer.national_id,
                    'full_name': existing_customer.full_name,
                    'phone': existing_customer.phone or '',
                    'email': existing_customer.email or ''
                }
            })

        # Crear nuevo cliente - TODOS LOS CAMPOS OPCIONALES excepto RUT
        customer = Customer.objects.create(
            national_id=national_id,
            full_name=full_name,
            phone=phone,  # Puede estar vacío
            email=email   # Puede estar vacío
        )
        
        print(f"✅ Cliente creado exitosamente: {customer}")
        
        return JsonResponse({
            'success': True,
            'created': True,
            'message': 'Cliente creado exitosamente',
            'customer': {
                'id': customer.id,
                'national_id': customer.national_id,
                'full_name': customer.full_name,
                'phone': customer.phone or '',
                'email': customer.email or ''
            }
        })
        
    except Exception as e:
        print(f"❌ Error creando cliente: {e}")
        return JsonResponse({'success': False, 'error': str(e)})