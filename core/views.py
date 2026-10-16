import json
from datetime import datetime
from decimal import Decimal
from django.db import transaction
from django.db.models import Sum,Q
from django.utils import timezone
from django.http import HttpResponse
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError,NotFound,PermissionDenied
from drf_spectacular.utils import extend_schema
from .models import *
from .policy import *
from .serializers import FIELDS,serialize
from .auth import identity,issue_token
from .services.common import command,event,version,decimal,Conflict
from .services import billing,cash,pickups,warehouse,imports

ALIASES={'movements':'inventory','handovers':'cash'}
def feature_scope(user,feature,qs=None):
    model=FIELDS[feature][0]
    qs=qs if qs is not None else model.objects.all()
    if feature=='jobs': return qs.filter(actor=user)
    if feature=='settings': require(user,'settings');return qs
    if feature=='imports': require(user,'imports');return qs
    qs=scope(user,qs,ALIASES.get(feature,feature))
    if feature=='pickups': qs=qs.filter(lead__isnull=False)
    return qs

def get_object(user,feature,pk,lock=False):
    if feature not in FIELDS: raise NotFound()
    qs=FIELDS[feature][0].objects.select_for_update() if lock else FIELDS[feature][0].objects.all()
    try: return feature_scope(user,feature,qs).get(pk=pk)
    except (FIELDS[feature][0].DoesNotExist,ValueError): raise NotFound()

class RegisterView(APIView):
    @extend_schema(responses=dict)
    def get(self,request,feature,pk=None):
        if feature not in FIELDS: raise NotFound()
        if pk: return Response(serialize(feature,get_object(request.user,feature,pk)))
        qs=feature_scope(request.user,feature)
        if feature=='loads' and request.query_params.get('settlement_state'):
            settlement=request.query_params['settlement_state']
            if settlement in ('awaiting_supervisor','verified','disputed'):
                qs=qs.filter(counted_in__handover__state=settlement)
            elif settlement=='on_route': qs=qs.filter(counted_in__isnull=True,state='on_route')
            elif settlement=='count_pending': qs=qs.filter(counted_in__isnull=True).exclude(state='on_route')
            else: raise ValidationError('Invalid settlement state.')
        if feature=='bills' and request.query_params.get('issued')=='1': qs=qs.exclude(state='draft')
        fields={f.name for f in qs.model._meta.fields}
        selected_book=request.query_params.get('franchise')
        if selected_book:
            from uuid import UUID
            try: UUID(selected_book)
            except ValueError: raise ValidationError('Invalid franchise filter.')
            path={'payments':'bill__franchise_id','inventory':'warehouse__franchise_id','movements':'warehouse__franchise_id','receipts':'warehouse__franchise_id','franchises':'id','products':'franchises__id'}.get(feature,'franchise_id' if 'franchise' in fields else None)
            if path:
                qs=qs.filter(Q(franchise_id=selected_book)|Q(franchise__isnull=True)) if feature=='rates' else qs.filter(**{path:selected_book})
        for field in ('franchise','driver','state','kind','warehouse','product','receipt','requirement'):
            if field=='franchise': continue
            value=request.query_params.get(field)
            if value and field in fields: qs=qs.filter(**{field:value})
        if feature=='receipts' and request.query_params.get('flagged')=='1':
            qs=qs.filter(flagged=True,submitted_at__isnull=False)
        if feature=='audit':
            action=request.query_params.get('action','')[:100]
            actor=request.query_params.get('actor')
            if action: qs=qs.filter(action__icontains=action)
            if actor: qs=qs.filter(actor_id=actor)
        search=request.query_params.get('search','')[:160]
        if search:
            candidates=[f for f in ('name','code','number','notes','action','reference','resource','items') if f in fields]
            query=Q()
            for f in candidates: query|=Q(**{f+'__icontains':search})
            if 'customer' in fields: query|=Q(customer__name__icontains=search)|Q(customer__phone__icontains=search)
            if query: qs=qs.filter(query)
        date_field='pickup_date' if feature=='leads' else 'paid_at' if feature=='payments' else 'submitted_at' if feature=='receipts' else 'issued_at' if feature=='bills' and request.query_params.get('issued')=='1' else 'created_at'
        if date_field in fields:
            for param,lookup in [('from','gte'),('to','lte')]:
                if request.query_params.get(param):
                    try: date=datetime.strptime(request.query_params[param],'%Y-%m-%d').date()
                    except ValueError: raise ValidationError('Invalid date filter.')
                    qs=qs.filter(**{date_field+('' if date_field=='pickup_date' else '__date')+'__'+lookup:date})
        count=qs.count()
        try: page=max(1,int(request.query_params.get('page',1)))
        except ValueError: raise ValidationError('Invalid page.')
        summary={}
        if feature=='handovers':
            totals=qs.aggregate(expected=Sum('expected'),received=Sum('received'),difference=Sum('difference'))
            summary={key:str(value or 0) for key,value in totals.items()}
            summary['basis']='Saved handovers in the full filtered result; each handover counted once'
            summary['currency']='INR'
            summary['states']={state:qs.filter(state=state).count() for state in ('awaiting_supervisor','verified','disputed')}
            qs=qs.select_related('driver','franchise','counter').prefetch_related('routes')
        if feature=='loads':
            qs=qs.select_related('counted_in__handover')
        if feature in ('bills','pickups'):
            active=qs.exclude(state__in=['draft','cancelled'])
            for direction,kinds in [('outgoing',['collection','b2b']),('incoming',['sale'])]:
                totals=active.filter(kind__in=kinds).aggregate(total=Sum('total'),settled=Sum('settled'))
                summary.update({direction+'_'+k:str(v or 0) for k,v in totals.items()})
                summary[direction+'_outstanding']=str((totals['total'] or 0)-(totals['settled'] or 0))
        return Response({'count':count,'page':page,'page_size':50,'date_basis':date_field,'summary':summary,'results':serialize(feature,qs.order_by('-created_at')[(page-1)*50:page*50] if 'created_at' in fields else qs.order_by('pk')[(page-1)*50:page*50],True)})

    def post(self,request,feature,pk=None):
        return RegisterPostMixin.post(self,request,feature,pk)

