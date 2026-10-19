"""Scoped, additive read projections for the authenticated workspace."""
from decimal import Decimal, ROUND_HALF_UP
from uuid import UUID
from django.db.models import Sum, Count, Max, Q
from django.utils import timezone
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError
from drf_spectacular.utils import extend_schema
from .models import Bill, Payment, Franchise, Product, Rate, EarningsEntry, User, Role
from .policy import scope, require, GRANTS, NETWORK, has_permission
from .queries import filter_records
from .presentation_schema import SearchResponse, PricingResponse


def totals(qs):
    values=qs.aggregate(total=Sum('total'),settled=Sum('settled'),subtotal=Sum('subtotal'))
    result={key: value or Decimal(0) for key,value in values.items()}
    result['outstanding']=result['total']-result['settled']
    return result


def active_bills(user):
    return scope(user,Bill.objects.all(),'bills').exclude(state__in=['draft','cancelled'])


def grouped(qs,key):
    return {str(row[key]):row for row in qs.values(key).annotate(count=Count('id'),total=Sum('total'),last=Max('issued_at'))}


def enhance_register(user,feature,qs,payload,params):
    rows=payload['results']
    if feature in ('bills','pickups'):
        for row in rows:
            row['payment_state']='not_issued' if row['state']=='draft' else 'cancelled' if row['state']=='cancelled' else 'paid' if Decimal(row['outstanding'])<=0 else 'partial' if Decimal(row['settled'])>0 else 'pending'
            row['items_text']=', '.join(str(line.get('name','')) for line in row.get('lines',[]))
    if feature=='payments':
        incoming=qs.filter(bill__kind='sale').aggregate(value=Sum('amount'))['value'] or 0
        outgoing=qs.exclude(bill__kind='sale').aggregate(value=Sum('amount'))['value'] or 0
        cohort_params={k:v for k,v in params.items() if k not in ('method','page','payment_state')}
        cohort_params.update({'issued':'1','kind':'sale','direction':'incoming'})
        cohort,_=filter_records(active_bills(user),'bills',cohort_params)
        t=totals(cohort)
        all_active=active_bills(user)
        if params.get('franchise'): all_active=all_active.filter(franchise_id=params['franchise'])
        payload['summary']={'received':str(incoming),'paid_out':str(outgoing),'receivable':str(totals(all_active.filter(kind='sale'))['outstanding']),'payable':str(totals(all_active.exclude(kind='sale'))['outstanding']),'collection_ratio':str((t['settled']/t['total']*100).quantize(Decimal('.1'))) if t['total'] else None}
    if feature in ('customers','franchises'):
        bills=active_bills(user) if has_permission(user,'bills') else scope(user,Bill.objects.filter(lead__isnull=False),'pickups').exclude(state__in=['draft','cancelled'])
        key='customer_id' if feature=='customers' else 'franchise_id'
        if feature=='franchises': bills=bills.filter(issued_at__date__gte=timezone.localdate().replace(day=1))
        values=grouped(bills.filter(**{key+'__in':[r['id'] for r in rows]}),key)
        for row in rows:
            value=values.get(str(row['id']),{})
            row.update(transaction_count=value.get('count',0),billing_total=str(value.get('total') or 0),last_transaction=value.get('last'))
            if feature=='franchises':
                row['product_count']=Product.objects.filter(franchises=row['id'],active=True,category__active=True).count()
                from .views import TeamView
                row['staff_count']=User.objects.filter(franchise_id=row['id'],is_active=True,role__in=TeamView().targets(user)).count() if has_permission(user,'team') else None
    if feature=='products':
        defaults={};selected_book=params.get('franchise')
        if selected_book:
            visible_book=scope(user,Franchise.objects.filter(pk=selected_book),'franchises').first()
            selected_book=str(visible_book.pk) if visible_book else None
        for rate in Rate.objects.filter(franchise__isnull=True,product_id__in=[r['id'] for r in rows]).order_by('-created_at','-id'):
            defaults.setdefault((str(rate.product_id),rate.kind),rate)
        overrides={}
        if selected_book:
            for rate in Rate.objects.filter(franchise_id=selected_book,product_id__in=[r['id'] for r in rows]).order_by('-created_at','-id'):
                overrides.setdefault((str(rate.product_id),rate.kind),rate)
        for row in rows:
            rate_kind='collection' if row.get('category_kind')=='scrap' else 'sale'
            rate=overrides.get((str(row['id']),rate_kind)) or defaults.get((str(row['id']),rate_kind))
            if rate:
                row['final_price']=str(final_unit_price(rate.amount,rate.discount_percent,Decimal(str(row.get('tax_percent') or 0))))
                row['default_discount_percent']=str(rate.discount_percent)
            else:
                row['final_price']=None;row['default_discount_percent']=None
    return payload


