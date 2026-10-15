import os
from pathlib import Path
from django.core.exceptions import ImproperlyConfigured
BASE_DIR = Path(__file__).resolve().parent.parent
from dotenv import load_dotenv
load_dotenv(BASE_DIR / '.env')
ENVIRONMENT = os.getenv('SCRAPPY_ENV', 'development')
SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY', '')
if not SECRET_KEY:
    raise ImproperlyConfigured('DJANGO_SECRET_KEY must be set; generate a unique local secret.')
DEBUG = os.getenv('DJANGO_DEBUG', '0') == '1'
ALLOWED_HOSTS = os.getenv('DJANGO_ALLOWED_HOSTS', 'localhost,127.0.0.1,backend,testserver').split(',')
INSTALLED_APPS = ['django.contrib.auth','django.contrib.contenttypes','django.contrib.sessions','rest_framework','drf_spectacular','core']
MIDDLEWARE = ['django.middleware.security.SecurityMiddleware','django.contrib.sessions.middleware.SessionMiddleware','django.middleware.common.CommonMiddleware','django.middleware.csrf.CsrfViewMiddleware','django.contrib.auth.middleware.AuthenticationMiddleware','core.middleware.AccessMiddleware']
ROOT_URLCONF = 'config.urls'
ASGI_APPLICATION = 'config.asgi.application'
DATABASES = {'default': {'ENGINE':'django.db.backends.postgresql','NAME':os.getenv('POSTGRES_DB','scrappy'),'USER':os.getenv('POSTGRES_USER','scrappy'),'PASSWORD':os.environ.get('POSTGRES_PASSWORD',''),'HOST':os.getenv('POSTGRES_HOST','127.0.0.1'),'PORT':os.getenv('POSTGRES_PORT','55432'),'CONN_MAX_AGE':0}}
DATABASES['default']['OPTIONS'] = {'sslmode':os.getenv('POSTGRES_SSLMODE','prefer')}
AUTH_USER_MODEL = 'core.User'
AUTHENTICATION_BACKENDS = ['django.contrib.auth.backends.ModelBackend']
PASSWORD_HASHERS = ['django.contrib.auth.hashers.Argon2PasswordHasher','django.contrib.auth.hashers.PBKDF2PasswordHasher']
AUTH_PASSWORD_VALIDATORS = [{'NAME':'django.contrib.auth.password_validation.MinimumLengthValidator','OPTIONS':{'min_length':15}},{'NAME':'django.contrib.auth.password_validation.CommonPasswordValidator'},{'NAME':'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'}]
TIME_ZONE = 'Asia/Kolkata'
USE_TZ = True
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
SESSION_COOKIE_SECURE = ENVIRONMENT == 'production'
CSRF_COOKIE_SECURE = ENVIRONMENT == 'production'
CSRF_TRUSTED_ORIGINS = os.getenv('CSRF_TRUSTED_ORIGINS','http://localhost:8080,http://127.0.0.1:8080').split(',')
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = 'no-referrer'
SESSION_COOKIE_AGE = 43200
SESSION_SAVE_EVERY_REQUEST = False
REST_FRAMEWORK = {'DEFAULT_AUTHENTICATION_CLASSES':['rest_framework.authentication.SessionAuthentication'],'DEFAULT_PERMISSION_CLASSES':['rest_framework.permissions.IsAuthenticated'],'DEFAULT_SCHEMA_CLASS':'drf_spectacular.openapi.AutoSchema','DEFAULT_PAGINATION_CLASS':'rest_framework.pagination.PageNumberPagination','PAGE_SIZE':50,'EXCEPTION_HANDLER':'core.api_errors.exception_handler','DEFAULT_RENDERER_CLASSES':['rest_framework.renderers.JSONRenderer']}
SPECTACULAR_SETTINGS = {'TITLE':'Scrappy API','VERSION':'1.0.0','SERVE_INCLUDE_SCHEMA':False,'COMPONENT_SPLIT_REQUEST':True}
REDIS_URL = os.getenv('REDIS_URL','redis://127.0.0.1:56379/0')
PRIVATE_ROOT = Path(os.getenv('PRIVATE_STORAGE_PATH',str(BASE_DIR / 'private-storage')))
EMAIL_BACKEND = os.getenv('EMAIL_BACKEND','django.core.mail.backends.console.EmailBackend')
EMAIL_HOST = os.getenv('EMAIL_HOST','localhost')
EMAIL_PORT = int(os.getenv('EMAIL_PORT','587'))
EMAIL_HOST_USER = os.getenv('EMAIL_HOST_USER','')
EMAIL_HOST_PASSWORD = os.getenv('EMAIL_HOST_PASSWORD','')
EMAIL_USE_TLS = os.getenv('EMAIL_USE_TLS','1') == '1'
EMAIL_TIMEOUT = 10
DEFAULT_FROM_EMAIL = os.getenv('DEFAULT_FROM_EMAIL','accounts@localhost')
PUBLIC_ORIGIN = os.getenv('PUBLIC_ORIGIN','http://localhost:8080')
MFA_ENCRYPTION_KEY = os.getenv('MFA_ENCRYPTION_KEY','')
if os.getenv('TRUST_PROXY_SSL_HEADER','0') == '1':
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO','https')
if ENVIRONMENT == 'production':
    SECURE_SSL_REDIRECT = True
    SECURE_HSTS_SECONDS = 31536000
    if DEBUG or EMAIL_BACKEND.endswith('console.EmailBackend'):
        raise ImproperlyConfigured('Production requires DEBUG=0 and configured email delivery.')
