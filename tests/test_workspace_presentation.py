from decimal import Decimal
import pytest
from django.utils import timezone
from core.models import Bill, Payment, Role, Customer, Rate
from core.queries import filter_records
from core.jobs import export_rows

@pytest.mark.django_db
def test_filters_summaries_and_export_cover_full_result(data,client_for):
    user=data['users'][Role.SUPER];book=data['books'][0];customer=Customer.objects.create(franchise=book,name='UI fixture',phone='7000111222')
    for i in range(55):
        Bill.objects.create(franchise=book,creator=user,customer=customer,kind='sale',state='issued',total=100,settled=25,issued_at=timezone.now())
    filters={'search':'7000111222','direction':'incoming','payment_state':'partial','issued':'1'}
    result=client_for(user).get('/api/v1/bills/',{**filters,'presentation':'1'}).data
    assert result['count']==55 and len(result['results'])==50
    assert Decimal(result['summary']['incoming_total'])==5500
    assert result['results'][0]['payment_state']=='partial'
    assert len(export_rows(user,{'feature':'bills','filters':filters}))==55

@pytest.mark.django_db
def test_dashboard_and_customer_aggregate_exclude_drafts_and_cancelled(data,client_for):
    user=data['users'][Role.SUPER];book=data['books'][0];customer=data['leads'][0].customer
    for state in ('draft','cancelled','issued'):
        Bill.objects.create(franchise=book,creator=user,customer=customer,kind='sale',state=state,total=100,issued_at=timezone.now())
    client=client_for(user)
    assert Decimal(client.get('/api/v1/dashboard/').data['summary']['today'])==100
    row=next(r for r in client.get('/api/v1/customers/?presentation=1').data['results'] if r['id']==str(customer.pk))
    assert row['transaction_count']==1 and Decimal(row['billing_total'])==100

@pytest.mark.django_db
def test_search_cannot_expose_other_franchise(data,client_for):
    user=data['users'][Role.APP];other=next(b for b in data['books'] if b.id!=user.franchise_id)
    Customer.objects.create(franchise=other,name='Hidden search example',phone='7000000000')
    assert client_for(user).get('/api/v1/search/?q=Hidden').data['results']==[]
    assert client_for(data['users'][Role.SUPER]).get('/api/v1/search/?q=Hidden').data['results']

@pytest.mark.django_db
def test_payment_summary_is_not_multiplied(data,client_for):
    user=data['users'][Role.SUPER];book=data['books'][0];customer=data['leads'][0].customer
    bill=Bill.objects.create(franchise=book,creator=user,customer=customer,kind='sale',state='issued',total=100,settled=50,issued_at=timezone.now())
    for _ in range(2): Payment.objects.create(bill=bill,actor=user,amount=25,method='Cash',paid_at=timezone.now())
    summary=client_for(user).get('/api/v1/payments/?presentation=1').data['summary']
    assert Decimal(summary['received'])==50 and Decimal(summary['collection_ratio'])==50
    assert client_for(user).get('/api/v1/bills/?direction=invalid').status_code==400

@pytest.mark.django_db
def test_pricing_matrix_matches_override(data,client_for):
    user=data['users'][Role.SUPER];product=data['products'][0];book=data['books'][0]
    Rate.objects.create(product=product,franchise=book,kind='collection',amount=99,actor=user)
    rows=client_for(user).get('/api/v1/pricing/').data['results']
    row=next(r for r in rows if r['id']==str(product.pk))
    cell=next(c for c in row['cells'] if c['franchise']==str(book.pk))
    assert cell['override'] and Decimal(cell['amount'])==99

@pytest.mark.django_db
@pytest.mark.parametrize('filters',[{'staff':'invalid'},{'category':'invalid'},{'franchise_type':'invalid'},{'active':'maybe'},{'from':'2026-10-20','to':'2026-10-01'}])
def test_invalid_filters_return_validation_error(data,client_for,filters):
    assert client_for(data['users'][Role.SUPER]).get('/api/v1/bills/',filters).status_code==400

@pytest.mark.django_db
def test_visible_related_filters_and_export_agree(data,client_for):
    user=data['users'][Role.SUPER];book=data['books'][0];product=data['products'][0];customer=data['leads'][0].customer
    bill=Bill.objects.create(franchise=book,creator=user,customer=customer,kind='collection',state='issued',total=90,lines=[{'product':str(product.pk),'name':product.name}],issued_at=timezone.now())
    filters={'product':str(product.pk),'category':str(product.category_id),'staff':str(user.pk),'franchise_type':book.kind,'direction':'outgoing','payment_state':'due'}
    result=client_for(user).get('/api/v1/bills/',{**filters,'presentation':'1'}).data
    assert result['count']==1 and result['results'][0]['id']==str(bill.pk)
    assert len(export_rows(user,{'feature':'bills','filters':filters}))==1
    filters['staff']=str(data['users'][Role.ADMIN].pk)
    assert client_for(user).get('/api/v1/bills/',filters).data['count']==0

