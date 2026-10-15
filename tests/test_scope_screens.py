import pytest
from core.models import Customer,Role

@pytest.mark.django_db
def test_customer_register_and_team_are_scoped(data,client_for):
    manager=data['users'][Role.APP]
    own=Customer.objects.create(franchise=manager.franchise,name='Own contact',phone='9000000101')
    other=Customer.objects.create(franchise=data['books'][1],name='Other contact',phone='9000000102')
    rows=client_for(manager).get('/api/v1/customers/').data['results']
    ids={str(row['id']) for row in rows}
    assert str(own.id) in ids and str(other.id) not in ids
    team=client_for(manager).get('/api/v1/team/')
    assert team.status_code==200
    assert all(row['franchise']==str(manager.franchise_id) for row in team.data['results'])

@pytest.mark.django_db
def test_team_disable_is_scoped_and_revokes_permission_version(data,client_for):
    manager=data['users'][Role.APP];target=data['users'][Role.COLLECTOR]
    payload={'id':str(target.id),'name':target.first_name,'email':target.email,'role':target.role,'franchise':str(target.franchise_id),'action':'disable'}
    response=client_for(manager).post('/api/v1/team/',payload,format='json',HTTP_IDEMPOTENCY_KEY='team-disable-screen')
    assert response.status_code==200
    target.refresh_from_db()
    assert target.is_active is False and target.permission_version==2

@pytest.mark.django_db
def test_team_delegation_rejects_privileged_targets_and_self_promotion(data,client_for):
    hq=data['users'][Role.HQ]
    super_admin=data['users'][Role.SUPER]
    denied=client_for(hq).post('/api/v1/team/',{'id':str(super_admin.id),'role':Role.SUPER,'franchise':str(data['books'][0].pk)},format='json',HTTP_IDEMPOTENCY_KEY='hq-super-target')
    assert denied.status_code==404
    app=data['users'][Role.APP]
    self_edit=client_for(app).post('/api/v1/team/',{'id':str(app.id),'role':Role.APP,'franchise':str(app.franchise_id)},format='json',HTTP_IDEMPOTENCY_KEY='self-promotion')
    assert self_edit.status_code==404
