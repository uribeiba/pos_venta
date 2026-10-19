# booking/utils/__init__.py

def validate_chilean_rut(rut):
    """Valida un RUT chileno."""
    if not rut:
        return False
    rut = str(rut).replace(".", "").replace("-", "").strip().upper()
    if len(rut) < 2:
        return False
    body = rut[:-1]
    dv = rut[-1]
    if not body.isdigit():
        return False
    
    suma = 0
    multiplicador = 2
    for c in reversed(body):
        suma += int(c) * multiplicador
        multiplicador = multiplicador + 1 if multiplicador < 7 else 2
    
    resto = suma % 11
    dv_esperado = 11 - resto
    
    if dv_esperado == 11:
        dv_esperado = "0"
    elif dv_esperado == 10:
        dv_esperado = "K"
    else:
        dv_esperado = str(dv_esperado)
        
    return dv == dv_esperado