@pytest.mark.django_db
def test_local_day_boundary_and_financial_drilldown(data,client_for):
    from datetime import datetime, timezone as tz
    from unittest.mock import patch
    user=data['users'][Role.SUPER];book=data['books'][0];customer=data['leads'][0].customer
    for instant in (datetime(2026,10,15,18,29,tzinfo=tz.utc),datetime(2026,10,15,18,31,tzinfo=tz.utc)):
        Bill.objects.create(franchise=book,creator=user,customer=customer,kind='collection',state='issued',total=100,issued_at=instant)
    Bill.objects.create(franchise=book,creator=user,customer=customer,kind='sale',state='cancelled',total=900,issued_at=datetime(2026,10,16,tzinfo=tz.utc))
    with patch('core.presentation.timezone.localdate',return_value=datetime(2026,10,16).date()):
        summary=client_for(user).get('/api/v1/dashboard/').data['summary']
    assert Decimal(summary['today'])==100 and Decimal(summary['outgoing_outstanding'])==200
    result=client_for(user).get('/api/v1/bills/',{'issued':'1','financial_only':'1','from':'2026-10-16','to':'2026-10-16'}).data
    assert result['count']==1 and Decimal(result['summary']['outgoing_total'])==100

@pytest.mark.django_db
def test_empty_cohort_and_payment_direction(data,client_for):
    user=data['users'][Role.SUPER];book=data['books'][0];customer=data['leads'][0].customer
    bill=Bill.objects.create(franchise=book,creator=user,customer=customer,kind='collection',state='issued',total=100,settled=25,issued_at=timezone.now())
    Payment.objects.create(bill=bill,actor=user,amount=25,method='Cash',paid_at=timezone.now())
    summary=client_for(user).get('/api/v1/payments/?presentation=1').data['summary']
    assert summary['collection_ratio'] is None and Decimal(summary['paid_out'])==25 and Decimal(summary['received'])==0

@pytest.mark.django_db
def test_options_staff_and_categories_do_not_broaden_access(data,client_for):
    user=data['users'][Role.DRIVER];book=user.franchise;customer=Customer.objects.create(franchise=book,name='Own customer')
    Bill.objects.create(franchise=book,creator=user,customer=customer,kind='collection')
    options=client_for(user).get('/api/v1/options/').data
    assert options['staff']==[{'id':str(user.pk),'name':user.get_full_name() or user.email}]
    assert options['categories']
    assert client_for(user).get('/api/v1/products/').status_code==403
    assert all(str(book.pk) in p['franchises'] for p in options['products'])

@pytest.mark.django_db
def test_catalogue_edit_and_assignment_restrictions(data,client_for):
    import uuid
    from core.models import Product
    product=data['products'][0];client=client_for(data['users'][Role.SUPER]);before=Product.objects.count()
    result=client.post(f'/api/v1/products/{product.pk}/edit/',{'name':'Renamed material','version':product.version},format='json',HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()))
    assert result.status_code==200 and Product.objects.count()==before
    product.refresh_from_db();assert product.name=='Renamed material'
    denied=client_for(data['users'][Role.ADMIN]).post(f'/api/v1/products/{product.pk}/edit/',{'name':'Denied','version':product.version},format='json',HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()))
    assert denied.status_code==403
    other=next(b for b in data['books'] if b.kind=='digester')
    assert client.post(f'/api/v1/products/{product.pk}/assign/',{'franchises':[str(other.pk)],'version':product.version},format='json',HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4())).status_code==400

@pytest.mark.django_db
def test_search_bounded_identifiers_and_privileged_team_accounts(data,client_for):
    user=data['users'][Role.SUPER];client=client_for(user)
    for i in range(12): Customer.objects.create(franchise=data['books'][0],name=f'Search match {i}',phone=f'71234567{i:02d}')
    assert len(client.get('/api/v1/search/?q=71234567').data['results'])==8
    customer=Customer.objects.filter(name='Search match 0').get()
    assert client.get('/api/v1/search/',{'q':str(customer.pk)}).data['results'][0]['id']==str(customer.pk)
    team=client.get('/api/v1/team/').data
    assert not any(r['role'] in (Role.SUPER,Role.ADMIN,Role.HQ) for r in team['results'])
    assert len(team['roles'])==9
    assert client.get('/api/v1/team/?active=unknown').status_code==400


@pytest.mark.django_db
def test_rate_history_search_and_catalogue_status_filters(data,client_for):
    client=client_for(data['users'][Role.SUPER]);product=data['products'][0]
    matched=client.get('/api/v1/rates/',{'search':product.name}).data
    assert matched['count'] and all(str(row['product'])==str(product.pk) for row in matched['results'])
    assert client.get('/api/v1/rates/?search=nonexistent-material').data['count']==0
    product.active=False;product.save()
    inactive=client.get('/api/v1/products/',{'active':'false','category':str(product.category_id)}).data
    assert inactive['count']==1 and inactive['results'][0]['id']==str(product.pk)


@pytest.mark.django_db
def test_earnings_projection_preserves_selected_franchise_with_no_entries(data,client_for):
    book=next(b for b in data['books'] if b.kind=='digester')
    response=client_for(data['users'][Role.SUPER]).get('/api/v1/payouts/',{'franchise':str(book.pk)}).data
    assert [r['id'] for r in response['franchises']]==[str(book.pk)]
    assert Decimal(response['summary']['balance'])==Decimal(response['balance'])==0


@pytest.mark.django_db
def test_franchise_staff_count_uses_delegated_account_scope(data,client_for):
    user=data['users'][Role.APP]
    client=client_for(user)
    team=client.get('/api/v1/team/?active=true').data['results']
    row=client.get('/api/v1/franchises/?presentation=1').data['results'][0]
    assert row['staff_count']==len(team)
