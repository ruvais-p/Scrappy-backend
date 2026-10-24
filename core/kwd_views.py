from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import F, Q, Sum
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Bill, Franchise, FranchiseTarget, KWDLead, KWDLeadEvent, KWDStockBalance, KWDStockMovement, PresentationAppointment, Product, Role
from .policy import eligible_product, require


def lead_data(lead):
    return {
        'id': str(lead.pk), 'version': lead.version, 'name': lead.name,
        'phone': lead.phone, 'place': lead.place, 'district': lead.district,
        'head_office_note': lead.head_office_note, 'status': lead.status,
        'discussion': lead.discussion,
        'follow_up_date': lead.follow_up_date.isoformat() if lead.follow_up_date else None,
        'assigned_at': lead.assigned_at.isoformat(), 'sale': str(lead.sale_id) if lead.sale_id else None,
        'franchise': str(lead.franchise_id), 'franchise_name': lead.franchise.name,
    }


class KWDLeadView(APIView):
    def get(self, request, pk=None):
        require(request.user, 'kwd_leads')
        rows = KWDLead.objects.select_related('franchise').order_by('-assigned_at')
        if request.user.role == Role.DIGESTER:
            rows = rows.filter(franchise_id=request.user.franchise_id)
        else:
            rows = rows.filter(franchise__kind='digester')
        if pk:
            row = rows.filter(pk=pk).first()
            if not row: raise NotFound()
            return Response(lead_data(row))
        status = request.query_params.get('status')
        if status and status != 'all': rows = rows.filter(status=status)
        q = request.query_params.get('q', '').strip()
        if q: rows = rows.filter(name__icontains=q) | rows.filter(phone__icontains=q) | rows.filter(place__icontains=q)
        return Response({'count': rows.count(), 'results': [lead_data(row) for row in rows[:200]]})

    def post(self, request, pk=None):
        data = request.data
        if pk:
            require(request.user, 'kwd_leads')
            with transaction.atomic():
                row = KWDLead.objects.select_for_update().filter(pk=pk).first()
                if not row or (request.user.role == Role.DIGESTER and row.franchise_id != request.user.franchise_id): raise NotFound()
                try: current_version = int(data.get('version', 0))
                except (ValueError, TypeError): raise ValidationError({'version': 'Reload this lead and try again.'})
                if current_version != row.version: raise ValidationError('This lead changed. Reload and try again.')
                old = row.status
                status = str(data.get('status', row.status))
                if status not in dict(KWDLead.STATUSES): raise ValidationError({'status': 'Choose a valid lead status.'})
                if status == 'closed' and row.sale_id is None: raise ValidationError({'status': 'Close this lead by recording its sale.'})
                row.status = status
                row.discussion = str(data.get('discussion', row.discussion))[:5000]
                raw_date = data.get('follow_up_date')
                if raw_date in (None, ''): row.follow_up_date = None
                else:
                    try: row.follow_up_date = date.fromisoformat(str(raw_date))
                    except ValueError: raise ValidationError({'follow_up_date': 'Enter a valid date.'})
                if status == 'closed' and not row.closed_at: row.closed_at = timezone.now()
                row.version += 1
                row.save()
                KWDLeadEvent.objects.create(lead=row, actor=request.user, action='updated', old_status=old,
                    new_status=status, note=row.discussion, follow_up_date=row.follow_up_date)
            return Response(lead_data(row))

        if request.user.role != Role.DIGESTER_MANAGER: raise PermissionDenied()
        try: franchise = Franchise.objects.get(pk=data.get('franchise'), kind='digester', status='active')
        except (Franchise.DoesNotExist, ValueError, TypeError): raise ValidationError({'franchise': 'Choose an active Digester franchise.'})
        name = str(data.get('name', '')).strip()
        if not name or len(name) > 180: raise ValidationError({'name': 'Enter the lead name.'})
        row = KWDLead.objects.create(franchise=franchise, name=name, phone=str(data.get('phone', ''))[:24],
            place=str(data.get('place', ''))[:180], district=str(data.get('district', ''))[:80],
            head_office_note=str(data.get('head_office_note', ''))[:5000], assigned_by=request.user, assigned_at=timezone.now())
        KWDLeadEvent.objects.create(lead=row, actor=request.user, action='assigned', new_status='new', note=row.head_office_note)
        return Response(lead_data(row), status=201)


