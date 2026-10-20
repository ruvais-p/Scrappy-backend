import json
from datetime import datetime
from decimal import Decimal
from django.contrib.auth.password_validation import validate_password
from django.db import transaction
from django.db.models import Case,Count,IntegerField,Sum,Q,Prefetch,When,F
from django.utils import timezone
from django.http import HttpResponse
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError,NotFound,PermissionDenied
from drf_spectacular.utils import extend_schema
from .models import *
from .policy import *
from .serializers import FIELDS,serialize
from .presentation_schema import DashboardResponse,RegisterResponse,EarningsResponse
from .auth import identity,issue_token
from .services.common import command,event,version,decimal,Conflict
from .services import billing,cash,pickups,warehouse,imports,quotations

ALIASES={'movements':'inventory','handovers':'cash'}
def feature_scope(user,feature,qs=None):
    model=FIELDS[feature][0]
    qs=qs if qs is not None else model.objects.all()
    if feature=='jobs': return qs.filter(actor=user)
    if feature=='settings':
        if has_permission(user,'company.billing.manage'): return qs
        require(user,'settings');return qs
    if feature=='imports': require(user,'imports');return qs
    if feature=='leads' and user.role==Role.APP:
        # HQ routes leads directly to an App Franchise Manager. Their work
        # queue can span franchises, while every lead remains explicitly
        # assigned to that manager and collector choices stay franchise-local.
        return qs.filter(franchise_manager=user)
    qs=scope(user,qs,ALIASES.get(feature,feature))
    if feature=='pickups': qs=qs.filter(lead__isnull=False)
    return qs

def get_object(user,feature,pk,lock=False):
    if feature not in FIELDS: raise NotFound()
    qs=FIELDS[feature][0].objects.select_for_update() if lock else FIELDS[feature][0].objects.all()
    try: return feature_scope(user,feature,qs).get(pk=pk)
    except (FIELDS[feature][0].DoesNotExist,ValueError): raise NotFound()

class RegisterView(APIView):
    @extend_schema(responses=RegisterResponse)
    def get(self,request,feature,pk=None):
        if feature not in FIELDS: raise NotFound()
        if pk: return Response(serialize(feature,get_object(request.user,feature,pk)))
        qs=feature_scope(request.user,feature)
        if feature=='leads': qs=qs.select_related('franchise_manager','driver','franchise','customer')
        from .queries import filter_records
        qs,date_field=filter_records(qs,feature,request.query_params)
        fields={f.name for f in qs.model._meta.fields}
        count=qs.count()
        try: page=max(1,int(request.query_params.get('page',1)))
        except ValueError: raise ValidationError('Invalid page.')
        summary={}
        if feature=='leads':
            summary.update({state:qs.filter(state=state).count() for state in ('unassigned','scheduled','weighing','sent','agreed','closed','not_completed','cancelled')})
            summary['unassigned']=qs.filter(franchise_manager__isnull=True,driver__isnull=True,state='unassigned').count()
            summary['assigned_to_app_franchise_manager']=qs.filter(franchise_manager__isnull=False,driver__isnull=True).count()
            paid=Bill.objects.filter(lead__in=qs).exclude(state='draft').aggregate(total=Sum('total'),settled=Sum('settled'))
            summary.update({'billed':str(paid['total'] or 0),'paid':str(paid['settled'] or 0),
                            'outstanding':str((paid['total'] or 0)-(paid['settled'] or 0))})
        if feature=='handovers':
            totals=qs.aggregate(expected=Sum('expected'),received=Sum('received'),difference=Sum('difference'))
            summary={key:str(value or 0) for key,value in totals.items()}
            summary['basis']='Saved handovers in the full filtered result; each handover counted once'
            summary['currency']='INR'
            summary['states']={state:qs.filter(state=state).count() for state in ('awaiting_supervisor','verified','disputed')}
            qs=qs.select_related('driver','franchise','counter').prefetch_related('routes')
        if feature=='loads':
            qs=qs.select_related('counted_in__handover').prefetch_related(
                Prefetch('lead_set',queryset=Lead.objects.select_related('customer','bill').order_by('pickup_date','created_at','pk'))
            ).annotate(
                pickup_count=Count('lead',distinct=True),
                paid_pickups=Count('lead',filter=Q(lead__bill__state='completed'),distinct=True),
                pickup_settled=Sum('lead__bill__settled'),
            )
        if feature in ('bills','pickups'):
            active=qs.exclude(state__in=['draft','cancelled'])
            for direction,kinds in [('outgoing',['collection','b2b']),('incoming',['sale'])]:
                totals=active.filter(kind__in=kinds).aggregate(total=Sum('total'),settled=Sum('settled'))
                summary.update({direction+'_'+k:str(v or 0) for k,v in totals.items()})
                summary[direction+'_outstanding']=str((totals['total'] or 0)-(totals['settled'] or 0))
        ordering=Case(When(state='closed',then=1),default=0,output_field=IntegerField()) if feature=='leads' else None
        ordered=qs.order_by(ordering,'-created_at') if ordering is not None and 'created_at' in fields else qs.order_by('-created_at') if 'created_at' in fields else qs.order_by('pk')
        payload={'count':count,'page':page,'page_size':50,'date_basis':date_field,'summary':summary,'results':serialize(feature,ordered[(page-1)*50:page*50],True)}
        if request.query_params.get('presentation')=='1':
            from .presentation import enhance_register
            payload=enhance_register(request.user,feature,qs,payload,request.query_params)
        return Response(payload)

    @extend_schema(request=dict,responses=dict)
    def post(self,request,feature,pk=None):
        return RegisterPostMixin.post(self,request,feature,pk)

class WarehouseView(APIView):
    def get(self,request):
        require(request.user,'warehouses')
        rows=Warehouse.objects.select_related('franchise').order_by('franchise__name','name')
        return Response({'results':[{'id':str(w.pk),'name':w.name,'franchise':str(w.franchise_id),'franchise_name':w.franchise.name,'version':w.version} for w in rows]})

    def post(self,request):
        require(request.user,'warehouses');data=request.data
        def run(actor):
            require(actor,'warehouses')
            if data.get('id'):
                try: obj=Warehouse.objects.select_for_update().select_related('franchise').get(pk=data['id'])
                except (Warehouse.DoesNotExist,ValueError): raise NotFound()
                version(obj,data.get('version'))
                name=data.get('name')
                if not isinstance(name,str) or not name.strip() or len(name.strip())>160: raise ValidationError({'name':'Enter a name up to 160 characters.'})
                obj.name=name.strip();obj.version+=1;obj.save(update_fields=['name','version','updated_at'])
                event(actor,'warehouses',obj,action='warehouse.updated')
            else:
                name=data.get('name');book_id=data.get('franchise')
                if not isinstance(name,str) or not name.strip() or len(name.strip())>160: raise ValidationError({'name':'Enter a name up to 160 characters.'})
                try: book=Franchise.objects.get(pk=book_id,status='active')
                except (Franchise.DoesNotExist,ValueError,TypeError): raise ValidationError({'franchise':'Choose an active franchise.'})
                book_allowed(actor,book)
                obj=Warehouse.objects.create(name=name.strip(),franchise=book)
                event(actor,'warehouses',obj,action='warehouse.created')
            return {'id':str(obj.pk),'name':obj.name,'franchise':str(obj.franchise_id),'franchise_name':obj.franchise.name,'version':obj.version}
        return Response(command(request.user,request.headers.get('Idempotency-Key'),'warehouse.write',data,run))

