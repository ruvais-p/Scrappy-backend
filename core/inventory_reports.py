"""Scoped stock reports. Period balances come from the immutable ledger."""
from datetime import date, datetime, time, timedelta
from uuid import UUID
from django.db.models import Sum, Q
from django.utils import timezone
from rest_framework.exceptions import ValidationError,NotFound
from rest_framework.response import Response
from rest_framework.views import APIView
from drf_spectacular.utils import extend_schema
from .models import StockMovement,StockBalance,Warehouse,Product
from .policy import scope,require
from .services.common import command,decimal,version,event


def report_filters(request, queryset):
    for param, field in [('franchise','warehouse__franchise_id'),('warehouse','warehouse_id'),('product','product_id')]:
        value=request.query_params.get(param)
        if value:
            try: UUID(value)
            except ValueError: raise ValidationError(f'Invalid {param} filter.')
            queryset=queryset.filter(**{field:value})
    return queryset


class InventoryReportView(APIView):
    @extend_schema(responses=dict)
    def get(self, request):
        today=timezone.localdate()
        try:
            start=date.fromisoformat(request.query_params.get('from') or today.replace(day=1).isoformat())
            end=date.fromisoformat(request.query_params.get('to') or today.isoformat())
            page=max(1,int(request.query_params.get('page','1')))
            if start>end: raise ValueError()
            since=timezone.make_aware(datetime.combine(start,time.min))
            until=timezone.make_aware(datetime.combine(end+timedelta(days=1),time.min))
        except (ValueError,OverflowError): raise ValidationError('Use a valid date range and page.')
        ledger=report_filters(request,scope(request.user,StockMovement.objects.all(),'inventory'))
        balances=report_filters(request,scope(request.user,StockBalance.objects.all(),'inventory'))
        visible_balances=scope(request.user,StockBalance.objects.all(),'inventory')
        warehouses=scope(request.user,Warehouse.objects.all(),'inventory').order_by('name')
        if request.user.role=='warehouse_manager':warehouses=warehouses.filter(pk=request.user.warehouse_id)
        products=scope(request.user,Product.objects.filter(active=True,tracks_stock=True,category__active=True),'inventory').filter(franchises__in=warehouses.values('franchise_id')).select_related('category').distinct().order_by('name')
        availability_qs=visible_balances.values('product_id','product__name','product__unit').annotate(quantity=Sum('quantity'))
        availability_map={str(row['product_id']):row for row in availability_qs}
        availability=[{'product':str(product.pk),'product_name':product.name,'category':product.category.name,
                       'unit':product.unit,'quantity':str(availability_map.get(str(product.pk),{}).get('quantity') or 0)}
                      for product in products[:2000]]
        warehouse_options=[{'id':str(warehouse.pk),'name':warehouse.name} for warehouse in warehouses]
        product_options=[{'id':str(product.pk),'name':product.name,'unit':product.unit} for product in products[:2000]]
        # Include depleted and movement-only rows; never combine unlike units.
        groups=ledger.values('warehouse_id','warehouse__name','product_id','product__name','product__unit').annotate(
            opening=Sum('quantity',filter=Q(created_at__lt=since)),
            receipts=Sum('quantity',filter=Q(created_at__gte=since,created_at__lt=until,kind='receipt')),
            releases=Sum('quantity',filter=Q(created_at__gte=since,created_at__lt=until,kind='release')),
            adjustments=Sum('quantity',filter=Q(created_at__gte=since,created_at__lt=until,kind__in=('adjustment','receipt_adjustment'))),
            closing=Sum('quantity',filter=Q(created_at__lt=until)),
        ).order_by('warehouse__name','product__name','warehouse_id','product_id')
        count=groups.count()
        rows=list(groups[(page-1)*50:page*50])
        current={(str(b.warehouse_id),str(b.product_id)):(b.quantity,b.version) for b in balances.filter(warehouse_id__in=[r['warehouse_id'] for r in rows],product_id__in=[r['product_id'] for r in rows])}
        results=[]
        for row in rows:
            warehouse=str(row['warehouse_id']);product=str(row['product_id'])
            results.append({'warehouse':warehouse,'warehouse_name':row['warehouse__name'],'product':product,'product_name':row['product__name'],'unit':row['product__unit'],
                'opening':str(row['opening'] or 0),'receipts':str(row['receipts'] or 0),'releases':str(-(row['releases'] or 0)),'adjustments':str(row['adjustments'] or 0),
                'closing':str(row['closing'] or 0),'current':str(current.get((warehouse,product),(0,1))[0]),
                'current_version':current.get((warehouse,product),(0,1))[1]})
        return Response({'from':start.isoformat(),'to':end.isoformat(),'timezone':str(timezone.get_current_timezone()),'current_as_of':timezone.now().isoformat(),'count':count,'page':page,'page_size':50,'results':results,'warehouses':warehouse_options,'products':product_options,'availability':availability})


class InventoryAdjustmentView(APIView):
    @extend_schema(request=dict,responses=dict)
    def post(self,request):
        require(request.user,'inventory.adjust')
        data=request.data
        def run(actor):
            require(actor,'inventory.adjust')
            warehouse=scope(actor,Warehouse.objects.all(),'inventory').filter(pk=data.get('warehouse')).first()
            if actor.role=='warehouse_manager' and (not warehouse or warehouse.pk!=actor.warehouse_id):raise NotFound()
            product=scope(actor,Product.objects.filter(active=True,tracks_stock=True,category__active=True),'inventory').filter(pk=data.get('product')).select_related('category').first()
            if not warehouse or not product:raise NotFound()
            if not product.franchises.filter(pk=warehouse.franchise_id).exists():
                raise ValidationError('This product is not assigned to the selected warehouse franchise.')
            quantity=decimal(data.get('quantity'),3,False)
            reason=str(data.get('reason','')).strip()
            if not reason or len(reason)>1000:raise ValidationError('Enter an adjustment reason of at most 1000 characters.')
            balance,_=StockBalance.objects.get_or_create(warehouse=warehouse,product=product,defaults={'quantity':0})
            balance=StockBalance.objects.select_for_update().get(pk=balance.pk)
            if data.get('version') is not None:version(balance,data.get('version'))
            delta=quantity-balance.quantity
            balance.quantity=quantity;balance.version+=1;balance.save(update_fields=['quantity','version'])
            if delta:
                movement=StockMovement.objects.create(warehouse=warehouse,product=product,actor=actor,
                                                       quantity=delta,kind='adjustment')
                event(actor,'inventory',movement,{'quantity_delta':str(delta),'quantity_after':str(quantity),'reason':reason},'stock.adjusted')
            else:
                event(actor,'inventory',balance,{'quantity_delta':'0','quantity_after':str(quantity),'reason':reason},'stock.adjusted')
            return {'warehouse':str(warehouse.pk),'product':str(product.pk),'quantity':str(quantity),'version':balance.version}
        return Response(command(request.user,request.headers.get('Idempotency-Key'),'inventory.adjust',data,run))
