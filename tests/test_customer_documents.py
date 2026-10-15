import hashlib
from unittest.mock import patch
import pytest
from django.core.management import call_command
from django.utils import timezone
from rest_framework.test import APIClient
from core.models import Role, CustomerLink, Job, Policy, Bill
from core.documents import Paragraph
from test_workflows import issue_pickup,post


def pay(client,bill):
    result=post(client,f'pickups/{bill.pk}/pay/',{'version':bill.version,'amount':str(bill.total),'method':'UPI','reference':'SECRET-FULL-UTR'})
    assert result.status_code==200,result.data

@pytest.mark.django_db
def test_same_link_paid_version_redaction_and_immutable_identity(data,client_for):
    bill=issue_pickup(data);number=bill.number;lines=bill.lines
    client=client_for(data['users'][Role.COLLECTOR]);public=APIClient()
    link=post(client,f'documents/{bill.pk}/share/',{},'stable-link').data['url']
    raw=link.split('/')[-2]
    assert CustomerLink.objects.get().digest==hashlib.sha256(raw.encode()).hexdigest()
    text=[]
    def capture(value,*args,**kwargs):
        text.append(str(value));return Paragraph(value,*args,**kwargs)
    with patch('core.documents.Paragraph',side_effect=capture):
        pending=public.get(link)
    assert pending.status_code==200 and 'PAYMENT PENDING' in text
    before_version=pending['X-Document-Version'];pay(client,bill);text.clear()
    with patch('core.documents.Paragraph',side_effect=capture):
        paid=public.get(link)
    assert paid.status_code==200 and text.count('Amount is PAID')==1 and 'PAYMENT PENDING' not in text
    assert 'SECRET-FULL-UTR' not in ' '.join(text)
    assert paid['X-Document-Version']!=before_version and paid['Cache-Control']=='private, no-store'
    assert paid['Referrer-Policy']=='no-referrer'
    bill.refresh_from_db();assert bill.number==number and bill.lines==lines and Bill.objects.count()==1
    assert public.get('/api/v1/pickups/?token='+raw).status_code in (401,403)
    assert public.post(link,{'action':'pay'},format='json').status_code==405

@pytest.mark.django_db
def test_link_expiry_revocation_and_renewal(data,client_for):
    bill=issue_pickup(data);client=client_for(data['users'][Role.COLLECTOR]);public=APIClient()
    url=f'documents/{bill.pk}/share/'
    expired=post(client,url,{},'first').data['url']
    CustomerLink.objects.update(expires_at=timezone.now()-timezone.timedelta(seconds=1))
    assert public.get(expired).status_code==404
    active=post(client,url,{},'second').data['url']
    assert public.get(active).status_code==200
    revoke=f'documents/{bill.pk}/revoke-links/'
    assert post(client_for(data['users'][Role.HQ]),revoke,{}).status_code==400
    result=post(client,revoke,{},'revoke-once')
    assert result.status_code==200
    assert post(client,revoke,{},'revoke-once').data==result.data
    assert public.get(active).status_code==404
    renewed=post(client,url,{},'third').data['url']
    assert public.get(renewed).status_code==200 and renewed!=active
    assert public.get('/api/v1/customer-bill/not-a-token/').status_code==404

@pytest.mark.django_db(transaction=True)
def test_old_job_download_refreshes_after_payment(data,client_for,settings,tmp_path):
    settings.PRIVATE_ROOT=tmp_path
    bill=issue_pickup(data);client=client_for(data['users'][Role.COLLECTOR])
    post(client,f'documents/{bill.pk}/pdf-jobs/',{})
    call_command('run_jobs',once=True)
    job=Job.objects.get();original=(tmp_path/job.file_name).read_bytes()
    old_version=job.payload['rendition_version'];assert old_version==bill.version
    pay(client,bill)
    text=[]
    def capture(value,*args,**kwargs):
        text.append(str(value));return Paragraph(value,*args,**kwargs)
    with patch('core.documents.Paragraph',side_effect=capture):
        response=client.get(f'/api/v1/jobs/{job.pk}/download/')
    assert response.status_code==200 and 'Amount is PAID' in text
    assert int(response['X-Document-Version'])>old_version
    assert (tmp_path/job.file_name).read_bytes()==original
    assert Job.objects.count()==1 and Bill.objects.count()==1

@pytest.mark.django_db
def test_missing_sharing_policy_blocks_new_links(data,client_for):
    bill=issue_pickup(data);Policy.objects.filter(key='customer_sharing').delete()
    result=post(client_for(data['users'][Role.COLLECTOR]),f'documents/{bill.pk}/share/',{})
    assert result.status_code==409 and not CustomerLink.objects.exists()
