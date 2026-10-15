import csv
import io
import uuid
from concurrent.futures import ThreadPoolExecutor
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction,close_old_connections
from django.utils import timezone
from django.core.exceptions import ValidationError
from core.models import Role,Lead,ImportBatch
from core.fixtures import synthetic_records
from core.services import imports
from core.services.common import Conflict

def upload(data):
    stream=io.StringIO();writer=csv.writer(stream);writer.writerow(imports.COLUMNS)
    writer.writerow(['Example, household','9000000099','Example address','North','','NTH','collector@example.test',timezone.localdate().strftime('%d-%m-%Y'),'','Paper',0,0])
    return SimpleUploadedFile('leads.csv',stream.getvalue().encode('utf-8-sig'))

@pytest.mark.django_db
def test_import_zero_coordinates_and_retry(data):
    actor=data['users'][Role.HQ];batch=imports.preview(actor,upload(data))
    assert batch.rows[0]['errors']==[]
    assert batch.rows[0]['data']['latitude']=='0'
    with transaction.atomic(): imports.commit(actor,batch)
    count=Lead.objects.count()
    with transaction.atomic(): imports.commit(actor,batch)
    assert Lead.objects.count()==count
    lead=Lead.objects.get(pk=batch.rows[0]['lead'])
    assert lead.latitude==0 and lead.longitude==0 and lead.pickup_time.hour==9

@pytest.mark.django_db
def test_corrupt_workbook_returns_validation(data):
    with pytest.raises(ValidationError):
        imports.preview(data['users'][Role.HQ],SimpleUploadedFile('bad.xlsx',b'not a workbook'))

@pytest.mark.django_db(transaction=True)
def test_identical_imports_serialize_across_accounts():
    data=synthetic_records(policies=True);actor=data['users'][Role.HQ]
    batches=[imports.preview(actor,upload(data)) for _ in range(2)]
    before=Lead.objects.count()
    def run(pk):
        close_old_connections()
        try:
            with transaction.atomic():
                imports.commit(actor,ImportBatch.objects.get(pk=pk))
            return True
        except Conflict:return False
        finally:close_old_connections()
    with ThreadPoolExecutor(max_workers=2) as pool: result=list(pool.map(run,[b.pk for b in batches]))
    assert sum(result)==1 and Lead.objects.count()==before+1
