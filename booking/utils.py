def validate_chilean_rut(rut: str) -> bool:
    """Valida RUT chileno (formato: 12345678-9 o 12.345.678-9). Retorna True si es válido."""
    rut = rut.upper().replace('.', '').replace('-', '').strip()
    if len(rut) < 2:
        return False
    body = rut[:-1]
    dv_expected = rut[-1]
    if not body.isdigit():
        return False
    # Calcular dígito verificador
    suma = 0
    mul = 2
    for digit in reversed(body):
        suma += int(digit) * mul
        mul = mul + 1 if mul < 7 else 2
    dv_calc = 11 - (suma % 11)
    if dv_calc == 11:
        dv_calc = '0'
    elif dv_calc == 10:
        dv_calc = 'K'
    else:
        dv_calc = str(dv_calc)
    return dv_calc == dv_expected