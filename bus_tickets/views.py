from django.shortcuts import render
from booking.models import CompanyDomain

def custom_404(request, exception):
    host = request.get_host().split(":")[0].lower()
    company = None
    
    domain_match = CompanyDomain.objects.filter(
        domain=host, 
        is_active=True
    ).select_related('company').first()
    
    if domain_match:
        company = domain_match.company

    return render(
        request,
        'error_404_custom.html', # <--- AQUÍ ESTÁ EL CAMBIO DE NOMBRE
        {'company': company},
        status=404
    )