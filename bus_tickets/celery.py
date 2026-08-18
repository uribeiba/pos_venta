# bus_tickets/celery.py
import os
from celery import Celery

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'bus_tickets.settings')

app = Celery('bus_tickets')
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()