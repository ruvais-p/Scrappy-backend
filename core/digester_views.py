from decimal import Decimal, InvalidOperation
from datetime import date, timedelta
import hashlib
import math
import mimetypes
import uuid
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.db import models
from django.http import FileResponse
from django.utils import timezone
from rest_framework import serializers
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import NotFound, ValidationError, PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema

from .models import AuditEvent, ClaimEvent, DataRevision, District, ExpenseItem, ExpensePayment, ExpensePolicy, FieldDay, FieldFollowUp, FieldVisit, LSGD, LSGDContact, LSGDOpportunity, Role, User, VisitContact, VisitPlan
from .policy import lsgd_scope, require, policy, digester_team_scope
from .services.common import command, version
from .services import lsgd_imports


def documented(cls):
    for method in ('get','post'):
        if hasattr(cls,method):
            setattr(cls,method,extend_schema(tags=['Digester field sales'],**({'request':OpenApiTypes.OBJECT} if method=='post' else {}),responses={200:OpenApiTypes.OBJECT,201:OpenApiTypes.OBJECT})(getattr(cls,method)))
    return cls


class LSGDImportUploadSerializer(serializers.Serializer):
    file = serializers.FileField()


class LSGDImportResponseSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    rows = serializers.ListField(child=serializers.JSONField())
    committed = serializers.BooleanField()
    count = serializers.IntegerField()


def lsgd_payload(row):
    return {
        'id': str(row.pk), 'version': row.version, 'name': row.name, 'kind': row.kind,
        'district': str(row.district_id), 'district_name': row.district.name,
        'latitude': str(row.latitude) if row.latitude is not None else None,
        'longitude': str(row.longitude) if row.longitude is not None else None,
        'address': row.address, 'active': row.active,
        'assigned_manager': str(row.assigned_manager_id) if row.assigned_manager_id else None,
        'assigned_manager_name': row.assigned_manager.get_full_name() or row.assigned_manager.email if row.assigned_manager_id else None,
        'assigned_tso': str(row.assigned_tso_id) if row.assigned_tso_id else None,
        'assigned_tso_name': row.assigned_tso.get_full_name() or row.assigned_tso.email if row.assigned_tso_id else None,
    }


@documented
class LSGDView(APIView):
    def get(self, request, pk=None):
        qs = lsgd_scope(request.user, LSGD.objects.select_related('district', 'assigned_manager', 'assigned_tso'))
        if pk:
            row = qs.filter(pk=pk).first()
            if not row: raise NotFound()
            return Response(lsgd_payload(row))
        term = request.query_params.get('search', '').strip()[:160]
        if term: qs = qs.filter(Q(name__icontains=term) | Q(district__name__icontains=term))
        if request.query_params.get('district'): qs = qs.filter(district_id=request.query_params['district'])
        if request.query_params.get('active') in ('true', 'false'):
            qs = qs.filter(active=request.query_params['active'] == 'true')
        count = qs.count()
        try: page = max(1, int(request.query_params.get('page', 1)))
        except ValueError: raise ValidationError('Invalid page.')
        return Response({'count': count, 'page': page, 'page_size': 50, 'results': [lsgd_payload(row) for row in qs.order_by('district__name', 'name')[(page - 1) * 50:page * 50]]})

    def post(self, request, pk=None):
        require(request.user, 'lsgd.create')
        if request.user.role != Role.DIGESTER_MANAGER: raise PermissionDenied()
        data = request.data
        name = str(data.get('name', '')).strip()
        district_name = str(data.get('district', '')).strip()
        if not name or not district_name: raise ValidationError('LSGD name and district are required.')
        if len(name) > 180: raise ValidationError('LSGD name is too long.')
        kind = str(data.get('kind', ''))
        valid_kinds = dict(LSGD.TYPES)
        if kind not in valid_kinds: raise ValidationError({'kind': 'Choose a supported LSGD type.'})
        try:
            latitude = Decimal(str(data['latitude'])) if data.get('latitude') not in (None, '') else None
            longitude = Decimal(str(data['longitude'])) if data.get('longitude') not in (None, '') else None
        except (InvalidOperation, ValueError): raise ValidationError('Coordinates must be valid numbers.')
        if (latitude is None) != (longitude is None): raise ValidationError('Enter both coordinates or leave both blank.')
        if latitude is not None and (latitude < Decimal('8.0') or latitude > Decimal('13.0') or longitude < Decimal('74.0') or longitude > Decimal('78.5')):
            raise ValidationError('Coordinates must be within Kerala.')

        def run(actor):
            require(actor, 'lsgd.create')
            district, _ = District.objects.get_or_create(name=district_name)
            row = LSGD.objects.create(name=name, kind=kind, district=district, address=str(data.get('address', ''))[:5000], latitude=latitude, longitude=longitude, created_by=actor)
            AuditEvent.objects.create(actor=actor, action='lsgd.created', resource=str(row.pk), details={'district': district.name, 'name': row.name})
            return lsgd_payload(row)
        return Response(command(request.user, request.headers.get('Idempotency-Key'), 'lsgd.create', data, run), status=201)


class LSGDImportView(APIView):
    @extend_schema(tags=['Digester field sales'], request=LSGDImportUploadSerializer, responses={201: LSGDImportResponseSerializer})
    def post(self, request):
        if 'file' not in request.FILES:
            raise ValidationError('Choose an LSGD CSV file.')
        batch = lsgd_imports.preview(request.user, request.FILES['file'])
        return Response(lsgd_imports.payload(batch), status=201)


class LSGDImportCommitView(APIView):
    @extend_schema(tags=['Digester field sales'], request=OpenApiTypes.OBJECT, responses={200: LSGDImportResponseSerializer})
    def post(self, request, pk):
        return Response(lsgd_imports.commit(request.user, pk))


@documented
class LSGDContactView(APIView):
    def get(self, request):
        require(request.user, 'lsgds')
        qs=LSGDContact.objects.filter(lsgd__in=lsgd_scope(request.user, LSGD.objects.all()))
        if request.query_params.get('lsgd'): qs=qs.filter(lsgd_id=request.query_params['lsgd'])
        return Response({'count':qs.count(),'results':[{'id':str(c.pk),'lsgd':str(c.lsgd_id),'lsgd_name':c.lsgd.name,'name':c.name,'designation':c.designation,'phone':c.phone,'email':c.email,'decision_maker':c.decision_maker,'verified':bool(c.verified_by_id)} for c in qs.select_related('lsgd').order_by('lsgd__name','name')]})

    def post(self, request):
        require(request.user, 'lsgd.contact.write')
        data=request.data
        lsgd=lsgd_scope(request.user,LSGD.objects.all()).filter(pk=data.get('lsgd')).first()
        if not lsgd: raise NotFound()
        name=str(data.get('name','')).strip();phone=''.join(c for c in str(data.get('phone','')) if c.isdigit())
        if not name: raise ValidationError({'name':'Contact name is required.'})
        if len(phone)!=10: raise ValidationError({'phone':'Enter a 10-digit mobile number.'})
        def run(actor):
            require(actor,'lsgd.contact.write')
            contact=LSGDContact.objects.create(lsgd=lsgd,name=name[:160],designation=str(data.get('designation',''))[:120],phone=phone,email=str(data.get('email',''))[:254],decision_maker=bool(data.get('decision_maker',False)))
            AuditEvent.objects.create(actor=actor,action='lsgd.contact.created',resource=str(contact.pk),details={'lsgd':lsgd.name,'name':contact.name})
            return {'id':str(contact.pk),'lsgd':str(lsgd.pk),'name':contact.name,'designation':contact.designation,'phone':contact.phone,'email':contact.email,'decision_maker':contact.decision_maker}
        return Response(command(request.user,request.headers.get('Idempotency-Key'),'lsgd.contact.create',data,run),status=201)


