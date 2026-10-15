from unittest.mock import patch
import pytest
from core.documents import amount_words,render_bill
from test_workflows import issue_pickup

@pytest.mark.parametrize('amount,expected',[
    ('0','Rupees zero only'),('280','Rupees two hundred eighty only'),
    ('100000.05','Rupees one lakh and five paise only'),
    ('10000000','Rupees one crore only'),
])
def test_amount_in_words(amount,expected):assert amount_words(amount)==expected

@pytest.mark.django_db
def test_document_uses_immutable_identity_and_redacts_internal_fields(data):
    bill=issue_pickup(data);captured=[]
    user=data['users']['collector'];user.first_name='Changed after issue';user.save()
    bill.notes='INTERNAL-PRIVATE-NOTE'
    with patch('core.documents.SimpleDocTemplate.build',side_effect=lambda story,**kwargs:captured.extend(story)):
        render_bill(bill)
    text=' '.join(item.getPlainText() for item in captured if hasattr(item,'getPlainText'))
    assert 'Collected by: Collector' in text and 'Changed after issue' not in text
    assert 'INTERNAL-PRIVATE-NOTE' not in text and 'PAYMENT PENDING' in text
    assert 'Total weight: 10 kg' in text and 'Rupees two hundred eighty only' in text

@pytest.mark.django_db
def test_pdf_text_cannot_fetch_resources_or_run_markup(data):
    bill=issue_pickup(data)
    bill.snapshot['terms']='<img src="file:///etc/passwd"/><img src="https://example.test/private"/><script>alert(1)</script>'
    with patch('reportlab.lib.utils.open_for_read',side_effect=AssertionError('External resource access')):
        result=render_bill(bill)
    assert result.startswith(b'%PDF-')

@pytest.mark.django_db
@pytest.mark.parametrize('oversize',['lines','field','document'])
def test_pdf_limits(data,oversize):
    from rest_framework.exceptions import ValidationError
    bill=issue_pickup(data)
    if oversize=='lines': bill.lines=bill.lines*101
    elif oversize=='field': bill.snapshot['terms']='x'*4001
    else: bill.snapshot['policy']={'oversize':'x'*200001}
    with pytest.raises(ValidationError):render_bill(bill)

@pytest.mark.django_db(transaction=True)
def test_pdf_jobs_retry_regenerate_and_authorize_delivery(data,client_for,tmp_path,settings):
    from django.core.management import call_command
    from core.models import Role,Job,Bill
    from test_workflows import post
    settings.PRIVATE_ROOT=tmp_path
    bill=issue_pickup(data);number=bill.number;lines=bill.lines
    client=client_for(data['users'][Role.COLLECTOR]);url=f'documents/{bill.pk}/pdf-jobs/'
    first=post(client,url,{},'pdf-first')
    assert first.status_code==202
    assert post(client,url,{},'pdf-first').data==first.data
    assert Job.objects.count()==1
    call_command('run_jobs',once=True)
    job=Job.objects.get();assert job.state=='done' and (tmp_path/job.file_name).read_bytes().startswith(b'%PDF-')
    response=client.get(f'/api/v1/jobs/{job.pk}/download/')
    assert response.status_code==200
    response.close()
    assert client_for(data['users'][Role.HQ]).get(f'/api/v1/jobs/{job.pk}/download/').status_code==404
    assert post(client,url,{},'pdf-regenerate').status_code==202
    call_command('run_jobs',once=True)
    assert Job.objects.filter(state='done').count()==2
    bill.refresh_from_db();assert bill.number==number and bill.lines==lines and Bill.objects.count()==1

@pytest.mark.django_db
def test_pdf_job_revocation_before_generation(data,client_for,tmp_path,settings):
    from django.core.management import call_command
    from core.models import Role,Job
    from test_workflows import post
    settings.PRIVATE_ROOT=tmp_path
    bill=issue_pickup(data);actor=data['users'][Role.COLLECTOR]
    assert post(client_for(actor),f'documents/{bill.pk}/pdf-jobs/',{}).status_code==202
    actor.permission_version+=1;actor.save()
    call_command('run_jobs',once=True)
    job=Job.objects.get();assert job.state=='failed' and job.error=='PermissionDenied'
    assert not list(tmp_path.iterdir())