class RegisterPostMixin(APIView):
    @extend_schema(responses=dict)
    def get(self,request,feature,pk):
        if feature not in ('leads','bills','pickups','requirements','receipts','inventory'):
            raise NotFound()
        if request.user.role not in (Role.ADMIN,Role.COLLECTOR,*SEGMENT_NETWORK):
            raise PermissionDenied('Record timelines are limited to local operational roles.')
        obj=get_object(request.user,feature,pk)
        events=AuditEvent.objects.filter(resource=str(obj.pk)).order_by('-created_at')
        if request.user.role==Role.COLLECTOR:
            allowed=feature in ('leads','pickups','bills') and (getattr(obj,'driver_id',None)==request.user.pk or getattr(obj,'creator_id',None)==request.user.pk or getattr(obj,'lead',None) and obj.lead.driver_id==request.user.pk)
            if not allowed: raise NotFound()
            events=events.filter(franchise_id=request.user.franchise_id)
        return Response({'resource':str(obj.pk),'feature':feature,'count':events.count(),'results':serialize('audit',events[:50],True)})
    @extend_schema(request=dict,responses=dict)
    def post(self,request,feature,pk=None):
        if pk: raise PermissionDenied('Use an explicit command.')
        data=request.data
        if feature=='imports':
            if 'file' not in request.FILES: raise ValidationError('Choose a CSV or XLSX file.')
            batch=imports.preview(request.user,request.FILES['file']);return Response(serialize(feature,batch),status=201)
        action={'franchises':'franchise.create','customers':'customer.create','products':'product.write','categories':'product.write','rates':'rate.write','bills':'bill.write','quotations':'quotation.write','requirements':'requirement.write'}.get(feature)
        if not action: raise PermissionDenied()
        require(request.user,action)
        def create(actor):
            require(actor,action)
            if feature=='bills': obj=billing.save_draft(actor,data)
            elif feature=='quotations': obj=quotations.save(actor,data)
            elif feature=='requirements': obj=warehouse.create_requirement(actor,data)
            elif feature=='rates':
                product=scope(actor,Product.objects.all(),'products').filter(pk=data.get('product')).first()
                if not product: raise NotFound()
                book=Franchise.objects.get(pk=data['franchise']) if data.get('franchise') else None
                if book and not eligible_product(actor,product,book): raise ValidationError('Product is not assigned to this franchise.')
                if data.get('kind') not in ('collection','b2b','sale'): raise ValidationError('Choose a rate type.')
                discount=decimal(data.get('discount_percent') or 0,2,positive=False)
                if discount>100: raise ValidationError('Discount must be between 0 and 100 percent.')
                if discount and actor.role not in (Role.SUPER,Role.ADMIN,*SEGMENT_NETWORK): raise PermissionDenied('Only Admin and Super Admin can set a discount.')
                obj=Rate.objects.create(product=product,franchise=book,kind=data['kind'],amount=decimal(data.get('amount')),discount_percent=discount,actor=actor,reason=str(data.get('reason',''))[:500]);event(actor,feature,obj,{'new':str(obj.amount),'discount_percent':str(obj.discount_percent),'reason':obj.reason},'rate.changed')
            else:
                allowed={'franchises':['name','code','kind','phone','email','address','district','state','gstin','owner_name'], 'customers':['name','phone','address','area','state','gstin'],'categories':['name','kind','default_billing_mode'],'products':['name','category','unit','tracks_stock','hsn','tax_percent']}[feature]
                values={k:data[k] for k in allowed if k in data}
                if feature=='products' and not values.get('tax_percent'): values['tax_percent']=0
                if feature=='customers':
                    book=Franchise.objects.get(pk=data.get('franchise') or actor.franchise_id);book_allowed(actor,book)
                    values['franchise']=book;values['phone']=''.join(c for c in str(values.get('phone','')) if c.isdigit())
                    if not 7<=len(values['phone'])<=15: raise ValidationError('Enter a valid phone number.')
                    if Customer.objects.filter(franchise=book,phone=values['phone']).exists():
                        raise Conflict('A customer with this phone already exists in this franchise.')
                if feature=='products': values['category']=Category.objects.get(pk=values.get('category'))
                if feature=='franchises' and values.get('kind') not in ('scrap','digester'): raise ValidationError('Choose Scrap or Digester.')
                obj=FIELDS[feature][0](**values);obj.full_clean();obj.save();event(actor,feature,obj)
            return serialize(feature,obj)
        return Response(command(request.user,request.headers.get('Idempotency-Key'),feature+'.create',dict(data),create),status=201)