@documented
class LSGDAssignmentView(APIView):
    def post(self, request, pk, assignment):
        if assignment not in ('manager', 'tso'): raise NotFound()
        action = 'lsgd.assign.manager' if assignment == 'manager' else 'lsgd.assign.tso'
        require(request.user, action)
        data = request.data

        def run(actor):
            require(actor, action)
            try: row = lsgd_scope(actor, LSGD.objects.select_for_update()).get(pk=pk)
            except LSGD.DoesNotExist: raise NotFound()
            version(row, data.get('version'))
            if assignment == 'manager':
                if actor.role != Role.DIGESTER_MANAGER: raise PermissionDenied()
                try: target = User.objects.get(pk=data.get('manager'), role=Role.ZONAL_MANAGER, is_active=True)
                except (User.DoesNotExist, ValueError, TypeError): raise ValidationError({'manager': 'Choose an active Zonal Manager.'})
                row.assigned_manager = target
                if row.assigned_tso_id and row.assigned_tso.manager_id != target.pk:
                    row.assigned_tso = None
                    row.tso_assigned_by = None
                row.manager_assigned_by = actor
            else:
                if actor.role != Role.ZONAL_MANAGER: raise PermissionDenied()
                if row.assigned_manager_id != actor.pk: raise PermissionDenied()
                try: target = User.objects.get(pk=data.get('tso'), role=Role.TSO, manager=actor, is_active=True)
                except (User.DoesNotExist, ValueError, TypeError): raise ValidationError({'tso': 'Choose an active TSO who reports to you.'})
                row.assigned_tso = target
                row.tso_assigned_by = actor
            row.version += 1
            row.save()
            AuditEvent.objects.create(actor=actor, action='lsgd.assigned.' + assignment, resource=str(row.pk), details={'user': str(target.pk), 'name': row.name})
            DataRevision.objects.get_or_create(key='global')
            DataRevision.objects.filter(key='global').update(value=models.F('value') + 1)
            return lsgd_payload(row)
        return Response(command(request.user, request.headers.get('Idempotency-Key'), action, data, run))


def opportunity_payload(row):
    return {'id': str(row.pk), 'version': row.version, 'lsgd': str(row.lsgd_id), 'lsgd_name': row.lsgd.name, 'district_name': row.lsgd.district.name, 'financial_year': row.financial_year, 'stage': row.stage, 'expected_units': row.expected_units, 'expected_value': str(row.expected_value), 'chance_percent': row.chance_percent, 'expected_month': row.expected_month.isoformat() if row.expected_month else None, 'lost_reason': row.lost_reason}


@documented
class OpportunityView(APIView):
    def get(self, request):
        require(request.user, 'opportunities')
        qs = LSGDOpportunity.objects.select_related('lsgd', 'lsgd__district').filter(lsgd__in=lsgd_scope(request.user, LSGD.objects.all()))
        year = request.query_params.get('financial_year')
        if year: qs = qs.filter(financial_year=year)
        if request.query_params.get('lsgd'): qs=qs.filter(lsgd_id=request.query_params['lsgd'])
        return Response({'count': qs.count(), 'results': [opportunity_payload(row) for row in qs.order_by('-financial_year', 'lsgd__name')]})

    def post(self, request):
        require(request.user, 'opportunity.write')
        data = request.data
        try: lsgd_id = data.get('lsgd'); units = int(data.get('expected_units', 0)); chance = int(data.get('chance_percent', 10))
        except (TypeError, ValueError): raise ValidationError('Choose an LSGD and valid opportunity values.')
        if units < 0 or chance < 0 or chance > 100: raise ValidationError('Expected units and chance are outside their valid range.')
        financial_year = str(data.get('financial_year', '')).strip()
        if len(financial_year) != 9 or financial_year[4] != '-': raise ValidationError('Financial year must use YYYY-YYYY format.')
        config = policy('digester_sales')
        try: unit_price = Decimal(str(config['unit_price']))
        except (KeyError, InvalidOperation, TypeError): raise ValidationError('The approved digester unit price is not configured.')
        try: stage = str(data.get('stage', 'new'))
        except Exception: stage = 'new'
        if stage not in dict(LSGDOpportunity.STAGES): raise ValidationError({'stage': 'Choose a supported pipeline stage.'})
        lsgd = lsgd_scope(request.user, LSGD.objects.all()).filter(pk=lsgd_id).first()
        if not lsgd: raise NotFound()
        def run(actor):
            require(actor, 'opportunity.write')
            if data.get('id'):
                row=LSGDOpportunity.objects.select_for_update().filter(pk=data['id'],lsgd=lsgd,financial_year=financial_year).first()
                if not row: raise NotFound()
                version(row,data.get('version'))
                row.stage=stage;row.expected_units=units;row.expected_value=(unit_price*units).quantize(Decimal('.01'));row.chance_percent=chance
                row.expected_month=data.get('expected_month') or None;row.lost_reason=str(data.get('lost_reason',''))[:500];row.updated_by=actor;row.version+=1;row.save()
                AuditEvent.objects.create(actor=actor, action='lsgd.opportunity.updated', resource=str(row.pk), details={'lsgd': lsgd.name, 'financial_year': financial_year, 'stage': stage})
            else:
                if LSGDOpportunity.objects.filter(lsgd=lsgd, financial_year=financial_year).exists():
                    raise ValidationError('An opportunity already exists for this LSGD and financial year.')
                row = LSGDOpportunity.objects.create(lsgd=lsgd, financial_year=financial_year, stage=stage, expected_units=units, expected_value=(unit_price * units).quantize(Decimal('.01')), chance_percent=chance, expected_month=data.get('expected_month') or None, lost_reason=str(data.get('lost_reason', ''))[:500], updated_by=actor)
                AuditEvent.objects.create(actor=actor, action='lsgd.opportunity.created', resource=str(row.pk), details={'lsgd': lsgd.name, 'financial_year': financial_year, 'stage': stage})
            return opportunity_payload(row)
        return Response(command(request.user, request.headers.get('Idempotency-Key'), 'lsgd.opportunity.create', data, run), status=201)


def plan_payload(row):
    missed=row.state=='planned' and row.planned_date<timezone.localdate()
    return {'id':str(row.pk),'version':row.version,'lsgd':str(row.lsgd_id),'lsgd_name':row.lsgd.name,'assigned_to':str(row.assigned_to_id),'assignee_name':row.assigned_to.get_full_name() or row.assigned_to.email,'planned_date':row.planned_date.isoformat(),'purpose':row.purpose,'state':'missed' if missed else row.state,'created_by':str(row.created_by_id)}


@documented
class VisitPlanView(APIView):
    def get(self,request):
        require(request.user,'visit_plans')
        qs=VisitPlan.objects.select_related('lsgd','assigned_to').filter(lsgd__in=lsgd_scope(request.user,LSGD.objects.all()))
        if request.user.role==Role.TSO: qs=qs.filter(assigned_to=request.user)
        elif request.query_params.get('assigned_to'): qs=qs.filter(assigned_to_id=request.query_params['assigned_to'])
        return Response({'count':qs.count(),'results':[plan_payload(row) for row in qs.order_by('planned_date','lsgd__name')]})

    def post(self,request):
        require(request.user,'visit.plan')
        data=request.data
        lsgd=lsgd_scope(request.user,LSGD.objects.all()).filter(pk=data.get('lsgd')).first()
        if not lsgd: raise NotFound()
        try:
            planned_date=date.fromisoformat(str(data.get('planned_date')))
        except (TypeError,ValueError): raise ValidationError({'planned_date':'Choose a valid visit date.'})
        if planned_date<timezone.localdate(): raise ValidationError({'planned_date':'Past dates cannot be planned.'})
        assignee=request.user if request.user.role==Role.ZONAL_MANAGER and not data.get('assigned_to') else User.objects.filter(pk=data.get('assigned_to'),is_active=True).first()
        if not assignee or assignee.role not in (Role.ZONAL_MANAGER,Role.TSO): raise ValidationError({'assigned_to':'Choose an active Zonal Manager or TSO.'})
        if assignee.role==Role.TSO and (not lsgd.assigned_manager_id or assignee.manager_id!=lsgd.assigned_manager_id or lsgd.assigned_tso_id!=assignee.pk): raise PermissionDenied('Assign the LSGD to this TSO before planning a visit.')
        if request.user.role==Role.ZONAL_MANAGER and (assignee.pk!=request.user.pk and assignee.manager_id!=request.user.pk): raise PermissionDenied()
        if request.user.role==Role.DIGESTER_MANAGER and assignee.role==Role.ZONAL_MANAGER and lsgd.assigned_manager_id!=assignee.pk: raise ValidationError('Assign the LSGD to the Zonal Manager before planning a visit.')
        def run(actor):
            require(actor,'visit.plan')
            row=VisitPlan.objects.create(lsgd=lsgd,assigned_to=assignee,planned_date=planned_date,purpose=str(data.get('purpose',''))[:500],created_by=actor)
            AuditEvent.objects.create(actor=actor,action='digester.visit_plan.created',resource=str(row.pk),details={'lsgd':lsgd.name,'assigned_to':str(assignee.pk),'planned_date':planned_date.isoformat()})
            return plan_payload(row)
        return Response(command(request.user,request.headers.get('Idempotency-Key'),'digester.visit_plan.create',data,run),status=201)


