from datetime import datetime
from uuid import UUID
from django.db.models import Q, F
from rest_framework.exceptions import ValidationError
from .models import Product

def filter_records(qs, feature, params):
    for key in ('franchise','customer','product','category','warehouse','receipt','requirement'):
        if params.get(key):
            try: UUID(str(params[key]))
            except (ValueError, TypeError): raise ValidationError(f'Invalid {key} filter.')
    for key in ('staff','actor','driver'):
        if params.get(key) and not str(params[key]).isdigit(): raise ValidationError(f'Invalid {key} filter.')
    if params.get('from') and params.get('to') and params['from']>params['to']:
        raise ValidationError('The end date must follow the start date.')
    if params.get('franchise_type') and params['franchise_type'] not in ('scrap','digester','hq'):
        raise ValidationError('Invalid franchise type.')
    for key in ('state','kind','mode','transaction_type'):
        field=next((f for f in qs.model._meta.fields if f.name==key),None)
        if field and field.choices and params.get(key) and params[key] not in dict(field.choices):
            raise ValidationError(f'Invalid {key} filter.')
    if feature=='payments' and params.get('method') and params['method'] not in ('Cash','UPI','Bank transfer','Card','Cheque'):
        raise ValidationError('Invalid payment method.')
    if feature=='bills' and params.get('financial_only')=='1': qs=qs.exclude(state__in=['draft','cancelled'])
    if feature=='loads' and params.get('settlement_state'):
        settlement=params['settlement_state']
        if settlement in ('awaiting_supervisor','verified','disputed'):
            qs=qs.filter(counted_in__handover__state=settlement)
        elif settlement=='on_route': qs=qs.filter(counted_in__isnull=True,state='on_route')
        elif settlement=='count_pending': qs=qs.filter(counted_in__isnull=True).exclude(state='on_route')
        else: raise ValidationError('Invalid settlement state.')
    if feature=='bills' and params.get('issued')=='1': qs=qs.exclude(state='draft')
    fields={f.name for f in qs.model._meta.fields}
    selected_book=params.get('franchise')
    if selected_book:
        try: UUID(selected_book)
        except ValueError: raise ValidationError('Invalid franchise filter.')
        path={'payments':'bill__franchise_id','inventory':'warehouse__franchise_id','movements':'warehouse__franchise_id','receipts':'warehouse__franchise_id','franchises':'id','products':'franchises__id'}.get(feature,'franchise_id' if 'franchise' in fields else None)
        if path:
            qs=qs.filter(Q(franchise_id=selected_book)|Q(franchise__isnull=True)) if feature=='rates' else qs.filter(**{path:selected_book})
    for field in ('franchise','driver','state','kind','mode','transaction_type','warehouse','product','receipt','requirement'):
        if field=='franchise': continue
        value=params.get(field)
        if value and field in fields: qs=qs.filter(**{field:value})
    if feature=='receipts' and params.get('flagged')=='1':
        qs=qs.filter(flagged=True,submitted_at__isnull=False)
    if feature=='receipts' and params.get('explained')=='1':
        qs=qs.exclude(explanation='')
    if feature=='audit':
        action=params.get('action','')[:100]
        actor=params.get('actor')
        if action: qs=qs.filter(action__icontains=action)
        if actor: qs=qs.filter(actor_id=actor)
    search=params.get('search','')[:160]
    if search:
        candidates=[f for f in ('name','code','number','phone','owner_name','district','notes','action','reference','resource','items') if f in fields]
        query=Q()
        for f in candidates: query|=Q(**{f+'__icontains':search})
        if feature=='rates': query|=Q(product__name__icontains=search)|Q(franchise__name__icontains=search)|Q(reason__icontains=search)
        if feature=='payments': query|=Q(bill__number__icontains=search)|Q(bill__customer__name__icontains=search)|Q(bill__customer__phone__icontains=search)
        if 'customer' in fields: query|=Q(customer__name__icontains=search)|Q(customer__phone__icontains=search)
        if query: qs=qs.filter(query)
    date_field='pickup_date' if feature=='leads' else 'paid_at' if feature=='payments' else 'submitted_at' if feature=='receipts' else 'issued_at' if feature=='bills' and params.get('issued')=='1' else 'created_at'
    if date_field in fields:
        for param,lookup in [('from','gte'),('to','lte')]:
            if params.get(param):
                try: date=datetime.strptime(params[param],'%Y-%m-%d').date()
                except ValueError: raise ValidationError('Invalid date filter.')
                qs=qs.filter(**{date_field+('' if date_field=='pickup_date' else '__date')+'__'+lookup:date})
    # Related-field filters shared by registers and exports.
    fields={f.name for f in qs.model._meta.fields}
    bill_prefix='bill__' if feature=='payments' else ''
    if feature in ('bills','pickups','payments','quotations'):
        for key,path in [('franchise_type','franchise__kind'),('staff','creator_id'),('customer','customer_id')]:
            if params.get(key): qs=qs.filter(**{bill_prefix+path:params[key]})
        if feature!='quotations':
            direction=params.get('direction')
            if direction:
                if direction not in ('incoming','outgoing'): raise ValidationError('Invalid payment direction.')
                qs=qs.filter(**{bill_prefix+'kind__in':['sale'] if direction=='incoming' else ['collection','b2b']})
            state=params.get('payment_state')
            if state:
                if state not in ('paid','partial','pending','due'): raise ValidationError('Invalid payment state.')
                qs=qs.exclude(**{bill_prefix+'state__in':['draft','cancelled']})
                total=F(bill_prefix+'total')
                if state=='paid': qs=qs.filter(**{bill_prefix+'settled__gte':total})
                elif state=='partial': qs=qs.filter(**{bill_prefix+'settled__gt':0,bill_prefix+'settled__lt':total})
                elif state=='pending': qs=qs.filter(**{bill_prefix+'settled':0})
                else: qs=qs.filter(**{bill_prefix+'settled__lt':total})
            if params.get('product') or params.get('category'):
                products=Product.objects.all()
                if params.get('product'): products=products.filter(pk=params['product'])
                if params.get('category'): products=products.filter(category_id=params['category'])
                line_query=Q(pk__in=[])
                for product in products.values_list('pk',flat=True):
                    line_query |= Q(**{bill_prefix+'lines__contains':[{'product':str(product)}]})
                qs=qs.filter(line_query)
    if feature=='payments' and params.get('method'): qs=qs.filter(method=params['method'])
    if params.get('category') and feature in ('products','rates'):
        qs=qs.filter(**{('category_id' if feature=='products' else 'product__category_id'):params['category']})
    if params.get('status') and 'status' in fields:
        choices=dict(qs.model._meta.get_field('status').choices)
        if choices and params['status'] not in choices: raise ValidationError('Invalid status.')
        qs=qs.filter(status=params['status'])
    if params.get('active'):
        if params['active'] not in ('true','false'): raise ValidationError('Invalid active filter.')
        if 'active' in fields: qs=qs.filter(active=params['active']=='true')
    return qs.distinct(),date_field