class KWDTargetView(APIView):
    def get(self, request):
        require(request.user, 'kwd_targets')
        try: month = date.fromisoformat(request.query_params.get('month', timezone.localdate().replace(day=1).isoformat()))
        except ValueError: raise ValidationError({'month': 'Enter a valid month date.'})
        month = month.replace(day=1)
        franchise_id = request.user.franchise_id if request.user.role == Role.DIGESTER else request.query_params.get('franchise')
        target = FranchiseTarget.objects.filter(franchise_id=franchise_id, month=month).first()
        actual = Bill.objects.filter(franchise_id=franchise_id, kind='sale', issued_at__year=month.year, issued_at__month=month.month).exclude(state__in=['draft','cancelled'])
        achieved = actual.aggregate(value=Sum('total'))['value'] or Decimal('0')
        units = sum(int(line.get('quantity', 0)) for bill in actual for line in (bill.lines or []) if isinstance(line, dict))
        months=[]
        for offset in range(6):
            year=month.year;number=month.month-offset
            while number<1: number+=12;year-=1
            start=date(year,number,1)
            bills=Bill.objects.filter(franchise_id=franchise_id,kind='sale',issued_at__year=year,issued_at__month=number).exclude(state__in=['draft','cancelled'])
            months.append({'month':start.isoformat(),'achieved_value':str(bills.aggregate(value=Sum('total'))['value'] or 0)})
        return Response({'month':month.isoformat(),'target_value':str(target.target_value) if target else None,'target_units':target.target_units if target else None,'achieved_value':str(achieved),'achieved_units':units,'remaining_value':str(max(Decimal('0'),(target.target_value if target else Decimal('0'))-achieved)),'remaining_units':max(0,(target.target_units if target else 0)-units),'configured':target is not None,'history':months})

    def post(self, request):
        require(request.user, 'kwd_targets')
        if request.user.role != Role.DIGESTER_MANAGER:
            raise PermissionDenied('Only Head Office can set monthly KWD targets.')
        try: franchise=Franchise.objects.get(pk=request.data.get('franchise') or request.user.franchise_id,kind='digester',status='active')
        except (Franchise.DoesNotExist,ValueError,TypeError): raise ValidationError({'franchise':'Choose an active Digester franchise.'})
        if request.user.role==Role.DIGESTER and franchise.pk!=request.user.franchise_id: raise NotFound()
        try:
            month=date.fromisoformat(str(request.data.get('month'))+'-01')
            target_value=Decimal(str(request.data.get('target_value','0')))
            target_units=int(request.data.get('target_units','0'))
        except (ValueError,InvalidOperation,TypeError): raise ValidationError('Enter a valid month, target value, and unit target.')
        if target_value<0 or target_units<0: raise ValidationError('Targets cannot be negative.')
        row,_=FranchiseTarget.objects.update_or_create(franchise=franchise,month=month,defaults={'target_value':target_value,'target_units':target_units,'set_by':request.user,'note':str(request.data.get('note',''))[:500]})
        return Response({'id':str(row.pk),'month':row.month.isoformat(),'target_value':str(row.target_value),'target_units':row.target_units})


def appointment_data(row):
    return {'id':str(row.pk),'version':row.version,'lead':str(row.lead_id) if row.lead_id else None,'lead_name':row.lead.name if row.lead_id else None,'scheduled_at':row.scheduled_at.isoformat(),'venue':row.venue,'person_name':row.person_name,'district':row.district,'address':row.address,'contact_person':row.contact_person,'contact_phone':row.contact_phone,'people_expected':row.people_expected,'products':[str(pk) for pk in row.products.values_list('pk',flat=True)],'status':row.status,'outcome':row.outcome}