def haversine_m(lat1,lon1,lat2,lon2):
    r=6371000
    a1,a2=math.radians(float(lat1)),math.radians(float(lat2))
    dlat=math.radians(float(lat2)-float(lat1));dlon=math.radians(float(lon2)-float(lon1))
    h=math.sin(dlat/2)**2+math.cos(a1)*math.cos(a2)*math.sin(dlon/2)**2
    return int(2*r*math.asin(math.sqrt(h)))


def visit_payload(row):
    return {'id':str(row.pk),'version':row.version,'lsgd':str(row.lsgd_id),'lsgd_name':row.lsgd.name,'owner':str(row.owner_id),'owner_name':row.owner.get_full_name() or row.owner.email,'visited_at':row.visited_at.isoformat(),'latitude':str(row.latitude) if row.latitude is not None else None,'longitude':str(row.longitude) if row.longitude is not None else None,'gps_accuracy':str(row.gps_accuracy) if row.gps_accuracy is not None else None,'distance_m':row.distance_m,'photo_path':row.photo_path,'discussion':row.discussion,'outcome':row.outcome,'interest':row.interest,'flags':row.flags,'details_at':row.details_at.isoformat() if row.details_at else None,'people_met':[{'id':str(v.contact_id),'name':v.contact.name,'designation':v.contact.designation,'phone':v.contact.phone} for v in row.people_met.select_related('contact').all()],'zm_status':row.zm_status,'zm_remark':row.zm_remark,'ho_remark':row.ho_remark,'ho_signed_at':row.ho_signed_at.isoformat() if row.ho_signed_at else None}


def read_visit_photo(upload):
    if not upload or upload.size>8*1024*1024: raise ValidationError({'photo':'A visit photo up to 8 MB is required.'})
    raw=upload.read()
    if not (raw.startswith(b'\xff\xd8\xff') or raw.startswith(b'\x89PNG\r\n\x1a\n') or raw.startswith(b'RIFF') and raw[8:12]==b'WEBP'):
        raise ValidationError({'photo':'Upload a JPEG, PNG, or WebP photo.'})
    return raw,hashlib.sha256(raw).hexdigest()


def save_visit_photo(raw, owner):
    extension = ".png" if raw.startswith(bytes([137, 80, 78, 71])) else ".webp" if raw.startswith(b"RIFF") else ".jpg"
    path=f'digester/visits/{owner.pk}/{uuid.uuid4().hex}{extension}'
    absolute=settings.PRIVATE_ROOT/path
    absolute.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    absolute.write_bytes(raw);absolute.chmod(0o600)
    return path


@documented
class FieldVisitView(APIView):
    def get(self,request):
        require(request.user,'visits')
        qs=FieldVisit.objects.select_related('lsgd','owner').filter(lsgd__in=lsgd_scope(request.user,LSGD.objects.all()))
        if request.user.role==Role.TSO: qs=qs.filter(owner=request.user)
        if request.query_params.get('status'): qs=qs.filter(zm_status=request.query_params['status'])
        return Response({'count':qs.count(),'results':[visit_payload(row) for row in qs.order_by('-visited_at')[:200]]})

    def post(self,request):
        require(request.user,'visit.write')
        data=request.data
        if data.get('id'):
            return self.add_details(request,data)
        lsgd=lsgd_scope(request.user,LSGD.objects.all()).filter(pk=data.get('lsgd')).first()
        if not lsgd: raise NotFound()
        contact_ids=data.getlist('contacts') if hasattr(data,'getlist') else data.get('contacts',[])
        if isinstance(contact_ids,str): contact_ids=[contact_ids]
        contacts=list(LSGDContact.objects.filter(lsgd=lsgd,pk__in=contact_ids))
        if not contacts: raise ValidationError({'contacts':'Choose at least one person met at this LSGD.'})
        try:
            latitude=Decimal(str(data['latitude'])) if data.get('latitude') not in (None,'') else None
            longitude=Decimal(str(data['longitude'])) if data.get('longitude') not in (None,'') else None
            accuracy=Decimal(str(data['gps_accuracy'])) if data.get('gps_accuracy') not in (None,'') else None
        except (InvalidOperation,ValueError): raise ValidationError('GPS coordinates and accuracy must be valid numbers.')
        if (latitude is None)!=(longitude is None): raise ValidationError('Enter both GPS coordinates or leave both blank.')
        if latitude is not None and (latitude<Decimal('8.0') or latitude>Decimal('13.0') or longitude<Decimal('74.0') or longitude>Decimal('78.5')): raise ValidationError('GPS coordinates must be within Kerala.')
        discussion=str(data.get('discussion','')).strip();outcome=str(data.get('outcome','')).strip()
        interest=data.get('interest') or None
        if interest:
            try: interest=int(interest)
            except (ValueError,TypeError): raise ValidationError({'interest':'Choose an interest level from 1 to 5.'})
            if interest<1 or interest>5: raise ValidationError({'interest':'Choose an interest level from 1 to 5.'})
        if discussion and len(discussion)<20: raise ValidationError({'discussion':'Discussion details must be at least 20 characters.'})
        plan=None
        if data.get('plan'):
            plan=VisitPlan.objects.filter(pk=data.get('plan'),lsgd=lsgd,assigned_to=request.user,state='planned',planned_date=timezone.localdate()).first()
            if not plan: raise ValidationError({'plan':'Choose an active plan for this LSGD and date.'})
        upload=request.FILES.get('photo')
        photo_bytes,photo_digest=read_visit_photo(upload)
        payload={**(data.dict() if hasattr(data,'dict') else dict(data)),'contacts':contact_ids,'photo_digest':photo_digest}
        photo_path=None
        try:
            def run(actor):
                nonlocal photo_path
                require(actor,'visit.write')
                photo_path=save_visit_photo(photo_bytes,actor)
                now=timezone.now();flags=[];distance=None
                if latitude is None: flags.append('no_gps')
                if lsgd.latitude is None or lsgd.longitude is None: flags.append('lsgd_location_missing')
                elif latitude is not None:
                    distance=haversine_m(lsgd.latitude,lsgd.longitude,latitude,longitude)
                    if distance>300: flags.append('outside_geofence')
                if accuracy is not None and accuracy>100: flags.append('weak_gps')
                selected_plan=plan or VisitPlan.objects.filter(lsgd=lsgd,assigned_to=actor,planned_date=timezone.localdate(),state='planned').first()
                if not selected_plan: flags.append('unplanned')
                if latitude is not None:
                    previous=FieldVisit.objects.filter(owner=actor,latitude__isnull=False,visited_at__lt=now).order_by('-visited_at').first()
                    if previous:
                        hours=(now-previous.visited_at).total_seconds()/3600
                        if hours>0 and haversine_m(previous.latitude,previous.longitude,latitude,longitude)/1000/hours>80: flags.append('impossible_travel')
                if FieldVisit.objects.filter(owner=actor,visited_at__date=timezone.localdate()).count()>=7: flags.append('high_volume_day')
                row=FieldVisit.objects.create(lsgd=lsgd,owner=actor,plan=selected_plan,visited_at=now,latitude=latitude,longitude=longitude,gps_accuracy=accuracy,distance_m=distance,photo_path=photo_path,photo_captured_at=now,discussion=discussion,outcome=outcome,interest=interest,flags=flags,details_at=now if discussion else None)
                VisitContact.objects.bulk_create([VisitContact(visit=row,contact=contact) for contact in contacts])
                if selected_plan:
                    selected_plan.state='done';selected_plan.version+=1;selected_plan.save(update_fields=['state','version','updated_at'])
                AuditEvent.objects.create(actor=actor,action='digester.visit.submitted',resource=str(row.pk),details={'lsgd':lsgd.name,'flags':flags,'plan':str(selected_plan.pk) if selected_plan else None})
                return visit_payload(row)
            return Response(command(request.user,request.headers.get('Idempotency-Key'),'digester.visit.create',payload,run),status=201)
        except Exception:
            if photo_path: (settings.PRIVATE_ROOT/photo_path).unlink(missing_ok=True)
            raise

    def add_details(self,request,data):
        try: row=lsgd_scope(request.user,FieldVisit.objects.select_related('lsgd')).filter(pk=data.get('id')).get()
        except FieldVisit.DoesNotExist: raise NotFound()
        if row.owner_id!=request.user.pk or row.zm_status!='pending': raise PermissionDenied()
        if timezone.now()-row.visited_at>timedelta(days=3): raise ValidationError('Visit details must be completed within 3 days.')
        discussion=str(data.get('discussion','')).strip()
        if len(discussion)<20: raise ValidationError({'discussion':'Discussion details must be at least 20 characters.'})
        contacts=data.get('contacts',[])
        if isinstance(contacts,str): contacts=[contacts]
        contact_rows=list(LSGDContact.objects.filter(lsgd=row.lsgd,pk__in=contacts))
        if not contact_rows: raise ValidationError({'contacts':'Choose at least one person met at this LSGD.'})
        try: interest=int(data.get('interest'))
        except (ValueError,TypeError): raise ValidationError({'interest':'Choose an interest level from 1 to 5.'})
        if not 1<=interest<=5: raise ValidationError({'interest':'Choose an interest level from 1 to 5.'})
        def run(actor):
            current=FieldVisit.objects.select_for_update().get(pk=row.pk)
            version(current,data.get('version'))
            if current.owner_id!=actor.pk or current.zm_status!='pending': raise PermissionDenied()
            current.discussion=discussion;current.outcome=str(data.get('outcome',''))[:5000];current.interest=interest;current.details_at=timezone.now();current.version+=1;current.save()
            current.people_met.all().delete()
            VisitContact.objects.bulk_create([VisitContact(visit=current,contact=c) for c in contact_rows])
            AuditEvent.objects.create(actor=actor,action='digester.visit.details_added',resource=str(current.pk),details={'lsgd':current.lsgd.name})
            return visit_payload(current)
        return Response(command(request.user,request.headers.get('Idempotency-Key'),'digester.visit.details',data,run))


