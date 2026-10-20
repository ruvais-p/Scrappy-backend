import csv
import hashlib
import io
from decimal import Decimal, InvalidOperation
from itertools import islice

from django.db import IntegrityError, connection, transaction
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from core.models import AuditEvent, District, ImportBatch, LSGD, Role
from core.policy import require
from .common import Conflict

COLUMNS = ['name', 'kind', 'district', 'address', 'latitude', 'longitude']
MAX_FILE_SIZE = 5 * 1024 * 1024
MAX_ROWS = 5000
MAX_CELL_LENGTH = 5000
VALID_KINDS = {value for value, _ in LSGD.TYPES}


def _text(value):
    return '' if value is None else str(value).strip()


def _can_import(actor):
    require(actor, 'lsgd.create')
    if actor.role != Role.DIGESTER_MANAGER:
        raise PermissionDenied('Only Digester HQ Managers can import LSGDs.')


def preview(actor, upload):
    _can_import(actor)
    if not upload.name.lower().endswith('.csv'):
        raise ValidationError('Upload a CSV file.')
    if upload.size > MAX_FILE_SIZE:
        raise ValidationError('File limit is 5 MB.')
    raw = upload.read()
    try:
        reader = csv.reader(io.StringIO(raw.decode('utf-8-sig'), newline=''))
        headers = next(reader)
        records = list(islice(reader, MAX_ROWS + 1))
    except (UnicodeError, csv.Error, StopIteration):
        raise ValidationError('Invalid UTF-8 CSV.')
    if headers != COLUMNS or len(records) > MAX_ROWS:
        raise ValidationError('Use the exact LSGD template columns and at most 5,000 rows.')
    if not records:
        raise ValidationError('The CSV must contain at least one LSGD row.')

    rows = []
    seen = set()
    for number, values in enumerate(records, 2):
        data = {key: _text(values[i]) if i < len(values) else '' for i, key in enumerate(COLUMNS)}
        errors = []
        if len(values) != len(COLUMNS) or any(len(_text(value)) > MAX_CELL_LENGTH for value in values):
            errors.append('Invalid column count or cell length.')
        if any(_text(value).lstrip().startswith(('=', '+', '-', '@')) for value in values):
            errors.append('Formula-like values are not permitted.')
        if not data['name'] or len(data['name']) > 180:
            errors.append('LSGD name is required and must be at most 180 characters.')
        if data['kind'] not in VALID_KINDS:
            errors.append('Choose a supported LSGD type.')
        if not data['district'] or len(data['district']) > 80:
            errors.append('District is required and must be at most 80 characters.')
        if len(data['address']) > MAX_CELL_LENGTH:
            errors.append('Address is too long.')

        try:
            if bool(data['latitude']) != bool(data['longitude']):
                raise ValueError()
            latitude = Decimal(data['latitude']) if data['latitude'] else None
            longitude = Decimal(data['longitude']) if data['longitude'] else None
            if latitude is not None and (
                not latitude.is_finite() or not longitude.is_finite()
                or not Decimal('8.0') <= latitude <= Decimal('13.0')
                or not Decimal('74.0') <= longitude <= Decimal('78.5')
            ):
                raise ValueError()
            if latitude is not None and (len(latitude.as_tuple().digits) > 10 or max(0, -latitude.as_tuple().exponent) > 7):
                raise ValueError()
            if longitude is not None and (len(longitude.as_tuple().digits) > 10 or max(0, -longitude.as_tuple().exponent) > 7):
                raise ValueError()
        except (InvalidOperation, ValueError, TypeError):
            errors.append('Enter a valid Kerala coordinate pair or leave both coordinates blank.')

        key = (data['district'].casefold(), data['name'].casefold())
        if key in seen:
            errors.append('Duplicate LSGD in this file.')
        seen.add(key)
        if data['name'] and data['district'] and LSGD.objects.filter(
            name__iexact=data['name'], district__name__iexact=data['district']
        ).exists():
            errors.append('An LSGD with this name already exists in the district.')
        rows.append({'row': number, 'data': data, 'errors': errors, 'lsgd': None})

    return ImportBatch.objects.create(
        actor=actor,
        digest=hashlib.sha256(raw).hexdigest(),
        rows=rows,
    )


def payload(batch):
    return {
        'id': str(batch.pk),
        'rows': batch.rows,
        'committed': batch.committed,
        'count': len(batch.rows),
    }


@transaction.atomic
def commit(actor, batch_id):
    _can_import(actor)
    try:
        batch = ImportBatch.objects.select_for_update().get(pk=batch_id, actor=actor)
    except (ImportBatch.DoesNotExist, ValueError, TypeError):
        raise NotFound()
    if batch.committed:
        return payload(batch)
    if any(row.get('errors') for row in batch.rows):
        raise ValidationError('Correct all invalid rows before importing.')

    with connection.cursor() as cursor:
        cursor.execute('SELECT pg_advisory_xact_lock(%s)', [int(batch.digest[:16], 16) % (2**63)])
    if ImportBatch.objects.filter(digest=batch.digest, committed=True).exclude(pk=batch.pk).exists():
        raise Conflict('This CSV has already been imported.')

    try:
        with transaction.atomic():
            for row in batch.rows:
                values = row['data']
                district, _ = District.objects.get_or_create(name=values['district'])
                lsgd = LSGD.objects.create(
                    name=values['name'],
                    kind=values['kind'],
                    district=district,
                    address=values['address'],
                    latitude=Decimal(values['latitude']) if values['latitude'] else None,
                    longitude=Decimal(values['longitude']) if values['longitude'] else None,
                    created_by=actor,
                )
                row['lsgd'] = str(lsgd.pk)
                row['status'] = 'created'
                row['message'] = 'Created; assign this LSGD to a Zonal Manager.'
                AuditEvent.objects.create(
                    actor=actor,
                    action='lsgd.created',
                    resource=str(lsgd.pk),
                    details={'district': district.name, 'name': lsgd.name, 'source': 'csv_import', 'import_batch': str(batch.pk)},
                )
            batch.rows = batch.rows
            batch.committed = True
            batch.save(update_fields=['rows', 'committed', 'updated_at'])
    except IntegrityError:
        raise Conflict('An LSGD in this file was created by another request. Refresh and preview the file again.')
    return payload(batch)
