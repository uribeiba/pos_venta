# ============================================================================
# ENVIRONMENT VARIABLES
# ============================================================================
import os
import sys
import warnings
from dotenv import load_dotenv
from pathlib import Path

# Determinar el directorio base
BASE_DIR = Path(__file__).resolve().parent.parent

# Cargar variables de entorno desde archivo .env con ruta absoluta
ENV_FILE = BASE_DIR / '.env'
if ENV_FILE.exists():
    load_dotenv(ENV_FILE, override=False)
else:
    env_fallback = Path('/etc/secrets/.env')
    if env_fallback.exists():
        load_dotenv(env_fallback, override=False)

# Crear directorios necesarios antes de cualquier otra operación
LOG_DIR = BASE_DIR / "logs"
MEDIA_DIR = BASE_DIR / "media"
STATIC_DIR = BASE_DIR / "staticfiles"

for directory in [LOG_DIR, MEDIA_DIR, STATIC_DIR]:
    if not directory.exists():
        directory.mkdir(parents=True, exist_ok=True)

# ============================================================================
# SECURITY & ENVIRONMENT
# ============================================================================
DEBUG = os.getenv("DJANGO_DEBUG", "1") == "1"

def get_secret_key() -> str:
    """Obtiene la SECRET_KEY de las variables de entorno."""
    secret_key = os.getenv("DJANGO_SECRET_KEY")
    if not secret_key:
        if DEBUG:
            return "dev-secret-key-change-me-in-production"
        raise ValueError(
            "DJANGO_SECRET_KEY no está configurada en el entorno. "
            "Esta variable es OBLIGATORIA en producción."
        )
    
    default_keys = [
        "dev-secret-key-change-me-in-production",
        "django-insecure-",
        "secret-key-for-development"
    ]
    if any(secret_key.startswith(key) for key in default_keys):
        raise ValueError(
            f"DJANGO_SECRET_KEY contiene una clave por defecto insegura: {secret_key[:20]}..."
        )
    
    return secret_key

SECRET_KEY = get_secret_key()
ALLOWED_HOSTS = os.getenv("DJANGO_ALLOWED_HOSTS", "127.0.0.1,localhost").split(",")

# ============================================================================
# APPLICATION DEFINITION
# ============================================================================
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "rest_framework",
    "corsheaders",
    "booking.apps.BookingConfig",
    "coordinator",
    "client_portal",
    "django_extensions",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "bus_tickets.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "bus_tickets.wsgi.application"
ASGI_APPLICATION = "bus_tickets.asgi.application"

# ============================================================================
# DATABASE
# ============================================================================
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.getenv("DB_NAME", "xbus"),
        "USER": os.getenv("DB_USER", "cejer_user"),
        "PASSWORD": os.getenv("DB_PASSWORD", ""),
        "HOST": os.getenv("DB_HOST", "127.0.0.1"),
        "PORT": os.getenv("DB_PORT", "5432"),
        "CONN_MAX_AGE": int(os.getenv("DB_CONN_MAX_AGE", "60")),
        "OPTIONS": {
            "connect_timeout": 10,
            "keepalives": 1,
            "keepalives_idle": 60,
            "keepalives_interval": 10,
            "keepalives_count": 3,
        }
    }
}

if not DEBUG:
    if not DATABASES["default"]["PASSWORD"]:
        raise ValueError("DB_PASSWORD es OBLIGATORIA en producción")

# ============================================================================
# PASSWORD VALIDATION
# ============================================================================
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 8}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# ============================================================================
# INTERNATIONALIZATION
# ============================================================================
LANGUAGE_CODE = "es-cl"
TIME_ZONE = "America/Santiago"
USE_I18N = True
USE_TZ = True

# ============================================================================
# STATIC & MEDIA FILES
# ============================================================================
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ============================================================================
# CORS CONFIGURATION
# ============================================================================
def get_cors_allowed_origins() -> list:
    cors_origins = os.getenv("CORS_ALLOWED_ORIGINS")
    
    if DEBUG:
        return ["http://127.0.0.1:7000", "http://localhost:7000"]
    
    if not cors_origins:
        raise ValueError("CORS_ALLOWED_ORIGINS no está configurada en producción.")
    
    origins = [origin.strip() for origin in cors_origins.split(",") if origin.strip()]
    
    for origin in origins:
        if not origin.startswith("https://") and not origin.startswith("http://localhost"):
            raise ValueError(f"En producción, todos los orígenes CORS deben usar HTTPS. Origen inválido: {origin}")
    
    return origins