@documented
class FieldVisitReviewView(APIView):
    def post(self,request,pk,action):
        if action not in ('review','sign-off','office-location'): raise NotFound()
        require(request.user,'visit.review')
        try: row=FieldVisit.objects.select_related('owner','lsgd').get(pk=pk)
        except FieldVisit.DoesNotExist: raise NotFound()
        if not lsgd_scope(request.user,LSGD.objects.all()).filter(pk=row.lsgd_id).exists(): raise NotFound()
        if request.user.role==Role.ZONAL_MANAGER and (row.owner_id==request.user.pk or row.owner.manager_id!=request.user.pk): raise PermissionDenied()
        if request.user.role==Role.DIGESTER_MANAGER and action=='review' and row.owner.role!=Role.ZONAL_MANAGER: raise PermissionDenied()
        data=request.data
        def run(actor):
            current=FieldVisit.objects.select_for_update().select_related('owner','lsgd').get(pk=row.pk)
            version(current,data.get('version'))
            if action=='office-location':
                if actor.role not in (Role.ZONAL_MANAGER,Role.DIGESTER_MANAGER): raise PermissionDenied()
                if current.latitude is None or current.longitude is None: raise ValidationError('This visit has no GPS position to save as the LSGD office location.')
                lsgd=LSGD.objects.select_for_update().get(pk=current.lsgd_id)
                if lsgd.latitude is not None and lsgd.longitude is not None: raise ValidationError('This LSGD already has an office location.')
                lsgd.latitude=current.latitude;lsgd.longitude=current.longitude;lsgd.version+=1;lsgd.save(update_fields=['latitude','longitude','version','updated_at'])
                AuditEvent.objects.create(actor=actor,action='digester.lsgd.office_location_saved',resource=str(lsgd.pk),details={'visit':str(current.pk),'latitude':str(lsgd.latitude),'longitude':str(lsgd.longitude)})
            elif action=='sign-off':
                if actor.role!=Role.DIGESTER_MANAGER or current.zm_status!='verified' or not current.details_at or not current.people_met.exists(): raise ValidationError('Only a complete Zonal-Manager-verified visit can be signed off.')
                current.ho_signed_at=timezone.now();current.ho_reviewer=actor;current.ho_remark=str(data.get('remark',''))[:2000]
            else:
                status=str(data.get('status',''))
                if status not in ('verified','flagged','rejected'): raise ValidationError({'status':'Choose verify, flag, or reject.'})
                remark=str(data.get('remark','')).strip()
                if status in ('flagged','rejected') and not remark: raise ValidationError({'remark':'A reason is required.'})
                if status=='verified' and (not current.details_at or not current.people_met.exists() or len(current.discussion)<20): raise ValidationError('Add visit details before verification.')
                if actor.role==Role.ZONAL_MANAGER:
                    if current.zm_status!='pending': raise ValidationError('This visit has already been reviewed.')
                    current.zm_reviewer=actor;current.zm_remark=remark
                else:
                    if current.owner.role!=Role.ZONAL_MANAGER or current.zm_status!='pending': raise ValidationError('HQ review is limited to pending Zonal Manager visits.')
                    current.ho_reviewer=actor;current.ho_remark=remark
                current.zm_status=status
            current.version+=1;current.save()
            AuditEvent.objects.create(actor=actor,action='digester.visit.'+('signed_off' if action=='sign-off' else status),resource=str(current.pk),details={'lsgd':current.lsgd.name,'remark':str(data.get('remark',''))[:2000]})
            return visit_payload(current)
        return Response(command(request.user,request.headers.get('Idempotency-Key'),'digester.visit.'+action,{**data,'visit':str(row.pk)},run))


@documented
class FieldVisitPhotoView(APIView):
    def get(self,request,pk):
        require(request.user,'visits')
        qs=FieldVisit.objects.filter(pk=pk,lsgd__in=lsgd_scope(request.user,LSGD.objects.all()))
        if request.user.role==Role.TSO: qs=qs.filter(owner=request.user)
        row=qs.first()
        if not row: raise NotFound()
        path=(settings.PRIVATE_ROOT/row.photo_path).resolve()
        root=settings.PRIVATE_ROOT.resolve()
        if root not in path.parents or not path.is_file(): raise NotFound()
        return FileResponse(path.open('rb'),content_type=mimetypes.guess_type(path.name)[0] or 'application/octet-stream')


def followup_payload(row):
    return {'id':str(row.pk),'version':row.version,'lsgd':str(row.lsgd_id),'lsgd_name':row.lsgd.name,'visit':str(row.visit_id) if row.visit_id else None,'owner':str(row.owner_id),'owner_name':row.owner.get_full_name() or row.owner.email,'contact':str(row.contact_id) if row.contact_id else None,'contact_name':row.contact.name if row.contact_id else None,'due_date':row.due_date.isoformat(),'mode':row.mode,'note':row.note,'completed_at':row.completed_at.isoformat() if row.completed_at else None,'completion_note':row.completion_note,'status':'completed' if row.completed_at else 'overdue' if row.due_date<timezone.localdate() else 'upcoming' if row.due_date>timezone.localdate() else 'today'}


