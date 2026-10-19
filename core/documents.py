import hashlib
import hmac
from django.conf import settings
import io
import re
from datetime import timedelta
from pathlib import Path
from decimal import Decimal,ROUND_HALF_UP
from xml.sax.saxutils import escape
from urllib.parse import urlencode
from django.http import HttpResponse
from django.db import transaction
from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT,TA_RIGHT
from reportlab.lib.pagesizes import A4,LETTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import Flowable,SimpleDocTemplate,Paragraph,Spacer,Table,TableStyle
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.exceptions import NotFound,ValidationError
from drf_spectacular.utils import extend_schema
from .models import AuditEvent,Bill,CustomerLink,Job,Lead,Quotation
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


class BrandBadge(Flowable):
    """Draw the official Scrappy badge vector supplied with the application."""
    path=Path(__file__).parent/'assets'/'scrappy-badge.svg'
    def __init__(self,width=132,height=78,fill_color=None):
        super().__init__();self.width=width;self.height=height;self.fill_color=fill_color or colors.HexColor('#8B1E24')
    def draw(self):
        source=self.path.read_text(encoding='utf-8')
        match=re.search(r'<path[^>]+d="([^"]+)"',source)
        if not match:return
        scale=min(self.width/1092,self.height/636)
        xoff=(self.width-1092*scale)/2;yoff=(self.height-636*scale)/2
        def point(x,y):return xoff+(x-245)*scale,yoff+(1104-y)*scale
        self.canv.saveState()
        self.canv.setFillColor(colors.white)
        cx,cy=point(790,780)
        self.canv.ellipse(cx-535*scale,cy-305*scale,cx+535*scale,cy+305*scale,fill=1,stroke=0)
        tokens=re.findall(r'[A-Za-z]|-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?',match.group(1))
        path=self.canv.beginPath();i=0;command=None;started=False
        while i<len(tokens):
            if tokens[i].isalpha():command=tokens[i];i+=1
            if command=='M':
                x,y=point(float(tokens[i]),float(tokens[i+1]));i+=2
                if started:path.lineTo(x,y)
                else:path.moveTo(x,y);started=True
                command='L'
            elif command=='L':
                x,y=point(float(tokens[i]),float(tokens[i+1]));i+=2;path.lineTo(x,y)
            elif command=='C':
                a=point(float(tokens[i]),float(tokens[i+1]));b=point(float(tokens[i+2]),float(tokens[i+3]));c=point(float(tokens[i+4]),float(tokens[i+5]));i+=6;path.curveTo(*a,*b,*c)
            elif command=='Z':path.close();command=None;started=False
            else:break
        self.canv.setFillColor(self.fill_color);self.canv.drawPath(path,fill=1,stroke=0)
        self.canv.restoreState()


class PaymentQR(Flowable):
    def __init__(self,payload,size=58):
        super().__init__();self.payload=payload;self.width=size;self.height=size
    def draw(self):
        from reportlab.graphics import renderPDF
        from reportlab.graphics.barcode.qr import QrCodeWidget
        from reportlab.graphics.shapes import Drawing
        qr=QrCodeWidget(self.payload);x0,y0,x1,y1=qr.getBounds()
        sx=self.width/max(x1-x0,1);sy=self.height/max(y1-y0,1)
        drawing=Drawing(self.width,self.height,transform=[sx,0,0,sy,-x0*sx,-y0*sy]);drawing.add(qr)
        renderPDF.draw(drawing,self.canv,0,0)


