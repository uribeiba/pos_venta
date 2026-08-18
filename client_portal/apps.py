from django.apps import AppConfig


class ClientPortalConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'client_portal'

    def ready(self):
        """
        Carga señales y configuraciones al iniciar la app.
        """
        # import client_portal.signals  # Si hubiera señales
        pass