import hashlib
import hmac
from django.conf import settings
import io
from datetime import timedelta
from decimal import Decimal
from xml.sax.saxutils import escape
from django.http import HttpResponse
from django.db import transaction
from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import SimpleDocTemplate,Paragraph,Spacer,Table,TableStyle
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.exceptions import NotFound,ValidationError
from drf_spectacular.utils import extend_schema
from .models import Bill,CustomerLink,Job
from .policy import policy,PolicyUnavailable
from .views import get_object
from .services.common import command,event
from .serializers import serialize
from reportlab.pdfgen.canvas import Canvas

def amount_words(value):
    ones=('zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen').split()
    tens=('zero ten twenty thirty forty fifty sixty seventy eighty ninety').split()
    def whole(n):
        if n<20:return ones[n]
        if n<100:return tens[n//10]+(' '+ones[n%10] if n%10 else '')
        for size,name in [(10000000,'crore'),(100000,'lakh'),(1000,'thousand'),(100,'hundred')]:
            if n>=size:return whole(n//size)+' '+name+(' '+whole(n%size) if n%size else '')
    amount=Decimal(value).quantize(Decimal('.01'));rupees=int(amount);paise=int((amount-rupees)*100)
    return ('Rupees '+whole(rupees)+(' and '+whole(paise)+' paise' if paise else '')+' only').capitalize()

class LimitedCanvas(Canvas):
    def showPage(self):
        if self.getPageNumber()>25:
            raise ValidationError('PDF exceeds the 25-page limit.')
        super().showPage()


def render_bill(bill):
    import json
    if bill.state=='draft': raise ValidationError('A draft is not an issued invoice.')
    if not isinstance(bill.lines,list) or not 1<=len(bill.lines)<=100:
        raise ValidationError('PDF requires between 1 and 100 bill items.')
    if len(json.dumps({'snapshot':bill.snapshot,'lines':bill.lines},default=str).encode())>200_000:
        raise ValidationError('Bill content exceeds the PDF size limit.')
    payments=list(bill.payments.order_by('paid_at')[:101])
    if len(payments)>100: raise ValidationError('PDF exceeds the payment entry limit.')
    buffer=io.BytesIO();styles=getSampleStyleSheet();doc=SimpleDocTemplate(buffer)
    def paragraph(text):
        value=str(text)
        if len(value)>4000: raise ValidationError('PDF text exceeds the field length limit.')
        return Paragraph(escape(value),styles['Normal'])
    snapshot=bill.snapshot
    story=[Paragraph('SCRAPPY',styles['Title']),Spacer(1,12),Paragraph('SCRAP PURCHASE BILL' if bill.kind=='collection' else 'INVOICE',styles['Heading1']),paragraph(bill.number),paragraph(snapshot.get('issuer',{}).get('name','') if isinstance(snapshot.get('issuer'),dict) else snapshot.get('issuer','')),Spacer(1,16),paragraph(('Sold by (customer): ' if bill.kind=='collection' else 'Customer: ')+snapshot.get('customer',{}).get('name','')),paragraph(snapshot.get('customer',{}).get('address','')),Spacer(1,16)]
    if bill.issued_at:story.append(paragraph('Issued: '+timezone.localtime(bill.issued_at).strftime('%d %b %Y, %I:%M %p IST')))
    pickup=snapshot.get('pickup',{})
    for key,title in [('lead','Lead'),('load','Load'),('collector','Collected by'),('vehicle','Vehicle'),('area','Area')]:
        if pickup.get(key):story.append(paragraph(title+': '+pickup[key]))
    story.append(Spacer(1,12))
    rows=[['Material','Quantity','Rate','Amount']]+[[paragraph(l['name']),l['quantity']+' '+l['unit'],l['rate'],l['amount']] for l in bill.lines]
    table=Table(rows,colWidths=[220,90,80,80],repeatRows=1);table.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#D9F1ED')),('LINEBELOW',(0,0),(-1,-1),.5,colors.HexColor('#D6E1DE')),('TOPPADDING',(0,0),(-1,-1),10),('BOTTOMPADDING',(0,0),(-1,-1),10)]))
    story += [table,Spacer(1,16)]
    if bill.kind=='collection':story.append(paragraph('Total weight: '+str(sum(Decimal(l['quantity']) for l in bill.lines if l['unit']=='kg'))+' kg'))
    story += [paragraph(f'Subtotal: INR {bill.subtotal} | Tax: INR {bill.tax}'),Paragraph(f'Total: INR {bill.total}',styles['Heading2']),paragraph(amount_words(bill.total)),Paragraph('Amount is PAID' if bill.total>0 and bill.settled==bill.total else 'PAYMENT PENDING',styles['Heading2'])]
    if bill.lead_id and bill.lead.agreed_at:story.append(paragraph('Customer agreement recorded: '+timezone.localtime(bill.lead.agreed_at).strftime('%d %b %Y, %I:%M %p IST')))
    for payment in payments:
        story.append(paragraph(f'{payment.method}: INR {payment.amount} recorded '+timezone.localtime(payment.paid_at).strftime('%d %b %Y, %I:%M %p IST')))
    story.append(paragraph(snapshot.get('terms','')))
    doc.build(story,canvasmaker=LimitedCanvas)
    output=buffer.getvalue()
    if len(output)>2_000_000: raise ValidationError('Rendered PDF exceeds the size limit.')
    return output

def render_current_bill(pk):
    # Payment commands lock the bill before changing settlement and payment rows.
    # Keep those reads coherent even when payment commits during a PDF request.
    with transaction.atomic():
        bill=Bill.objects.select_for_update().get(pk=pk)
        return render_bill(bill),bill.version


def pdf_response(data, version, filename=None):
    response=HttpResponse(data,content_type='application/pdf')
    response['Cache-Control']='private, no-store'
    response['X-Content-Type-Options']='nosniff'
    response['Referrer-Policy']='no-referrer'
    response['X-Document-Version']=str(version)
    if filename: response['Content-Disposition']=f'attachment; filename="{filename}"'
    return response

class BillPDF(APIView):
    @extend_schema(responses=bytes)
    def get(self,request,pk):
        feature='pickups' if request.user.role in ('hq_manager','collector') else 'bills'
        bill=get_object(request.user,feature,pk)
        if bill.state=='draft': raise ValidationError('A draft is not an issued invoice.')
        data,version=render_current_bill(bill.pk)
        response=pdf_response(data,version);response['Content-Disposition']=f'inline; filename="bill-{bill.id}.pdf"';return response

class PDFJobView(APIView):
    @extend_schema(request=dict,responses=dict)
    def post(self,request,pk):
        feature='pickups' if request.user.role in ('hq_manager','collector') else 'bills'
        bill=get_object(request.user,feature,pk)
        if bill.state=='draft': raise ValidationError('A draft is not an issued invoice.')
        payload={'feature':feature,'bill':str(pk),'permission_version':request.user.permission_version}
        def enqueue(actor):
            current=get_object(actor,feature,pk)
            if current.state=='draft': raise ValidationError('A draft is not an issued invoice.')
            job=Job.objects.create(actor=actor,kind='pdf',payload=payload)
            return serialize('jobs',job)
        return Response(command(request.user,request.headers.get('Idempotency-Key'),'pdf.create',payload,enqueue),status=202)

class ShareLink(APIView):
    @extend_schema(request=dict,responses=dict)
    def post(self,request,pk):
        bill=get_object(request.user,'pickups',pk)
        if request.user.pk!=bill.creator_id or bill.state=='draft': raise ValidationError('Only the collector may share an issued own bill.')
        config=policy('customer_sharing')
        if config.get('recipient_check')!='bearer_link' or not 1<=config.get('lifetime_hours',0)<=168: raise PolicyUnavailable('Configure the approved customer-link lifetime and recipient policy.')
        key=request.headers.get('Idempotency-Key','')
        raw=hmac.new(settings.SECRET_KEY.encode(),f'customer-bill:{request.user.pk}:{bill.pk}:{key}'.encode(),hashlib.sha256).hexdigest()
        def run(actor):
            current=get_object(actor,'pickups',pk,True)
            if current.creator_id!=actor.pk or current.state=='draft': raise ValidationError('Only the collector may share an issued own bill.')
            CustomerLink.objects.create(bill=bill,digest=hashlib.sha256(raw.encode()).hexdigest(),expires_at=timezone.now()+timedelta(hours=config['lifetime_hours']))
            return {'bill':str(bill.pk)}
        command(request.user,key,'bill.share',{'bill':str(pk)},run)
        return Response({'url':'/api/v1/customer-bill/'+raw+'/'})

class RevokeCustomerLinks(APIView):
    @extend_schema(request=dict,responses=dict)
    def post(self,request,pk):
        bill=get_object(request.user,'pickups',pk)
        if bill.creator_id!=request.user.pk: raise ValidationError('Only the issuing collector may revoke these links.')
        def revoke(actor):
            current=get_object(actor,'pickups',pk,True)
            if current.creator_id!=actor.pk: raise ValidationError('Only the issuing collector may revoke these links.')
            count=CustomerLink.objects.filter(bill=current,revoked=False).update(revoked=True)
            event(actor,'pickups',current,{'revoked_links':count},'customer_links.revoked')
            return {'revoked':count}
        return Response(command(request.user,request.headers.get('Idempotency-Key'),'bill.links.revoke',{'bill':str(pk)},revoke))

class CustomerPDF(APIView):
    permission_classes=[AllowAny]
    authentication_classes=[]
    @extend_schema(responses=bytes)
    def get(self,request,token):
        link=CustomerLink.objects.select_related('bill').filter(digest=hashlib.sha256(token.encode()).hexdigest(),revoked=False,expires_at__gt=timezone.now()).first()
        if not link: raise NotFound()
        with transaction.atomic():
            bill=Bill.objects.select_for_update().get(pk=link.bill_id)
            if not CustomerLink.objects.filter(pk=link.pk,revoked=False,expires_at__gt=timezone.now()).exists(): raise NotFound()
            response=pdf_response(render_bill(bill),bill.version)
        response['X-Robots-Tag']='noindex, nofollow'
        return response