CORS_ALLOWED_ORIGINS = get_cors_allowed_origins()
CORS_ALLOW_CREDENTIALS = True
CORS_EXPOSE_HEADERS = ['Content-Type', 'X-CSRFToken']
CORS_ALLOW_HEADERS = [
    'accept', 'accept-encoding', 'authorization', 'content-type',
    'dnt', 'origin', 'user-agent', 'x-csrftoken', 'x-requested-with',
]

# ============================================================================
# CSRF TRUSTED ORIGINS
# ============================================================================
def get_csrf_trusted_origins() -> list:
    """Obtiene los orígenes confiables para CSRF. Django 4.0+ requiere esquema."""
    if DEBUG:
        return [
        "http://127.0.0.1:8000",
        "http://localhost:8000",
        "http://192.168.1.8:8000",
    ]
    
    csrf_origins = os.getenv("CSRF_TRUSTED_ORIGINS")
    if not csrf_origins:
        raise ValueError("CSRF_TRUSTED_ORIGINS no está configurada en producción.")
    
    origins = [origin.strip() for origin in csrf_origins.split(",") if origin.strip()]
    
    for origin in origins:
        if not origin.startswith(("http://", "https://")):
            raise ValueError(
                f"CSRF_TRUSTED_ORIGINS debe incluir el esquema (http:// o https://). "
                f"Origen inválido: {origin}"
            )
    
    return origins

CSRF_TRUSTED_ORIGINS = get_csrf_trusted_origins()

# ============================================================================
# REST FRAMEWORK
# ============================================================================
REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ],
    "DEFAULT_PARSER_CLASSES": [
        "rest_framework.parsers.JSONParser",
        "rest_framework.parsers.FormParser",
    ],
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework.authentication.SessionAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    'DEFAULT_THROTTLE_CLASSES': [
        'rest_framework.throttling.AnonRateThrottle',
        'rest_framework.throttling.UserRateThrottle',
    ],
    'DEFAULT_THROTTLE_RATES': {
        'anon': '100/day',
        'user': '1000/day',
        'burst': '60/min',
    },
}

# ============================================================================
# SECURITY HEADERS & HTTPS
# ============================================================================
SECURE_SSL_REDIRECT = os.getenv('SECURE_SSL_REDIRECT', 'False') == 'True'
SESSION_COOKIE_SECURE = os.getenv('SESSION_COOKIE_SECURE', 'False') == 'True'
CSRF_COOKIE_SECURE = os.getenv('CSRF_COOKIE_SECURE', 'False') == 'True'
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https') if os.getenv('SECURE_PROXY_SSL_HEADER', 'False') == 'True' else None
SECURE_HSTS_SECONDS = int(os.getenv('SECURE_HSTS_SECONDS', '0'))
SECURE_HSTS_INCLUDE_SUBDOMAINS = os.getenv('SECURE_HSTS_INCLUDE_SUBDOMAINS', 'False') == 'True'
SECURE_HSTS_PRELOAD = os.getenv('SECURE_HSTS_PRELOAD', 'False') == 'True'
SECURE_BROWSER_XSS_FILTER = os.getenv('SECURE_BROWSER_XSS_FILTER', 'True') == 'True'
SECURE_CONTENT_TYPE_NOSNIFF = os.getenv('SECURE_CONTENT_TYPE_NOSNIFF', 'True') == 'True'
SECURE_REFERRER_POLICY = os.getenv('SECURE_REFERRER_POLICY', 'strict-origin-when-cross-origin')

# ============================================================================
# CSRF & SESSION - CONFIGURACIÓN PARA DESARROLLO (NUEVO)
# ============================================================================

# ✅ PERMITIR QUE JAVASCRIPT LEA LA COOKIE CSRF
CSRF_COOKIE_HTTPONLY = False

# ✅ USAR COOKIES PARA CSRF (no sesiones)
CSRF_USE_SESSIONS = False

# ✅ CONFIGURACIÓN PARA DESARROLLO LOCAL
if DEBUG:
    CSRF_COOKIE_SECURE = False
    SESSION_COOKIE_SECURE = False
    CSRF_COOKIE_SAMESITE = 'Lax'
    SESSION_COOKIE_SAMESITE = 'Lax'

# ============================================================================
# SESSION CONFIGURATION (adicional)
# ============================================================================
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_AGE = 86400  # 24 horas
SESSION_SAVE_EVERY_REQUEST = True
SESSION_EXPIRE_AT_BROWSER_CLOSE = False

