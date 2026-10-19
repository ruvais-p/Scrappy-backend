import hashlib
import json
from decimal import Decimal, InvalidOperation
from django.db import transaction
from rest_framework.exceptions import APIException, ValidationError, PermissionDenied
from core.models import User, CommandResult, AuditEvent, Outbox, DataRevision

class Conflict(APIException):
    status_code=409
    default_code='conflict'

def decimal(value,places=2,positive=True):
    try:
        n=Decimal(str(value)); quantum=Decimal(10)**-places
        if not n.is_finite() or n<0 or (positive and n==0) or n != n.quantize(quantum) or n>=Decimal('1000000000000'): raise ValueError()
        return n
    except (ValueError,InvalidOperation): raise ValidationError(f'Enter a valid {"positive" if positive else "nonnegative"} number with at most {places} decimals.')

def version(obj,value):
    if value is None or str(obj.version)!=str(value): raise Conflict('This record changed. Refresh and review before saving.')

def event(actor,feature,obj,details=None,action=None):
    from django.db.models import F
    DataRevision.objects.get_or_create(key='global')
    DataRevision.objects.filter(key='global').update(value=F('value')+1)
    franchise_id=getattr(obj,'franchise_id',None)
    if franchise_id is None and hasattr(obj,'warehouse'): franchise_id=obj.warehouse.franchise_id
    AuditEvent.objects.create(actor=actor,franchise_id=franchise_id,action=action or feature,resource=str(obj.pk),details=details or {})
    owner_id=getattr(obj,'creator_id',None) or getattr(obj,'driver_id',None) or getattr(obj,'franchise_manager_id',None)
    Outbox.objects.create(actor=actor,franchise_id=franchise_id,feature=feature,resource_id=str(obj.pk),owner_id=owner_id,warehouse_id=getattr(obj,'warehouse_id',None))

def command(actor,key,action,payload,fn):
    if not key or len(key)>100: raise ValidationError('An Idempotency-Key of at most 100 characters is required.')
    digest=hashlib.sha256(json.dumps(payload,sort_keys=True,default=str).encode()).hexdigest()
    reviewed_permission_version=actor.permission_version
    with transaction.atomic():
        # Actor lock serializes duplicate keys and coordinates revocation.
        # Keep permission changes serialized without blocking foreign-key
        # references to this actor in another driver's cash transaction.
        actor=User.objects.select_for_update(no_key=True).get(pk=actor.pk)
        if not actor.is_active: raise ValidationError('Account is inactive.')
        if actor.permission_version!=reviewed_permission_version: raise PermissionDenied('Permissions changed. Sign in again before continuing.')
        prior=CommandResult.objects.filter(actor=actor,key=key).first()
        if prior:
            if prior.digest!=digest or prior.action!=action: raise Conflict('This retry key was already used with different input.')
            # caller must enforce current object scope before entering command
            return prior.result
        result=fn(actor)
        result=json.loads(json.dumps(result,default=str))
        CommandResult.objects.create(actor=actor,key=key,action=action,digest=digest,result=result)
        return result