class CommandView(APIView):
    @extend_schema(request=dict,responses=dict)
    def post(self,request,feature,pk,action):
        data=request.data
        get_object(request.user,feature,pk) # replay also requires current scope
        permission={('leads','assign'):'lead.assign',('leads','reschedule'):'lead.schedule',('leads','cancel'):'lead.schedule',('leads','start'):'pickup.write',('leads','sent'):'pickup.write',('leads','agree'):'pickup.write',('leads','failed'):'pickup.write',('leads','whatsapp-opened'):'pickup.write',('leads','receive'):'receipt.write',('leads','save-reconciliation'):'receipt.write',('loads','unload'):'load.unload',('loads','resolve'):'load.resolve',('loads','vehicle-full'):'load.vehicle_full',('receipts','explain'):'receipt.explain',('handovers','review'):'handover.review',('bills','save'):'bill.write',('bills','issue'):'bill.write',('bills','pay'):'bill.write',('bills','request-cancellation'):'bill.write',('pickups','save'):'pickup.write',('pickups','issue'):'pickup.write',('pickups','pay'):'pickup.write',('requirements','release'):'release.write',('requirements','cancel'):'requirement.write',('imports','commit'):'imports',('franchises','edit'):'franchise.edit',('franchises','suspend'):'franchise.suspend',('customers','edit'):'customer.edit',('products','assign'):'product.assign',('products','edit'):'product.write'}.get((feature,action))
        if feature=='quotations' and action in ('save','send','accept','reject','convert'):
            permission='bill.write' if action=='convert' else 'quotation.write'
        if feature=='categories' and action=='edit':
            permission='company.billing.manage' if 'default_billing_mode' in data else 'product.write'
        if not permission: raise PermissionDenied('This action is not defined.')
        require(request.user,permission)
        def run(actor):
            require(actor,permission);obj=get_object(actor,feature,pk,True);out=feature
            if feature in ('bills','pickups'):
                if obj.lead_id and obj.creator_id!=actor.pk: raise PermissionDenied('Pickup writes belong to the assigned collector.')
                if action=='request-cancellation':
                    obj=billing.request_cancellation(actor,obj,data)
                else: obj=billing.save_draft(actor,data,obj) if action=='save' else billing.issue(actor,obj,data) if action=='issue' else billing.pay(actor,obj,data)
            elif feature=='quotations':
                if action=='save': obj=quotations.save(actor,data,obj)
                elif action=='convert': obj=quotations.convert(actor,obj,data);out='bills'
                else: obj=quotations.transition(actor,obj,action,data)
            elif feature=='leads':
                if action=='assign': obj=pickups.assign(actor,obj,data)
                elif action=='start': obj=pickups.start(actor,obj,data);out='pickups'
                elif action in ('receive','save-reconciliation'):
                    obj=warehouse.save_measurements(actor,obj,data,submit=action=='receive');out='receipts'
                elif action in ('reschedule','cancel'):
                    version(obj,data.get('version'))
                    if obj.started_at or obj.state not in ('scheduled','unassigned'): raise Conflict('Only unstarted leads can be changed.')
                    if action=='cancel':
                        obj.state='cancelled';obj.failure_reason='Cancelled before pickup start.'
                    else:
                        try: obj.pickup_date=datetime.strptime(data['pickup_date'],'%Y-%m-%d').date();obj.pickup_time=datetime.strptime(data['pickup_time'],'%H:%M').time()
                        except (KeyError,ValueError): raise ValidationError('Enter a valid date and time.')
                        if obj.pickup_date<timezone.localdate(): raise ValidationError('Date cannot be in the past.')
                    obj.version+=1;obj.save();event(actor,'leads',obj,action='lead.'+action)
                else: obj=pickups.advance(actor,obj,action,data)
            elif feature=='loads' and action=='vehicle-full':
                version(obj,data.get('version'))
                if obj.driver_id!=actor.pk: raise PermissionDenied('You can only flag your assigned vehicle.')
                if obj.state!='on_route': raise Conflict('Only an active route can be marked vehicle full.')
                if obj.vehicle_full_at: raise Conflict('This vehicle has already been marked full.')
                obj.vehicle_full_at=timezone.now();obj.version+=1;obj.save(update_fields=['vehicle_full_at','version','updated_at'])
                event(actor,'loads',obj,{'warehouse':str(obj.warehouse_id),'vehicle_full_at':obj.vehicle_full_at.isoformat()},'load.vehicle_full')
            elif feature=='loads' and action=='unload':
                version(obj,data.get('version'))
                if actor.role==Role.WAREHOUSE and obj.warehouse_id!=actor.warehouse_id: raise PermissionDenied()
                if obj.state=='returned' and obj.unloaded_at and obj.eligibility_reference.strip():
                    raise Conflict('This load already has unloading evidence.')
                reference=data.get('reference')
                if not isinstance(reference,str) or not reference.strip() or len(reference.strip())>500:
                    raise ValidationError('Enter unloading evidence or a reference (1 to 500 characters).')
                obj.state='returned';obj.returned_at=timezone.now();obj.unloaded_at=timezone.now()
                obj.eligibility_reference=reference.strip();obj.version+=1;obj.save()
                event(actor,'loads',obj,{'reference':obj.eligibility_reference},'load.unloaded')
            elif feature=='loads' and action=='resolve':
                obj=warehouse.resolve_flagged_load(actor,obj,data)
            elif feature=='receipts' and action=='explain':
                version(obj,data.get('version'))
                if actor.role!=Role.WAREHOUSE or obj.warehouse_id!=actor.warehouse_id:
                    raise PermissionDenied('Only the assigned Warehouse Manager can explain this receipt.')
                if not obj.flagged or not obj.submitted_at:
                    raise Conflict('Only a submitted flagged receipt can be explained.')
                explanation=data.get('explanation')
                if not isinstance(explanation,str) or not explanation.strip() or len(explanation.strip())>1000:
                    raise ValidationError('Enter an explanation (1 to 1,000 characters).')
                obj.explanation=explanation.strip();obj.version+=1
                obj.save(update_fields=['explanation','version','updated_at'])
                event(actor,'receipts',obj,{'customer':obj.lead.customer.name,'explanation':obj.explanation},'receipt.explanation_added')
            elif feature=='handovers' and action=='review':
                obj=cash.review_handover(actor,obj,data)
            elif feature=='requirements':
                if action=='release': obj=warehouse.release(actor,obj,data)
                else: obj=warehouse.cancel_requirement(actor,obj,data)
            elif feature=='imports': obj=imports.commit(actor,obj)
            elif feature in ('franchises','customers'):
                version(obj,data.get('version'))
                if feature=='franchises' and obj.kind=='hq' and actor.role!=Role.SUPER: raise PermissionDenied()
                if action=='suspend':
                    policy('franchise_suspension');obj.status='suspended'
                else:
                    for k in ('name','phone','email','address','district','state','gstin','owner_name','area'):
                        if hasattr(obj,k) and k in data: setattr(obj,k,data[k])
                obj.full_clean();obj.version+=1;obj.save();event(actor,feature,obj)
            elif feature in ('products','categories'):
                version(obj,data.get('version'))
                old_billing_mode=obj.default_billing_mode if feature=='categories' else None
                if action=='assign':
                    books=Franchise.objects.filter(pk__in=data.get('franchises',[]))
                    if any(b.kind!=obj.category.kind and b.kind!='hq' for b in books): raise ValidationError('Incompatible franchise type.')
                    obj.franchises.set(books)
                else:
                    if feature=='categories' and 'default_billing_mode' in data:
                        require(actor,'company.billing.manage')
                    allowed=('name','unit','tracks_stock','hsn','tax_percent','active') if feature=='products' else ('name','active','default_billing_mode')
                    if feature=='categories' and not has_permission(actor,'product.write'):
                        if set(data)-{'id','version','default_billing_mode'}:
                            raise PermissionDenied('Category billing settings can only change the default billing mode.')
                        allowed=('default_billing_mode',)
                    if feature=='categories' and data.get('kind',obj.kind)!=obj.kind:
                        raise ValidationError('Category type cannot change after creation.')
                    for key in allowed:
                        if key in data: setattr(obj,key,0 if feature=='products' and key=='tax_percent' and not data[key] else data[key])
                    if feature=='products' and data.get('category'):
                        category=Category.objects.get(pk=data['category'])
                        if obj.franchises.exclude(kind__in=[category.kind,'hq']).exists(): raise ValidationError('Category is incompatible with assigned franchises.')
                        obj.category=category
                obj.full_clean();obj.version+=1;obj.save()
                details={'previous_billing_mode':old_billing_mode,'billing_mode':obj.default_billing_mode} if feature=='categories' and old_billing_mode!=obj.default_billing_mode else None
                event(actor,feature,obj,details,action='product.assigned' if action=='assign' else feature+'.updated')
            return serialize(out,obj)
        return Response(command(request.user,request.headers.get('Idempotency-Key'),feature+'.'+action,{'id':str(pk),**data},run))