@documented
class FollowUpView(APIView):
    def get(self,request):
        require(request.user,'followups')
        qs=FieldFollowUp.objects.select_related('lsgd','owner','contact').filter(lsgd__in=lsgd_scope(request.user,LSGD.objects.all()))
        if request.user.role==Role.TSO: qs=qs.filter(owner=request.user)
        state=request.query_params.get('state')
        today=timezone.localdate()
        if state=='overdue': qs=qs.filter(completed_at__isnull=True,due_date__lt=today)
        elif state=='today': qs=qs.filter(completed_at__isnull=True,due_date=today)
        elif state=='upcoming': qs=qs.filter(completed_at__isnull=True,due_date__gt=today)
        elif state=='completed': qs=qs.filter(completed_at__isnull=False)
        return Response({'count':qs.count(),'results':[followup_payload(row) for row in qs.order_by('completed_at','due_date','lsgd__name')[:500]]})

    def post(self,request,pk=None):
        require(request.user,'followup.write')
        if pk: return self.complete(request,pk)
        data=request.data
        lsgd=lsgd_scope(request.user,LSGD.objects.all()).filter(pk=data.get('lsgd')).first()
        if not lsgd: raise NotFound()
        try: due_date=date.fromisoformat(str(data.get('due_date')))
        except (TypeError,ValueError): raise ValidationError({'due_date':'Choose a valid follow-up date.'})
        mode=str(data.get('mode',''))
        if mode not in dict(FieldFollowUp.MODES): raise ValidationError({'mode':'Choose a supported follow-up mode.'})
        note=str(data.get('note','')).strip()
        if not note: raise ValidationError({'note':'A follow-up note is required.'})
        contact=LSGDContact.objects.filter(pk=data.get('contact'),lsgd=lsgd).first() if data.get('contact') else None
        if data.get('contact') and not contact: raise ValidationError({'contact':'Choose a contact from this LSGD.'})
        visit=FieldVisit.objects.filter(pk=data.get('visit'),lsgd=lsgd).first() if data.get('visit') else None
        if data.get('visit') and not visit: raise ValidationError({'visit':'Choose a visit from this LSGD.'})
        def run(actor):
            row=FieldFollowUp.objects.create(lsgd=lsgd,visit=visit,owner=actor,contact=contact,due_date=due_date,mode=mode,note=note[:5000])
            AuditEvent.objects.create(actor=actor,action='digester.followup.created',resource=str(row.pk),details={'lsgd':lsgd.name,'due_date':due_date.isoformat(),'mode':mode})
            return followup_payload(row)
        return Response(command(request.user,request.headers.get('Idempotency-Key'),'digester.followup.create',data,run),status=201)

    def complete(self,request,pk):
        data=request.data;note=str(data.get('completion_note','')).strip()
        if not note: raise ValidationError({'completion_note':'Record what happened before closing this follow-up.'})
        qs=FieldFollowUp.objects.filter(pk=pk,lsgd__in=lsgd_scope(request.user,LSGD.objects.all()))
        if request.user.role==Role.TSO: qs=qs.filter(owner=request.user)
        row=qs.select_related('lsgd','owner','contact').first()
        if not row: raise NotFound()
        def run(actor):
            current=FieldFollowUp.objects.select_for_update().select_related('lsgd','owner','contact').get(pk=row.pk)
            version(current,data.get('version'))
            if current.completed_at: raise ValidationError('This follow-up is already complete.')
            if actor.role==Role.TSO and current.owner_id!=actor.pk: raise PermissionDenied()
            current.completed_at=timezone.now();current.completion_note=note[:5000];current.version+=1;current.save()
            AuditEvent.objects.create(actor=actor,action='digester.followup.completed',resource=str(current.pk),details={'lsgd':current.lsgd.name,'completion_note':current.completion_note})
            next_due=data.get('next_due_date')
            if next_due:
                try: parsed=date.fromisoformat(str(next_due))
                except ValueError: raise ValidationError({'next_due_date':'Choose a valid next follow-up date.'})
                next_mode=str(data.get('next_mode',current.mode))
                if next_mode not in dict(FieldFollowUp.MODES): raise ValidationError({'next_mode':'Choose a supported follow-up mode.'})
                next_note=str(data.get('next_note','')).strip()
                if not next_note: raise ValidationError({'next_note':'Add a note for the next follow-up.'})
                FieldFollowUp.objects.create(lsgd=current.lsgd,visit=current.visit,owner=actor,contact=current.contact,due_date=parsed,mode=next_mode,note=next_note[:5000])
            return followup_payload(current)
        return Response(command(request.user,request.headers.get('Idempotency-Key'),'digester.followup.complete',{**data,'followup':str(row.pk)},run))


def expense_scope(user, qs):
    if user.role==Role.DIGESTER_MANAGER: return qs
    if user.role==Role.ZONAL_MANAGER: return qs.filter(owner__manager=user)
    return qs.filter(owner=user)


def claim_payload(row):
    history=row.events.select_related('actor').order_by('created_at')
    if row.owner.role!=Role.DIGESTER_MANAGER: history=history.exclude(action__in=['flag','unflag'])
    return {'id':str(row.pk),'version':row.version,'owner':str(row.owner_id),'owner_name':row.owner.get_full_name() or row.owner.email,'manager':str(row.owner.manager_id) if row.owner.manager_id else None,'day':row.day.isoformat(),'status':row.status,'travel_mode':row.travel_mode,'vehicle_number':row.vehicle_number,'start_odometer':row.start_odometer,'end_odometer':row.end_odometer,'start_photo':f'/api/v1/digester/field-days/{row.pk}/photo-start/' if row.start_photo_path else None,'end_photo':f'/api/v1/digester/field-days/{row.pk}/photo-end/' if row.end_photo_path else None,'started_at':row.started_at.isoformat(),'ended_at':row.ended_at.isoformat() if row.ended_at else None,'night_halt':row.night_halt,'halt_place':row.halt_place,'note':row.note,'da_requested':str(row.da_requested),'ta_km':str(row.ta_km),'gps_km':str(row.gps_km) if row.gps_km is not None else None,'ta_amount':str(row.ta_amount),'total_claimed':str(row.total_claimed),'approved_amount':str(row.approved_amount) if row.approved_amount is not None else None,'policy_version':row.policy_version,'policy_snapshot':row.policy_snapshot,'flags':row.flags,'review_note':row.review_note,'ho_note':row.ho_note,'items':[{'id':str(i.pk),'category':i.category,'amount':str(i.amount),'description':i.description,'route_from':i.route_from,'route_to':i.route_to,'bill_number':i.bill_number,'photo':f'/api/v1/digester/expense-items/{i.pk}/photo/'} for i in row.items.all()],'history':[{'action':e.action,'actor':e.actor.get_full_name() or e.actor.email,'at':e.created_at.isoformat(),'old_status':e.old_status,'new_status':e.new_status,'amount':str(e.amount) if e.amount is not None else None,'note':e.note} for e in history]}

def recalc_claim_total(row):
    non_travel=row.items.exclude(category__in=['da_bill','bus_train','auto_taxi','parking_toll']).aggregate(total=models.Sum('amount'))['total'] or Decimal('0')
    row.total_claimed=row.ta_amount+row.da_requested+non_travel
    return row.total_claimed


def _claim_policy(mode=None):
    row=ExpensePolicy.objects.first()
    missing=not row or not row.da_local_max or not row.da_night_max
    if row and mode=='two_wheeler' and not row.two_wheeler_rate: missing=True
    if row and mode=='car' and not row.car_rate: missing=True
    if missing:
        from rest_framework.exceptions import APIException
        raise APIException('Head Office must configure TA/DA rates and limits before claims can be started.')
    return row


def _claim_policy_value(day, key, fallback):
    try: return Decimal(str(day.policy_snapshot[key]))
    except (KeyError, TypeError, InvalidOperation): return fallback


def _claim_upload(upload, owner, prefix):
    raw,digest=read_visit_photo(upload)
    ext='.png' if raw.startswith(b'\x89PNG') else '.webp' if raw.startswith(b'RIFF') else '.jpg'
    path=f'digester/expenses/{owner.pk}/{prefix}-{uuid.uuid4().hex}{ext}'
    absolute=settings.PRIVATE_ROOT/path;absolute.parent.mkdir(mode=0o700,parents=True,exist_ok=True);absolute.write_bytes(raw);absolute.chmod(0o600)
    return path,digest


