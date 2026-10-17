from django.utils import timezone
from django.db import transaction
from rest_framework.exceptions import ValidationError,PermissionDenied
from core.models import *
from core.policy import *
from .common import version,event,Conflict

def ensure_load(actor,lead):
    """Attach a pickup to its driver's current route, creating that route if needed."""
    if lead.load_id:
        load=lead.load
        if load.driver_id!=lead.driver_id or load.franchise_id!=lead.franchise_id:
            raise Conflict('This pickup is attached to a different driver route. Ask HQ to review it.')
        return load
    if not lead.driver_id:
        raise Conflict('Assign this pickup to a collector before adding it to a load.')
    load=Load.objects.filter(driver_id=lead.driver_id,franchise_id=lead.franchise_id,
                             state='on_route',counted_in__isnull=True).order_by('-created_at','pk').first()
    if not load:
        warehouse=Warehouse.objects.filter(franchise_id=lead.franchise_id).order_by('created_at','pk').first()
        if not warehouse:
            raise Conflict('Set up a warehouse for this franchise before starting a collector route.')
        load=Load.objects.create(driver_id=lead.driver_id,franchise_id=lead.franchise_id,warehouse=warehouse)
    lead.load=load
    lead.version+=1
    lead.save(update_fields=['load','version','updated_at'])
    event(actor,'loads',load,{'lead':str(lead.pk)},'load.pickup_linked')
    return load

@transaction.atomic
def assign(actor,lead,data):
    require(actor,'lead.assign');book_allowed(actor,lead.franchise);version(lead,data.get('version'))
    if lead.franchise.status!='active': raise Conflict('This franchise is not accepting new assignments.')
    if lead.started_at or lead.state not in ('unassigned','scheduled'): raise Conflict('Only unstarted leads can be assigned.')
    driver=User.objects.filter(pk=data.get('driver'),is_active=True,franchise=lead.franchise,role__in=[Role.DRIVER,Role.COLLECTOR]).first()
    if not driver: raise ValidationError('Choose an active driver from this franchise.')
    old=lead.driver_id;lead.driver=driver;lead.state='scheduled';lead.version+=1;lead.save()
    event(actor,'leads',lead,{'old_driver':old,'new_driver':driver.id},'lead.assigned')
    # An old audience reset carries no record or new-owner data.
    if old and old!=driver.id: Outbox.objects.create(actor=actor,feature='scope_reset',owner_id=old,resource_id='')
    return lead

@transaction.atomic
def start(actor,lead,data):
    require(actor,'pickup.write');book_allowed(actor,lead.franchise)
    if lead.driver_id!=actor.id: raise PermissionDenied()
    if lead.state not in ('scheduled','weighing'): raise Conflict('This pickup cannot be started or reopened.')
    if lead.franchise.status!='active': raise Conflict('Franchise is inactive.')
    if lead.state=='weighing' and not lead.started_at:
        raise Conflict('Pickup state is inconsistent. Refresh the assigned lead.')
    if lead.state=='scheduled':
        version(lead,data.get('version'));ensure_load(actor,lead);lead.state='weighing';lead.started_at=timezone.now();lead.version+=1;lead.save()
    elif data.get('version') is not None and str(data['version'])!=str(lead.version):
        raise Conflict('This pickup changed. Refresh before continuing.')
    bill,_=Bill.objects.get_or_create(lead=lead,defaults={'franchise':lead.franchise,'creator':actor,'customer':lead.customer,'kind':'collection'})
    event(actor,'leads',lead,action='pickup.started');return bill

@transaction.atomic
def advance(actor,lead,action,data):
    locked=Lead.objects.select_for_update().select_related('franchise').get(pk=lead.pk)
    # Preserve the caller's model instance semantics for service callers that
    # chain sent -> agreement using the returned version.
    lead.__dict__.update(locked.__dict__)
    require(actor,'pickup.write');book_allowed(actor,lead.franchise)
    if lead.driver_id!=actor.id: raise PermissionDenied()
    version(lead,data.get('version'))
    if action=='failed':
        if lead.state not in ('scheduled','weighing') or (hasattr(lead,'bill') and lead.bill.state!='draft'): raise Conflict('This pickup cannot be marked not completed.')
        reason=str(data.get('reason','')).strip()
        if not reason or len(reason)>500: raise ValidationError('Enter a failure reason of at most 500 characters.')
        lead.state='not_completed';lead.failure_reason=reason
    else:
        if not hasattr(lead,'bill') or lead.bill.state!='issued': raise Conflict('Lock the purchase bill first.')
        if action=='whatsapp-opened':
            event(actor,'leads',lead,{'channel':'whatsapp','attempted_at':timezone.now().isoformat()},'share.whatsapp_opened');return lead
        if action=='sent':
            if policy('customer_sharing').get('confirmation')!='collector_assertion': raise PolicyUnavailable('Sent confirmation policy is not supported.')
            if lead.sent_at: raise Conflict('Sharing was already confirmed.')
            lead.sent_at=timezone.now();lead.state='sent'
        elif action=='agree':
            if not lead.sent_at: raise Conflict('Confirm sharing before recording agreement.')
            if lead.agreed_at: raise Conflict('Agreement was already recorded.')
            lead.agreed_at=timezone.now();lead.state='agreed'
        else: raise ValidationError('Unknown pickup action.')
    lead.version+=1;lead.save()
    details={'reason':lead.failure_reason} if action=='failed' else None
    event(actor,'leads',lead,details, 'pickup.'+action)
    return lead