class TimelineView(APIView):
    @extend_schema(responses=dict)
    def get(self,request,feature,pk):
        if feature not in ('leads','bills','pickups','requirements','receipts','inventory'):
            raise NotFound()
        if request.user.role not in (Role.ADMIN,Role.COLLECTOR,*SEGMENT_NETWORK):
            raise PermissionDenied('Record timelines are limited to local operational roles.')
        obj=get_object(request.user,feature,pk)
        events=AuditEvent.objects.filter(resource=str(obj.pk)).order_by('-created_at')
        if request.user.role in SEGMENT_NETWORK:
            events=events.filter(franchise__kind=SEGMENT_NETWORK[request.user.role])
        elif request.user.role==Role.COLLECTOR:
            lead = getattr(obj,'lead',None) if feature in ('bills','pickups') else None
            allowed=feature=='leads' and getattr(obj,'driver_id',None)==request.user.pk or feature in ('bills','pickups') and (getattr(obj,'creator_id',None)==request.user.pk or (lead and lead.driver_id==request.user.pk))
            if not allowed: raise NotFound()
            events=events.filter(franchise_id=request.user.franchise_id)
        return Response({'resource':str(obj.pk),'feature':feature,'count':events.count(),'results':serialize('audit',events[:50],True)})

class CashView(APIView):
    @extend_schema(responses=dict)
    def get(self,request):
        require(request.user,'cash')
        users=User.objects.filter(role__in=[Role.DRIVER,Role.COLLECTOR],is_active=True)
        if request.user.role not in NETWORK: users=users.filter(franchise_id=request.user.franchise_id)
        if request.user.role in OWN: users=users.filter(pk=request.user.pk)
        if request.user.role==Role.WAREHOUSE: users=users.filter(load__warehouse_id=request.user.warehouse_id).distinct()
        result=[]
        for driver in users:
            row=DriverAccount.objects.filter(driver=driver).first();loads=list(Load.objects.filter(driver=driver,counted_in__isnull=True))
            on_route=any(l.state=='on_route' for l in loads)
            reason=''
            if request.user.role not in (Role.WAREHOUSE,Role.HQ): reason='Cash counting is available to Warehouse Managers and HQ.'
            elif not loads: reason='No eligible uncounted routes.'
            elif on_route: reason='Driver still out.'
            elif any(l.franchise_id!=driver.franchise_id or (request.user.role==Role.WAREHOUSE and l.warehouse_id!=request.user.warehouse_id) for l in loads): reason='All covered routes must belong to your assigned warehouse.'
            elif any(l.state!='returned' or not l.returned_at or l.returned_at>timezone.now() or not l.eligibility_reference.strip() for l in loads): reason='Valid route-return evidence is required before counting.'
            if not reason:
                try: policy('route_eligibility')
                except PolicyUnavailable as exc: reason=str(exc.detail)
            visible=[l for l in loads if request.user.role!=Role.WAREHOUSE or l.warehouse_id==request.user.warehouse_id]
            result.append({'id':driver.id,'name':driver.get_full_name() or driver.email,'franchise':str(driver.franchise_id),'balance':str(row.balance if row else 0),'version':row.version if row else 1,'routes':[str(l.id) for l in visible],'on_route':on_route,'count_available':not reason,'count_blocked_reason':reason})
        return Response({'results':result})
    @extend_schema(request=dict,responses=dict)
    def post(self,request):
        action=request.data.get('action');permission='float.write' if action=='float' else 'count.write' if action=='count' else ''
        require(request.user,permission)
        driver=User.objects.filter(pk=request.data.get('driver'),role__in=[Role.DRIVER,Role.COLLECTOR],is_active=True).first()
        if not driver: raise NotFound()
        book_allowed(request.user,driver.franchise)
        def run(actor):
            obj=cash.float_to(actor,driver,request.data) if action=='float' else cash.count(actor,driver,request.data)
            return serialize('cash' if action=='float' else 'handovers',obj)
        return Response(command(request.user,request.headers.get('Idempotency-Key'),'cash.'+action,request.data,run))

class DashboardView(APIView):
    @extend_schema(responses=DashboardResponse)
    def get(self,request):
        user=request.user;result={'identity':identity(user),'date':timezone.localdate().isoformat(),'metrics':[]}
        if has_permission(user,'leads'):
            qs=scope(user,Lead.objects.all(),'leads');result['metrics'] += [{'label':'Open leads','value':qs.exclude(state__in=['closed','not_completed','cancelled']).count(),'unit':'leads','href':'leads'},{'label':'Closed pickups','value':qs.filter(state='closed').count(),'unit':'pickups','href':'leads'},{'label':'Not completed','value':qs.filter(state='not_completed').count(),'unit':'leads','href':'leads'}]
            result['leads']=serialize('leads',qs.select_related('customer','franchise','driver','load')[:8],True)
            result['pickup_chart']=pickup_chart(qs)
        if has_permission(user,'bills'):
            qs=scope(user,Bill.objects.all(),'bills');active=qs.exclude(state__in=['draft','cancelled'])
            for title,kinds in [('Pay suppliers',['collection','b2b']),('Receive from customers',['sale'])]:
                tot=active.filter(kind__in=kinds).aggregate(total=Sum('total'),paid=Sum('settled'))
                result['metrics'].append({'label':title+' · outstanding','value':str((tot['total'] or 0)-(tot['paid'] or 0)),'unit':'INR','href':'bills'})
            result['metrics'].append({'label':'Drafts in progress','value':qs.filter(state='draft').count(),'unit':'drafts','href':'bills'})
            result['bills']=serialize('bills',qs[:8],True)
        if user.role in (Role.SUPER,Role.ADMIN,*SEGMENT_NETWORK):
            from .presentation import dashboard_projection
            result.update(dashboard_projection(user))
        return Response(result)

