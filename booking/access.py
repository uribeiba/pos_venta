from django.core.exceptions import PermissionDenied


# ============================================================
# FASE 2.18.3-A2.3
# CONTROL CENTRALIZADO DE ALCANCE DE DATOS
# ============================================================


def get_user_scope(user):
    """
    Devuelve el alcance operativo del usuario.

    Posibles valores:
        superuser
        company
        owner
        terminal
        none
    """

    if not user or not user.is_authenticated:
        return {
            "type": "none",
            "company": None,
            "fleet_owner": None,
            "terminal": None,
        }

    if user.is_superuser:
        return {
            "type": "superuser",
            "company": None,
            "fleet_owner": None,
            "terminal": None,
        }

    profile = getattr(user, "profile", None)

    if not profile:
        return {
            "type": "none",
            "company": None,
            "fleet_owner": None,
            "terminal": None,
        }

    role = profile.role

    # --------------------------------------------------------
    # PROPIETARIO / SOCIO
    # --------------------------------------------------------
    if role == "owner":
        if not profile.company_id or not profile.fleet_owner_id:
            return {
                "type": "none",
                "company": profile.company,
                "fleet_owner": profile.fleet_owner,
                "terminal": profile.terminal,
            }

        return {
            "type": "owner",
            "company": profile.company,
            "fleet_owner": profile.fleet_owner,
            "terminal": profile.terminal,
        }

    # --------------------------------------------------------
    # ROLES CON ALCANCE DE EMPRESA COMPLETA
    # --------------------------------------------------------
    if role in {
        "admin",
        "supervisor",
        "coordinator",
        "executive",
        "secretary",
    }:
        return {
            "type": "company",
            "company": profile.company,
            "fleet_owner": None,
            "terminal": profile.terminal,
        }

    # --------------------------------------------------------
    # POS / VENTA
    # --------------------------------------------------------
    if role in {
        "vendedor",
        "cajero",
        "convenio",
    }:
        return {
            "type": "terminal",
            "company": profile.company,
            "fleet_owner": None,
            "terminal": profile.terminal,
        }

    return {
        "type": "none",
        "company": profile.company,
        "fleet_owner": profile.fleet_owner,
        "terminal": profile.terminal,
    }


# ============================================================
# BUS
# ============================================================

def buses_for_user(user, queryset):
    scope = get_user_scope(user)

    if scope["type"] == "superuser":
        return queryset

    if scope["type"] == "owner":
        if not scope["company"] or not scope["fleet_owner"]:
            return queryset.none()

        return queryset.filter(
            company=scope["company"],
            owner=scope["fleet_owner"],
        )

    if scope["type"] in {"company", "terminal"}:
        if not scope["company"]:
            return queryset.none()

        return queryset.filter(
            company=scope["company"],
        )

    return queryset.none()


# ============================================================
# ROUTE
# ============================================================

def routes_for_user(user, queryset):
    """
    Aplica el alcance multiempresa sobre Route.

    REGLAS:
    - Superusuario:
      puede ver todas las rutas.

    - Administrador / supervisor / coordinador / ejecutivo /
      secretaria:
      puede ver las rutas de su empresa.

    - Propietario / socio:
      puede ver las rutas de su empresa.
      La restricción por propietario se aplica sobre buses,
      viajes, ventas y tickets, no sobre Route.

    - Vendedor / cajero / convenio:
      puede ver las rutas de su empresa.
    """

    scope = get_user_scope(user)

    if scope["type"] == "superuser":
        return queryset

    if scope["type"] in {"company", "owner", "terminal"}:
        if not scope["company"]:
            return queryset.none()

        return queryset.filter(
            company=scope["company"],
        )

    return queryset.none()


# ============================================================
# DRIVER
# ============================================================

def drivers_for_user(user, queryset):
    """
    Aplica el alcance multiempresa sobre Driver.

    REGLAS:
    - Superusuario:
      puede ver todos los choferes.

    - Empresa:
      puede ver únicamente los choferes de su empresa.

    - Propietario / socio:
      puede ver los choferes de su empresa.
      No se filtra por FleetOwner, porque el chofer pertenece
      a la empresa y puede operar distintos buses autorizados.

    - Terminal:
      puede ver los choferes de su empresa.
      Si más adelante se asignan choferes por terminal,
      esta función podrá endurecer ese alcance.
    """

    scope = get_user_scope(user)

    if scope["type"] == "superuser":
        return queryset

    if scope["type"] in {"company", "owner", "terminal"}:
        if not scope["company"]:
            return queryset.none()

        return queryset.filter(
            company=scope["company"],
        )

    return queryset.none()


# ============================================================
# ASSISTANT
# ============================================================

def assistants_for_user(user, queryset):
    """
    Aplica el alcance multiempresa sobre Assistant.

    REGLAS:
    - Superusuario:
      puede ver todos los auxiliares.

    - Empresa:
      puede ver únicamente los auxiliares de su empresa.

    - Propietario / socio:
      puede ver los auxiliares de su empresa.

    - Terminal:
      puede ver los auxiliares de su empresa.
    """

    scope = get_user_scope(user)

    if scope["type"] == "superuser":
        return queryset

    if scope["type"] in {"company", "owner", "terminal"}:
        if not scope["company"]:
            return queryset.none()

        return queryset.filter(
            company=scope["company"],
        )

    return queryset.none()


