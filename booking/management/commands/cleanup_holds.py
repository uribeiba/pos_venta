from django.core.management.base import BaseCommand
from django.utils import timezone
from booking.models import SeatHold

class Command(BaseCommand):
    help = 'Elimina bloqueos de asientos expirados'

    def handle(self, *args, **options):
        expired = SeatHold.objects.filter(expires_at__lt=timezone.now(), active=True)
        count = expired.update(active=False)
        self.stdout.write(self.style.SUCCESS(f'Se liberaron {count} bloqueos expirados.'))