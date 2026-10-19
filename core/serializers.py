from rest_framework import serializers
from .models import *
FIELDS={
'franchises':(Franchise,['id','name','code','kind','status','phone','email','address','district','state','gstin','owner_name','share_percent','version']),
'customers':(Customer,['id','franchise','name','phone','address','area','state','gstin','active','version']),
'categories':(Category,['id','name','kind','default_billing_mode','active','version']),
'products':(Product,['id','name','category','unit','active','tracks_stock','hsn','tax_percent','franchises','version']),
'rates':(Rate,['id','product','franchise','kind','amount','discount_percent','actor','reason','created_at','version']),
'leads':(Lead,['id','franchise','customer','franchise_manager','driver','load','pickup_date','pickup_time','items','latitude','longitude','state','failure_reason','started_at','sent_at','agreed_at','closed_at','version']),
'bills':(Bill,['id','franchise','creator','customer','lead','kind','transaction_type','mode','billing_mode','state','number','lines','snapshot','subtotal','tax','total','settled','share_percent','notes','issued_at','created_at','version']),
'quotations':(Quotation,['id','franchise','creator','customer','kind','billing_mode','number','state','lines','subtotal','tax','total','notes','sent_at','accepted_at','rejected_at','converted_at','converted_bill','created_at','version']),
'pickups':(Bill,['id','franchise','creator','customer','lead','kind','state','number','lines','subtotal','tax','total','settled','notes','issued_at','version']),
'payments':(Payment,['id','bill','actor','amount','method','reference','paid_at','created_at']),
'earnings':(EarningsEntry,['id','franchise','bill','actor','amount','kind','reference','created_at']),
'loads':(Load,['id','franchise','driver','warehouse','state','vehicle_full_at','returned_at','unloaded_at','created_at','reconciliation_state','reweigh_version','resolution_action','resolution_note','resolved_by','resolved_at','version']),
'receipts':(Receipt,['id','lead','warehouse','actor','lines','note','explanation','submitted_at','flagged','version']),
'inventory':(StockBalance,['id','warehouse','product','quantity','version']),
'movements':(StockMovement,['id','warehouse','product','actor','quantity','kind','receipt','receipt_revision','requirement','created_at']),
'requirements':(Requirement,['id','franchise','product','actor','quantity','released','due_date','note','state','created_at','version']),
'cash':(CashMovement,['id','driver','franchise','actor','amount','kind','payment','handover','created_at']),
'handovers':(Handover,['id','driver','franchise','counter','expected','received','difference','note','state','cutoff','created_at','reviewed_by','reviewed_at','review_note','version']),
'audit':(AuditEvent,['id','actor','franchise','action','resource','details','created_at']),
'settings':(CompanySetting,['id','section','values','version']),
'imports':(ImportBatch,['id','rows','committed','created_at','version']),
'jobs':(Job,['id','kind','state','attempts','error','expires_at','created_at']),
}
SERIALIZERS={}
for name,(model,fields) in FIELDS.items():
    meta=type('Meta',(),{'model':model,'fields':fields})
    SERIALIZERS[name]=type(name.title()+'Serializer',(serializers.ModelSerializer,),{'Meta':meta,'__module__':__name__})