def pickup_chart(qs):
    """Return scope-preserving daily pickup counts and settled totals."""
    rows={}
    for lead in qs.only('pickup_date','state').order_by('pickup_date','pk'):
        key=lead.pickup_date.isoformat()
        row=rows.setdefault(key,{'date':key,'scheduled':0,'closed':0,'not_completed':0,'paid':'0'})
        if lead.state in ('scheduled','weighing','sent','agreed'): row['scheduled']+=1
        elif lead.state in ('closed','not_completed'): row[lead.state]+=1
    totals=Bill.objects.filter(lead__in=qs).exclude(state='draft').aggregate(paid=Sum('settled'),billed=Sum('total'))
    return {'daily':list(rows.values()),'totals':{'billed':str(totals['billed'] or 0),'paid':str(totals['paid'] or 0),
            'outstanding':str((totals['billed'] or 0)-(totals['paid'] or 0))}}

class OptionsView(APIView):
    @extend_schema(responses=dict)
    def get(self,request):
        user=request.user
        if user.role in (Role.DIGESTER_MANAGER,Role.ZONAL_MANAGER,Role.TSO):
            from .digester_views import lsgd_payload
            lsgds=lsgd_scope(user,LSGD.objects.select_related('district','assigned_manager','assigned_tso'))
            managers=User.objects.filter(role=Role.ZONAL_MANAGER,is_active=True).order_by('first_name','last_name','pk') if user.role==Role.DIGESTER_MANAGER else User.objects.none()
            tsos=digester_team_scope(user,User.objects.select_related('manager')) if user.role in (Role.DIGESTER_MANAGER,Role.ZONAL_MANAGER) else User.objects.none()
            return Response({'franchises':[],'products':[],'categories':[],'staff':[],'drivers':[],'franchise_managers':[],'warehouses':[],'billing_access':{},'lsgds':[lsgd_payload(row) for row in lsgds.order_by('district__name','name')[:500]],'zonal_managers':[{'id':str(row.pk),'name':row.get_full_name() or row.email} for row in managers],'tsos':[{'id':str(row.pk),'name':row.get_full_name() or row.email,'manager':str(row.manager_id)} for row in tsos]})
        books=Franchise.objects.filter(status='active')
        if user.role in SEGMENT_NETWORK: books=books.filter(kind=SEGMENT_NETWORK[user.role])
        elif user.role not in NETWORK: books=books.filter(pk=user.franchise_id)
        if user.role in (Role.COLLECTOR,Role.WAREHOUSE): books=books.filter(pk=user.franchise_id)
        book_ids=set(books.values_list('pk',flat=True))
        products=Product.objects.filter(active=True,category__active=True,franchises__in=books).distinct().prefetch_related('franchises')
        staff=User.objects.filter(pk__in=feature_scope(user,'bills').values('creator_id')) if has_permission(user,'bills') else User.objects.none()
        franchise_managers=User.objects.filter(role=Role.APP,is_active=True,franchise__in=books).select_related('franchise').order_by('first_name','last_name','pk')
        if user.role==Role.APP: franchise_managers=franchise_managers.filter(pk=user.pk)
        driver_books=books
        if user.role==Role.APP:
            driver_books=Franchise.objects.filter(
                pk__in=Lead.objects.filter(franchise_manager=user).values('franchise_id'),status='active')
        # Selecting eligible items does not grant catalogue maintenance.
        from .cache import cached_read
        minimum_version = request.headers.get('X-Minimum-Version')
        try:
            minimum_version = int(minimum_version) if minimum_version is not None else None
        except (TypeError, ValueError):
            minimum_version = None
        categories=Category.objects.filter(active=True)
        if user.role in SEGMENT_NETWORK: categories=categories.filter(kind=SEGMENT_NETWORK[user.role])
        elif not has_permission(user,'products'): categories=categories.filter(pk__in=products.values('category_id'))
        return Response(cached_read(user,'options',{'presentation':8},lambda:{'franchises':[{'id':str(b.pk),'name':b.name,'kind':b.kind} for b in books],'products':[{'id':str(p.pk),'name':p.name,'unit':p.unit,'category':str(p.category_id),'billing_mode':p.category.default_billing_mode,'franchises':[str(b.pk) for b in p.franchises.all() if b.pk in book_ids]} for p in products],'categories':[{'id':str(c.pk),'name':c.name,'kind':c.kind,'default_billing_mode':c.default_billing_mode} for c in categories],'staff':[{'id':str(d.pk),'name':d.get_full_name() or d.email} for d in staff],'drivers':[{'id':str(d.pk),'name':d.get_full_name() or d.email,'franchise':str(d.franchise_id)} for d in User.objects.filter(franchise__in=driver_books,role__in=[Role.DRIVER,Role.COLLECTOR],is_active=True)] if has_permission(user,'lead.assign') or has_permission(user,'team') else [],'franchise_managers':[{'id':str(manager.pk),'name':manager.get_full_name() or manager.email,'franchise':str(manager.franchise_id),'franchise_name':manager.franchise.name} for manager in franchise_managers] if has_permission(user,'lead.assign') else [],'warehouses':[{'id':str(w.pk),'name':w.name,'franchise':str(w.franchise_id)} for w in Warehouse.objects.filter(franchise__in=books)] if has_permission(user,'team') else [],'billing_access':billing.billing_access(user)},minimum_version=minimum_version))

