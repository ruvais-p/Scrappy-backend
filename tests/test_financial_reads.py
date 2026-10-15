from decimal import Decimal
import pytest
from core.models import Bill,Payment,Role,Customer

@pytest.mark.django_db
def test_totals_keep_directions_separate(data,client_for):
    actor=data['users'][Role.SUPER];book=data['books'][0];customer=data['leads'][0].customer
    for kind,total,settled in [('collection',100,20),('sale',200,50)]:
        Bill.objects.create(franchise=book,creator=actor,customer=customer,kind=kind,state='issued',subtotal=total,total=total,settled=settled)
    client=client_for(actor);response=client.get('/api/v1/bills/')
    assert response.status_code==200
    assert Decimal(response.data['summary']['outgoing_outstanding'])==80
    assert Decimal(response.data['summary']['incoming_outstanding'])==150
    assert 'outstanding' not in response.data['summary']
    assert {r['direction'] for r in response.data['results']}=={'incoming','outgoing'}

@pytest.mark.django_db
def test_payment_book_filter_uses_related_bill(data,client_for):
    actor=data['users'][Role.SUPER]
    for i,book in enumerate(data['books'][:2]):
        customer=Customer.objects.create(franchise=book,name='Filter example',phone='9000000098')
        bill=Bill.objects.create(franchise=book,creator=actor,customer=customer,kind='sale',state='issued',subtotal=100,total=100)
        from django.utils import timezone
        Payment.objects.create(bill=bill,actor=actor,amount=10,method='Cash',paid_at=timezone.now())
    r=client_for(actor).get('/api/v1/payments/',{'franchise':str(data['books'][0].pk)})
    assert r.status_code==200 and r.data['count']==1
    row=r.data['results'][0]
    assert row['direction']=='incoming'
    assert row['bill_number']==bill.number
    assert row['customer_name']=='Filter example'
    assert Decimal(row['bill_outstanding'])==100

@pytest.mark.django_db
def test_multi_line_and_multi_payment_reads_do_not_double_count(data,client_for):
    actor=data['users'][Role.SUPER];book=data['books'][0]
    customer=Customer.objects.create(franchise=book,name='Join-safe customer',phone='9000000199')
    bill=Bill.objects.create(franchise=book,creator=actor,customer=customer,kind='sale',state='issued',subtotal=300,total=300,settled=75,lines=[{'product':'a','amount':'150'},{'product':'b','amount':'150'}])
    from django.utils import timezone
    Payment.objects.create(bill=bill,actor=actor,amount=25,method='UPI',paid_at=timezone.now())
    Payment.objects.create(bill=bill,actor=actor,amount=50,method='Cash',paid_at=timezone.now())
    client=client_for(actor)
    bills=client.get('/api/v1/bills/').data
    assert Decimal(bills['summary']['incoming_total'])==300
    assert Decimal(bills['summary']['incoming_settled'])==75
    assert Decimal(bills['summary']['incoming_outstanding'])==225
    payments=client.get('/api/v1/payments/').data
    rows=[row for row in payments['results'] if str(row['bill'])==str(bill.pk)]
    assert len(rows)==2 and all(Decimal(row['bill_outstanding'])==225 for row in rows)
