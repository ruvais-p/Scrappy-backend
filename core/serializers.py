from rest_framework import serializers
from .models import *
FIELDS={
'franchises':(Franchise,['id','name','code','kind','status','phone','email','address','district','state','gstin','owner_name','share_percent','version']),
'customers':(Customer,['id','franchise','name','phone','address','area','state','gstin','active','version']),
'categories':(Category,['id','name','kind','active','version']),
'products':(Product,['id','name','category','unit','active','tracks_stock','hsn','tax_percent','franchises','version']),
'rates':(Rate,['id','product','franchise','kind','amount','actor','reason','created_at','version']),
'leads':(Lead,['id','franchise','customer','driver','load','pickup_date','pickup_time','items','latitude','longitude','state','failure_reason','started_at','sent_at','agreed_at','closed_at','version']),
'bills':(Bill,['id','franchise','creator','customer','lead','kind','state','number','lines','snapshot','subtotal','tax','total','settled','share_percent','notes','issued_at','created_at','version']),
'pickups':(Bill,['id','franchise','creator','customer','lead','kind','state','number','lines','subtotal','tax','total','settled','notes','issued_at','version']),
'payments':(Payment,['id','bill','actor','amount','method','reference','paid_at','created_at']),
'earnings':(EarningsEntry,['id','franchise','bill','actor','amount','kind','reference','created_at']),
'loads':(Load,['id','franchise','driver','warehouse','state','returned_at','unloaded_at','created_at','version']),
'receipts':(Receipt,['id','lead','warehouse','actor','lines','note','submitted_at','flagged','version']),
'inventory':(StockBalance,['id','warehouse','product','quantity','version']),
'movements':(StockMovement,['id','warehouse','product','actor','quantity','kind','receipt','requirement','created_at']),
'requirements':(Requirement,['id','franchise','product','actor','quantity','released','due_date','note','state','created_at','version']),
'cash':(CashMovement,['id','driver','franchise','actor','amount','kind','payment','handover','created_at']),
'handovers':(Handover,['id','driver','franchise','counter','expected','received','difference','note','state','cutoff','version']),
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
    for key in ('franchise','customer','product','warehouse','driver','actor','creator','counter','category'):
        if key in data and data[key] is not None:
            value=getattr(obj,key)
            data[key+'_name']=(value.get_full_name() or value.email) if isinstance(value,User) else getattr(value,'name',str(value.pk))
    if isinstance(obj,(StockBalance,StockMovement,Requirement)):
        data['unit']=obj.product.unit
    if isinstance(obj,Requirement):
        data['remaining']=str(obj.quantity-obj.released)
    if isinstance(obj,Lead):
        data['phone']=obj.customer.phone;data['address']=obj.customer.address;data['area']=obj.customer.area
        data['bill_id']=str(obj.bill.id) if hasattr(obj,'bill') else None
    if isinstance(obj,Bill):
        data['outstanding']=str(obj.total-obj.settled)
        data['direction']='incoming' if obj.kind=='sale' else 'outgoing'
    if isinstance(obj,Payment):
        data['direction']='incoming' if obj.bill.kind=='sale' else 'outgoing'
        data['bill_number']=obj.bill.number
        data['customer_name']=obj.bill.customer.name
        data['bill_total']=str(obj.bill.total)
        data['bill_outstanding']=str(obj.bill.total-obj.bill.settled)
    if isinstance(obj,Handover): data['included_routes']=[str(r.load_id) for r in obj.routes.all()]
    if isinstance(obj,Receipt):
        data['lead_version']=obj.lead.version
        data['customer_name']=obj.lead.customer.name
        data['load']=str(obj.lead.load_id) if obj.lead.load_id else None
        data['state']='submitted' if obj.submitted_at else 'draft'
    return data
