import csv
import io

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.exceptions import PermissionDenied, ValidationError

from core.models import AuditEvent, District, LSGD, Role
from core.services import lsgd_imports
from core.services.common import Conflict


def upload(rows, headers=None):
    stream = io.StringIO(newline='')
    writer = csv.writer(stream)
    writer.writerow(headers or lsgd_imports.COLUMNS)
    writer.writerows(rows)
    return SimpleUploadedFile('lsgds.csv', stream.getvalue().encode('utf-8-sig'))


def row(name='CSV Test Panchayat', kind='grama_panchayat', district='CSV Test District', address='Synthetic test record', latitude='10.1200000', longitude='76.3000000'):
    return [name, kind, district, address, latitude, longitude]


@pytest.mark.django_db
def test_hq_previews_and_commits_csv_lsgds_atomically(data):
    actor = data['users'][Role.DIGESTER_MANAGER]
    batch = lsgd_imports.preview(actor, upload([row(), row('CSV Test Municipality', 'municipality')]))
    assert all(not item['errors'] for item in batch.rows)

    result = lsgd_imports.commit(actor, batch.pk)
    assert result['committed'] is True
    created = list(LSGD.objects.filter(created_by=actor, district__name='CSV Test District'))
    assert len(created) == 2
    assert all(item.assigned_manager_id is None and item.assigned_tso_id is None for item in created)
    assert District.objects.filter(name='CSV Test District').exists()
    assert AuditEvent.objects.filter(actor=actor, action='lsgd.created', details__source='csv_import').count() == 2
    assert lsgd_imports.commit(actor, batch.pk)['committed'] is True


@pytest.mark.django_db
@pytest.mark.parametrize('bad_row', [
    row('Missing coordinate', latitude='10.1', longitude=''),
    row('Outside Kerala', latitude='7.9', longitude='76.3'),
    row('Unsupported type', kind='local_body'),
    row('=FORMULA()'),
])
def test_invalid_rows_are_reported_and_never_partially_imported(data, bad_row):
    actor = data['users'][Role.DIGESTER_MANAGER]
    batch = lsgd_imports.preview(actor, upload([row(), bad_row]))
    assert batch.rows[0]['errors'] == []
    assert batch.rows[1]['errors']
    with pytest.raises(ValidationError, match='Correct all invalid rows'):
        lsgd_imports.commit(actor, batch.pk)
    assert not LSGD.objects.filter(name='CSV Test Panchayat').exists()


@pytest.mark.django_db
def test_import_rejects_duplicate_rows_and_reimported_file(data):
    actor = data['users'][Role.DIGESTER_MANAGER]
    duplicated = upload([row(), row()])
    batch = lsgd_imports.preview(actor, duplicated)
    assert any(batch.rows[1]['errors'])
    with pytest.raises(ValidationError, match='Correct all invalid rows'):
        lsgd_imports.commit(actor, batch.pk)

    same_file = upload([row('CSV Test Another Panchayat')])
    committed = lsgd_imports.preview(actor, same_file)
    lsgd_imports.commit(actor, committed.pk)
    LSGD.objects.filter(created_by=actor, name='CSV Test Another Panchayat').delete()
    retry = lsgd_imports.preview(actor, upload([row('CSV Test Another Panchayat')]))
    with pytest.raises(Conflict, match='already been imported'):
        lsgd_imports.commit(actor, retry.pk)


@pytest.mark.django_db
def test_only_digester_hq_manager_can_preview_lsgd_csv(data):
    for role in (Role.ZONAL_MANAGER, Role.TSO):
        with pytest.raises(PermissionDenied):
            lsgd_imports.preview(data['users'][role], upload([row()]))


@pytest.mark.django_db
def test_lsgd_csv_api_previews_and_commits_for_hq_only(data, client_for):
    manager = data['users'][Role.DIGESTER_MANAGER]
    client = client_for(manager)
    response = client.post('/api/v1/digester/lsgd-imports/', {'file': upload([row('API CSV Panchayat')])}, format='multipart')
    assert response.status_code == 201
    batch_id = response.data['id']
    committed = client.post(f'/api/v1/digester/lsgd-imports/{batch_id}/commit/', {}, format='json')
    assert committed.status_code == 200
    assert committed.data['committed'] is True
    assert LSGD.objects.filter(name='API CSV Panchayat', created_by=manager).exists()

    denied = client_for(data['users'][Role.ZONAL_MANAGER]).post(
        '/api/v1/digester/lsgd-imports/', {'file': upload([row('Denied CSV Panchayat')])}, format='multipart'
    )
    assert denied.status_code == 403
