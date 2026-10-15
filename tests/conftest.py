import time
import pytest
from rest_framework.test import APIClient
from core.fixtures import synthetic_records
@pytest.fixture
def data(db): return synthetic_records(policies=True)
@pytest.fixture
def client_for():
    def make(user,csrf=False):
        c=APIClient(enforce_csrf_checks=csrf);c.force_login(user)
        session=c.session;session.update({'permission_version':user.permission_version,'signed_in_at':time.time(),'last_activity':time.time()});session.save()
        return c
    return make
