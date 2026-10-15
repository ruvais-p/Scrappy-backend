import time
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone
from core.models import Job
from core.jobs import process
class Command(BaseCommand):
    help='Process durable document and invitation jobs; retry transient failures.'
    def add_arguments(self,p):p.add_argument('--once',action='store_true')
    def handle(self,*args,**opts):
        while True:
            with transaction.atomic():
                job=Job.objects.select_for_update(skip_locked=True).filter(state='pending',attempts__lt=3).order_by('created_at').first()
                if job:
                    job.attempts+=1
                    try:
                        with transaction.atomic():process(job)
                        job.state='done';job.error=''
                    except Exception as exc:
                        job.error=type(exc).__name__
                        if job.attempts>=3 or job.error in ('PermissionDenied','ValidationError','NotFound'):job.state='failed'
                    job.save()
            for expired in Job.objects.filter(expires_at__lt=timezone.now()).exclude(file_name='')[:100]:
                path=settings.PRIVATE_ROOT/expired.file_name
                if path.parent==settings.PRIVATE_ROOT:path.unlink(missing_ok=True)
                expired.file_name='';expired.state='expired';expired.save()
            Job.objects.filter(expires_at__lt=timezone.now(),file_name='').exclude(state='expired').update(state='expired')
            if opts['once']:return
            time.sleep(.25 if job else 1)