class SettingsView(APIView):
    @extend_schema(request=dict,responses=dict)
    def post(self,request,section):
        require(request.user,'company.billing.manage' if section=='billing_permissions' else 'settings')
        if section not in ('company','invoice','gst','payment_methods','billing_permissions'): raise NotFound()
        def run(actor):
            require(actor,'company.billing.manage' if section=='billing_permissions' else 'settings')
            # UI settings cannot approve unresolved business-policy gates.
            obj,_=CompanySetting.objects.get_or_create(section=section)
            version(obj,request.data.get('version',1));old=obj.values
            values=request.data.get('values',{})
            if not isinstance(values,dict) or len(json.dumps(values))>10000: raise ValidationError('Settings are too large.')
            if section=='company' and len(str(values.get('company_name','')))>160: raise ValidationError('Company name is too long.')
            if section=='invoice' and (not str(values.get('prefix','')).strip() or len(str(values.get('prefix','')))>32): raise ValidationError('Invoice prefix is required and must be at most 32 characters.')
            if section=='gst' and values.get('pricing_basis') not in (None,'exclusive','inclusive','none'): raise ValidationError('Choose a supported pricing basis.')
            if section=='payment_methods':
                methods=values.get('methods',[])
                if isinstance(methods,str): methods=[m.strip() for m in methods.split(',') if m.strip()]
                if not isinstance(methods,list) or any(m not in ('Cash','UPI','Bank transfer','Card','Cheque') for m in methods): raise ValidationError('Choose supported payment methods.')
                values={**values,'methods':methods}
            if section=='billing_permissions':
                from .services.billing import DEFAULT_BILLING_ACCESS
                roles=values.get('roles')
                valid_modes={'purchase','sales'};valid_types={'B2B','B2C','B2G'}
                if not isinstance(roles,dict) or any(role not in DEFAULT_BILLING_ACCESS for role in roles): raise ValidationError('Choose valid billing roles.')
                for role,access in roles.items():
                    if not isinstance(access,dict) or not isinstance(access.get('modes'),list) or not isinstance(access.get('types'),list) or set(access['modes'])-valid_modes or set(access['types'])-valid_types: raise ValidationError('Choose valid transaction modes and types.')
            obj.values=values;obj.version+=1;obj.save();event(actor,'settings',obj,{'old':old,'new':obj.values})
            return serialize('settings',obj)
        return Response(command(request.user,request.headers.get('Idempotency-Key'),'settings.'+section,request.data,run))

class TeamView(APIView):
    def targets(self,user):
        if user.role==Role.APP: return [Role.DRIVER,Role.COLLECTOR]
        if user.role==Role.DIGESTER_MANAGER: return [Role.ZONAL_MANAGER,Role.TSO]
        if user.role==Role.ZONAL_MANAGER: return [Role.TSO]
        if user.role==Role.HQ: return [Role.DRIVER,Role.COLLECTOR,Role.APP,Role.WAREHOUSE]
        if user.role==Role.APP_MANAGER: return [Role.APP,Role.DRIVER,Role.COLLECTOR,Role.WAREHOUSE]
        return [Role.DRIVER,Role.COLLECTOR,Role.APP,Role.WAREHOUSE,Role.APP_MANAGER,Role.DIGESTER_MANAGER,Role.ZONAL_MANAGER,Role.TSO]
    @extend_schema(responses=dict)
    def get(self,request):
        require(request.user,'team');qs=User.objects.filter(role__in=self.targets(request.user))
        if request.user.role in (Role.DIGESTER_MANAGER,Role.ZONAL_MANAGER): qs=digester_team_scope(request.user,qs)
        elif request.user.role in SEGMENT_NETWORK: qs=qs.filter(franchise__kind=SEGMENT_NETWORK[request.user.role])
        elif request.user.role not in NETWORK: qs=qs.filter(franchise=request.user.franchise)
        search=request.query_params.get('search','')[:160]
        if search: qs=qs.filter(Q(first_name__icontains=search)|Q(email__icontains=search))
        if request.query_params.get('role'):
            selected=request.query_params['role']
            if selected.startswith('custom:'):
                try: custom_id=selected.split(':',1)[1]
                except ValueError: raise ValidationError('Invalid role filter.')
                if not RoleDefinition.objects.filter(pk=custom_id,base_role__in=self.targets(request.user)).exists(): raise ValidationError('Invalid role filter.')
                qs=qs.filter(custom_role_id=custom_id)
            else:
                if selected not in Role.values: raise ValidationError('Invalid role filter.')
                qs=qs.filter(role=selected,custom_role__isnull=True)
        if request.query_params.get('franchise'): qs=qs.filter(franchise_id=request.query_params['franchise'])
        if request.query_params.get('active'):
            if request.query_params['active'] not in ('true','false'): raise ValidationError('Invalid active filter.')
            qs=qs.filter(is_active=request.query_params['active']=='true')
        targets=self.targets(request.user)
        roles=[{'id':role,'name':name,'permissions':sorted(role_permissions(role))} for role,name in Role.choices if role in targets]
        roles.extend({'id':'custom:'+str(r.id),'name':r.name,'base_role':r.base_role,'permissions':r.permissions} for r in RoleDefinition.objects.filter(base_role__in=targets).order_by('name'))
        return Response({'results':[{**identity(u),'is_active':u.is_active,'last_login':u.last_login,'can_edit':True} for u in qs.select_related('franchise','custom_role')], 'targets':targets, 'roles':roles})
    @extend_schema(request=dict,responses=dict)
    def post(self,request):
        require(request.user,'team');data=request.data
        def run(actor):
            require(actor,'team')
            target=None
            if data.get('id'):
                target_qs=User.objects.select_for_update().filter(pk=data['id'],role__in=self.targets(actor))
                if actor.role in (Role.DIGESTER_MANAGER,Role.ZONAL_MANAGER): target_qs=digester_team_scope(actor,target_qs)
                target=target_qs.first()
                if not target: raise NotFound()
            if target.role in (Role.APP_MANAGER,Role.DIGESTER_MANAGER):
                if actor.role not in (Role.SUPER,Role.ADMIN): raise NotFound()
            elif target.role not in (Role.ZONAL_MANAGER,Role.TSO): book_allowed(actor,target.franchise)
            if data.get('action')=='set_password':
                if not target: raise ValidationError('Choose a user account.')
                password=str(data.get('password',''))
                validate_password(password,target)
                target.set_password(password)
                target.permission_version+=1
                target.save(update_fields=['password','permission_version'])
                AuditEvent.objects.create(actor=actor,franchise_id=target.franchise_id,
                                          action='account.password_admin_changed',resource=str(target.pk))
                Outbox.objects.create(actor=actor,feature='scope_reset',owner_id=target.id,resource_id='')
                return {'detail':'Password changed successfully.'}
            selection=data.get('role',('custom:'+str(target.custom_role_id) if target and target.custom_role_id else target.role) if target else '')
            custom_role=None
            if isinstance(selection,str) and selection.startswith('custom:'):
                try: custom_role=RoleDefinition.objects.get(pk=selection.split(':',1)[1])
                except (RoleDefinition.DoesNotExist,ValueError): raise PermissionDenied('This target role is not delegated.')
                role=custom_role.base_role
            else: role=selection
            if role not in self.targets(actor) or (custom_role and custom_role.base_role not in self.targets(actor)):
                raise PermissionDenied('This target role is not delegated.')
            network_role=role in (Role.APP_MANAGER,Role.DIGESTER_MANAGER,Role.ZONAL_MANAGER,Role.TSO)
            if network_role:
                allowed_actor=(actor.role in (Role.SUPER,Role.ADMIN) or actor.role==Role.DIGESTER_MANAGER and role in (Role.ZONAL_MANAGER,Role.TSO) or actor.role==Role.ZONAL_MANAGER and role==Role.TSO)
                if not allowed_actor: raise PermissionDenied('This role cannot manage that field-sales account.')
                if data.get('franchise'): raise ValidationError('Network tracking roles cannot be assigned to one franchise.')
                book=None
            else:
                book_id=data.get('franchise') or (target.franchise_id if target else actor.franchise_id)
                if not book_id: raise ValidationError('Choose a franchise for this account.')
                book=Franchise.objects.get(pk=book_id);book_allowed(actor,book)
                if book.status!='active': raise ValidationError('Choose an active franchise.')
                if target and target.franchise_id!=book.id and Lead.objects.filter(driver=target).exclude(state__in=['closed','not_completed','cancelled']).exists(): raise Conflict('Resolve active assignments before moving this account.')
            email=str(data.get('email',target.email if target else '')).strip().lower()
            if not target: target=User(username=email,email=email);target.set_unusable_password()
            target.email=email;target.username=email;target.first_name=str(data.get('name',target.first_name))[:150];target.role=role;target.custom_role=custom_role;target.franchise=book
            if role==Role.TSO:
                manager_id=data.get('manager')
                if actor.role==Role.ZONAL_MANAGER: manager_id=actor.pk
                if not manager_id and target.manager_id: manager_id=target.manager_id
                manager=User.objects.filter(pk=manager_id,role=Role.ZONAL_MANAGER,is_active=True).first()
                if not manager: raise ValidationError({'manager':'Choose an active Zonal Manager.'})
                if actor.role==Role.ZONAL_MANAGER and manager.pk!=actor.pk: raise PermissionDenied()
                target.manager=manager
            else:
                target.manager=None
            target.warehouse=Warehouse.objects.get(pk=data['warehouse']) if data.get('warehouse') else target.warehouse
            if role!=Role.WAREHOUSE: target.warehouse=None
            if data.get('action')=='disable': target.is_active=False
            elif data.get('action')=='enable': target.is_active=True
            target.permission_version+=1;target.full_clean();target.save()
            # Options (including lead assignment choices) are cached against
            # this revision. User role, active status, and franchise changes
            # must immediately invalidate those choices.
            DataRevision.objects.get_or_create(key='global')
            DataRevision.objects.filter(key='global').update(value=F('value')+1)
            AuditEvent.objects.create(actor=actor,franchise=book,action='team.updated',resource=str(target.pk),details={'role':role,'active':target.is_active})
            Outbox.objects.create(actor=actor,feature='scope_reset',owner_id=target.id,resource_id='')
            # Invitation delivery is queued after transaction, with durable job record.
            if not target.has_usable_password() or data.get('action')=='reset': Job.objects.create(actor=actor,kind='account_invitation',payload={'user':target.id,'purpose':'reset' if target.has_usable_password() else 'invite'})
            return identity(target)
        return Response(command(request.user,request.headers.get('Idempotency-Key'),'team.write',data,run))