class RegisterPostMixin(APIView):
    @extend_schema(responses=dict)
    def get(self,request,feature,pk):
        if feature not in ('leads','bills','pickups','requirements','receipts','inventory'):
            raise NotFound()
        if request.user.role not in (Role.ADMIN,Role.COLLECTOR):
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
        action={'franchises':'franchise.create','customers':'customer.create','products':'product.write','categories':'product.write','rates':'rate.write','bills':'bill.write','requirements':'requirement.write'}.get(feature)
        if not action: raise PermissionDenied()
        require(request.user,action)
        def create(actor):
            require(actor,action)
            if feature=='bills': obj=billing.save_draft(actor,data)
            elif feature=='requirements': obj=warehouse.create_requirement(actor,data)
            elif feature=='rates':
                product=Product.objects.get(pk=data.get('product'));book=Franchise.objects.get(pk=data['franchise']) if data.get('franchise') else None
                if book and not eligible_product(actor,product,book): raise ValidationError('Product is not assigned to this franchise.')
                if data.get('kind') not in ('collection','b2b','sale'): raise ValidationError('Choose a rate type.')
                obj=Rate.objects.create(product=product,franchise=book,kind=data['kind'],amount=decimal(data.get('amount')),actor=actor,reason=str(data.get('reason',''))[:500]);event(actor,feature,obj,{'new':str(obj.amount),'reason':obj.reason},'rate.changed')
            else:
                allowed={'franchises':['name','code','kind','phone','email','address','district','state','gstin','owner_name'], 'customers':['name','phone','address','area','state','gstin'],'categories':['name','kind'],'products':['name','category','unit','tracks_stock','hsn','tax_percent']}[feature]
                values={k:data[k] for k in allowed if k in data}
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
        permission={('leads','assign'):'lead.assign',('leads','reschedule'):'lead.schedule',('leads','cancel'):'lead.schedule',('leads','start'):'pickup.write',('leads','sent'):'pickup.write',('leads','agree'):'pickup.write',('leads','failed'):'pickup.write',('leads','whatsapp-opened'):'pickup.write',('leads','receive'):'receipt.write',('leads','save-reconciliation'):'receipt.write',('bills','save'):'bill.write',('bills','issue'):'bill.write',('bills','pay'):'bill.write',('bills','request-cancellation'):'bill.write',('pickups','save'):'pickup.write',('pickups','issue'):'pickup.write',('pickups','pay'):'pickup.write',('requirements','release'):'release.write',('requirements','cancel'):'requirement.write',('imports','commit'):'imports',('franchises','edit'):'franchise.edit',('franchises','suspend'):'franchise.suspend',('customers','edit'):'customer.edit',('products','assign'):'product.assign'}.get((feature,action))
        if not permission: raise PermissionDenied('This action is not defined.')
        require(request.user,permission)
        def run(actor):
            require(actor,permission);obj=get_object(actor,feature,pk,True);out=feature
            if feature in ('bills','pickups'):
                if obj.lead_id and obj.creator_id!=actor.pk: raise PermissionDenied('Pickup writes belong to the assigned collector.')
                if action=='request-cancellation':
                    obj=billing.request_cancellation(actor,obj,data)
                else: obj=billing.save_draft(actor,data,obj) if action=='save' else billing.issue(actor,obj,data) if action=='issue' else billing.pay(actor,obj,data)
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
            elif feature=='products':
                version(obj,data.get('version'));books=Franchise.objects.filter(pk__in=data.get('franchises',[]))
                if any(b.kind!=obj.category.kind and b.kind!='hq' for b in books): raise ValidationError('Incompatible franchise type.')
                obj.franchises.set(books);obj.version+=1;obj.save();event(actor,'products',obj,action='product.assigned')
            return serialize(out,obj)
        return Response(command(request.user,request.headers.get('Idempotency-Key'),feature+'.'+action,{'id':str(pk),**data},run))

