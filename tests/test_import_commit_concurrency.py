import uuid
from concurrent.futures import ThreadPoolExecutor
from django.db import close_old_connections,transaction
from django.utils import timezone
from django.core.files.uploadedfile import SimpleUploadedFile
import csv,io,pytest
from core.models import Lead,Role
from core.services import imports
from core.services.common import Conflict
from test_imports import upload

@pytest.mark.django_db(transaction=True)
def test_same_batch_commit_is_idempotent_under_concurrency(data):
    actor=data['users'][Role.HQ];batch=imports.preview(actor,upload(data));before=Lead.objects.count()
    def run(_):
        close_old_connections()
        try:
            with transaction.atomic(): imports.commit(actor,batch)
            return True
        finally: close_old_connections()
    with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(run,[1,2]))
    assert results==[True,True]
    assert Lead.objects.count()==before+1
    batch.refresh_from_db();assert batch.committed and batch.rows[0]['status']=='created'
