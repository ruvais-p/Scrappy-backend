"""Derived reads only: database generation prevents stale fills from becoming current."""
import hashlib
import json
import redis
from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from .models import DataRevision

def generation():
    row,_=DataRevision.objects.get_or_create(key='global')
    return row.value

def cached_read(user,name,filters,compute,bypass=False,minimum_version=None):
    before=generation()
    scope={'user':user.pk,'permission':user.permission_version,'franchise':str(user.franchise_id),'warehouse':str(user.warehouse_id),'query':name,'filters':filters,'revision':before}
    key='scrappy:cache:v1:'+hashlib.sha256(json.dumps(scope,sort_keys=True).encode()).hexdigest()
    client=redis.Redis.from_url(settings.REDIS_URL,socket_connect_timeout=.1,socket_timeout=.1)
    # A caller that observed a committed mutation must not receive an older
    # disposable cache entry. The database remains the read authority.
    if minimum_version is not None and before < int(minimum_version):
        bypass = True
    if not bypass:
        try:
            hit=client.get(key)
            if hit and before==generation():return json.loads(hit)
        except redis.RedisError:pass
    value=compute()
    if not bypass and before==generation():
        try:client.setex(key,30,json.dumps(value,cls=DjangoJSONEncoder))
        except redis.RedisError:pass
    return value
