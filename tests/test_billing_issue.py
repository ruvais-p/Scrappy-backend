import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.db import close_old_connections

from core.models import Bill, Customer, Role
from core.services import billing
from core.services.common import command


@pytest.mark.django_db(transaction=True)
def test_concurrent_issue_allocates_distinct_invoice_numbers(data):
    book = data['books'][0]
    product = data['products'][0]
    customer = Customer.objects.filter(franchise=book).first()
    actors = [data['users'][Role.APP], data['users'][Role.ADMIN]]
    drafts = [billing.save_draft(actor, {
        'franchise': str(book.pk), 'customer': str(customer.pk), 'kind': 'collection',
        'lines': [{'product': str(product.pk), 'quantity': '1'}],
    }) for actor in actors]
    barrier = Barrier(2)

    def issue_one(index):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            actor = actors[index]
            bill = Bill.objects.get(pk=drafts[index].pk)
            return command(actor, str(uuid.uuid4()), 'bill.issue', {'version': bill.version},
                           lambda current: {'id': str(billing.issue(current, bill, {'version': bill.version}).pk)})
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(issue_one, range(2)))
    numbers = list(Bill.objects.filter(pk__in=[draft.pk for draft in drafts]).values_list('number', flat=True))
    assert all(result['id'] for result in results)
    assert all(number for number in numbers) and len(set(numbers)) == 2
