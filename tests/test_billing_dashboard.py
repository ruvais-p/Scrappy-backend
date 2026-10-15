from decimal import Decimal
import pytest
from core.models import Role, Bill, Customer, Rate
from core.services import billing
from core.services.common import Conflict
from test_workflows import issue_pickup, post

@pytest.mark.django_db
def test_dashboard_billing_metrics_are_scoped_and_drafts_excluded_from_financial_totals(data,client_for):
    actor=data['users'][Role.APP]
    customer=Customer.objects.filter(franchise=actor.franchise).first()
    draft=billing.save_draft(actor,{'franchise':str(actor.franchise_id),'customer':str(customer.pk),'kind':'collection','lines':[{'product':str(data['products'][0].pk),'quantity':'2'}]})
    client=client_for(actor);dashboard=client.get('/api/v1/dashboard/')
    assert dashboard.status_code==200
    labels={m['label']:m['value'] for m in dashboard.data['metrics']}
    assert labels['Drafts in progress']>=1
    assert labels['Pay suppliers · outstanding']=='0'
    assert all(row['state']=='draft' or row['state'] in ('issued','completed','cancellation_requested') for row in dashboard.data['bills'])
    assert client.get('/api/v1/bills/?issued=1').data['count']==0

@pytest.mark.django_db
def test_draft_resume_keeps_server_rates_and_rejects_issued_edits(data,client_for):
    bill=issue_pickup(data);client=client_for(data['users'][Role.COLLECTOR])
    assert post(client,f'pickups/{bill.pk}/save/',{'version':bill.version,'lines':bill.lines,'customer':str(bill.customer_id)}).status_code==409
    assert client.get(f'/api/v1/pickups/{bill.pk}/').data['number']==bill.number

@pytest.mark.django_db
def test_draft_issue_requires_review_when_rate_changes(data):
    actor=data['users'][Role.APP];book=actor.franchise;customer=Customer.objects.filter(franchise=book).first();product=data['products'][0]
    draft=billing.save_draft(actor,{'franchise':str(book.id),'customer':str(customer.id),'kind':'collection','lines':[{'product':str(product.id),'quantity':'2'}]})
    Rate.objects.create(product=product,franchise=book,kind='collection',amount=Decimal('99.00'),actor=data['users'][Role.HQ],reason='Updated price')
    with pytest.raises(Conflict) as error:
        billing.issue(actor,draft,{'version':draft.version})
    assert 'Rates or products changed' in str(error.value)