def final_unit_price(amount,discount_percent,tax_percent):
    discounted=(Decimal(amount)*(Decimal('1')-Decimal(discount_percent)/Decimal('100'))).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)
    return (discounted*(Decimal('1')+Decimal(tax_percent)/Decimal('100'))).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)


def dashboard_projection(user):
    qs=active_bills(user);today=timezone.localdate();month=today.replace(day=1)
    books=scope(user,Franchise.objects.all(),'franchises').order_by('name')
    rows=[]
    earnings=scope(user,EarningsEntry.objects.all(),'earnings') if has_permission(user,'earnings') else EarningsEntry.objects.none()
    for book in books:
        b=qs.filter(franchise=book);period=b.filter(issued_at__date__gte=month,issued_at__date__lte=today)
        rows.append({'id':str(book.pk),'name':book.name,'kind':book.kind,'today':str(totals(b.filter(issued_at__date=today))['total']),'month':str(totals(period)['total']),'count':period.count(),'receivable':str(totals(b.filter(kind='sale'))['outstanding']),'payable':str(totals(b.exclude(kind='sale'))['outstanding']),'share_owed':str(earnings.filter(franchise=book).aggregate(v=Sum('amount'))['v'] or 0) if book.kind=='digester' else None})
    return {'franchises':rows,'cancellations':qs.filter(state='cancellation_requested').count(),'summary':{'today':str(totals(qs.filter(issued_at__date=today))['total']),'month':str(totals(qs.filter(issued_at__date__gte=month,issued_at__date__lte=today))['total']),'incoming_outstanding':str(totals(qs.filter(kind='sale'))['outstanding']),'outgoing_outstanding':str(totals(qs.exclude(kind='sale'))['outstanding']),'share_owed':str(earnings.aggregate(v=Sum('amount'))['v'] or 0)}}


def earnings_projection(user,qs,franchise_id=None):
    books=scope(user,Franchise.objects.filter(kind='digester'),'franchises')
    if franchise_id: books=books.filter(pk=franchise_id)
    rows=[]
    for book in books:
        entries=qs.filter(franchise=book);earned=entries.exclude(kind='payout').aggregate(v=Sum('amount'))['v'] or Decimal(0);paid=-(entries.filter(kind='payout').aggregate(v=Sum('amount'))['v'] or Decimal(0))
        bills=active_bills(user).filter(franchise=book,kind='sale');t=totals(bills)
        rows.append({'id':str(book.pk),'name':book.name,'share_percent':str(book.share_percent),'billed':str(t['subtotal']),'collected':str(t['settled']),'earned':str(earned),'paid':str(paid),'balance':str(earned-paid)})
    return {'franchises':rows,'summary':{key:str(sum((Decimal(r[key]) for r in rows),Decimal(0))) for key in ('billed','collected','earned','paid','balance')}}


