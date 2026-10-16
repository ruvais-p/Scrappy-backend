import pytest

from core.models import Role


@pytest.mark.django_db
@pytest.mark.parametrize('role', list(Role.values))
def test_every_role_has_scoped_identity_and_navigation_contract(data, client_for, role):
    response = client_for(data['users'][role]).get('/api/v1/auth/me/')
    assert response.status_code == 200
    assert response.data['role'] == role
    assert isinstance(response.data['capabilities'], list)


@pytest.mark.django_db
def test_nested_foreign_identifiers_do_not_cross_scope(data, client_for):
    local = data['users'][Role.APP]
    foreign = data['leads'][0]
    foreign.franchise = data['books'][1]
    foreign.save(update_fields=['franchise'])

    assert client_for(local).get(f'/api/v1/leads/{foreign.pk}/').status_code == 404
    assert client_for(local).get(f'/api/v1/timeline/leads/{foreign.pk}/').status_code in (403, 404)
