import pytest
from core.models import Category,Product,Role

@pytest.mark.django_db
def test_product_assignment_respects_category_kind_and_selector_scope(data,client_for):
    actor=data['users'][Role.SUPER];product=data['products'][0]
    compatible=data['books'][0];digester=data['books'][2]
    assign=client_for(actor).post(f'/api/v1/products/{product.id}/assign/',{'version':product.version,'franchises':[str(compatible.id)]},format='json',HTTP_IDEMPOTENCY_KEY='product-compatible')
    assert assign.status_code==200
    product.refresh_from_db()
    incompatible=client_for(actor).post(f'/api/v1/products/{product.id}/assign/',{'version':product.version,'franchises':[str(digester.id)]},format='json',HTTP_IDEMPOTENCY_KEY='product-incompatible')
    assert incompatible.status_code==400
    assert not product.franchises.filter(pk=digester.pk).exists()

@pytest.mark.django_db
def test_service_product_keeps_stock_distinction_and_unit(data):
    category=Category.objects.create(name='Services',kind='scrap')
    service=Product.objects.create(name='Sorting service',category=category,unit='set',tracks_stock=False)
    assert service.tracks_stock is False and service.unit=='set'