class TimelineView(APIView):
    @extend_schema(responses=dict)
    def get(self,request,feature,pk):
        if feature not in ('leads','bills','pickups','requirements','receipts','inventory'):
            raise NotFound()
        if request.user.role not in (Role.ADMIN,Role.COLLECTOR):
            raise PermissionDenied('Record timelines are limited to local operational roles.')
        obj=get_object(request.user,feature,pk)
        events=AuditEvent.objects.filter(resource=str(obj.pk)).order_by('-created_at')
        if request.user.role==Role.COLLECTOR:
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
            if request.user.role!=Role.WAREHOUSE: reason='Cash counting is available to the assigned warehouse manager.'
            elif not loads: reason='No eligible uncounted routes.'
            elif on_route: reason='Driver still out.'
            elif any(l.warehouse_id!=request.user.warehouse_id or l.franchise_id!=request.user.franchise_id for l in loads): reason='All covered routes must belong to your assigned warehouse.'
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
    @extend_schema(responses=dict)
    def get(self,request):
        user=request.user;result={'identity':identity(user),'date':timezone.localdate().isoformat(),'metrics':[]}
        if user.role in GRANTS['leads']:
            qs=scope(user,Lead.objects.all(),'leads');result['metrics'] += [{'label':'Open leads','value':qs.exclude(state__in=['closed','not_completed','cancelled']).count(),'unit':'leads','href':'leads'},{'label':'Closed pickups','value':qs.filter(state='closed').count(),'unit':'pickups','href':'leads'},{'label':'Not completed','value':qs.filter(state='not_completed').count(),'unit':'leads','href':'leads'}]
            result['leads']=serialize('leads',qs.select_related('customer','franchise','driver','load')[:8],True)
        if user.role in BILLING:
            qs=scope(user,Bill.objects.all(),'bills');active=qs.exclude(state__in=['draft','cancelled'])
            for title,kinds in [('Pay suppliers',['collection','b2b']),('Receive from customers',['sale'])]:
                tot=active.filter(kind__in=kinds).aggregate(total=Sum('total'),paid=Sum('settled'))
                result['metrics'].append({'label':title+' · outstanding','value':str((tot['total'] or 0)-(tot['paid'] or 0)),'unit':'INR','href':'bills'})
            result['metrics'].append({'label':'Drafts in progress','value':qs.filter(state='draft').count(),'unit':'drafts','href':'bills'})
            result['bills']=serialize('bills',qs[:8],True)
        return Response(result)