# ============================================================
# TRIP
# ============================================================

def trips_for_user(user, queryset):
    scope = get_user_scope(user)

    if scope["type"] == "superuser":
        return queryset

    if scope["type"] == "owner":
        if not scope["company"] or not scope["fleet_owner"]:
            return queryset.none()

        return queryset.filter(
            bus__company=scope["company"],
            bus__owner=scope["fleet_owner"],
        )

    if scope["type"] in {"company", "terminal"}:
        if not scope["company"]:
            return queryset.none()

        return queryset.filter(
            bus__company=scope["company"],
        )

    return queryset.none()


# ============================================================
# TICKET
# ============================================================

def tickets_for_user(user, queryset):
    """
    FASE 2.18.3-A2.3.3

    Aplica el alcance económico/histórico sobre Ticket.

    REGLA IMPORTANTE:
    - Para propietario/socio se utiliza Ticket.revenue_owner.
    - Para empresa se utiliza Ticket.revenue_bus como referencia económica.
    - Los tickets históricos anteriores al congelamiento pueden tener
      revenue_bus/revenue_owner = NULL.

    No se utiliza trip__bus__owner para determinar la propiedad económica
    de un ticket, porque el propietario operacional del bus puede cambiar
    después de realizada la venta.
    """

    scope = get_user_scope(user)

    if scope["type"] == "superuser":
        return queryset

    # ============================================================
    # PROPIETARIO / SOCIO
    # ============================================================
    if scope["type"] == "owner":
        if not scope["company"] or not scope["fleet_owner"]:
            return queryset.none()

        return queryset.filter(
            revenue_owner=scope["fleet_owner"],
            revenue_bus__company=scope["company"],
        )

    # ============================================================
    # EMPRESA / TERMINAL
    # ============================================================
    if scope["type"] in {"company", "terminal"}:
        if not scope["company"]:
            return queryset.none()

        return queryset.filter(
            revenue_bus__company=scope["company"],
        )

    return queryset.none()


# ============================================================
# SALE
# ============================================================

def sales_for_user(user, queryset):
    scope = get_user_scope(user)

    if scope["type"] == "superuser":
        return queryset

    if scope["type"] == "owner":
        if not scope["company"] or not scope["fleet_owner"]:
            return queryset.none()

        return queryset.filter(
            trip__bus__company=scope["company"],
            trip__bus__owner=scope["fleet_owner"],
        )

    if scope["type"] in {"company", "terminal"}:
        if not scope["company"]:
            return queryset.none()

        return queryset.filter(
            trip__bus__company=scope["company"],
        )

    return queryset.none()


# ============================================================
# BOOKING ORDER
# ============================================================

def booking_orders_for_user(user, queryset):
    scope = get_user_scope(user)

    if scope["type"] == "superuser":
        return queryset

    if scope["type"] == "owner":
        if not scope["company"] or not scope["fleet_owner"]:
            return queryset.none()

        return queryset.filter(
            trip__bus__company=scope["company"],
            trip__bus__owner=scope["fleet_owner"],
        )

    if scope["type"] in {"company", "terminal"}:
        if not scope["company"]:
            return queryset.none()

        return queryset.filter(
            trip__bus__company=scope["company"],
        )

    return queryset.none()


# ============================================================
# PROTECCIÓN DE OBJETOS INDIVIDUALES
# ============================================================

def assert_bus_access(user, bus):
    """
    Impide acceder a un bus por ID manipulando la URL.
    """

    allowed = buses_for_user(
        user,
        bus.__class__.objects.filter(
            pk=bus.pk
        ),
    ).exists()

    if not allowed:
        raise PermissionDenied(
            "No tiene autorización para acceder a este bus."
        )

    return True


def assert_route_access(user, route):
    """
    Impide acceder a una ruta de otra empresa manipulando la URL.
    """

    allowed = routes_for_user(
        user,
        route.__class__.objects.filter(
            pk=route.pk
        ),
    ).exists()

    if not allowed:
        raise PermissionDenied(
            "No tiene autorización para acceder a esta ruta."
        )

    return True


def assert_driver_access(user, driver):
    """
    Impide acceder a un chofer de otra empresa manipulando la URL.
    """

    allowed = drivers_for_user(
        user,
        driver.__class__.objects.filter(
            pk=driver.pk
        ),
    ).exists()

    if not allowed:
        raise PermissionDenied(
            "No tiene autorización para acceder a este chofer."
        )

    return True


def assert_assistant_access(user, assistant):
    """
    Impide acceder a un auxiliar de otra empresa manipulando la URL.
    """

    allowed = assistants_for_user(
        user,
        assistant.__class__.objects.filter(
            pk=assistant.pk
        ),
    ).exists()

    if not allowed:
        raise PermissionDenied(
            "No tiene autorización para acceder a este auxiliar."
        )

    return True


def assert_trip_access(user, trip):
    """
    Impide acceder a un viaje por ID manipulando la URL.
    """

    allowed = trips_for_user(
        user,
        trip.__class__.objects.filter(
            pk=trip.pk
        ),
    ).exists()

    if not allowed:
        raise PermissionDenied(
            "No tiene autorización para acceder a este viaje."
        )

    return True