X_FRAME_OPTIONS = os.getenv('X_FRAME_OPTIONS', 'DENY')

# ============================================================================
# VALIDACIÓN DE SEGURIDAD
# ============================================================================
def validate_security_settings():
    if not DEBUG:
        security_checks = [
            (SECURE_SSL_REDIRECT, "SECURE_SSL_REDIRECT debe ser True en producción"),
            (SESSION_COOKIE_SECURE, "SESSION_COOKIE_SECURE debe ser True en producción"),
            (CSRF_COOKIE_SECURE, "CSRF_COOKIE_SECURE debe ser True en producción"),
            (SECURE_HSTS_SECONDS >= 31536000, "SECURE_HSTS_SECONDS debe ser 31536000 en producción"),
            (SECURE_BROWSER_XSS_FILTER, "SECURE_BROWSER_XSS_FILTER debe ser True en producción"),
            (SECURE_CONTENT_TYPE_NOSNIFF, "SECURE_CONTENT_TYPE_NOSNIFF debe ser True en producción"),
            (X_FRAME_OPTIONS in ['DENY', 'SAMEORIGIN'], f"X_FRAME_OPTIONS debe ser DENY o SAMEORIGIN, actual: {X_FRAME_OPTIONS}"),
        ]
        
        missing_security = []
        for check, message in security_checks:
            if not check:
                missing_security.append(message)
        
        if missing_security:
            for message in missing_security:
                warnings.warn(f"⚠️ {message}. Ajusta tus variables de entorno.", RuntimeWarning)

validate_security_settings()

LOGIN_URL = "login"

# ============================================================================
# EMAIL CONFIGURATION
# ============================================================================
EMAIL_ATTACHMENT_MAX_SIZE = int(os.getenv("EMAIL_ATTACHMENT_MAX_SIZE", "5242880"))

if EMAIL_ATTACHMENT_MAX_SIZE < 1024 * 1024:
    raise ValueError(f"EMAIL_ATTACHMENT_MAX_SIZE debe ser al menos 1MB. Valor actual: {EMAIL_ATTACHMENT_MAX_SIZE}")

if EMAIL_ATTACHMENT_MAX_SIZE > 25 * 1024 * 1024:
    warnings.warn(f"EMAIL_ATTACHMENT_MAX_SIZE es muy grande ({EMAIL_ATTACHMENT_MAX_SIZE} bytes). Gmail limita a 25MB.", RuntimeWarning)

EMAIL_BACKEND = os.getenv("EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend")
EMAIL_HOST = os.getenv("EMAIL_HOST", "smtp.gmail.com")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", "587"))
EMAIL_USE_TLS = os.getenv("EMAIL_USE_TLS", "True") == "True"
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "")
DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", "Cejer <noreply@cejer.cl>")

# ============================================================================
# LOGGING
# ============================================================================
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{levelname} {asctime} {module} {process:d} {thread:d} {message}",
            "style": "{",
        },
        "simple": {
            "format": "{levelname} {asctime} {module} {message}",
            "style": "{",
        },
        "detailed": {
            "format": "[{asctime}] {levelname} {name}: {message}",
            "style": "{",
            "datefmt": "%Y-%m-%d %H:%M:%S",
        },
    },
    "filters": {
        "require_debug_false": {"()": "django.utils.log.RequireDebugFalse"},
        "require_debug_true": {"()": "django.utils.log.RequireDebugTrue"},
    },
    "handlers": {
        "console": {
            "level": "INFO",
            "class": "logging.StreamHandler",
            "formatter": "simple",
            "filters": ["require_debug_true"],
        },
        "console_prod": {
            "level": "WARNING",
            "class": "logging.StreamHandler",
            "formatter": "detailed",
            "filters": ["require_debug_false"],
        },
        "file": {
            "level": "ERROR",
            "class": "logging.handlers.RotatingFileHandler",
            "filename": LOG_DIR / "django.log",
            "maxBytes": 1024 * 1024 * 10,
            "backupCount": 5,
            "formatter": "verbose",
        },
        "file_security": {
            "level": "WARNING",
            "class": "logging.handlers.RotatingFileHandler",
            "filename": LOG_DIR / "security.log",
            "maxBytes": 1024 * 1024 * 10,
            "backupCount": 10,
            "formatter": "verbose",
        },
        "file_audit": {
            "level": "INFO",
            "class": "logging.handlers.RotatingFileHandler",
            "filename": LOG_DIR / "audit.log",
            "maxBytes": 1024 * 1024 * 50,
            "backupCount": 30,
            "formatter": "detailed",
        },
    },
    "root": {
        "handlers": ["console", "console_prod", "file"],
        "level": "INFO",
    },
    "loggers": {
        "django": {"handlers": ["file"], "level": "ERROR", "propagate": False},
        "django.security": {"handlers": ["file", "file_security", "console", "console_prod"], "level": "WARNING", "propagate": False},
        "django.request": {"handlers": ["file"], "level": "ERROR", "propagate": False},
        "booking": {"handlers": ["file", "file_audit"], "level": "INFO", "propagate": False},
        "coordinator": {"handlers": ["file", "file_audit"], "level": "INFO", "propagate": False},
        "client_portal": {"handlers": ["file", "file_audit"], "level": "INFO", "propagate": False},
        "celery": {"handlers": ["file"], "level": "INFO", "propagate": False},
    },
}

