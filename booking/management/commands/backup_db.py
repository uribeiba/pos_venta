from django.core.management.base import BaseCommand
from coordinator.views import create_backup
import logging

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = 'Crea un respaldo automático de la base de datos'

    def handle(self, *args, **options):
        self.stdout.write('Creando respaldo...')
        backup_file = create_backup()
        if backup_file:
            self.stdout.write(self.style.SUCCESS(f'Respaldo creado: {backup_file}'))
        else:
            self.stdout.write(self.style.ERROR('Error al crear el respaldo'))