def render_bill(bill):
    import json
    if bill.state=='draft': raise ValidationError('A draft is not an issued invoice.')
    if not isinstance(bill.lines,list) or not 1<=len(bill.lines)<=100:
        raise ValidationError('PDF requires between 1 and 100 bill items.')
    if len(json.dumps({'snapshot':bill.snapshot,'lines':bill.lines},default=str).encode())>200_000:
        raise ValidationError('Bill content exceeds the PDF size limit.')
    payments=list(bill.payments.order_by('paid_at')[:101])
    if len(payments)>100: raise ValidationError('PDF exceeds the payment entry limit.')
    is_purchase=bill.kind=='collection'
    page_size=A4 if is_purchase else LETTER
    page_width,_=page_size
    buffer=io.BytesIO()
    margin=30 if is_purchase else 42
    doc=SimpleDocTemplate(buffer,pagesize=page_size,rightMargin=margin,leftMargin=margin,
                          topMargin=30 if is_purchase else 72,bottomMargin=38 if is_purchase else 120,
                          title=str(bill.number or 'Scrappy bill'),author='Scrappy Innovations')
    oxblood=colors.HexColor('#8B1E24') if is_purchase else colors.HexColor('#087FA8')
    oxblood_dark=colors.HexColor('#74181E') if is_purchase else colors.HexColor('#066B8E')
    line=colors.HexColor('#E4E1DC') if is_purchase else colors.HexColor('#D8E3E8')
    ink=colors.HexColor('#20272B');muted=colors.HexColor('#667982')
    soft=colors.HexColor('#FBF1F1') if is_purchase else colors.HexColor('#EEF7FA')
    styles={
        'body':ParagraphStyle('ScrappyBody',fontName='Helvetica',fontSize=8.5,leading=12,textColor=ink,spaceAfter=0),
        'small':ParagraphStyle('ScrappySmall',fontName='Helvetica',fontSize=7.5,leading=10,textColor=muted),
        'tiny':ParagraphStyle('ScrappyTiny',fontName='Helvetica',fontSize=6.8,leading=9,textColor=muted),
        'company':ParagraphStyle('ScrappyCompany',fontName='Helvetica-Bold',fontSize=15,leading=19,textColor=ink),
        'title':ParagraphStyle('ScrappyTitle',fontName='Helvetica-Bold',fontSize=15,leading=18,textColor=colors.white,alignment=TA_LEFT),
        'section':ParagraphStyle('ScrappySection',fontName='Helvetica-Bold',fontSize=8.5,leading=11,textColor=oxblood),
        'customer':ParagraphStyle('ScrappyCustomer',fontName='Helvetica-Bold',fontSize=9,leading=12,textColor=ink),
        'table_head':ParagraphStyle('ScrappyTableHead',fontName='Helvetica-Bold',fontSize=7.5,leading=10,textColor=colors.white),
        'table':ParagraphStyle('ScrappyTable',fontName='Helvetica',fontSize=7.5,leading=10,textColor=ink),
        'table_bold':ParagraphStyle('ScrappyTableBold',fontName='Helvetica-Bold',fontSize=7.5,leading=10,textColor=ink),
        'right':ParagraphStyle('ScrappyRight',fontName='Helvetica',fontSize=8,leading=11,textColor=ink,alignment=TA_RIGHT),
        'center':ParagraphStyle('ScrappyCenter',fontName='Helvetica',fontSize=7.5,leading=10,textColor=ink,alignment=1),
        'total':ParagraphStyle('ScrappyTotal',fontName='Helvetica-Bold',fontSize=9,leading=12,textColor=colors.white,alignment=TA_RIGHT),
        'total_label':ParagraphStyle('ScrappyTotalLabel',fontName='Helvetica-Bold',fontSize=8,leading=11,textColor=colors.white),
        'accent':ParagraphStyle('ScrappyAccent',fontName='Helvetica-Bold',fontSize=7.5,leading=10,textColor=oxblood_dark),
        'accent_right':ParagraphStyle('ScrappyAccentRight',fontName='Helvetica-Bold',fontSize=7.5,leading=10,textColor=oxblood_dark,alignment=TA_RIGHT),
        'words':ParagraphStyle('ScrappyWords',fontName='Helvetica-Bold',fontSize=8.5,leading=12,textColor=ink),
    }
    def paragraph(text,style='body'):
        value=str(text)
        if len(value)>4000: raise ValidationError('PDF text exceeds the field length limit.')
        return Paragraph(escape(value).replace('\n','<br/>'),styles[style])
    def labelled(label,value,style='small'):
        text=f'<b>{escape(str(label))}:</b> {escape(str(value))}'
        if len(text)>4000:raise ValidationError('PDF text exceeds the field length limit.')
        return Paragraph(text,styles[style])
    snapshot=bill.snapshot if isinstance(bill.snapshot,dict) else {}
    issuer=snapshot.get('issuer',{});issuer=issuer if isinstance(issuer,dict) else {'name':str(issuer)}
    customer=snapshot.get('customer',{});customer=customer if isinstance(customer,dict) else {'name':str(customer)}
    customer.setdefault('state',bill.customer.state)
    customer.setdefault('area',bill.customer.area)
    issuer_name=issuer.get('name') or snapshot.get('book') or 'Scrappy Innovations'
    customer_name=customer.get('name') or 'Customer'
    document_title=('COMPANY BILLING · ' if snapshot.get('billing_mode')=='company' else '')+('SCRAP PURCHASE BILL' if is_purchase else 'TAX INVOICE')
    issued=timezone.localtime(bill.issued_at).strftime('%d/%m/%Y') if bill.issued_at else '—'
    def money(value):return 'INR '+format(Decimal(value or 0).quantize(Decimal('.01')),',.2f')
    def detail(key,label):
        value=issuer.get(key)
        return labelled(label,value) if value else None
    if not is_purchase:
        available_width=page_width-2*margin
        address_parts=[str(issuer.get('address') or '—')]
        address_cell=[paragraph('Address:','table_bold'),Spacer(1,5),paragraph('\n'.join(address_parts),'body')]
        def field_cell(name,key):
            value=issuer.get(key)
            return [paragraph(name+':','table_bold'),Spacer(1,5),paragraph(value or '—','body')]
        seller=Table([[address_cell,field_cell('Phone','phone'),field_cell('GSTIN','gstin')],
                      ['',field_cell('Email','email'),field_cell('State','state')]],
                     colWidths=[available_width*.36,available_width*.32,available_width*.32])
        seller.setStyle(TableStyle([('SPAN',(0,0),(0,1)),('LINEBELOW',(0,-1),(-1,-1),1,oxblood),
                                    ('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),6),
                                    ('RIGHTPADDING',(0,0),(-1,-1),6),('TOPPADDING',(0,0),(-1,-1),6),
                                    ('BOTTOMPADDING',(0,0),(-1,-1),9)]))
        customer_lines=[paragraph('Bill To:','table_bold'),paragraph(customer_name,'body')]
        if customer.get('address'):customer_lines.append(paragraph(customer['address'],'body'))
        if customer.get('area'):customer_lines.append(paragraph(customer['area'],'body'))
        if customer.get('phone'):customer_lines.append(paragraph('Contact No.: '+str(customer['phone']),'body'))
        if customer.get('gstin'):customer_lines.append(paragraph('GSTIN: '+str(customer['gstin']),'body'))
        invoice_rows=[[paragraph('Invoice No.','body'),paragraph(bill.number or '—','body')],
                      [paragraph('Date:','body'),paragraph(issued,'body')]]
        invoice_left=Table(invoice_rows,colWidths=[available_width*.21,available_width*.40])
        invoice_left.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),5),
                                          ('RIGHTPADDING',(0,0),(-1,-1),5),('TOPPADDING',(0,0),(-1,-1),8),
                                          ('BOTTOMPADDING',(0,0),(-1,-1),8)]))
        identity=Table([[invoice_left,customer_lines]],colWidths=[available_width*.64,available_width*.36])
        identity.setStyle(TableStyle([('BOX',(0,0),(-1,-1),.6,line),('BACKGROUND',(1,0),(1,0),soft),('VALIGN',(0,0),(-1,-1),'TOP'),
                                      ('LEFTPADDING',(0,0),(-1,-1),6),('RIGHTPADDING',(0,0),(-1,-1),6),
                                      ('TOPPADDING',(0,0),(-1,-1),7),('BOTTOMPADDING',(0,0),(-1,-1),7)]))
        sales_heading=Table([[paragraph(document_title,'title'),BrandBadge(width=100,height=58,fill_color=oxblood)]],
                            colWidths=[available_width*.78,available_width*.22],rowHeights=[62])
        sales_heading.setStyle(TableStyle([('BACKGROUND',(0,0),(0,0),oxblood),('BACKGROUND',(1,0),(1,0),colors.white),
                                           ('VALIGN',(0,0),(-1,-1),'MIDDLE'),('LEFTPADDING',(0,0),(0,0),16),
                                           ('RIGHTPADDING',(0,0),(-1,-1),8),('TOPPADDING',(0,0),(-1,-1),2),
                                           ('BOTTOMPADDING',(0,0),(-1,-1),2)]))
        sales_story=[sales_heading,Spacer(1,7),paragraph(issuer_name,'company'),Spacer(1,5),
                     seller,Spacer(1,8),identity,Spacer(1,14)]
        headings=['#','Item name','HSN/SAC','Quantity','Unit','Price/Unit','GST','Amount']
        widths=[24,116,56,64,40,70,64,94]
        item_rows=[[paragraph(value,'right' if index in (3,5,6,7) else 'center' if index in (0,2,4) else 'table_bold')
                    for index,value in enumerate(headings)]]
        totals_by_unit={}
        for index,line_item in enumerate(bill.lines,1):
            unit=str(line_item.get('unit',''))
            quantity=Decimal(str(line_item.get('quantity') or 0))
            totals_by_unit[unit]=totals_by_unit.get(unit,Decimal('0'))+quantity
            tax_percent=Decimal(str(line_item.get('tax_percent') or 0))
            item_rows.append([paragraph(index,'table'),paragraph(line_item.get('name',''),'table'),
                              paragraph(line_item.get('hsn') or '—','table'),
                              paragraph(format(quantity,',.2f'),'right'),paragraph(unit,'table'),
                              paragraph('Rs '+format(Decimal(line_item.get('rate') or 0),',.2f'),'right'),
                              paragraph(format(tax_percent.normalize(),'f')+'%','right'),
                              paragraph('Rs '+format(Decimal(line_item.get('amount') or 0),',.2f'),'right')])
        quantity_total=' · '.join(f'{format(q,",.2f")} {u}' for u,q in totals_by_unit.items())
        item_rows.append([paragraph('','table'),paragraph('Total','accent'),paragraph('','table'),
                          paragraph(quantity_total,'accent_right'),paragraph('','table'),paragraph('','table'),
                          paragraph('','table'),paragraph('Rs '+format(Decimal(bill.total),',.2f'),'accent_right')])
        items=Table(item_rows,colWidths=widths,repeatRows=1,hAlign='CENTER')
        items.setStyle(TableStyle([('GRID',(0,0),(-1,-1),.4,line),('BOX',(0,0),(-1,-1),.8,oxblood_dark),
                                   ('LINEBELOW',(0,0),(-1,0),.7,oxblood_dark),('BACKGROUND',(0,0),(-1,0),oxblood),
                                   ('BACKGROUND',(0,-1),(-1,-1),soft),('TEXTCOLOR',(1,-1),(-1,-1),oxblood_dark),
                                   ('VALIGN',(0,0),(-1,-1),'MIDDLE'),('ALIGN',(0,0),(0,-1),'CENTER'),
                                   ('ALIGN',(2,0),(2,-1),'CENTER'),('ALIGN',(3,0),(3,-1),'RIGHT'),
                                   ('ALIGN',(4,0),(4,-1),'CENTER'),('ALIGN',(5,0),(-1,-1),'RIGHT'),
                                   ('LEFTPADDING',(0,0),(-1,-1),4),('RIGHTPADDING',(0,0),(-1,-1),4),
                                   ('TOPPADDING',(0,0),(-1,-1),8),('BOTTOMPADDING',(0,0),(-1,-1),8)]))
        state_issuer=str(issuer.get('state','')).strip().casefold()
        state_customer=str(customer.get('state','')).strip().casefold()
        tax_amount=Decimal(bill.tax)
        tax_percent=(tax_amount/Decimal(bill.subtotal)*100).quantize(Decimal('.01'),rounding=ROUND_HALF_UP) if bill.subtotal and tax_amount else Decimal('0')
        if state_issuer and state_customer and state_issuer==state_customer:
            sgst=(tax_amount/2).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)
            tax_rows=[(f'SGST@{format(tax_percent/2,"f")}% ',sgst),
                      (f'CGST@{format(tax_percent/2,"f")}% ',tax_amount-sgst)]
        else:
            tax_rows=[(f'IGST@{format(tax_percent,"f")}% ',tax_amount)]
        payments_methods=[]
        for payment in payments:
            if payment.method not in payments_methods:payments_methods.append(payment.method)
        payment_mode=' + '.join(payments_methods) if payments_methods else 'Credit'
        totals_rows=[('Total quantity',quantity_total),('Sub Total','Rs '+format(Decimal(bill.subtotal),',.2f'))]
        totals_rows.extend((label,'Rs '+format(Decimal(value),',.2f')) for label,value in tax_rows)
        totals_rows.extend([('Total','Rs '+format(Decimal(bill.total),',.2f')),
                            ('Received','Rs '+format(Decimal(bill.settled),',.2f')),
                            ('Balance','Rs '+format(max(Decimal('0'),Decimal(bill.total)-Decimal(bill.settled)),',.2f')),
                            ('Payment Mode',payment_mode),('Invoice Amount In Words',amount_words(bill.total))])
        invoice_total_row=2+len(tax_rows)
        totals_table=Table([[paragraph(label,'total_label' if index==invoice_total_row else 'table_bold' if label=='Total' else 'table'),
                             paragraph(value,'total' if index==invoice_total_row else 'right' if label!='Invoice Amount In Words' else 'table')]
                            for index,(label,value) in enumerate(totals_rows)],
                           colWidths=[available_width*.5,available_width*.5],hAlign='CENTER')
        totals_table.setStyle(TableStyle([('GRID',(0,0),(-1,-1),.4,line),('BOX',(0,0),(-1,-1),.8,oxblood_dark),
                                          ('BACKGROUND',(0,invoice_total_row),(-1,invoice_total_row),oxblood),
                                          ('TEXTCOLOR',(0,invoice_total_row),(-1,invoice_total_row),colors.white),
                                          ('BACKGROUND',(0,-1),(-1,-1),soft),('VALIGN',(0,0),(-1,-1),'MIDDLE'),
                                          ('LEFTPADDING',(0,0),(-1,-1),6),('RIGHTPADDING',(0,0),(-1,-1),6),
                                          ('TOPPADDING',(0,0),(-1,-1),6),('BOTTOMPADDING',(0,0),(-1,-1),6)]))
        sales_story.extend([items,Spacer(1,8),totals_table])
        terms=snapshot.get('terms')
        if terms:sales_story.extend([Spacer(1,8),paragraph(terms,'tiny')])
        def sales_footer(canvas,document):
            canvas.saveState();canvas.setFont('Helvetica',7);canvas.setFillColor(muted)
            canvas.drawCentredString(page_width/2,22,f'{bill.number or "Bill"} · Page {document.page}')
            canvas.restoreState()
        doc.build(sales_story,onFirstPage=sales_footer,onLaterPages=sales_footer,canvasmaker=LimitedCanvas)
        output=buffer.getvalue()
        if len(output)>2_000_000:raise ValidationError('Rendered PDF exceeds the size limit.')
        return output
    story=[]
    title=Table([[paragraph(document_title,'title'),BrandBadge(width=120,height=70)]],colWidths=[385,150],rowHeights=[84])
    title.setStyle(TableStyle([('BACKGROUND',(0,0),(0,0),oxblood),('VALIGN',(0,0),(-1,-1),'MIDDLE'),('LEFTPADDING',(0,0),(0,0),18),('RIGHTPADDING',(0,0),(0,0),8),('LEFTPADDING',(1,0),(1,0),8),('RIGHTPADDING',(1,0),(1,0),0),('TOPPADDING',(0,0),(-1,-1),5),('BOTTOMPADDING',(0,0),(-1,-1),5)]))
    story += [title,Spacer(1,8),paragraph(issuer_name,'company')]
    company_cols=[[],[],[]]
    for key,label in [('address','Address'),('phone','Phone'),('email','Email'),('gstin','GSTIN'),('state','State')]:
        cell=detail(key,label)
        if cell:company_cols[0 if key=='address' else 1 if key in ('phone','email') else 2].append(cell)
    company_data=[[company_cols[i] or [paragraph(' ','small')] for i in range(3)]]
    company=Table(company_data,colWidths=[250,150,135])
    company.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),4),('RIGHTPADDING',(0,0),(-1,-1),8),('TOPPADDING',(0,0),(-1,-1),4),('BOTTOMPADDING',(0,0),(-1,-1),7)]))
    story += [company,Table([['']],colWidths=[535],rowHeights=[1],style=TableStyle([('BACKGROUND',(0,0),(-1,-1),oxblood)])),Spacer(1,9)]
    identity_label='Seller' if is_purchase else 'Bill To'
    contact_lines=[paragraph(identity_label,'section'),paragraph(customer_name,'customer')]
    if customer.get('address'):contact_lines.append(paragraph(customer['address'],'small'))
    if customer.get('phone'):contact_lines.append(paragraph('Contact No.: '+str(customer['phone']),'small'))
    if customer.get('gstin'):contact_lines.append(paragraph('GSTIN: '+str(customer['gstin']),'small'))
    invoice_info=Table([[ [paragraph('Invoice No.','section'),paragraph(bill.number or '—','customer'),paragraph(f'{bill.transaction_type} · {"Sales" if bill.mode=="sales" else "Purchase"}','small'),Spacer(1,4),paragraph('Date: '+issued,'small')], contact_lines]],colWidths=[175,360])
    invoice_info.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),5),('RIGHTPADDING',(0,0),(-1,-1),12),('TOPPADDING',(0,0),(-1,-1),4),('BOTTOMPADDING',(0,0),(-1,-1),11)]))
    story += [invoice_info]
    headings=['#','Item name','HSN/SAC','Quantity','Unit','Price / unit','GST','Amount']
    rows=[[paragraph(h,'table_head') for h in headings]]
    totals_by_unit={}
    for index,line_item in enumerate(bill.lines,1):
        unit=str(line_item.get('unit',''))
        quantity=Decimal(str(line_item.get('quantity') or 0));totals_by_unit[unit]=totals_by_unit.get(unit,Decimal('0'))+quantity
        tax_amount=Decimal(str(line_item.get('tax') or 0));tax_percent=Decimal(str(line_item.get('tax_percent') or 0))
        tax_cell=money(tax_amount)+' ('+format(tax_percent.normalize(),'f')+'%)'
        rows.append([paragraph(index,'table'),paragraph(line_item.get('name',''),'table_bold'),paragraph(line_item.get('hsn') or '—','table'),paragraph(format(quantity.normalize(),',f'),'right'),paragraph(unit,'table'),paragraph(money(line_item.get('rate')),'right'),paragraph(tax_cell,'right'),paragraph(money(line_item.get('amount')),'right')])
    quantity_total=' · '.join(f'{format(q.normalize(),",f")} {u}' for u,q in totals_by_unit.items())
    rows.append([paragraph('','table_bold'),paragraph('Total','table_bold'),paragraph('','table'),paragraph(quantity_total,'right'),paragraph('','table'),paragraph('','table'),paragraph('','table'),paragraph(money(bill.total),'right')])
    widths=[18,124,50,52,33,68,82,108]
    items=Table(rows,colWidths=widths,repeatRows=1,hAlign='LEFT')
    items.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),oxblood),('BACKGROUND',(0,-1),(-1,-1),soft),('TEXTCOLOR',(0,-1),(-1,-1),ink),('LINEBELOW',(0,0),(-1,0),.7,oxblood_dark),('LINEBELOW',(0,1),(-1,-2),.35,line),('LINEABOVE',(0,-1),(-1,-1),.8,line),('VALIGN',(0,0),(-1,-1),'MIDDLE'),('ALIGN',(3,1),(-1,-1),'RIGHT'),('LEFTPADDING',(0,0),(-1,-1),5),('RIGHTPADDING',(0,0),(-1,-1),5),('TOPPADDING',(0,0),(-1,-1),8),('BOTTOMPADDING',(0,0),(-1,-1),8)]))
    story += [items,Spacer(1,5)]
    if is_purchase:
        kg=totals_by_unit.get('kg')
        if kg is not None:story.append(paragraph('Total weight: '+format(kg.normalize(),',f')+' kg','small'))
    state_issuer=str(issuer.get('state','')).strip().casefold();state_customer=str(customer.get('state','')).strip().casefold()
    tax_label='GST'
    if bill.tax and state_issuer and state_customer and state_issuer==state_customer:
        tax_label='CGST + SGST'
    elif bill.tax and state_issuer and state_customer and state_issuer!=state_customer:
        tax_label='IGST'
    outstanding=max(Decimal('0'),Decimal(bill.total)-Decimal(bill.settled))
    methods=[]
    for p in payments:
        if p.method not in methods:methods.append(p.method)
    payment_mode=' + '.join(methods) if methods else ('Credit' if not is_purchase else 'Pending')
    totals_rows=[['Sub Total',money(bill.subtotal)],[tax_label,money(bill.tax)],['Total',money(bill.total)],['Paid to seller' if is_purchase else 'Received',money(bill.settled)],['Balance',money(outstanding)],['Payment Mode',payment_mode]]
    total_data=[[paragraph(a,'table_bold' if a=='Total' else 'table'),paragraph(b,'right' if a!='Total' else 'table_bold')] for a,b in totals_rows]
    summary=Table(total_data,colWidths=[130,105],hAlign='RIGHT')
    summary.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,-1),colors.white),('BACKGROUND',(0,2),(-1,2),oxblood),('TEXTCOLOR',(0,2),(-1,2),colors.white),('LINEBELOW',(0,0),(-1,-1),.35,line),('LINEABOVE',(0,2),(-1,2),.6,oxblood_dark),('VALIGN',(0,0),(-1,-1),'MIDDLE'),('LEFTPADDING',(0,0),(-1,-1),7),('RIGHTPADDING',(0,0),(-1,-1),7),('TOPPADDING',(0,0),(-1,-1),5),('BOTTOMPADDING',(0,0),(-1,-1),5)]))
    # Payment instructions appear only when they are present in the immutable issuer snapshot.
    pay_labels=[('bank_name','Bank name'),('bank_account','Account no.'),('bank_account_no','Account no.'),('bank_ifsc','IFSC'),('ifsc','IFSC'),('account_holder','Account holder'),('upi_id','UPI ID')]
    payment_lines=[];seen=set()
    upi_id=issuer.get('upi_id') if isinstance(issuer.get('upi_id'),str) else ''
    if not is_purchase:
        for key,label in pay_labels:
            value=issuer.get(key)
            if value and label not in seen:
                payment_lines.append(labelled(label,value));seen.add(label)
        if upi_id:
            uri='upi://pay?'+urlencode({'pa':upi_id,'pn':issuer_name,'am':format(outstanding,'.2f'),'cu':'INR'})
            payment_lines.insert(0,paragraph('Scan to pay','section'))
    if is_purchase:
        payment_lines=[paragraph('Seller settlement','section')]
        if payments:
            payment_lines.extend(paragraph(f'{p.method} · {money(p.amount)} · {timezone.localtime(p.paid_at).strftime("%d/%m/%Y")}','small') for p in payments)
        else:payment_lines.append(paragraph('Settlement pending','small'))
    payment_lines.insert(0,paragraph('Pay To' if not is_purchase else 'Settlement details','section')) if not is_purchase else None
    left_width=300;right_width=235
    left_payment=Table([[PaymentQR(uri),payment_lines]],colWidths=[68,220]) if not is_purchase and upi_id else (payment_lines or [paragraph(' ','small')])
    bottom=Table([[left_payment,summary]],colWidths=[left_width,right_width])
    bottom.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),6),('RIGHTPADDING',(0,0),(-1,-1),7),('TOPPADDING',(0,0),(-1,-1),8),('BOTTOMPADDING',(0,0),(-1,-1),6)]))
    is_paid=Decimal(bill.total)>0 and Decimal(bill.settled)==Decimal(bill.total)
    story += [bottom,Spacer(1,8),paragraph('PAID' if is_paid else 'PAYMENT PENDING','section'),Spacer(1,3),paragraph('Invoice Amount In Words','section'),paragraph(amount_words(bill.total),'words')]
    pickup=snapshot.get('pickup',{}) if isinstance(snapshot.get('pickup',{}),dict) else {}
    context=[]
    for key,label in [('collector','Collected by'),('vehicle','Vehicle'),('area','Area')]:
        if pickup.get(key):context.append(f'{label}: {pickup[key]}')
    if context:story += [Spacer(1,5),paragraph(' · '.join(context),'small')]
    if bill.lead_id and bill.lead.agreed_at:
        story.append(paragraph('Customer agreement recorded: '+timezone.localtime(bill.lead.agreed_at).strftime('%d/%m/%Y %I:%M %p IST'),'tiny'))
    terms=snapshot.get('terms')
    if terms:story += [Spacer(1,4),paragraph(terms,'tiny')]
    signer=issuer.get('signatory') or issuer_name
    signature=Table([[paragraph('For: '+str(signer),'small')],[Spacer(1,18)],[paragraph('Authorized Signatory','table_bold')]],colWidths=[230])
    signature.setStyle(TableStyle([('LINEABOVE',(0,2),(0,2),.5,line),('ALIGN',(0,0),(-1,-1),'CENTER'),('TOPPADDING',(0,0),(-1,-1),3),('BOTTOMPADDING',(0,0),(-1,-1),2)]))
    signature.hAlign='LEFT'
    story += [Spacer(1,7),signature]
    def footer(canvas,document):
        canvas.saveState();canvas.setStrokeColor(line);canvas.setLineWidth(.5);canvas.line(30,25,page_width-30,25)
        canvas.setFont('Helvetica',7);canvas.setFillColor(muted);canvas.drawString(30,14,'Scrappy Innovations · Waste management, made simple.')
        canvas.drawRightString(page_width-30,14,f'{bill.number or "Bill"} · Page {document.page}')
        canvas.restoreState()
    doc.build(story,onFirstPage=footer,onLaterPages=footer,canvasmaker=LimitedCanvas)
    output=buffer.getvalue()
    if len(output)>2_000_000: raise ValidationError('Rendered PDF exceeds the size limit.')
    return output