@documented
class FieldDayView(APIView):
    def get(self,request,pk=None,action=None):
        require(request.user,'field_days')
        if action in ('photo','photo-start','photo-end'):
            return self.photo(request,pk)
        qs=expense_scope(request.user,FieldDay.objects.select_related('owner','owner__manager').prefetch_related('items'))
        if pk:
            row=qs.filter(pk=pk).first()
            if not row: raise NotFound()
            return Response(claim_payload(row))
        if request.query_params.get('month'):
            try: year,month=map(int,request.query_params['month'].split('-'))
            except (ValueError,TypeError): raise ValidationError({'month':'Use YYYY-MM.'})
            qs=qs.filter(day__year=year,day__month=month)
        return Response({'count':qs.count(),'results':[claim_payload(row) for row in qs.order_by('-day','owner__last_name')[:300]]})

    def post(self,request,pk=None,action=None):
        data=request.data
        if not action:
            require(request.user,'claim.write')
            if request.user.role not in (Role.TSO,Role.ZONAL_MANAGER): raise PermissionDenied()
            today=timezone.localdate()
            if FieldDay.objects.filter(owner=request.user,day=today).exists(): raise ValidationError('A field day already exists for today.')
            mode=str(data.get('travel_mode',''))
            if mode not in dict(FieldDay.MODES): raise ValidationError({'travel_mode':'Choose a supported travel mode.'})
            policy_row=_claim_policy(mode)
            vehicle=str(data.get('vehicle_number','')).strip()
            personal=mode in ('two_wheeler','car')
            if personal and not vehicle: raise ValidationError({'vehicle_number':'Enter the vehicle number.'})
            try: odometer=int(data.get('start_odometer')) if personal else None
            except (TypeError,ValueError): raise ValidationError({'start_odometer':'Enter a valid starting odometer.'})
            if odometer is not None and odometer<0: raise ValidationError({'start_odometer':'Odometer must be zero or greater.'})
            try: latitude=Decimal(str(data.get('latitude')));longitude=Decimal(str(data.get('longitude')))
            except (InvalidOperation,TypeError,ValueError): raise ValidationError({'latitude':'Record a valid GPS position before starting.'})
            if latitude<Decimal('8.0') or latitude>Decimal('13.0') or longitude<Decimal('74.0') or longitude>Decimal('78.5'): raise ValidationError({'latitude':'GPS position must be within Kerala.'})
            if personal: photo,digest=_claim_upload(request.FILES.get('start_photo'),request.user,'start')
            else: photo,digest='', ''
            try:
                def run(actor):
                    snapshot={key:str(getattr(policy_row,key)) for key in ('two_wheeler_rate','car_rate','da_local_max','da_night_max','da_bill_threshold','lodging_cap','auto_taxi_cap','daily_km_limit','gps_allowance_percent','road_factor','submit_days')}
                    row=FieldDay.objects.create(owner=actor,day=today,travel_mode=mode,vehicle_number=vehicle,start_odometer=odometer,start_photo_path=photo,start_latitude=latitude,start_longitude=longitude,started_at=timezone.now(),policy_version=policy_row.version,policy_snapshot=snapshot)
                    ClaimEvent.objects.create(field_day=row,actor=actor,action='start',new_status='open',note=f'evidence:{digest}')
                    return claim_payload(row)
                return Response(command(request.user,request.headers.get('Idempotency-Key'),'digester.fieldday.start',{**data.dict(),'photo':digest},run),status=201)
            except Exception:
                if photo: (settings.PRIVATE_ROOT/photo).unlink(missing_ok=True)
                raise
        require(request.user,'claim.write')
        qs=expense_scope(request.user,FieldDay.objects.select_related('owner').prefetch_related('items'))
        row=qs.filter(pk=pk).first()
        if not row: raise NotFound()
        if action=='edit':
            if row.owner_id!=request.user.pk or row.status!='open': raise PermissionDenied()
            policy_row=_claim_policy(row.travel_mode)
            try: da=Decimal(str(data.get('da_requested','0')))
            except InvalidOperation: raise ValidationError({'da_requested':'Enter a valid DA amount.'})
            maximum=policy_row.da_night_max if data.get('night_halt') in (True,'true','1','on') else policy_row.da_local_max
            if da<0 or da>maximum: raise ValidationError({'da_requested':f'DA cannot exceed ₹{maximum}.'})
            def run(actor):
                current=FieldDay.objects.select_for_update().get(pk=row.pk);version(current,data.get('version'))
                current.night_halt=data.get('night_halt') in (True,'true','1','on');current.halt_place=str(data.get('halt_place',''))[:180];current.note=str(data.get('note',''))[:5000];current.da_requested=da;current.version+=1;current.save()
                recalc_claim_total(current);current.save(update_fields=['total_claimed','updated_at'])
                ClaimEvent.objects.create(field_day=current,actor=actor,action='edit',new_status=current.status,amount=current.total_claimed,note=current.note)
                return claim_payload(current)
            return Response(command(request.user,request.headers.get('Idempotency-Key'),'digester.fieldday.edit',{**data.dict(),'day':str(row.pk)},run))
        if action=='end':
            if row.owner_id!=request.user.pk or row.status!='open' or row.ended_at: raise PermissionDenied()
            policy_row=_claim_policy(row.travel_mode)
            personal=row.travel_mode in ('two_wheeler','car')
            if personal: photo,digest=_claim_upload(request.FILES.get('end_photo'),request.user,'end')
            else: photo,digest='', ''
            try: end_odo=int(data.get('end_odometer')) if row.start_odometer is not None else None
            except (TypeError,ValueError): raise ValidationError({'end_odometer':'Enter a valid ending odometer.'})
            if end_odo is not None and end_odo<row.start_odometer: raise ValidationError('Ending odometer cannot be below the start reading.')
            try: latitude=Decimal(str(data.get('latitude')));longitude=Decimal(str(data.get('longitude')))
            except (InvalidOperation,TypeError,ValueError): raise ValidationError({'latitude':'Record a valid GPS position before ending.'})
            if latitude<Decimal('8.0') or latitude>Decimal('13.0') or longitude<Decimal('74.0') or longitude>Decimal('78.5'): raise ValidationError({'latitude':'GPS position must be within Kerala.'})
            def run(actor):
                current=FieldDay.objects.select_for_update().get(pk=row.pk);version(current,data.get('version'))
                current.end_odometer=end_odo;current.end_photo_path=photo;current.end_latitude=latitude;current.end_longitude=longitude;current.ended_at=timezone.now()
                current.ta_km=Decimal(end_odo-row.start_odometer) if end_odo is not None else Decimal('0')
                rate=_claim_policy_value(current,'two_wheeler_rate',policy_row.two_wheeler_rate) if current.travel_mode=='two_wheeler' else _claim_policy_value(current,'car_rate',policy_row.car_rate) if current.travel_mode=='car' else Decimal('0')
                travel=sum((i.amount for i in current.items.filter(category__in=['bus_train','auto_taxi','parking_toll'])),Decimal('0'))
                current.ta_amount=current.ta_km*rate+travel
                current.flags=[]
                daily_limit=int(current.policy_snapshot.get('daily_km_limit',policy_row.daily_km_limit))
                if current.ta_km>daily_limit: current.flags.append('daily_km_limit')
                current_visits=list(FieldVisit.objects.filter(owner=actor,visited_at__date=current.day).order_by('visited_at'))
                if not current_visits: current.flags.append('no_lsgd_visit')
                if any(v.zm_status=='rejected' for v in current_visits): current.flags.append('rejected_visit')
                points=[(current.start_latitude,current.start_longitude)]+[(v.latitude,v.longitude) for v in current_visits]+[(current.end_latitude,current.end_longitude)]
                if any(lat is None or lon is None for lat,lon in points): current.flags.append('weak_or_missing_gps')
                else:
                    raw_km=sum(haversine_m(points[i][0],points[i][1],points[i+1][0],points[i+1][1]) for i in range(len(points)-1))/1000
                    road_factor=_claim_policy_value(current,'road_factor',policy_row.road_factor)
                    allowance=_claim_policy_value(current,'gps_allowance_percent',Decimal(policy_row.gps_allowance_percent))
                    current.gps_km=Decimal(str(raw_km))*road_factor
                    if current.gps_km and current.ta_km>current.gps_km*(1+allowance/100): current.flags.append('odometer_above_gps')
                item_totals={item['category']:Decimal(str(item['total'])) for item in current.items.values('category').annotate(total=models.Sum('amount'))}
                if item_totals.get('lodging',Decimal(0))>_claim_policy_value(current,'lodging_cap',policy_row.lodging_cap): current.flags.append('lodging_over_cap')
                if item_totals.get('auto_taxi',Decimal(0))>_claim_policy_value(current,'auto_taxi_cap',policy_row.auto_taxi_cap): current.flags.append('auto_taxi_over_cap')
                if current.da_requested>_claim_policy_value(current,'da_bill_threshold',policy_row.da_bill_threshold) and not current.items.filter(category='da_bill').exists(): current.flags.append('missing_da_bill')
                if current.day.weekday()==6: current.flags.append('sunday')
                if timezone.localtime(current.started_at).hour>9: current.flags.append('late_start')
                previous=FieldDay.objects.filter(owner=actor,day__lt=current.day,end_odometer__isnull=False).order_by('-day').first()
                if previous and current.start_odometer is not None and previous.end_odometer>current.start_odometer: current.flags.append('odometer_overlap')
                recalc_claim_total(current);current.version+=1;current.save()
                ClaimEvent.objects.create(field_day=current,actor=actor,action='end',new_status=current.status,amount=current.total_claimed,note=f'evidence:{digest}')
                return claim_payload(current)
            try: return Response(command(request.user,request.headers.get('Idempotency-Key'),'digester.fieldday.end',{**data.dict(),'day':str(row.pk),'photo':digest},run))
            except Exception:
                if photo: (settings.PRIVATE_ROOT/photo).unlink(missing_ok=True)
                raise
        if action in ('submit','review','approve','send-back','reject','flag','unflag'):
            return self.transition(request,row,action)
        raise NotFound()

    def transition(self,request,row,action):
        data=request.data;actor=request.user;note=str(data.get('note','')).strip()
        if action in ('flag','unflag'):
            if actor.role!=Role.DIGESTER_MANAGER: raise PermissionDenied()
            if not note: raise ValidationError({'note':'A reason is required.'})
            if action=='flag' and 'ho_flagged' in row.flags: raise ValidationError('This claim is already flagged.')
            if action=='unflag' and 'ho_flagged' not in row.flags: raise ValidationError('This claim is not flagged.')
            target=row.status;approved=row.approved_amount
        elif action=='submit':
            if row.owner_id!=actor.pk or row.status!='open' or not row.ended_at: raise ValidationError('End the field day before submitting.')
            policy_row=_claim_policy()
            if policy_row.submit_days and timezone.localdate()-row.day>timedelta(days=policy_row.submit_days): raise ValidationError('The submission window for this field day has passed.')
            if row.da_requested>_claim_policy_value(row,'da_bill_threshold',policy_row.da_bill_threshold) and not row.items.filter(category='da_bill').exists(): raise ValidationError('Add a DA bill photo before submitting this amount.')
            target='submitted'
        elif actor.role==Role.ZONAL_MANAGER and action in ('review','send-back','reject'):
            if actor.role!=Role.ZONAL_MANAGER or row.owner.manager_id!=actor.pk or row.status!='submitted': raise PermissionDenied()
            target={'review':'zm_approved','send-back':'open','reject':'rejected'}[action]
        elif actor.role==Role.DIGESTER_MANAGER and action in ('approve','send-back','reject'):
            if row.owner_id==actor.pk or row.owner.role not in (Role.TSO,Role.ZONAL_MANAGER): raise PermissionDenied()
            if row.owner.role==Role.TSO and row.status!='zm_approved': raise PermissionDenied()
            if row.owner.role==Role.ZONAL_MANAGER and row.status!='submitted': raise PermissionDenied()
            target={'approve':'ho_approved','reject':'rejected','send-back':'open'}.get(action)
            if target is None: raise NotFound()
        else:
            raise PermissionDenied()
        if action in ('review','approve'):
            try: approved=Decimal(str(data.get('approved_amount',row.total_claimed)))
            except InvalidOperation: raise ValidationError({'approved_amount':'Enter a valid amount.'})
            if approved<0 or approved>row.total_claimed: raise ValidationError('Approved amount must be between zero and the amount claimed.')
            if approved<row.total_claimed and not note: raise ValidationError({'note':'A reason is required when reducing the claim.'})
        else: approved=row.approved_amount
        if action in ('send-back','reject') and not note: raise ValidationError({'note':'A reason is required.'})
        def run(user):
            current=FieldDay.objects.select_for_update().get(pk=row.pk);version(current,data.get('version'));old=current.status;current.status=target
            if action=='flag': current.flags=list(current.flags)+['ho_flagged']
            if action=='unflag': current.flags=[flag for flag in current.flags if flag!='ho_flagged']
            if action in ('review','approve'): current.approved_amount=approved
            if action=='review': current.reviewed_by=user;current.review_note=note
            if action=='approve': current.ho_approved_by=user;current.ho_note=note
            if action=='submit': current.submitted_at=timezone.now()
            current.version+=1;current.save()
            ClaimEvent.objects.create(field_day=current,actor=user,action=action,old_status=old,new_status=target,amount=approved if approved is not None else current.total_claimed,note=note)
            AuditEvent.objects.create(actor=user,action='digester.claim.'+action,resource=str(current.pk),details={'owner':current.owner_id,'amount':str(approved or current.total_claimed),'note':note})
            return claim_payload(current)
        payload=data.dict() if hasattr(data,'dict') else dict(data)
        return Response(command(actor,request.headers.get('Idempotency-Key'),'digester.claim.'+action,{**payload,'day':str(row.pk)},run))

    def photo(self,request,pk):
        field_day=FieldDay.objects.filter(pk=pk,owner__in=User.objects.filter(pk__in=expense_scope(request.user,User.objects.all()).values('pk'))).prefetch_related('items').first()
        if not field_day: raise NotFound()
        photo_path=field_day.end_photo_path if request.resolver_match.kwargs.get('action')=='photo-end' else field_day.start_photo_path
        path=settings.PRIVATE_ROOT/photo_path
        if not path.is_file(): raise NotFound()
        return FileResponse(path.open('rb'),content_type=mimetypes.guess_type(path.name)[0] or 'application/octet-stream')


