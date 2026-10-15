import pytest
from core.models import Role,Customer

@pytest.mark.django_db
def test_role_matrix_and_related_identifiers_are_enforced(data,client_for):
    assert client_for(data['users'][Role.ADMIN]).get('/api/v1/audit/').status_code==403
    assert client_for(data['users'][Role.COLLECTOR]).get('/api/v1/bills/').status_code==403
    assert client_for(data['users'][Role.APP]).get('/api/v1/settings/').status_code==403
    foreign=Customer.objects.create(franchise=data['books'][1],name='Foreign contact',phone='9000000111')
    assert client_for(data['users'][Role.APP]).get(f'/api/v1/customers/{foreign.id}/').status_code==404
    assert client_for(data['users'][Role.WAREHOUSE]).get('/api/v1/payments/').status_code==403
