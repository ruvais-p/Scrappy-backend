from django.db import connection
from django.http import JsonResponse
from django.conf import settings
from redis import Redis

def live(request):
    return JsonResponse({'status':'alive'})

def ready(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
        with Redis.from_url(settings.REDIS_URL,socket_connect_timeout=1,socket_timeout=1) as client:
            client.ping()
    except Exception:
        return JsonResponse({'status':'unavailable'},status=503)
    return JsonResponse({'status':'ready'})