def render_current_bill(pk):
    # Payment commands lock the bill before changing settlement and payment rows.
    # Keep those reads coherent even when payment commits during a PDF request.
    with transaction.atomic():
        bill=Bill.objects.select_for_update().get(pk=pk)
        return render_bill(bill),bill.version


def render_quotation(quotation):
    import json
    if not isinstance(quotation.lines,list) or not 1<=len(quotation.lines)<=100:
        raise ValidationError('PDF requires between 1 and 100 quotation items.')
    if len(json.dumps({'lines':quotation.lines,'notes':quotation.notes},default=str).encode())>200_000:
        raise ValidationError('Quotation content exceeds the PDF size limit.')
    buffer=io.BytesIO();page_width,_=A4
    doc=SimpleDocTemplate(buffer,pagesize=A4,rightMargin=30,leftMargin=30,topMargin=30,bottomMargin=38,
                          title=f'Quotation {quotation.number}',author='Scrappy Innovations')
    oxblood=colors.HexColor('#8B1E24');oxblood_dark=colors.HexColor('#74181E')
    line=colors.HexColor('#E4E1DC');ink=colors.HexColor('#2A2426');muted=colors.HexColor('#5A5452')
    soft=colors.HexColor('#FBF1F1')
    styles={
        'body':ParagraphStyle('QuoteBody',fontName='Helvetica',fontSize=8.5,leading=12,textColor=ink),
        'small':ParagraphStyle('QuoteSmall',fontName='Helvetica',fontSize=7.5,leading=10,textColor=muted),
        'tiny':ParagraphStyle('QuoteTiny',fontName='Helvetica',fontSize=6.8,leading=9,textColor=muted),
        'company':ParagraphStyle('QuoteCompany',fontName='Helvetica-Bold',fontSize=15,leading=19,textColor=ink),
        'title':ParagraphStyle('QuoteTitle',fontName='Helvetica-Bold',fontSize=15,leading=18,textColor=colors.white),
        'section':ParagraphStyle('QuoteSection',fontName='Helvetica-Bold',fontSize=8,leading=10,textColor=oxblood),
        'customer':ParagraphStyle('QuoteCustomer',fontName='Helvetica-Bold',fontSize=9,leading=12,textColor=ink),
        'head':ParagraphStyle('QuoteHead',fontName='Helvetica-Bold',fontSize=7,leading=9,textColor=colors.white),
        'table':ParagraphStyle('QuoteTable',fontName='Helvetica',fontSize=7,leading=9,textColor=ink),
        'bold':ParagraphStyle('QuoteBold',fontName='Helvetica-Bold',fontSize=7,leading=9,textColor=ink),
        'right':ParagraphStyle('QuoteRight',fontName='Helvetica',fontSize=8,leading=11,textColor=ink,alignment=TA_RIGHT),
        'total':ParagraphStyle('QuoteTotal',fontName='Helvetica-Bold',fontSize=9,leading=12,textColor=colors.white,alignment=TA_RIGHT),
    }
    def p(value,style='body'):
        value=str(value or '')
        if len(value)>4000:raise ValidationError('PDF text exceeds the field length limit.')
        return Paragraph(escape(value).replace('\n','<br/>'),styles[style])
    def money(value):return 'INR '+format(Decimal(value or 0).quantize(Decimal('.01')),',.2f')
    book=quotation.franchise;customer=quotation.customer
    issued=timezone.localtime(quotation.created_at).strftime('%d/%m/%Y')
    story=[]
    title=Table([[p('QUOTATION','title'),BrandBadge(width=120,height=70)]],colWidths=[385,150],rowHeights=[84])
    title.setStyle(TableStyle([('BACKGROUND',(0,0),(0,0),oxblood),('VALIGN',(0,0),(-1,-1),'MIDDLE'),('LEFTPADDING',(0,0),(0,0),18),('RIGHTPADDING',(0,0),(0,0),8),('LEFTPADDING',(1,0),(1,0),8),('RIGHTPADDING',(1,0),(1,0),0),('TOPPADDING',(0,0),(-1,-1),5),('BOTTOMPADDING',(0,0),(-1,-1),5)]))
    story += [title,Spacer(1,8),p(book.name,'company')]
    issuer_details=[]
    for label,value in [('Address',book.address),('Phone',book.phone),('Email',book.email),('GSTIN',book.gstin),('State',book.state)]:
        if value:issuer_details.append(p(f'{label}: {value}','small'))
    if issuer_details:story.append(Table([[issuer_details]],colWidths=[535],style=TableStyle([('LEFTPADDING',(0,0),(-1,-1),4),('TOPPADDING',(0,0),(-1,-1),2),('BOTTOMPADDING',(0,0),(-1,-1),7)])))
    story += [Table([['']],colWidths=[535],rowHeights=[1],style=TableStyle([('BACKGROUND',(0,0),(-1,-1),oxblood)])),Spacer(1,9)]
    customer_lines=[p('Prepared for','section'),p(customer.name,'customer')]
    if customer.address:customer_lines.append(p(customer.address,'small'))
    if customer.phone:customer_lines.append(p('Contact No.: '+customer.phone,'small'))
    if customer.gstin:customer_lines.append(p('GSTIN: '+customer.gstin,'small'))
    meta=[p('Quotation No.','section'),p(quotation.number,'customer'),Spacer(1,4),p('Date: '+issued,'small'),p('Status: '+quotation.state.replace('_',' ').title(),'small')]
    info=Table([[meta,customer_lines]],colWidths=[175,360])
    info.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),5),('RIGHTPADDING',(0,0),(-1,-1),12),('TOPPADDING',(0,0),(-1,-1),4),('BOTTOMPADDING',(0,0),(-1,-1),11)]))
    story.append(info)
    headings=['#','Item name','HSN/SAC','Quantity','Unit','Price / unit','GST','Amount']
    rows=[[p(h,'head') for h in headings]];totals_by_unit={}
    for index,item in enumerate(quotation.lines,1):
        unit=str(item.get('unit',''));quantity=Decimal(str(item.get('quantity') or 0))
        totals_by_unit[unit]=totals_by_unit.get(unit,Decimal('0'))+quantity
        tax_amount=Decimal(str(item.get('tax') or 0));tax_percent=Decimal(str(item.get('tax_percent') or 0))
        tax_cell=money(tax_amount)+' ('+format(tax_percent.normalize(),'f')+'%)'
        rows.append([p(index,'table'),p(item.get('name'),'bold'),p(item.get('hsn') or '—','table'),p(format(quantity.normalize(),',f'),'right'),p(unit,'table'),p(money(item.get('rate')),'right'),p(tax_cell,'right'),p(money(item.get('amount')),'right')])
    quantity_total=' · '.join(f'{format(q.normalize(),",f")} {u}' for u,q in totals_by_unit.items())
    rows.append([p('','bold'),p('Total','bold'),p('','table'),p(quantity_total,'right'),p('','table'),p('','table'),p('','table'),p(money(quotation.total),'right')])
    widths=[18,124,50,52,33,68,82,108]
    items=Table(rows,colWidths=widths,repeatRows=1,hAlign='LEFT')
    items.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),oxblood),('BACKGROUND',(0,-1),(-1,-1),soft),('LINEBELOW',(0,0),(-1,0),.7,oxblood_dark),('LINEBELOW',(0,1),(-1,-2),.35,line),('LINEABOVE',(0,-1),(-1,-1),.8,line),('VALIGN',(0,0),(-1,-1),'MIDDLE'),('ALIGN',(3,1),(-1,-1),'RIGHT'),('LEFTPADDING',(0,0),(-1,-1),5),('RIGHTPADDING',(0,0),(-1,-1),5),('TOPPADDING',(0,0),(-1,-1),8),('BOTTOMPADDING',(0,0),(-1,-1),8)]))
    story += [items,Spacer(1,10)]
    totals=[['Sub Total',money(quotation.subtotal)],['GST',money(quotation.tax)],['Estimated Total',money(quotation.total)]]
    summary=Table([[p(a,'bold' if a=='Estimated Total' else 'table'),p(b,'right' if a!='Estimated Total' else 'total')] for a,b in totals],colWidths=[130,105],hAlign='RIGHT')
    summary.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,-1),colors.white),('BACKGROUND',(0,-1),(-1,-1),oxblood),('LINEBELOW',(0,0),(-1,-1),.35,line),('VALIGN',(0,0),(-1,-1),'MIDDLE'),('LEFTPADDING',(0,0),(-1,-1),7),('RIGHTPADDING',(0,0),(-1,-1),7),('TOPPADDING',(0,0),(-1,-1),6),('BOTTOMPADDING',(0,0),(-1,-1),6)]))
    story.append(summary)
    if quotation.notes:story += [Spacer(1,10),p('Notes','section'),p(quotation.notes,'small')]
    story += [Spacer(1,12),p('This quotation is an estimate and is not a tax invoice. Final billing is subject to customer approval and conversion into a bill.','tiny'),Spacer(1,18)]
    signature=Table([[p('For: '+book.name,'small')],[Spacer(1,18)],[p('Authorized Signatory','bold')]],colWidths=[230])
    signature.setStyle(TableStyle([('LINEABOVE',(0,2),(0,2),.5,line),('ALIGN',(0,0),(-1,-1),'CENTER'),('TOPPADDING',(0,0),(-1,-1),3),('BOTTOMPADDING',(0,0),(-1,-1),2)]));story.append(signature)
    def footer(canvas,document):
        canvas.saveState();canvas.setStrokeColor(line);canvas.setLineWidth(.5);canvas.line(30,25,page_width-30,25)
        canvas.setFont('Helvetica',7);canvas.setFillColor(muted);canvas.drawString(30,14,'Scrappy Innovations · Waste management, made simple.')
        canvas.drawRightString(page_width-30,14,f'{quotation.number} · Page {document.page}');canvas.restoreState()
    doc.build(story,onFirstPage=footer,onLaterPages=footer,canvasmaker=LimitedCanvas)
    output=buffer.getvalue()
    if len(output)>2_000_000:raise ValidationError('Rendered PDF exceeds the size limit.')
    return output


