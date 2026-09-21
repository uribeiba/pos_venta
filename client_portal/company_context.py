from booking.models import CompanyDomain


def get_public_company(request):
    """
    Devuelve la empresa pública asociada al dominio actual.

    Ejemplos:
        portena.online      -> Buses La Porteña
        www.portena.online  -> Buses La Porteña

    Si el dominio no está registrado o está inactivo,
    devuelve None.
    """

    host = request.get_host()

    # Eliminar puerto en desarrollo:
    # 127.0.0.1:8000 -> 127.0.0.1
    host = host.split(":")[0]

    # Normalizar
    host = host.strip().lower().rstrip(".")

    company_domain = (
        CompanyDomain.objects
        .select_related("company")
        .filter(
            domain__iexact=host,
            is_active=True,
        )
        .first()
    )

    if not company_domain:
        return None

    return company_domain.company