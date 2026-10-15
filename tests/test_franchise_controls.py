import pytest
from core.models import Bill,Role

@pytest.mark.django_db
def test_hq_can_edit_ordinary_franchise_and_restricted_suspension_is_gated(data,client_for):
    book=data['books'][0];hq=data['users'][Role.HQ];admin=data['users'][Role.ADMIN];original=book.name
    edit=client_for(hq).post(f'/api/v1/franchises/{book.id}/edit/',{'version':book.version,'name':'North Yard Updated'},format='json',HTTP_IDEMPOTENCY_KEY='franchise-edit-test')
    assert edit.status_code==200
    book.refresh_from_db();assert book.name=='North Yard Updated'
    assert client_for(admin).post(f'/api/v1/franchises/{book.id}/suspend/',{'version':book.version},format='json',HTTP_IDEMPOTENCY_KEY='admin-suspend-test').status_code==403
    assert client_for(hq).post(f'/api/v1/franchises/{book.id}/suspend/',{'version':book.version},format='json',HTTP_IDEMPOTENCY_KEY='hq-suspend-test').status_code==403

@pytest.mark.django_db
def test_issued_bill_keeps_historical_franchise_identity_after_profile_edit(data,client_for):
    book=data['books'][0];actor=data['users'][Role.SUPER];customer=data['leads'][0].customer
    bill=Bill.objects.create(franchise=book,creator=actor,customer=customer,kind='collection',state='issued',subtotal=10,total=10,snapshot={'book':book.name,'customer':{'name':customer.name}})
    old=book.name
    response=client_for(actor).post(f'/api/v1/franchises/{book.id}/edit/',{'version':book.version,'name':'Renamed Yard'},format='json',HTTP_IDEMPOTENCY_KEY='franchise-history-test')
    assert response.status_code==200
    bill.refresh_from_db();assert bill.snapshot['book']==old