def serialize(feature,obj,many=False):
    if many: return [serialize(feature,o) for o in obj]
    data=dict(SERIALIZERS[feature](obj).data)
    for key in ('franchise','customer','product','warehouse','driver','franchise_manager','actor','creator','counter','category','reviewed_by','resolved_by'):
        if key in data and data[key] is not None:
            value=getattr(obj,key)
            data[key+'_name']=(value.get_full_name() or value.email) if isinstance(value,User) else getattr(value,'name',str(value.pk))
    if isinstance(obj,Product):
        data['category_kind']=obj.category.kind
        data['updated_at']=obj.updated_at.isoformat()
    if isinstance(obj,(StockBalance,StockMovement,Requirement)):
        data['unit']=obj.product.unit
    if isinstance(obj,Requirement):
        data['remaining']=str(obj.quantity-obj.released)
    if isinstance(obj,Lead):
        data['phone']=obj.customer.phone;data['address']=obj.customer.address;data['area']=obj.customer.area
        data['assignment_status']=('assigned_to_app_franchise_manager'
                                   if obj.franchise_manager_id and not obj.driver_id else obj.state)
        data['bill_id']=str(obj.bill.id) if hasattr(obj,'bill') else None
        load=getattr(obj,'load',None)
        data['current_load']={'id':str(load.pk),'state':load.state,'warehouse':str(load.warehouse_id),
                              'returned_at':load.returned_at.isoformat() if load.returned_at else None,
                              'unloaded_at':load.unloaded_at.isoformat() if load.unloaded_at else None} if load else None
        data['failure_history']=[{'id':str(entry.pk),'at':entry.created_at.isoformat(),
                                  'actor':str(entry.actor_id),'reason':entry.details.get('reason','')}
                                 for entry in AuditEvent.objects.filter(resource=str(obj.pk),
                                     franchise_id=obj.franchise_id,action='pickup.failed').order_by('-created_at','-pk')]
    if isinstance(obj,Bill):
        from .services.billing import cancellation_history
        data['outstanding']=str(obj.total-obj.settled)
        data['direction']='incoming' if obj.kind=='sale' else 'outgoing'
        data['cancellation_requests']=[{'id':str(entry.pk),'requested_at':entry.created_at.isoformat(),
                                       'requested_by':str(entry.actor_id),'reason':entry.details.get('reason',''),
                                       'status':'pending'} for entry in cancellation_history(obj)]
    if isinstance(obj,Quotation):
        data['converted_bill_number']=obj.converted_bill.number if obj.converted_bill_id else None
    if isinstance(obj,Payment):
        data['direction']='incoming' if obj.bill.kind=='sale' else 'outgoing'
        data['franchise_name']=obj.bill.franchise.name
        data['bill_number']=obj.bill.number
        data['customer_name']=obj.bill.customer.name
        data['bill_total']=str(obj.bill.total)
        data['bill_outstanding']=str(obj.bill.total-obj.bill.settled)
    if isinstance(obj,Handover):
        data['included_routes']=[str(r.load_id) for r in obj.routes.all()]
        data['decision_actions']=['verified','disputed'] if obj.state=='awaiting_supervisor' else []
    if isinstance(obj,Receipt):
        data['state']='submitted' if obj.submitted_at else 'draft'
        data['revision_history']=[{'revision':r.revision,'lines':r.lines,'note':r.note,'actor':str(r.actor_id),
                                   'actor_name':r.actor.get_full_name() or r.actor.email,
                                   'submitted_at':r.submitted_at.isoformat() if r.submitted_at else None,
                                   'flagged':r.flagged,'superseded_at':r.superseded_at.isoformat() if r.superseded_at else None}
                                  for r in obj.revisions.filter(superseded_at__isnull=False).select_related('actor').order_by('revision')]
    if isinstance(obj,Load):
        membership=getattr(obj,'counted_in',None)
        data['handover']=str(membership.handover_id) if membership else None
        data['settlement_state']=membership.handover.state if membership else 'on_route' if obj.state=='on_route' else 'count_pending'
        data['included_in_handover']=bool(membership)
        data['pickup_count']=getattr(obj,'pickup_count',0)
        data['paid_pickups']=getattr(obj,'paid_pickups',0)
        data['pickup_settled']=str(getattr(obj,'pickup_settled',None) or 0)
        data['pickups']=[{
            'id':str(lead.pk),'customer_name':lead.customer.name,'pickup_date':lead.pickup_date.isoformat(),
            'lead_state':lead.state,'bill_number':lead.bill.number if hasattr(lead,'bill') else None,
            'bill_state':lead.bill.state if hasattr(lead,'bill') else 'not_issued',
            'total':str(lead.bill.total) if hasattr(lead,'bill') else '0',
            'settled':str(lead.bill.settled) if hasattr(lead,'bill') else '0',
            'outstanding':str(lead.bill.total-lead.bill.settled) if hasattr(lead,'bill') else '0',
            'items':lead.bill.lines if hasattr(lead,'bill') else [],
        } for lead in obj.lead_set.all()]
    if isinstance(obj,Receipt):
        data['lead_version']=obj.lead.version
        data['customer_name']=obj.lead.customer.name
        data['load']=str(obj.lead.load_id) if obj.lead.load_id else None
        data['state']='submitted' if obj.submitted_at else 'draft'
    return data
