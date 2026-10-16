import uuid

import pytest

from core.models import Role
from test_workflows import issue_pickup, post


@pytest.mark.django_db
def test_failed_lead_register_preserves_attempt_history_and_load_context(data, client_for):
    lead = data['leads'][1]
    driver = data['users'][Role.COLLECTOR]
    response = post(client_for(driver), f'leads/{lead.pk}/failed/',
                    {'version': lead.version, 'reason': 'Customer unavailable at the address.'})
    assert response.status_code == 200, response.data

    register = client_for(driver).get('/api/v1/leads/?state=not_completed')
    assert register.status_code == 200
    row = next(item for item in register.data['results'] if item['id'] == str(lead.pk))
    assert row['failure_reason'] == 'Customer unavailable at the address.'
    assert row['failure_history'][0]['reason'] == row['failure_reason']
    assert row['current_load']['id'] == str(data['load'].pk)
    assert register.data['summary']['not_completed'] == 1


@pytest.mark.django_db
def test_dashboard_pickup_chart_uses_scoped_paid_totals(data, client_for):
    bill = issue_pickup(data)
    driver = data['users'][Role.COLLECTOR]
    response = post(client_for(driver), f'pickups/{bill.pk}/pay/',
                    {'version': bill.version, 'amount': str(bill.total), 'method': 'UPI', 'reference': str(uuid.uuid4())})
    assert response.status_code == 200, response.data

    dashboard = client_for(driver).get('/api/v1/dashboard/')
    assert dashboard.status_code == 200
    totals = dashboard.data['pickup_chart']['totals']
    assert totals['billed'] == str(bill.total)
    assert totals['paid'] == str(bill.total)
    assert totals['outstanding'] == '0.00'
    assert dashboard.data['pickup_chart']['daily'][0]['closed'] == 1
