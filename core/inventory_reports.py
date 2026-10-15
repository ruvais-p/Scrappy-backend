"""Scoped stock reports. Period balances come from the immutable ledger."""
from datetime import date, datetime, time, timedelta
from uuid import UUID
from django.db.models import Sum, Q
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView
from .models import StockMovement, StockBalance
from .policy import scope


def report_filters(request, queryset):
    for param, field in [('franchise','warehouse__franchise_id'),('warehouse','warehouse_id'),('product','product_id')]:
        value=request.query_params.get(param)
        if value:
            try: UUID(value)
            except ValueError: raise ValidationError(f'Invalid {param} filter.')
            queryset=queryset.filter(**{field:value})
    return queryset


class InventoryReportView(APIView):
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
        # Include depleted and movement-only rows; never combine unlike units.
        groups=ledger.values('warehouse_id','warehouse__name','product_id','product__name','product__unit').annotate(
            opening=Sum('quantity',filter=Q(created_at__lt=since)),
            receipts=Sum('quantity',filter=Q(created_at__gte=since,created_at__lt=until,kind='receipt')),
            releases=Sum('quantity',filter=Q(created_at__gte=since,created_at__lt=until,kind='release')),
            closing=Sum('quantity',filter=Q(created_at__lt=until)),
        ).order_by('warehouse__name','product__name','warehouse_id','product_id')
        count=groups.count()
        rows=list(groups[(page-1)*50:page*50])
        current={(str(b.warehouse_id),str(b.product_id)):b.quantity for b in balances.filter(warehouse_id__in=[r['warehouse_id'] for r in rows],product_id__in=[r['product_id'] for r in rows])}
        results=[]
        for row in rows:
            warehouse=str(row['warehouse_id']);product=str(row['product_id'])
            results.append({'warehouse':warehouse,'warehouse_name':row['warehouse__name'],'product':product,'product_name':row['product__name'],'unit':row['product__unit'],
                'opening':str(row['opening'] or 0),'receipts':str(row['receipts'] or 0),'releases':str(-(row['releases'] or 0)),
                'closing':str(row['closing'] or 0),'current':str(current.get((warehouse,product),0))})
        return Response({'from':start.isoformat(),'to':end.isoformat(),'timezone':str(timezone.get_current_timezone()),'current_as_of':timezone.now().isoformat(),'count':count,'page':page,'page_size':50,'results':results})
