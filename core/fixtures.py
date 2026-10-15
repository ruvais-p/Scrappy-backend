from datetime import time,timedelta
from django.utils import timezone
from .models import *

def synthetic_records(password=None,policies=False):
    books=[]
    for code,name,kind in [('NTH','North Yard','scrap'),('STH','South Yard','scrap'),('DGN','North Digester','digester'),('DGS','South Digester','digester')]:
        b,_=Franchise.objects.get_or_create(code=code,defaults={'name':name,'kind':kind,'share_percent':20 if kind=='digester' else 0});books.append(b)
    warehouses=[Warehouse.objects.get_or_create(franchise=b,name=b.name+' Warehouse')[0] for b in books[:2]]
    accounts={}
    for role in Role.values:
        b=None if role in [Role.SUPER,Role.ADMIN,Role.HQ] else books[2] if role in [Role.DIGESTER,Role.STAFF] else books[0]
        email=role+'@example.test';u,created=User.objects.get_or_create(username=email,defaults={'email':email,'role':role,'franchise':b,'warehouse':warehouses[0] if role==Role.WAREHOUSE else None,'first_name':Role(role).label})
        if created:
            if password:u.set_password(password)
            else:u.set_unusable_password()
            u.full_clean();u.save()
        accounts[role]=u
    for b in books:
        role=Role.COLLECTOR if b.kind=='scrap' else Role.STAFF
        email=b.code.lower()+'-staff@example.test'
        u,created=User.objects.get_or_create(username=email,defaults={'email':email,'role':role,'franchise':b,'first_name':b.name+' Staff'})
        if created:u.set_unusable_password();u.save()
    category,_=Category.objects.get_or_create(name='Recyclable materials',kind='scrap')
    products=[]
    for name,rate in [('Iron',28),('Paper',10),('Aluminium',120),('Plastic',15)]:
        p,_=Product.objects.get_or_create(name=name,category=category,defaults={'unit':'kg'})
        p.franchises.add(*books[:2]);products.append(p)
        if not Rate.objects.filter(product=p).exists(): Rate.objects.create(product=p,kind='collection',amount=rate,actor=accounts[Role.SUPER])
    collector=accounts[Role.COLLECTOR]
    load,_=Load.objects.get_or_create(driver=collector,franchise=books[0],warehouse=warehouses[0])
    leads=[]
    for i,name in enumerate(['Sample Household One','Sample Household Two','Sample Workshop']):
        customer,_=Customer.objects.get_or_create(franchise=books[0],phone='900000000'+str(i),defaults={'name':name,'address':'Synthetic test address','area':'North district'})
        lead,_=Lead.objects.get_or_create(customer=customer,pickup_date=timezone.localdate(),defaults={'franchise':books[0],'driver':collector,'load':load,'pickup_time':time(9+i),'items':'Paper, iron','state':'scheduled'})
        leads.append(lead)
    if policies:
        values={'billing_issue':{'issuer_by_book':{str(b.id):{'name':b.name+' — synthetic issuer'} for b in books},'tax_mode':'none','rounding':'half_up_2','number_prefix':'TEST'},'customer_sharing':{'confirmation':'collector_assertion','recipient_check':'bearer_link','lifetime_hours':24},'export_delivery':{'boundary':'all_filtered','formats':['csv','xlsx'],'max_rows':10000},'audit_delivery':{'enabled':False,'endpoint':'','timeout_seconds':5},'warehouse_receipt_eligibility':{},'route_eligibility':{},'digester_earnings':{},'lead_import':{'commit':'whole_file','duplicates':'reject_same_file'}}
        for key,config in values.items():Policy.objects.update_or_create(key=key,defaults={'config':config,'decision_reference':'Synthetic test configuration; not a production approval','test_only':True})
    return {'books':books,'warehouses':warehouses,'users':accounts,'products':products,'leads':leads,'load':load}
