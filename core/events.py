import asyncio
import json
import time
from contextlib import suppress
from cryptography.fernet import Fernet,InvalidToken
import redis.asyncio as aioredis
from asgiref.sync import sync_to_async
from django.conf import settings
from django.http import StreamingHttpResponse,JsonResponse
from django.contrib.sessions.models import Session
from django.utils import timezone
from .models import User,Outbox,Rate
from .policy import GRANTS,NETWORK,OWN,has_permission
STREAM='scrappy:events:v1'

class Hub:
    def __init__(self): self.listeners=set();self.task=None;self.initialized=asyncio.Event()
    async def add(self):
        q=asyncio.Queue(maxsize=100);self.listeners.add(q)
        if self.task is None or self.task.done():
            self.initialized.clear();self.task=asyncio.create_task(self.run())
        try: await self.initialized.wait()
        except BaseException:
            self.listeners.discard(q)
            raise
        return q
    async def run(self):
        client=aioredis.from_url(settings.REDIS_URL,decode_responses=True);cursor='0-0'
        try:
            latest=await client.xrevrange(STREAM,count=1)
            cursor=latest[0][0] if latest else '0-0'
        except Exception: pass
        self.initialized.set()
        try:
            while self.listeners:
                try:
                    batches=await client.xread({STREAM:cursor},count=100,block=1000)
                    for _,rows in batches:
                        for ident,fields in rows:
                            cursor=ident
                            for q in tuple(self.listeners):
                                if q.full():
                                    while not q.empty(): q.get_nowait()
                                    q.put_nowait(('reset',{}))
                                else: q.put_nowait((ident,fields))
                except Exception:
                    for q in tuple(self.listeners):
                        if not q.full(): q.put_nowait(('delayed',{}))
                    await asyncio.sleep(2)
        finally: await client.aclose()
    def remove(self,q): self.listeners.discard(q)
HUB=Hub()

def current_user(uid,session_key,pv):
    user=User.objects.filter(pk=uid,is_active=True,permission_version=pv).first()
    session=Session.objects.filter(session_key=session_key,expire_date__gt=timezone.now()).first()
    if not user or not session: return None
    values=session.get_decoded();idle=900 if user.role in NETWORK else 1800
    if time.time()-values.get('last_activity',0)>idle or time.time()-values.get('signed_in_at',0)>43200: return None
    return user

def allowed(user,event):
    feature=event['feature']
    if feature=='scope_reset': return str(user.pk)==str(event.get('owner_id'))
    if not has_permission(user,feature): return False
    if user.role in NETWORK: return True
    if event.get('franchise_id') and str(user.franchise_id)!=str(event['franchise_id']): return False
    if feature=='rates' and not Rate.objects.filter(pk=event.get('resource_id'),product__franchises=user.franchise_id).exists(): return False
    if user.role in OWN and str(user.pk)!=str(event.get('owner_id')): return False
    if user.role=='warehouse_manager' and event.get('warehouse_id') and str(user.warehouse_id)!=str(event['warehouse_id']): return False
    return True

def frame(name,data,cursor=None):
    return ((f'id: {cursor}\n' if cursor else '')+f'event: {name}\ndata: {json.dumps(data)}\n\n').encode()

async def stream(request):
    user=await request.auser()
    if not user.is_authenticated: return JsonResponse({'detail':'Sign in required.'},status=401)
    uid,pv,key=user.pk,user.permission_version,request.session.session_key
    if not settings.MFA_ENCRYPTION_KEY: return JsonResponse({'detail':'Stream encryption is not configured.'},status=503)
    cipher=Fernet(settings.MFA_ENCRYPTION_KEY.encode())
    def cursor_for(value): return cipher.encrypt(json.dumps({'cursor':value,'user':uid,'version':pv}).encode()).decode()
    async def generate():
        q=await HUB.add();client=aioredis.from_url(settings.REDIS_URL,decode_responses=True)
        try:
            try:
                latest=await client.xrevrange(STREAM,count=1);boundary=latest[0][0] if latest else '0-0'
                raw=request.headers.get('Last-Event-ID') or request.GET.get('cursor')
                if raw:
                    try:
                        saved=json.loads(cipher.decrypt(raw.encode(),ttl=86400))
                        if saved['user']!=uid or saved['version']!=pv: raise ValueError()
                        oldest=await client.xrange(STREAM,count=1)
                        if not oldest or tuple(map(int,saved['cursor'].split('-')))<tuple(map(int,oldest[0][0].split('-'))): raise ValueError()
                        rows=await client.xrange(STREAM,min='('+saved['cursor'],max=boundary,count=1001)
                        if len(rows)>1000: raise ValueError()
                        for ident,fields in rows:
                            actor=await sync_to_async(current_user)(uid,key,pv)
                            if not actor: yield frame('session.expired',{});return
                            e=json.loads(fields['data'])
                            if await sync_to_async(allowed)(actor,e): yield frame('scope.reset' if e['feature']=='scope_reset' else 'resource.changed',{'event_id':e['event_id'],'invalidate':[e['feature']]},cursor_for(ident))
                    except (InvalidToken,ValueError,KeyError): yield frame('resync.required',{})
                yield frame('ready',{'synchronized':False},cursor_for(boundary))
            except Exception:
                yield frame('updates.delayed',{})
            while True:
                actor=await sync_to_async(current_user)(uid,key,pv)
                if not actor: yield frame('session.expired',{});return
                try: ident,fields=await asyncio.wait_for(q.get(),timeout=15)
                except asyncio.TimeoutError:
                    lag=await sync_to_async(lambda:Outbox.objects.filter(published_at__isnull=True,created_at__lt=timezone.now()-timezone.timedelta(seconds=5)).exists())()
                    yield frame('updates.delayed',{}) if lag else b': heartbeat\n\n';continue
                # Permissions may have changed while this connection awaited work.
                actor=await sync_to_async(current_user)(uid,key,pv)
                if not actor: yield frame('session.expired',{});return
                if ident=='reset': yield frame('resync.required',{});return
                if ident=='delayed': yield frame('updates.delayed',{});continue
                e=json.loads(fields['data'])
                if await sync_to_async(allowed)(actor,e): yield frame('scope.reset' if e['feature']=='scope_reset' else 'resource.changed',{'event_id':e['event_id'],'invalidate':[e['feature']]},cursor_for(ident))
        finally:
            HUB.remove(q);await client.aclose()
    response=StreamingHttpResponse(generate(),content_type='text/event-stream')
    response['Cache-Control']='no-store, no-transform';response['X-Accel-Buffering']='no'
    return response
