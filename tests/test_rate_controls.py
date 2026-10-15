from decimal import Decimal
import pytest
from django.core.exceptions import ValidationError
from core.models import Customer,Rate,Role
from core.services.billing import resolved_rate

@pytest.mark.django_db
def test_rate_override_precedes_default_and_history_is_append_only(data):
    product=data['products'][0];hq=data['users'][Role.HQ];book=data['books'][0]
    default=Rate.objects.create(product=product,kind='collection',amount=Decimal('31.00'),actor=hq)
    assert resolved_rate(product,book,'collection')==Decimal('31.00')
    override=Rate.objects.create(product=product,franchise=book,kind='collection',amount=Decimal('37.00'),actor=hq,reason='Book agreement')
    assert resolved_rate(product,book,'collection')==Decimal('37.00')
    default.refresh_from_db();override.refresh_from_db()
    assert default.amount==Decimal('31.00') and override.reason=='Book agreement'

@pytest.mark.django_db
def test_missing_rate_type_blocks_draft_instead_of_substituting_collection(data):
    from core.services import billing
    actor=data['users'][Role.APP];book=actor.franchise;customer=Customer.objects.filter(franchise=book).first();product=data['products'][0]
    Rate.objects.filter(product=product,franchise=book,kind='b2b').delete()
    with pytest.raises(ValidationError) as error:
        billing.save_draft(actor,{'franchise':str(book.id),'customer':str(customer.id),'kind':'b2b','lines':[{'product':str(product.id),'quantity':'1'}]})
    assert 'No applicable b2b rate' in str(error.value)