class QuotationPDF(APIView):
    @extend_schema(responses=bytes)
    def get(self,request,pk):
        quotation=get_object(request.user,'quotations',pk)
        quotation=Quotation.objects.select_related('franchise','customer').get(pk=quotation.pk)
        data=render_quotation(quotation)
        response=pdf_response(data,quotation.version,f'quotation-{quotation.number}.pdf')
        return response


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
        return Response({'url':'/bill/'+raw})

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
        response=pdf_response(render_bill(bill),bill.version,f'bill-{bill.number or bill.pk}.pdf')
        response['X-Robots-Tag']='noindex, nofollow'
        return response


class CustomerBillPage(APIView):
    permission_classes=[AllowAny]
    authentication_classes=[]

    def _link(self,token):
        return CustomerLink.objects.filter(
            digest=hashlib.sha256(token.encode()).hexdigest(),
            revoked=False,expires_at__gt=timezone.now(),
        ).first()

    def get(self,request,token):
        link=self._link(token)
        if not link: raise NotFound()
        bill=Bill.objects.select_related('customer','lead').filter(pk=link.bill_id).first()
        if not bill or bill.state=='draft': raise NotFound()
        paid=Decimal(bill.total)>0 and Decimal(bill.settled)>=Decimal(bill.total)
        response=Response({
            'number':bill.number,'customer_name':bill.customer.name,'lines':bill.lines,
            'total':str(bill.total),'settled':str(bill.settled),'paid':paid,
            'agreed':bool(bill.lead_id and bill.lead.agreed_at),
        })
        response['Cache-Control']='private, no-store'
        response['X-Robots-Tag']='noindex, nofollow'
        return response

    @transaction.atomic
    def post(self,request,token):
        if request.data.get('action')!='agree': raise ValidationError('Choose the rate agreement action.')
        digest=hashlib.sha256(token.encode()).hexdigest()
        initial=CustomerLink.objects.filter(digest=digest,revoked=False,expires_at__gt=timezone.now()).values('id','bill_id').first()
        if not initial: raise NotFound()
        bill=Bill.objects.select_for_update().select_related('customer').filter(pk=initial['bill_id']).first()
        link=CustomerLink.objects.select_for_update().filter(
            pk=initial['id'],digest=digest,revoked=False,expires_at__gt=timezone.now(),
        ).first()
        if not bill or not link or bill.state not in ('issued','completed') or not bill.lead_id: raise NotFound()
        lead=Lead.objects.select_for_update().get(pk=bill.lead_id)
        if lead.agreed_at:
            return Response({'agreed':True})
        if lead.state in ('not_completed','cancelled') or lead.closed_at:
            raise ValidationError('This pickup is no longer open for a rate agreement.')
        now=timezone.now()
        lead.sent_at=lead.sent_at or now
        lead.agreed_at=now
        lead.state='agreed'
        lead.version+=1
        lead.save(update_fields=['sent_at','agreed_at','state','version','updated_at'])
        AuditEvent.objects.create(
            actor=None,franchise_id=bill.franchise_id,action='customer.rate_agreed',
            resource=str(lead.pk),details={'bill':str(bill.pk),'customer_link':str(link.pk)},
        )
        return Response({'agreed':True})