class RoleDefinitionsView(APIView):
    def get(self,request):
        require(request.user,'role.write')
        targets=TeamView().targets(request.user)
        roles=[{'id':role,'name':name} for role,name in Role.choices if role in targets]
        permissions={role:sorted(role_permissions(role)-{'role.write'}) for role in targets}
        role_qs=RoleDefinition.objects.filter(base_role__in=targets) if request.user.role in SEGMENT_NETWORK else RoleDefinition.objects.all()
        results=[{'id':str(r.id),'name':r.name,'base_role':r.base_role,'permissions':r.permissions,'version':r.version,'users':r.users.count()} for r in role_qs.order_by('name')]
        return Response({'base_roles':roles,'permissions':permissions,'results':results})

    def post(self,request):
        require(request.user,'role.write')
        data=request.data
        name=str(data.get('name','')).strip()
        base_role=data.get('base_role')
        permissions=data.get('permissions')
        targets=TeamView().targets(request.user)
        if not name or len(name)>80: raise ValidationError('Role name is required and must be at most 80 characters.')
        if base_role not in targets: raise ValidationError('Choose a role scope you are allowed to manage.')
        if not isinstance(permissions,list) or len(permissions)>len(GRANTS): raise ValidationError('Choose valid permissions.')
        allowed=role_permissions(base_role)-{'role.write'}
        if any(not isinstance(permission,str) or permission not in allowed for permission in permissions):
            raise ValidationError('A permission is not available for the selected role scope.')
        permissions=sorted(set(permissions))
        with transaction.atomic():
            role=RoleDefinition.objects.select_for_update().filter(pk=data.get('id')).first() if data.get('id') else None
            if data.get('id') and not role: raise NotFound()
            if role and role.base_role not in targets: raise PermissionDenied()
            if role and role.users.exists() and role.base_role!=base_role: raise ValidationError('Unassign users before changing this role’s scope template.')
            if RoleDefinition.objects.filter(name__iexact=name).exclude(pk=role.pk if role else None).exists():
                raise ValidationError('A role with this name already exists.')
            if role and data.get('version')!=role.version: raise Conflict('This role changed. Refresh and review your edits.')
            if role is None: role=RoleDefinition(created_by=request.user)
            role.name=name;role.base_role=base_role;role.permissions=permissions;role.version+=1;role.save()
            assigned=list(role.users.select_for_update().all())
            for user in assigned:
                user.permission_version+=1
                user.save(update_fields=['permission_version'])
                Outbox.objects.create(actor=request.user,feature='scope_reset',owner_id=user.id,resource_id='')
            AuditEvent.objects.create(actor=request.user,action='role_definition.updated',resource=str(role.id),details={'name':role.name,'base_role':role.base_role,'permissions':permissions,'assigned_users':len(assigned)})
        return Response({'id':str(role.id),'name':role.name,'base_role':role.base_role,'permissions':role.permissions,'version':role.version,'users':role.users.count()})