class SearchView(APIView):
    @extend_schema(responses=SearchResponse)
    def get(self,request):
        term=request.query_params.get('q','').strip()[:160]
        if len(term)<2: return Response({'results':[]})
        from .views import feature_scope
        from .serializers import FIELDS
        results=[]
        for feature in ('bills','customers'):
            if not has_permission(request.user,feature): continue
            qs=feature_scope(request.user,feature).select_related('franchise',*(['customer'] if feature=='bills' else []))
            query=Q()
            if feature=='bills': query=Q(number__icontains=term)|Q(customer__name__icontains=term)|Q(customer__phone__icontains=term)
            else: query=Q(name__icontains=term)|Q(phone__icontains=term)
            try: query|=Q(pk=UUID(term))
            except ValueError: pass
            for row in qs.filter(query).order_by('-created_at')[:8]:
                results.append({'id':str(row.pk),'screen':feature,'type':'Invoice' if feature=='bills' and row.number else 'Transaction' if feature=='bills' else 'Customer','name':(row.number or 'Draft')+' · '+row.customer.name if feature=='bills' else row.name,'detail':row.franchise.name})
        return Response({'results':results})


class PricingView(APIView):
    @extend_schema(responses=PricingResponse)
    def get(self,request):
        user=request.user;can_manage_rates=has_permission(user,'rates')
        if not can_manage_rates and not (has_permission(user,'bill.write') or has_permission(user,'pickup.write')):
            require(user,'rates')
        kind=request.query_params.get('kind','collection')
        if kind not in ('collection','b2b','sale'): raise ValidationError('Invalid rate type.')
        if can_manage_rates:
            books=scope(user,Franchise.objects.all(),'franchises')
            rate_rows=scope(user,Rate.objects.filter(kind=kind),'rates')
        elif user.role in (Role.APP_MANAGER,Role.DIGESTER_MANAGER):
            books=Franchise.objects.filter(kind='scrap' if user.role==Role.APP_MANAGER else 'digester',status='active')
            rate_rows=Rate.objects.filter(kind=kind,product__franchises__in=books).filter(Q(franchise__in=books)|Q(franchise__isnull=True))
        else:
            books=Franchise.objects.filter(pk=user.franchise_id,status='active')
            rate_rows=Rate.objects.filter(kind=kind,product__franchises__in=books).filter(Q(franchise__in=books)|Q(franchise__isnull=True))
        books=books.order_by('name')
        products=Product.objects.filter(active=True,category__active=True,franchises__in=books).distinct().select_related('category').prefetch_related('franchises')
        if request.query_params.get('category'): products=products.filter(category_id=request.query_params['category'])
        rates={}
        for rate in rate_rows.order_by('-created_at','-id'):
            rates.setdefault((str(rate.product_id),str(rate.franchise_id) if rate.franchise_id else ''),rate)
        rows=[]
        for product in products.order_by('name'):
            default=rates.get((str(product.pk),''));assigned={b.pk for b in product.franchises.all()}
            cells=[]
            for book in books:
                override=rates.get((str(product.pk),str(book.pk)));eligible=book.pk in assigned;rate=override or default
                cells.append({'franchise':str(book.pk),'amount':str(rate.amount) if rate and eligible else None,'discount_percent':str(rate.discount_percent) if rate and eligible else None,'final_price':str(final_unit_price(rate.amount,rate.discount_percent,product.tax_percent)) if rate and eligible else None,'override':bool(override) and eligible,'eligible':eligible})
            rows.append({'id':str(product.pk),'name':product.name,'category_name':product.category.name,'unit':product.unit,'default':str(default.amount) if default else None,'tax_percent':str(product.tax_percent),'hsn':product.hsn,'default_final_price':str(final_unit_price(default.amount,default.discount_percent,product.tax_percent)) if default else None,'default_discount_percent':str(default.discount_percent) if default else None,'cells':cells})
        return Response({'franchises':[{'id':str(b.pk),'name':b.name,'kind':b.kind} for b in books],'results':rows})
