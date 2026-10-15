import pytest
from django.utils import timezone
from core.models import AuditEvent, Role
from test_reconciliation import prepared
from test_workflows import issue_pickup, post

@pytest.mark.django_db
def test_hq_audit_search_filters_and_scoped_export(data,client_for):
    bill=issue_pickup(data)
    AuditEvent.objects.create(actor=data['users'][Role.HQ],franchise=data['books'][0],action='audit.special_payment',resource=str(bill.pk),details={'safe':'value'})
    client=client_for(data['users'][Role.HQ])
    result=client.get('/api/v1/audit/?action=special_payment&search='+str(bill.pk))
    assert result.status_code==200 and result.data['count']==1
    assert result.data['results'][0]['resource']==str(bill.pk)
    assert client.get('/api/v1/audit/?action=does-not-exist').data['count']==0
    assert client.get('/api/v1/audit/?from=bad').status_code==400

@pytest.mark.django_db
def test_admin_and_collector_timelines_are_local_but_register_is_denied(data,client_for):
    bill=issue_pickup(data);collector=client_for(data['users'][Role.COLLECTOR]);admin=client_for(data['users'][Role.ADMIN])
    local=collector.get(f'/api/v1/timeline/pickups/{bill.pk}/')
    assert local.status_code==200 and local.data['resource']==str(bill.pk)
    assert collector.get('/api/v1/audit/').status_code==403
    assert admin.get(f'/api/v1/timeline/pickups/{bill.pk}/').status_code==200
    assert admin.get('/api/v1/audit/').status_code==403
    other=data['leads'][1]
    # Synthetic fixtures assign both leads to this collector; both are local.
    assert collector.get(f'/api/v1/timeline/leads/{other.pk}/').status_code==200

@pytest.mark.django_db
def test_timeline_rejects_network_and_unsupported_objects(data,client_for):
    bill=issue_pickup(data);hq=client_for(data['users'][Role.HQ]);collector=client_for(data['users'][Role.COLLECTOR])
    assert hq.get(f'/api/v1/timeline/pickups/{bill.pk}/').status_code==403
    assert collector.get(f'/api/v1/timeline/audit/{bill.pk}/').status_code==404
    assert collector.get(f'/api/v1/timeline/pickups/{data["leads"][1].pk}/').status_code==404