class ReconciliationQueueView(APIView):
    @extend_schema(responses=dict)
    def get(self,request):
        require(request.user,'receipts')
        rows=scope(request.user,Lead.objects.all(),'leads').filter(load__isnull=False,bill__isnull=False).exclude(bill__state='draft').filter(Q(receipt__isnull=True)|Q(receipt__submitted_at__isnull=True)|Q(load__reconciliation_state='reweigh_requested'))
        if request.user.role==Role.WAREHOUSE:
            rows=rows.filter(load__warehouse_id=request.user.warehouse_id)
        search=request.query_params.get('search','')[:160]
        if search:rows=rows.filter(Q(customer__name__icontains=search)|Q(customer__phone__icontains=search))
        try:page=max(1,int(request.query_params.get('page',1)))
        except ValueError:raise ValidationError('Invalid page.')
        count=rows.count();result=[]
        for lead in rows.select_related('customer','driver','load__warehouse','bill','receipt').order_by('pickup_date','pk')[(page-1)*50:page*50]:
            ready=lead.load.state=='returned' and bool(lead.load.unloaded_at and lead.load.eligibility_reference)
            result.append({'id':str(lead.pk),'customer_name':lead.customer.name,'driver_name':lead.driver.get_full_name() if lead.driver else '',
                           'warehouse_name':lead.load.warehouse.name,'pickup_date':lead.pickup_date,'bill_number':lead.bill.number,
                           'state':'awaiting_unload' if not ready else 'reweigh_requested' if lead.load.reconciliation_state=='reweigh_requested' else 'draft' if hasattr(lead,'receipt') else 'awaiting_measurement',
                           'version':lead.version})
        return Response({'count':count,'page':page,'page_size':50,'results':result})


class ReconciliationView(APIView):
    def get_lead(self,request,pk):
        require(request.user,'receipts')
        lead=Lead.objects.select_related('load','customer','franchise').filter(pk=pk).first()
        if not lead or not lead.load_id: raise NotFound()
        book_allowed(request.user,lead.franchise)
        if request.user.role==Role.WAREHOUSE and lead.load.warehouse_id!=request.user.warehouse_id: raise NotFound()
        if not hasattr(lead,'bill') or lead.bill.state=='draft': raise ValidationError('An issued pickup bill is required.')
        return lead

    @extend_schema(request=dict,responses=dict)
    def post(self,request,pk):
        require(request.user,'receipt.write')
        lead=self.get_lead(request,pk)
        version(lead,request.data.get('version'))
        lines=warehouse.measurement_lines(lead,lead.bill,request.data.get('lines',[]),complete=False)
        measured={line['product'] for line in lines}
        return Response({'version':lead.version,'lines':lines,'complete':all(line['product'] in measured for line in lead.bill.lines),
                         'actual_total':str(sum((Decimal(line['actual']) for line in lines),Decimal(0))),
                         'billed_total':str(sum((Decimal(line['quantity']) for line in lead.bill.lines),Decimal(0)))})

    @extend_schema(responses=dict)
    def get(self,request,pk):
        lead=self.get_lead(request,pk)
        receipt = Receipt.objects.filter(lead=lead).first()
        reweigh_required=lead.load.reconciliation_state=='reweigh_requested' and bool(receipt and receipt.submitted_at and receipt.revision<lead.load.reweigh_version)
        reweigh_draft=ReceiptRevision.objects.filter(receipt=receipt,revision=lead.load.reweigh_version,
            submitted_at__isnull=True,superseded_at__isnull=True).first() if reweigh_required else None
        reason = ''
        try:
            policy('warehouse_receipt_eligibility')
        except PolicyUnavailable as exc:
            reason = str(exc.detail)
        if not reason and (lead.load.state != 'returned' or not lead.load.unloaded_at or not lead.load.eligibility_reference):
            reason = 'Authorised unloading on a returned load must be recorded before entering measurements.'
        if receipt and receipt.submitted_at and not reweigh_required:
            reason = 'This reconciliation has already been submitted.'
        materials = Product.objects.filter(franchises=lead.franchise,unit='kg',tracks_stock=True,active=True,category__active=True,category__kind='scrap')
        receipt_data=serialize('receipts',receipt) if receipt else None
        previous_lines=receipt.lines if reweigh_required else []
        if reweigh_required and receipt_data:
            if reweigh_draft:
                receipt_data['lines']=reweigh_draft.lines;receipt_data['note']=reweigh_draft.note;receipt_data['state']='draft'
            else:
                receipt_data['lines']=[{**line,'actual':''} for line in receipt.lines];receipt_data['note']='';receipt_data['state']='reweigh'
        return Response({
            'lead':str(lead.pk), 'version':lead.version, 'customer':lead.customer.name,
            'warehouse':str(lead.load.warehouse_id), 'unloaded':bool(lead.load.unloaded_at),
            'can_edit':not reason and request.user.role in (Role.WAREHOUSE,Role.HQ),
            'unavailable_reason':reason,
            'reweigh_required':reweigh_required,
            'previous_lines':previous_lines,
            'lines':[{'product':line['product'],'name':line['name'],'billed':line['quantity'],'unit':line['unit']} for line in lead.bill.lines],
            'materials':[{'id':str(product.pk),'name':product.name} for product in materials.order_by('name')],
            'receipt':receipt_data,
        })


class PayoutView(APIView):
    @extend_schema(responses=EarningsResponse)
    def get(self,request):
        require(request.user,'earnings');policy('digester_earnings')
        qs=EarningsEntry.objects.select_related('franchise','bill').order_by('-created_at')
        if request.user.role==Role.DIGESTER:
            qs=qs.filter(franchise_id=request.user.franchise_id)
        elif request.user.role in SEGMENT_NETWORK:
            qs=scope(request.user,qs,'earnings')
        elif request.user.role not in NETWORK:
            raise PermissionDenied('Earnings statements are limited to digester managers and head office.')
        selected=request.query_params.get('franchise')
        if selected:
            qs=qs.filter(franchise_id=selected)
        total=qs.aggregate(total=Sum('amount'))['total'] or Decimal(0)
        from .presentation import earnings_projection
        return Response({'balance':str(total),'can_payout':request.user.role in (Role.SUPER,Role.ADMIN,Role.DIGESTER_MANAGER),'results':serialize('earnings',qs[:200],True),**earnings_projection(request.user,qs,selected)})

    @extend_schema(request=dict,responses=dict)
    def post(self,request):
        require(request.user,'payout.write');policy('digester_earnings')
        def run(actor):
            require(actor,'payout.write');policy('digester_earnings')
            book=Franchise.objects.select_for_update().filter(pk=request.data.get('franchise'),kind='digester').first()
            if not book: raise NotFound()
            book_allowed(actor,book)
            available=EarningsEntry.objects.filter(franchise=book).aggregate(total=Sum('amount'))['total'] or Decimal(0)
            amount=decimal(request.data.get('amount'))
            if amount>available: raise Conflict('Payout exceeds available earnings.')
            entry=EarningsEntry.objects.create(franchise=book,actor=actor,kind='payout',amount=-amount,reference=str(request.data.get('reference',''))[:160])
            event(actor,'earnings',entry,action='earnings.payout');return serialize('earnings',entry)
        return Response(command(request.user,request.headers.get('Idempotency-Key'),'earnings.payout',request.data,run))
