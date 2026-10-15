import pytest

from core.models import Bill, CompanySetting, Role


@pytest.mark.django_db
def test_settings_save_is_super_admin_only_and_versions_values(data, client_for):
    super_admin = data['users'][Role.SUPER]
    payload = {'version': 1, 'values': {'company_name': 'Updated Company'}}
    response = client_for(super_admin).post(
        '/api/v1/settings/company/save/', payload, format='json',
        HTTP_IDEMPOTENCY_KEY='settings-company-version-test',
    )
    assert response.status_code == 200
    setting = CompanySetting.objects.get(section='company')
    assert setting.values['company_name'] == 'Updated Company'
    assert setting.version == 2

    for role in (Role.ADMIN, Role.HQ, Role.APP):
        denied = client_for(data['users'][role]).post(
            '/api/v1/settings/company/save/', payload, format='json',
            HTTP_IDEMPOTENCY_KEY=f'settings-denied-{role}',
        )
        assert denied.status_code == 403


@pytest.mark.django_db
def test_settings_validate_invoice_and_payment_method_configuration(data, client_for):
    super_admin = data['users'][Role.SUPER]
    bad_invoice = client_for(super_admin).post(
        '/api/v1/settings/invoice/save/',
        {'version': 1, 'values': {'prefix': ''}}, format='json',
        HTTP_IDEMPOTENCY_KEY='settings-invalid-invoice-test',
    )
    assert bad_invoice.status_code == 400

    bad_payment = client_for(super_admin).post(
        '/api/v1/settings/payment_methods/save/',
        {'version': 1, 'values': {'methods': 'Cash,Bitcoin'}}, format='json',
        HTTP_IDEMPOTENCY_KEY='settings-invalid-payment-test',
    )
    assert bad_payment.status_code == 400

    valid_payment = client_for(super_admin).post(
        '/api/v1/settings/payment_methods/save/',
        {'version': 1, 'values': {'methods': 'Cash, UPI'}}, format='json',
        HTTP_IDEMPOTENCY_KEY='settings-valid-payment-test',
    )
    assert valid_payment.status_code == 200
    assert CompanySetting.objects.get(section='payment_methods').values['methods'] == ['Cash', 'UPI']


@pytest.mark.django_db
def test_settings_changes_do_not_mutate_issued_bill_snapshot(data, client_for):
    book = data['books'][0]
    super_admin = data['users'][Role.SUPER]
    customer = data['leads'][0].customer
    bill = Bill.objects.create(
        franchise=book, creator=super_admin, customer=customer,
        kind='collection', state='issued', subtotal=10, total=10,
        snapshot={'company_name': 'Original Company', 'invoice_prefix': 'OLD'},
    )
    response = client_for(super_admin).post(
        '/api/v1/settings/company/save/',
        {'version': 1, 'values': {'company_name': 'New Company'}}, format='json',
        HTTP_IDEMPOTENCY_KEY='settings-history-test',
    )
    assert response.status_code == 200
    bill.refresh_from_db()
    assert bill.snapshot == {'company_name': 'Original Company', 'invoice_prefix': 'OLD'}