# ============================================================================
# REDIS CACHE
# ============================================================================
try:
    import django_redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False
    warnings.warn("django_redis no está instalado. Usando caché en memoria local.", RuntimeWarning)

if REDIS_AVAILABLE and not os.getenv('REDIS_DISABLED', 'False') == 'True':
    CACHES = {
        'default': {
            'BACKEND': 'django_redis.cache.RedisCache',
            'LOCATION': os.getenv('REDIS_CACHE_URL', 'redis://127.0.0.1:6379/1'),
            'OPTIONS': {
                'CLIENT_CLASS': 'django_redis.client.DefaultClient',
                'CONNECTION_POOL_CLASS': 'redis.BlockingConnectionPool',
                'CONNECTION_POOL_CLASS_KWARGS': {
                    'max_connections': 50,
                    'timeout': 20,
                },
                'MAX_CONNECTIONS': 1000,
                'PICKLE_VERSION': -1,
                'SOCKET_CONNECT_TIMEOUT': 5,
                'SOCKET_TIMEOUT': 5,
                'RETRY_ON_TIMEOUT': True,
            },
            'KEY_PREFIX': f'bus_tickets_{os.getenv("ENVIRONMENT", "dev")}',
            'TIMEOUT': 300,
        },
        'ratelimit': {
            'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
            'LOCATION': 'ratelimit_cache',
            'TIMEOUT': 60,
            'OPTIONS': {'MAX_ENTRIES': 10000},
        },
        'session': {
            'BACKEND': 'django_redis.cache.RedisCache',
            'LOCATION': os.getenv('REDIS_SESSION_URL', 'redis://127.0.0.1:6379/2'),
            'OPTIONS': {
                'CLIENT_CLASS': 'django_redis.client.DefaultClient',
                'CONNECTION_POOL_CLASS': 'redis.BlockingConnectionPool',
                'CONNECTION_POOL_CLASS_KWARGS': {'max_connections': 100},
            },
            'KEY_PREFIX': 'sessions',
            'TIMEOUT': 86400,
        },
    }
else:
    CACHES = {
        'default': {
            'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
            'LOCATION': 'fallback_cache',
            'OPTIONS': {'MAX_ENTRIES': 5000},
            'TIMEOUT': 300,
        },
        'ratelimit': {
            'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
            'LOCATION': 'ratelimit_cache',
            'TIMEOUT': 60,
            'OPTIONS': {'MAX_ENTRIES': 10000},
        },
        'session': {
            'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
            'LOCATION': 'session_cache',
            'TIMEOUT': 86400,
            'OPTIONS': {'MAX_ENTRIES': 10000},
        },
    }

RATELIMIT_USE_CACHE = 'ratelimit'
SESSION_ENGINE = 'django.contrib.sessions.backends.cache'
SESSION_CACHE_ALIAS = 'session'

# ============================================================================
# CELERY CONFIGURATION
# ============================================================================
CELERY_BROKER_URL = os.getenv('CELERY_BROKER_URL', 'redis://localhost:6379/3')
CELERY_RESULT_BACKEND = os.getenv('CELERY_RESULT_BACKEND', 'redis://localhost:6379/4')
CELERY_RESULT_EXPIRES = int(os.getenv('CELERY_RESULT_EXPIRES', '3600'))
CELERY_BEAT_SCHEDULE_FILENAME = BASE_DIR / 'celerybeat-schedule'

CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'
CELERY_TIMEZONE = TIME_ZONE
CELERY_ENABLE_UTC = True
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = 30 * 60
CELERY_TASK_SOFT_TIME_LIMIT = 25 * 60
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_WORKER_MAX_TASKS_PER_CHILD = 100
CELERY_WORKER_MAX_MEMORY_PER_CHILD = 200

