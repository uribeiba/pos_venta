from django.db import migrations

def copy_data(apps, schema_editor):
    Discount = apps.get_model('booking', 'Discount')
    Coupon = apps.get_model('booking', 'Coupon')
    Promotion = apps.get_model('booking', 'Promotion')
    
    # Copiar descuentos (Discount)
    for d in Discount.objects.all():
        Promotion.objects.create(
            name=d.name,
            code=d.code or f"DISC-{d.id}",
            discount_type='percentage',
            discount_value=d.percentage,
            min_purchase_amount=0,  # Discount no tiene este campo, ponemos 0
            max_discount_amount=None,
            valid_from=d.valid_from,
            valid_to=d.valid_to,
            max_uses=d.max_uses,
            used_count=d.used_count,
            is_active=d.is_active,
        )
    
    # Copiar cupones (Coupon)
    for c in Coupon.objects.all():
        Promotion.objects.create(
            name=f"Cupon {c.code}",
            code=c.code,
            discount_type=c.discount_type,
            discount_value=c.discount_value,
            min_purchase_amount=c.min_purchase_amount,
            max_discount_amount=c.max_discount_amount,
            valid_from=c.valid_from.date() if c.valid_from else None,
            valid_to=c.valid_to.date() if c.valid_to else None,
            max_uses=c.max_uses,
            used_count=c.used_count,
            is_active=c.is_active,
        )

def reverse_copy(apps, schema_editor):
    # No se puede revertir fácilmente, pero podemos eliminar todos los Promotion
    Promotion = apps.get_model('booking', 'Promotion')
    Promotion.objects.all().delete()

class Migration(migrations.Migration):
   dependencies = [
    ('booking', '0032_add_promotion'),   # ← la migración que creó el modelo Promotion
    ]
operations = [
        migrations.RunPython(copy_data, reverse_copy),
    ]