class OptionsView(APIView):
    @extend_schema(responses=dict)
    def get(self,request):
        user=request.user
        books=Franchise.objects.filter(status='active')
        if user.role not in NETWORK: books=books.filter(pk=user.franchise_id)
        if user.role in (Role.COLLECTOR,Role.WAREHOUSE): books=books.filter(pk=user.franchise_id)
        products=Product.objects.filter(active=True,category__active=True,franchises__in=books).distinct()
        # Selecting eligible items does not grant catalogue maintenance.
        from .cache import cached_read
        minimum_version = request.headers.get('X-Minimum-Version')
        try:
            minimum_version = int(minimum_version) if minimum_version is not None else None
        except (TypeError, ValueError):
            minimum_version = None
        return Response(cached_read(user,'options',{},lambda:{'franchises':[{'id':str(b.pk),'name':b.name,'kind':b.kind} for b in books],'products':[{'id':str(p.pk),'name':p.name,'unit':p.unit} for p in products],'categories':[{'id':str(c.pk),'name':c.name} for c in Category.objects.filter(active=True)] if user.role in GRANTS['products'] else [],'drivers':[{'id':str(d.pk),'name':d.get_full_name() or d.email,'franchise':str(d.franchise_id)} for d in User.objects.filter(franchise__in=books,role__in=[Role.DRIVER,Role.COLLECTOR],is_active=True)] if user.role in GRANTS['lead.assign'] or user.role in GRANTS['team'] else [],'warehouses':[{'id':str(w.pk),'name':w.name,'franchise':str(w.franchise_id)} for w in Warehouse.objects.filter(franchise__in=books)] if user.role in GRANTS['team'] else []},minimum_version=minimum_version))

class SettingsView(APIView):
    @extend_schema(request=dict,responses=dict)
    def post(self,request,section):
        require(request.user,'settings')
        if section not in ('company','invoice','gst','payment_methods'): raise NotFound()
        def run(actor):
            require(actor,'settings')
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
            obj.values=values;obj.version+=1;obj.save();event(actor,'settings',obj,{'old':old,'new':obj.values})
            return serialize('settings',obj)
        return Response(command(request.user,request.headers.get('Idempotency-Key'),'settings.'+section,request.data,run))

class TeamView(APIView):
    def targets(self,user):
        if user.role==Role.APP: return [Role.DRIVER,Role.COLLECTOR]
        if user.role==Role.DIGESTER: return [Role.STAFF]
        if user.role==Role.HQ: return [Role.DRIVER,Role.COLLECTOR,Role.APP,Role.WAREHOUSE]
        return [Role.DRIVER,Role.COLLECTOR,Role.APP,Role.WAREHOUSE,Role.DIGESTER,Role.STAFF]
    @extend_schema(responses=dict)
    def get(self,request):
        require(request.user,'team');qs=User.objects.filter(role__in=self.targets(request.user))
        if request.user.role not in NETWORK: qs=qs.filter(franchise=request.user.franchise)
        return Response({'results':[{**identity(u),'is_active':u.is_active} for u in qs.select_related('franchise')]})
    @extend_schema(request=dict,responses=dict)
    def post(self,request):
        require(request.user,'team');data=request.data
        def run(actor):
            require(actor,'team')
            target=None
            if data.get('id'):
                target=User.objects.select_for_update().filter(pk=data['id'],role__in=self.targets(actor)).first()
                if not target: raise NotFound()
                book_allowed(actor,target.franchise)
            role=data.get('role',target.role if target else '')
            if role not in self.targets(actor): raise PermissionDenied('This target role is not delegated.')
            book=Franchise.objects.get(pk=data.get('franchise') or (target.franchise_id if target else actor.franchise_id));book_allowed(actor,book)
            if book.status!='active': raise ValidationError('Choose an active franchise.')
            if target and target.franchise_id!=book.id and Lead.objects.filter(driver=target).exclude(state__in=['closed','not_completed','cancelled']).exists(): raise Conflict('Resolve active assignments before moving this account.')
            email=str(data.get('email',target.email if target else '')).strip().lower()
            if not target: target=User(username=email,email=email);target.set_unusable_password()
            target.email=email;target.username=email;target.first_name=str(data.get('name',target.first_name))[:150];target.role=role;target.franchise=book
            target.warehouse=Warehouse.objects.get(pk=data['warehouse']) if data.get('warehouse') else target.warehouse
            if role!=Role.WAREHOUSE: target.warehouse=None
            if data.get('action')=='disable': target.is_active=False
            target.permission_version+=1;target.full_clean();target.save()
            AuditEvent.objects.create(actor=actor,franchise=book,action='team.updated',resource=str(target.pk),details={'role':role,'active':target.is_active})
            Outbox.objects.create(actor=actor,feature='scope_reset',owner_id=target.id,resource_id='')
            # Invitation delivery is queued after transaction, with durable job record.
            if not target.has_usable_password() or data.get('action')=='reset': Job.objects.create(actor=actor,kind='account_invitation',payload={'user':target.id,'purpose':'reset' if target.has_usable_password() else 'invite'})
            return identity(target)
        return Response(command(request.user,request.headers.get('Idempotency-Key'),'team.write',data,run))