class KWDPresentationView(APIView):
    def get(self, request):
        require(request.user, 'kwd_presentations')
        rows=PresentationAppointment.objects.filter(franchise_id=request.user.franchise_id).select_related('lead').prefetch_related('products').order_by('scheduled_at')
        if request.query_params.get('state'): rows=rows.filter(status=request.query_params['state'])
        return Response({'count':rows.count(),'results':[appointment_data(row) for row in rows[:300]]})

    def post(self, request, pk=None):
        require(request.user, 'kwd_presentations')
        data=request.data
        if pk:
            row=PresentationAppointment.objects.filter(pk=pk,franchise_id=request.user.franchise_id).first()
            if not row: raise NotFound()
            try: version=int(data.get('version',0))
            except (ValueError,TypeError): raise ValidationError({'version':'Reload this appointment and try again.'})
            if version!=row.version: raise ValidationError('This appointment changed. Reload and try again.')
            status=str(data.get('status',row.status))
            if status not in dict(PresentationAppointment.STATES): raise ValidationError({'status':'Choose a valid appointment status.'})
            row.status=status;row.outcome=str(data.get('outcome',row.outcome))[:5000];row.version+=1;row.save(update_fields=['status','outcome','version','updated_at'])
            return Response(appointment_data(row))
        try: scheduled=datetime.fromisoformat(str(data.get('scheduled_at')).replace('Z','+00:00'))
        except ValueError: raise ValidationError({'scheduled_at':'Enter a valid date and time.'})
        if timezone.is_naive(scheduled): scheduled=timezone.make_aware(scheduled,timezone.get_current_timezone())
        venue=str(data.get('venue',''))
        if venue not in dict(PresentationAppointment.VENUES): raise ValidationError({'venue':'Choose a presentation venue.'})
        lead=None
        if data.get('lead'):
            lead=KWDLead.objects.filter(pk=data.get('lead'),franchise_id=request.user.franchise_id).first()
            if not lead: raise NotFound()
        row=PresentationAppointment.objects.create(franchise=request.user.franchise,lead=lead,scheduled_at=scheduled,venue=venue,person_name=str(data.get('person_name',''))[:180],district=str(data.get('district',''))[:80],address=str(data.get('address',''))[:500],contact_person=str(data.get('contact_person',''))[:180],contact_phone=str(data.get('contact_phone',''))[:24],people_expected=str(data.get('people_expected',''))[:300],created_by=request.user)
        product_ids=data.get('products',[])
        if isinstance(product_ids,str): product_ids=[product_ids]
        products=list(Product.objects.filter(pk__in=product_ids))
        if len(products)!=len(set(product_ids)) or any(not eligible_product(request.user,product,request.user.franchise) for product in products):
            row.delete();raise ValidationError({'products':'Choose products assigned to this franchise.'})
        row.products.set(products)
        return Response(appointment_data(row),status=201)


class KWDStockView(APIView):
    def get(self, request):
        require(request.user, 'kwd_stock')
        assigned=Product.objects.filter(active=True,category__active=True,category__kind='digester',franchises=request.user.franchise).order_by('name')
        balances={row.product_id:row for row in KWDStockBalance.objects.filter(franchise_id=request.user.franchise_id,product__in=assigned)}
        products=[]
        for product in assigned:
            row=balances.get(product.pk)
            quantity=row.quantity if row else Decimal('0');held=row.held_quantity if row else Decimal('0');threshold=row.low_stock_threshold if row else Decimal('0')
            products.append({'id':str(product.pk),'name':product.name,'unit':product.unit,'quantity':str(quantity),'held_quantity':str(held),'available':str(quantity-held),'low_stock_threshold':str(threshold),'low':quantity-held<=threshold,'tracks_stock':product.tracks_stock})
        rows=KWDStockBalance.objects.filter(franchise_id=request.user.franchise_id,product__category__kind='digester')
        movements=KWDStockMovement.objects.filter(franchise_id=request.user.franchise_id).select_related('product','actor').order_by('-created_at')[:200]
        totals=rows.aggregate(quantity=Sum('quantity'))
        return Response({'count':len(products),'units':str(totals['quantity'] or 0),'results':products,'history':[{'id':str(m.pk),'product':m.product.name,'kind':m.kind,'quantity':str(m.quantity),'reason':m.reason,'actor':m.actor.get_full_name() or m.actor.email,'at':m.created_at.isoformat()} for m in movements]})

    def post(self, request):
        require(request.user, 'kwd_stock')
        data=request.data
        try: product=Product.objects.select_related('category').get(pk=data.get('product'))
        except (Product.DoesNotExist,ValueError,TypeError): raise ValidationError({'product':'Choose a valid KWD product.'})
        if not eligible_product(request.user,product,request.user.franchise) or product.category.kind!='digester': raise NotFound()
        kind=str(data.get('kind',''))
        if kind not in ('receipt','adjustment','hold','release'): raise ValidationError({'kind':'Choose a stock receipt, adjustment, hold, or release.'})
        try: amount=Decimal(str(data.get('quantity')))
        except (InvalidOperation,TypeError): raise ValidationError({'quantity':'Enter a valid quantity.'})
        if amount<=0: raise ValidationError({'quantity':'Enter a quantity greater than zero.'})
        reason=str(data.get('reason','')).strip()[:500]
        if kind=='adjustment' and not reason: raise ValidationError({'reason':'Enter a reason for the adjustment.'})
        with transaction.atomic():
            balance,_=KWDStockBalance.objects.select_for_update().get_or_create(franchise=request.user.franchise,product=product)
            if kind in ('receipt','adjustment'): balance.quantity+=amount if kind=='receipt' or data.get('direction')!='subtract' else -amount
            elif kind=='hold':
                if amount>balance.quantity-balance.held_quantity: raise ValidationError({'quantity':'Cannot hold more than the available stock.'})
                balance.held_quantity+=amount
            else:
                if amount>balance.held_quantity: raise ValidationError({'quantity':'Cannot release more than the held stock.'})
                balance.held_quantity-=amount
            if balance.quantity<balance.held_quantity or balance.quantity<0: raise ValidationError({'quantity':'Stock cannot be reduced below held or available quantities.'})
            threshold=data.get('low_stock_threshold')
            if threshold not in (None,''):
                try: balance.low_stock_threshold=Decimal(str(threshold))
                except InvalidOperation: raise ValidationError({'low_stock_threshold':'Enter a valid low-stock threshold.'})
                if balance.low_stock_threshold<0: raise ValidationError({'low_stock_threshold':'Threshold cannot be negative.'})
            balance.version+=1;balance.save()
            signed=-amount if kind=='adjustment' and data.get('direction')=='subtract' else amount
            KWDStockMovement.objects.create(franchise=request.user.franchise,product=product,actor=request.user,kind=kind,quantity=signed,reason=reason)
        return Response({'id':str(balance.pk),'product':str(product.pk),'quantity':str(balance.quantity),'held_quantity':str(balance.held_quantity),'available':str(balance.quantity-balance.held_quantity),'version':balance.version},status=201)


