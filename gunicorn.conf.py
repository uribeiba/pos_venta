# ============================================================================
# GUNICORN CONFIGURATION
# ============================================================================
import multiprocessing
import os

# Bind
bind = "0.0.0.0:8000"

# Worker configuration
workers = multiprocessing.cpu_count() * 2 + 1
worker_class = "sync"
worker_connections = 1000
max_requests = 1000
max_requests_jitter = 100

# Timeouts
timeout = 120
graceful_timeout = 30
keepalive = 5

# Logging
accesslog = "/var/log/gunicorn/access.log"
errorlog = "/var/log/gunicorn/error.log"
loglevel = "info"

# Security
user = "www-data"
group = "www-data"

# Process naming
proc_name = "bus_tickets"

# Environment
raw_env = [
    "DJANGO_SETTINGS_MODULE=bus_tickets.settings",
]

# Preload for better performance
preload_app = True

# Development settings
if os.getenv("DJANGO_DEBUG", "1") == "1":
    reload = True
    loglevel = "debug"