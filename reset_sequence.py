# reset_sequence.py
import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'bus_tickets.settings')
django.setup()

from django.db import connection
from booking.models import Ticket

def reset_ticket_sequence():
    """Reinicia la secuencia de tickets al último valor + 1."""
    try:
        # Obtener el último número
        last_ticket = Ticket.objects.all().order_by('-number').first()
        if last_ticket:
            try:
                last_num = int(last_ticket.number[2:])
            except:
                last_num = 0
        else:
            last_num = 0
        
        print(f"📌 Último ticket: {last_ticket.number if last_ticket else 'Ninguno'}")
        print(f"📌 Último número: {last_num}")
        
        # Reiniciar la secuencia
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT setval('ticket_number_seq', {last_num}, true)")
            print(f"✅ Secuencia reiniciada a {last_num}")
        
        # Verificar
        with connection.cursor() as cursor:
            cursor.execute("SELECT nextval('ticket_number_seq')")
            next_id = cursor.fetchone()[0]
            print(f"✅ Próximo número: {next_id}")
            print(f"✅ Próximo ticket: T-{next_id:06d}")
        
        return True
    except Exception as e:
        print(f"❌ Error: {e}")
        return False

if __name__ == "__main__":
    reset_ticket_sequence()