CELERY_TASK_DEFAULT_QUEUE = 'default'
CELERY_TASK_DEFAULT_EXCHANGE = 'default'
CELERY_TASK_DEFAULT_ROUTING_KEY = 'default'

CELERY_TASK_QUEUES = {
    'default': {'exchange': 'default', 'routing_key': 'default'},
    'high_priority': {'exchange': 'high_priority', 'routing_key': 'high_priority'},
    'low_priority': {'exchange': 'low_priority', 'routing_key': 'low_priority'},
    'emails': {'exchange': 'emails', 'routing_key': 'emails'},
    'reports': {'exchange': 'reports', 'routing_key': 'reports'},
}

CELERY_TASK_ROUTES = {
    'booking.tasks.release_expired_holds': {'queue': 'high_priority'},
    'booking.tasks.process_payment': {'queue': 'high_priority'},
    'booking.tasks.send_ticket_email': {'queue': 'emails'},
    'booking.tasks.generate_report': {'queue': 'reports'},
    'booking.tasks.sync_external_api': {'queue': 'low_priority'},
}

CELERY_WORKER_SEND_TASK_EVENTS = True
CELERY_TASK_SEND_SENT_EVENT = True
CELERY_EVENT_QUEUE_TTL = 60
CELERY_EVENT_QUEUE_EXPIRES = 60

# ============================================================================
# TAREAS PROGRAMADAS (Celery Beat)
# ============================================================================
from celery.schedules import crontab

CELERY_BEAT_SCHEDULE = {
    'release_expired_holds': {
        'task': 'booking.tasks.release_expired_holds',
        'schedule': 10.0,
        'options': {'queue': 'high_priority', 'expires': 60},
    },
    'clean_expired_sessions': {
        'task': 'booking.tasks.clean_expired_sessions',
        'schedule': 3600.0,
        'options': {'queue': 'low_priority', 'expires': 300},
    },
    'generate_daily_reports': {
        'task': 'booking.tasks.generate_daily_reports',
        'schedule': crontab(hour=0, minute=5),
        'options': {'queue': 'reports', 'expires': 3600},
    },
    'send_scheduled_notifications': {
        'task': 'booking.tasks.send_scheduled_notifications',
        'schedule': 60.0,
        'options': {'queue': 'emails', 'expires': 30},
    },
    'update_revenue_metrics': {
        'task': 'booking.tasks.update_revenue_metrics',
        'schedule': 300.0,
        'options': {'queue': 'low_priority', 'expires': 60},
    },
    'update_seat_availability_cache': {
        'task': 'booking.tasks.update_seat_availability_cache',
        'schedule': 5.0,
        'options': {'queue': 'high_priority', 'expires': 5},
    },
    'clean_old_logs': {
        'task': 'booking.tasks.clean_old_logs',
        'schedule': crontab(hour=2, minute=0),
        'options': {'queue': 'low_priority', 'expires': 7200},
    },
    'archive_old_tickets': {
        'task': 'booking.tasks.archive_old_tickets',
        'schedule': crontab(hour=3, minute=0),
        'options': {'queue': 'low_priority', 'expires': 7200},
    },
}

# ============================================================================
# VALIDACIÓN
# ============================================================================
if not DEBUG and ('localhost' in CELERY_BROKER_URL or '127.0.0.1' in CELERY_BROKER_URL):
    warnings.warn("⚠️ En producción, CELERY_BROKER_URL debe apuntar a un Redis externo.", RuntimeWarning)

# ============================================================================
# MÉTRICAS
# ============================================================================
if os.getenv('ENABLE_METRICS', 'False') == 'True':
    try:
        import django_prometheus
        INSTALLED_APPS.append('django_prometheus')
        MIDDLEWARE.insert(0, 'django_prometheus.middleware.PrometheusBeforeMiddleware')
        MIDDLEWARE.append('django_prometheus.middleware.PrometheusAfterMiddleware')
    except ImportError:
        warnings.warn("django_prometheus no está instalado. Para monitoreo: pip install django-prometheus", RuntimeWarning)

# ============================================================================
# INICIO
# ============================================================================
print(f"✅ Sistema iniciado en modo {'DESARROLLO' if DEBUG else 'PRODUCCIÓN'}")
print(f"📂 Directorio de logs: {LOG_DIR}")
print(f"🔑 SECRET_KEY {'✅ CONFIGURADA' if SECRET_KEY else '❌ FALTANTE'}")