@documented
class ExpenseItemView(APIView):
    def get(self,request,pk=None,action=None):
        require(request.user,'expense_items')
        if action=='photo':
            item=ExpenseItem.objects.filter(pk=pk,field_day__in=expense_scope(request.user,FieldDay.objects.all())).first()
            if not item: raise NotFound()
            path=settings.PRIVATE_ROOT/item.photo_path
            if not path.is_file(): raise NotFound()
            return FileResponse(path.open('rb'),content_type=mimetypes.guess_type(path.name)[0] or 'application/octet-stream')
        qs=ExpenseItem.objects.filter(field_day__in=expense_scope(request.user,FieldDay.objects.all()))
        if request.query_params.get('field_day'): qs=qs.filter(field_day_id=request.query_params['field_day'])
        return Response({'count':qs.count(),'results':[{'id':str(item.pk),'field_day':str(item.field_day_id),'day':item.field_day.day.isoformat(),'owner':str(item.field_day.owner_id),'category':item.category,'amount':str(item.amount),'description':item.description,'route_from':item.route_from,'route_to':item.route_to,'bill_number':item.bill_number,'photo':f'/api/v1/digester/expense-items/{item.pk}/photo/'} for item in qs.select_related('field_day','field_day__owner').order_by('-created_at')[:500]]})
    def post(self,request,pk=None,action=None):
        require(request.user,'claim.write')
        day=expense_scope(request.user,FieldDay.objects.all()).filter(pk=request.data.get('field_day'),owner=request.user,status='open').first()
        if not day: raise NotFound()
        category=str(request.data.get('category',''))
        if category not in dict(ExpenseItem.CATEGORIES): raise ValidationError({'category':'Choose an expense category.'})
        try: amount=Decimal(str(request.data.get('amount','0')))
        except InvalidOperation: raise ValidationError({'amount':'Enter a valid amount.'})
        if amount<0 or (category!='da_bill' and amount<=0): raise ValidationError({'amount':'Enter a positive bill amount.'})
        if category=='da_bill': amount=Decimal('0')
        route_from=str(request.data.get('route_from','')).strip()[:180]
        route_to=str(request.data.get('route_to','')).strip()[:180]
        if category in ('bus_train','auto_taxi') and (not route_from or not route_to):
            raise ValidationError({'route_from':'Enter the travel route from and to.'})
        upload=request.FILES.get('photo');path,digest=_claim_upload(upload,request.user,'bill')
        policy_row=_claim_policy() if day.ended_at else None
        try:
            row=ExpenseItem.objects.create(field_day=day,category=category,amount=amount,description=str(request.data.get('description',''))[:500],route_from=route_from,route_to=route_to,bill_number=str(request.data.get('bill_number',''))[:100],photo_path=path,photo_digest=digest,created_by=request.user)
            duplicate_photo=ExpenseItem.objects.filter(photo_digest=digest).exclude(pk=row.pk).exists()
            duplicate_bill=bool(row.bill_number and ExpenseItem.objects.filter(category=category,amount=amount,bill_number=row.bill_number).exclude(pk=row.pk).exists())
            if duplicate_photo or duplicate_bill:
                day.flags=list(day.flags)+(['duplicate_bill_photo'] if duplicate_photo else [])+(['duplicate_bill_number'] if duplicate_bill else [])
            if day.ended_at:
                rate=_claim_policy_value(day,'two_wheeler_rate',policy_row.two_wheeler_rate) if day.travel_mode=='two_wheeler' else _claim_policy_value(day,'car_rate',policy_row.car_rate) if day.travel_mode=='car' else Decimal('0')
                travel=day.items.filter(category__in=['bus_train','auto_taxi','parking_toll']).aggregate(total=models.Sum('amount'))['total'] or Decimal('0')
                day.ta_amount=day.ta_km*rate+travel
            ClaimEvent.objects.create(field_day=day,actor=request.user,action='bill_added',new_status=day.status,amount=amount,note=category)
            recalc_claim_total(day);day.version+=1;day.save(update_fields=['total_claimed','flags','version','updated_at'])
            return Response({'id':str(row.pk),'category':category,'amount':str(amount)},status=201)
        except Exception: (settings.PRIVATE_ROOT/path).unlink(missing_ok=True);raise


