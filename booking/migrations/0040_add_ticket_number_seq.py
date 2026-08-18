# booking/migrations/XXXX_add_ticket_number_seq.py
from django.db import migrations

class Migration(migrations.Migration):

    dependencies = [
        ('booking', '0039_auditlog'),  # Reemplaza con tu última migración
    ]

    operations = [
        migrations.RunSQL(
            sql="""
                CREATE SEQUENCE IF NOT EXISTS ticket_number_seq
                START 1
                INCREMENT 1
                NO MINVALUE
                NO MAXVALUE
                CACHE 1;
            """,
            reverse_sql="DROP SEQUENCE IF EXISTS ticket_number_seq;"
        ),
    ]