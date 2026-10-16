import csv
import io
from datetime import datetime, time, timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.exceptions import ValidationError
from django.utils import timezone
from openpyxl import Workbook

from core.models import Role
from core.services import imports


def csv_upload(row, name='leads.csv'):
    stream=io.StringIO(newline='')
    writer=csv.writer(stream)
    writer.writerow(imports.COLUMNS)
    writer.writerow(row)
    return SimpleUploadedFile(name,stream.getvalue().encode('utf-8-sig'))


def valid_row(data):
    return ['Household, quoted','0001234567','12, Main Road','North','upi@id','NTH',
            'collector@example.test',timezone.localdate().strftime('%d-%m-%Y'),'11:30 AM','Paper, iron','','']


@pytest.mark.django_db
def test_csv_exact_width_and_bom_quotes_are_normalized(data):
    row=valid_row(data)
    batch=imports.preview(data['users'][Role.HQ],csv_upload(row))
    assert batch.rows[0]['errors']==[]
    assert batch.rows[0]['data']['customer_name']=='Household, quoted'
    assert batch.rows[0]['data']['phone']=='0001234567'
    assert batch.rows[0]['data']['items']=='Paper, iron'
    batch=imports.preview(data['users'][Role.HQ],csv_upload(row+['extra']))
    assert any('column count' in error for error in batch.rows[0]['errors'])


@pytest.mark.django_db
def test_xlsx_native_date_time_and_leads_sheet(data):
    workbook=Workbook();sheet=workbook.active;sheet.title='Leads'
    sheet.append(imports.COLUMNS)
    row=valid_row(data)
    row[7]=datetime.combine(timezone.localdate()+timedelta(days=1),time())
    row[8]=time(14,45)
    sheet.append(row)
    payload=io.BytesIO();workbook.save(payload)
    batch=imports.preview(data['users'][Role.HQ],SimpleUploadedFile('leads.xlsx',payload.getvalue()))
    assert batch.rows[0]['errors']==[]
    assert batch.rows[0]['data']['pickup_date']==(timezone.localdate()+timedelta(days=1)).isoformat()
    assert batch.rows[0]['data']['pickup_time']=='14:45:00'


@pytest.mark.django_db
@pytest.mark.parametrize('date_value',[None,'','not-a-date',
    (timezone.localdate()-timedelta(days=1)).strftime('%d-%m-%Y')])
def test_invalid_dates_are_row_errors(data,date_value):
    row=valid_row(data);row[7]=date_value
    batch=imports.preview(data['users'][Role.HQ],csv_upload(row))
    assert any('date today or later' in error for error in batch.rows[0]['errors'])


@pytest.mark.django_db
@pytest.mark.parametrize('coords',[("1",''),('91','0'),('0','181'),('NaN','0'),('=1','2')])
def test_coordinates_require_finite_paired_bounds(data,coords):
    row=valid_row(data);row[10],row[11]=coords
    batch=imports.preview(data['users'][Role.HQ],csv_upload(row))
    assert any('coordinate pair' in error or 'Formulas' in error for error in batch.rows[0]['errors'])


@pytest.mark.django_db
def test_formula_with_leading_whitespace_is_rejected(data):
    row=valid_row(data);row[2]='  =HYPERLINK("https://evil.example")'
    batch=imports.preview(data['users'][Role.HQ],csv_upload(row))
    assert any('Formulas' in error for error in batch.rows[0]['errors'])


@pytest.mark.django_db
def test_xlsx_requires_leads_sheet(data):
    workbook=Workbook();workbook.active.title='Other';workbook.active.append(imports.COLUMNS)
    payload=io.BytesIO();workbook.save(payload)
    with pytest.raises(ValidationError,match='Leads sheet'):
        imports.preview(data['users'][Role.HQ],SimpleUploadedFile('leads.xlsx',payload.getvalue()))