@documented
class ExpenseSettlementView(APIView):
    def get(self,request):
        require(request.user,'settlements')
        return Response({'results':[{'id':str(p.pk),'owner':str(p.owner_id),'owner_name':p.owner.get_full_name() or p.owner.email,'month':p.month,'amount':str(p.amount),'reference':p.reference,'paid_at':p.paid_at.isoformat()} for p in ExpensePayment.objects.select_related('owner').order_by('-month','owner__last_name')]})
    def post(self,request):
        require(request.user,'claim.pay')
        data=request.data;owner=User.objects.filter(pk=data.get('owner'),role__in=[Role.TSO,Role.ZONAL_MANAGER]).first()
        if not owner: raise NotFound()
        month=str(data.get('month',''))
        try: date.fromisoformat(month+'-01')
        except ValueError: raise ValidationError({'month':'Use YYYY-MM.'})
        today=timezone.localdate()
        if month>=today.strftime('%Y-%m'): raise ValidationError('A settlement can only be paid after the month has ended.')
        ref=str(data.get('reference','')).strip()
        if not ref: raise ValidationError({'reference':'A UTR or voucher reference is required.'})
        year,month_number=map(int,month.split('-'))
        def run(actor):
            User.objects.select_for_update().get(pk=owner.pk)
            if ExpensePayment.objects.filter(owner=owner,month=month).exists(): raise ValidationError('This person already has a settlement for that month.')
            days=[day for day in FieldDay.objects.select_for_update().filter(owner=owner,day__year=year,day__month=month_number,status='ho_approved') if 'ho_flagged' not in day.flags]
            if not days: raise ValidationError('No unflagged approved claims are available for this month.')
            total=sum((day.approved_amount if day.approved_amount is not None else day.total_claimed for day in days),Decimal('0'))
            payment=ExpensePayment.objects.create(owner=owner,month=month,amount=total,reference=ref,note=str(data.get('note',''))[:2000],paid_by=request.user,paid_at=timezone.now())
            for day in days:
                day.status='paid';day.paid_at=payment.paid_at;day.version+=1;day.save(update_fields=['status','paid_at','version','updated_at'])
                ClaimEvent.objects.create(field_day=day,actor=actor,action='paid',old_status='ho_approved',new_status='paid',amount=day.approved_amount or day.total_claimed,note=ref)
            AuditEvent.objects.create(actor=actor,action='digester.claim.month_paid',resource=str(payment.pk),details={'owner':owner.pk,'month':month,'amount':str(total),'reference':ref})
            return {'id':str(payment.pk),'owner':str(owner.pk),'month':month,'amount':str(total),'reference':ref}
        return Response(command(request.user,request.headers.get('Idempotency-Key'),'digester.claim.month_paid',data.dict() if hasattr(data,'dict') else data,run),status=201)


@documented
class ExpensePolicyView(APIView):
    def get(self,request):
        require(request.user,'field_days')
        row=ExpensePolicy.objects.first()
        if not row: return Response({'configured':False})
        return Response({'configured':True,**{f.name:str(getattr(row,f.name)) for f in row._meta.fields if f.name not in ('id','created_at','updated_at','version')}})
    def post(self,request):
        require(request.user,'expense.policy.write')
        data=request.data
        fields=['two_wheeler_rate','car_rate','da_local_max','da_night_max','da_bill_threshold','lodging_cap','auto_taxi_cap','daily_km_limit','gps_allowance_percent','road_factor','submit_days']
        row=ExpensePolicy.objects.first() or ExpensePolicy()
        for key in fields:
            if key in data:
                try: value=Decimal(str(data[key])) if key not in ('daily_km_limit','gps_allowance_percent','submit_days') else int(data[key])
                except (InvalidOperation,TypeError,ValueError): raise ValidationError({key:'Enter a valid limit or rate.'})
                if value<0: raise ValidationError({key:'Rates and limits cannot be negative.'})
                setattr(row,key,value)
        row.version+=1;row.save()
        AuditEvent.objects.create(actor=request.user,action='digester.expense_policy.updated',resource=str(row.pk),details={key:str(getattr(row,key)) for key in fields})
        return Response({'configured':True,**{key:str(getattr(row,key)) for key in fields}})


@documented
class DigesterDashboardView(APIView):
    def get(self,request):
        require(request.user,'dashboard')
        lsgds=lsgd_scope(request.user,LSGD.objects.all())
        visits=FieldVisit.objects.filter(lsgd__in=lsgds)
        plans=VisitPlan.objects.filter(lsgd__in=lsgds)
        followups=FieldFollowUp.objects.filter(lsgd__in=lsgds)
        opportunities=LSGDOpportunity.objects.filter(lsgd__in=lsgds)
        if request.user.role==Role.TSO:
            visits=visits.filter(owner=request.user);plans=plans.filter(assigned_to=request.user);followups=followups.filter(owner=request.user)
        today=timezone.localdate()
        return Response({'lsgds':lsgds.count(),'visits_today':visits.filter(visited_at__date=today).count(),'visits_waiting_review':visits.filter(zm_status='pending',details_at__isnull=False).count(),'plans_today':plans.filter(planned_date=today,state='planned').count(),'missed_plans':plans.filter(planned_date__lt=today,state='planned').count(),'followups_today':followups.filter(completed_at__isnull=True,due_date=today).count(),'followups_overdue':followups.filter(completed_at__isnull=True,due_date__lt=today).count(),'opportunities':opportunities.count(),'pipeline_value':str(opportunities.exclude(stage='lost').aggregate(total=models.Sum('expected_value'))['total'] or Decimal('0')),'claims_waiting_review':expense_scope(request.user,FieldDay.objects.filter(status__in=['submitted','zm_approved'])).count() if request.user.role!=Role.TSO else 0})