class ReconciliationQueueView(APIView):
    @extend_schema(responses=dict)
    def get(self,request):
        require(request.user,'receipts')
        rows=scope(request.user,Lead.objects.all(),'leads').filter(load__isnull=False,bill__isnull=False).exclude(bill__state='draft').filter(Q(receipt__isnull=True)|Q(receipt__submitted_at__isnull=True))
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
                           'state':'awaiting_unload' if not ready else 'draft' if hasattr(lead,'receipt') else 'awaiting_measurement',
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
        reason = ''
        try:
            policy('warehouse_receipt_eligibility')
        except PolicyUnavailable as exc:
            reason = str(exc.detail)
        if not reason and (lead.load.state != 'returned' or not lead.load.unloaded_at or not lead.load.eligibility_reference):
            reason = 'Authorised unloading on a returned load must be recorded before entering measurements.'
        if receipt and receipt.submitted_at:
            reason = 'This reconciliation has already been submitted.'
        materials = Product.objects.filter(franchises=lead.franchise,unit='kg',tracks_stock=True,active=True,category__active=True,category__kind='scrap')
        return Response({
            'lead':str(lead.pk), 'version':lead.version, 'customer':lead.customer.name,
            'warehouse':str(lead.load.warehouse_id), 'unloaded':bool(lead.load.unloaded_at),
            'can_edit':not reason and request.user.role == Role.WAREHOUSE,
            'unavailable_reason':reason,
            'lines':[{'product':line['product'],'name':line['name'],'billed':line['quantity'],'unit':line['unit']} for line in lead.bill.lines],
            'materials':[{'id':str(product.pk),'name':product.name} for product in materials.order_by('name')],
            'receipt':serialize('receipts',receipt) if receipt else None,
        })


class PayoutView(APIView):
    @extend_schema(responses=dict)
    def get(self,request):
        require(request.user,'earnings');policy('digester_earnings')
        qs=EarningsEntry.objects.select_related('franchise','bill').order_by('-created_at')
        if request.user.role==Role.DIGESTER:
            qs=qs.filter(franchise_id=request.user.franchise_id)
        elif request.user.role not in NETWORK:
            raise PermissionDenied('Earnings statements are limited to digester managers and head office.')
        selected=request.query_params.get('franchise')
        if selected:
            qs=qs.filter(franchise_id=selected)
        total=qs.aggregate(total=Sum('amount'))['total'] or Decimal(0)
        return Response({'balance':str(total),'can_payout':request.user.role in (Role.SUPER,Role.ADMIN),'results':serialize('earnings',qs[:200],True)})

    @extend_schema(request=dict,responses=dict)
    def post(self,request):
        require(request.user,'payout.write');policy('digester_earnings')
        def run(actor):
            require(actor,'payout.write');policy('digester_earnings')
            book=Franchise.objects.select_for_update().filter(pk=request.data.get('franchise'),kind='digester').first()
            if not book: raise NotFound()
            available=EarningsEntry.objects.filter(franchise=book).aggregate(total=Sum('amount'))['total'] or Decimal(0)
            amount=decimal(request.data.get('amount'))
            if amount>available: raise Conflict('Payout exceeds available earnings.')
            entry=EarningsEntry.objects.create(franchise=book,actor=actor,kind='payout',amount=-amount,reference=str(request.data.get('reference',''))[:160])
            event(actor,'earnings',entry,action='earnings.payout');return serialize('earnings',entry)
        return Response(command(request.user,request.headers.get('Idempotency-Key'),'earnings.payout',request.data,run))
