from django.shortcuts import render
from booking.models import CompanyDomain

def custom_404(request, exception):
    # Obtenemos el dominio actual (ej: portena.online)
    host = request.get_host().split(":")[0].lower()
    company = None
    
    # Buscamos en la base de datos si ese dominio pertenece a una empresa
    domain_match = CompanyDomain.objects.filter(
        domain=host, 
        is_active=True
    ).select_related('company').first()
    
    if domain_match:
        company = domain_match.company

    return render(
        request,
        '404.html',
        {'company': company}, # Le pasamos la empresa a la plantilla
        status=404
    )