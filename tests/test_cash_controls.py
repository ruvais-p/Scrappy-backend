import pytest

from core.models import DriverAccount, Role


@pytest.mark.django_db
def test_float_issuance_requires_positive_amount_and_scope(data, client_for):
    hq = data['users'][Role.HQ]
    driver = data['users'][Role.COLLECTOR]
    client = client_for(hq)
    for amount in ('0', '-1'):
        response = client.post('/api/v1/driver-cash/', {
            'action': 'float', 'driver': str(driver.pk), 'amount': amount,
        }, format='json', HTTP_IDEMPOTENCY_KEY=f'float-invalid-{amount}')
        assert response.status_code == 400
    valid = client.post('/api/v1/driver-cash/', {
        'action': 'float', 'driver': str(driver.pk), 'amount': '125',
    }, format='json', HTTP_IDEMPOTENCY_KEY='float-positive-control')
    assert valid.status_code == 200
    assert DriverAccount.objects.get(driver=driver).balance == 125

    other_driver = data['users'][Role.DIGESTER]
    denied = client.post('/api/v1/driver-cash/', {
        'action': 'float', 'driver': str(other_driver.pk), 'amount': '10',
    }, format='json', HTTP_IDEMPOTENCY_KEY='float-cross-franchise')
    assert denied.status_code == 404
