import json
import time
import redis
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone
from core.models import Outbox
from core.events import STREAM
class Command(BaseCommand):
    help='Publish durable committed events, with at-least-once retry.'
    def add_arguments(self,p): p.add_argument('--once',action='store_true')
    def handle(self,*args,**opts):
        client=redis.Redis.from_url(settings.REDIS_URL,decode_responses=True,socket_timeout=5)
        while True:
            try:
                with transaction.atomic():
                    records=list(Outbox.objects.select_for_update(skip_locked=True).filter(published_at__isnull=True).order_by('created_at')[:100])
                    for record in records:
                        payload={k:str(getattr(record,k)) if getattr(record,k) is not None else None for k in ('franchise_id','warehouse_id','owner_id','resource_id')}
                        payload.update(event_id=str(record.pk),feature=record.feature)
                        client.xadd(STREAM,{'data':json.dumps(payload)},maxlen=100000,approximate=True)
                        record.published_at=timezone.now();record.save(update_fields=['published_at'])
                if opts['once']: return
                time.sleep(.25 if records else 1)
            except Exception as exc:
                self.stderr.write('Event dispatch delayed: '+type(exc).__name__)
                if opts['once']: raise
                time.sleep(2)
