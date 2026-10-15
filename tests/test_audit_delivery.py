import json
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest
from django.core.management import call_command
from django.utils import timezone
from core.models import AuditEvent, Job, Policy, Role
from test_workflows import post

@pytest.mark.django_db(transaction=True)
def test_redacted_audit_delivery_retries_and_keeps_failure_evidence(data,client_for):
    event=AuditEvent.objects.create(actor=data['users'][Role.HQ],franchise=data['books'][0],action='payment.recorded',resource='bill-1',details={'reference':'FULL-UTR','secret_value':'hide','amount':'10','nested':{'token':'hide','ok':'yes'}})
    Policy.objects.update_or_create(key='audit_delivery',defaults={'config':{'enabled':True,'endpoint':'https://audit.example.test/events','timeout_seconds':5},'decision_reference':'Synthetic delivery test','test_only':True})
    client=client_for(data['users'][Role.SUPER]);result=post(client,'audit-delivery/',{'event':str(event.pk)},'delivery-once')
    assert result.status_code==202,result.data
    assert post(client,'audit-delivery/',{'event':str(event.pk)},'delivery-once').data==result.data
    job=Job.objects.get(kind='audit_delivery');sent=[]
    response=MagicMock(status=202);response.__enter__.return_value=response;response.__exit__.return_value=False
    with patch('core.jobs.urllib.request.urlopen',side_effect=lambda request,timeout: sent.append(json.loads(request.data)) or response):
        call_command('run_jobs',once=True)
    job.refresh_from_db();assert job.state=='done' and len(sent)==1
    payload=sent[0];assert payload['details']=={'amount':'10','nested':{'ok':'yes'}}
    assert 'FULL-UTR' not in json.dumps(payload) and 'hide' not in json.dumps(payload)
    # A transport failure is retried three times, then remains as a durable failed record.
    failed=post(client,'audit-delivery/',{'event':str(event.pk)},'delivery-fails').data
    with patch('core.jobs.urllib.request.urlopen',side_effect=TimeoutError('offline')):
        call_command('run_jobs',once=True);call_command('run_jobs',once=True);call_command('run_jobs',once=True)
    failed_job=Job.objects.get(pk=failed['id']);assert failed_job.state=='failed' and failed_job.attempts==3 and failed_job.error=='TimeoutError'
    assert failed_job.payload['event']==str(event.pk)

@pytest.mark.django_db(transaction=True)
def test_delivery_is_superadmin_only_and_cleanup_expires_private_file(data,client_for,tmp_path,settings):
    from core.models import Job
    settings.PRIVATE_ROOT=tmp_path;event=AuditEvent.objects.create(actor=data['users'][Role.HQ],action='safe',resource='x')
    Policy.objects.update_or_create(key='audit_delivery',defaults={'config':{'enabled':True,'endpoint':'https://audit.example.test/events'},'decision_reference':'test','test_only':True})
    assert post(client_for(data['users'][Role.HQ]),'audit-delivery/',{'event':str(event.pk)}).status_code==403
    old=timezone.now()-timezone.timedelta(days=2);path=tmp_path/'old.csv';path.write_bytes(b'private')
    job=Job.objects.create(actor=data['users'][Role.SUPER],kind='export',payload={},state='done',file_name=path.name,expires_at=old)
    call_command('run_jobs',once=True)
    job.refresh_from_db();assert job.state=='expired' and job.file_name=='' and not path.exists()