class KWDSaleProgressView(APIView):
    def post(self, request, pk):
        require(request.user, 'kwd_sales')
        with transaction.atomic():
            bill=Bill.objects.select_for_update().filter(pk=pk,franchise_id=request.user.franchise_id,franchise__kind='digester',kind='sale').first()
            if not bill or bill.state=='draft': raise NotFound()
            try: version=int(request.data.get('version',0))
            except (TypeError,ValueError): raise ValidationError({'version':'Reload this sale and try again.'})
            if version!=bill.version: raise ValidationError('This sale changed. Reload and try again.')
            delivery=str(request.data.get('delivery_status',bill.delivery_status))
            installation=str(request.data.get('installation_status',bill.installation_status))
            if delivery not in dict(Bill._meta.get_field('delivery_status').choices): raise ValidationError({'delivery_status':'Choose a valid delivery status.'})
            if installation not in dict(Bill._meta.get_field('installation_status').choices): raise ValidationError({'installation_status':'Choose a valid installation status.'})
            bill.delivery_status=delivery;bill.installation_status=installation;bill.version+=1
            bill.save(update_fields=['delivery_status','installation_status','version','updated_at'])
        return Response({'id':str(bill.pk),'delivery_status':bill.delivery_status,'installation_status':bill.installation_status,'version':bill.version})


class KWDSalesView(APIView):
    def get(self, request):
        require(request.user, 'kwd_sales')
        rows=Bill.objects.filter(franchise_id=request.user.franchise_id,franchise__kind='digester',kind='sale',mode='sales').select_related('customer').exclude(state='draft').order_by('-issued_at','-created_at')
        search=request.query_params.get('search','').strip()[:160]
        if search:
            rows=rows.filter(Q(number__icontains=search)|Q(customer__name__icontains=search)|Q(customer__phone__icontains=search)|Q(customer__area__icontains=search))
        state=request.query_params.get('state','all')
        if state=='delivery': rows=rows.filter(delivery_status='pending')
        elif state=='installation': rows=rows.filter(installation_status='pending')
        elif state=='money': rows=rows.filter(settled__lt=F('total'))
        elif state=='completed': rows=rows.filter(delivery_status__in=['delivered','cancelled'],installation_status__in=['installed','not_required'])
        elif state=='cancelled': rows=rows.filter(state='cancelled')
        elif state not in ('all','issued','completed','cancellation_requested'):
            raise ValidationError({'state':'Choose a valid sales filter.'})
        elif state!='all': rows=rows.filter(state=state)
        # Aggregate before the UI filter so summary tiles always describe the full franchise register.
        all_rows=Bill.objects.filter(franchise_id=request.user.franchise_id,franchise__kind='digester',kind='sale',mode='sales').exclude(state__in=['draft','cancelled'])
        today=timezone.localdate()
        totals=all_rows.aggregate(all_time=Sum('total'),month=Sum('total',filter=Q(issued_at__year=today.year,issued_at__month=today.month)),due=Sum(F('total')-F('settled')))
        return Response({'count':rows.count(),'summary':{'month_value':str(totals['month'] or 0),'all_time_value':str(totals['all_time'] or 0),'to_deliver':all_rows.filter(delivery_status='pending').count(),'to_install':all_rows.filter(installation_status='pending').count(),'money_due':str(totals['due'] or 0)},'results':[{'id':str(row.pk),'version':row.version,'number':row.number,'customer':str(row.customer_id),'customer_name':row.customer.name,'customer_area':row.customer.area,'kind':row.kind,'state':row.state,'total':str(row.total),'settled':str(row.settled),'delivery_status':row.delivery_status,'installation_status':row.installation_status,'issued_at':row.issued_at.isoformat() if row.issued_at else None,'created_at':row.created_at.isoformat()} for row in rows[:500]]})
