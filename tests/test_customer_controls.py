import pytest
from core.models import Role

@pytest.mark.django_db
def test_customer_phone_is_normalized_per_franchise_and_cross_book_isolated(data,client_for):
    actor=data['users'][Role.SUPER];first=data['books'][0];second=data['books'][1]
    payload={'franchise':str(first.id),'name':'Normalized contact','phone':'+91 90000-00123'}
    created=client_for(actor).post('/api/v1/customers/',payload,format='json',HTTP_IDEMPOTENCY_KEY='customer-normalized-one')
    assert created.status_code==201 and created.data['phone']=='919000000123'
    duplicate={**payload,'name':'Duplicate contact','phone':'919000000123'}
    assert client_for(actor).post('/api/v1/customers/',duplicate,format='json',HTTP_IDEMPOTENCY_KEY='customer-normalized-two').status_code==409
    cross={**payload,'franchise':str(second.id),'name':'Other book contact','phone':'919000000123'}
    assert client_for(actor).post('/api/v1/customers/',cross,format='json',HTTP_IDEMPOTENCY_KEY='customer-normalized-three').status_code==201
    assert client_for(data['users'][Role.APP]).get('/api/v1/customers/',{'search':'Other book contact'}).data['count']==0
