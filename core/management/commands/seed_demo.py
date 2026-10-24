import os
from datetime import timedelta
from decimal import Decimal
from django.conf import settings
from django.core.management.base import BaseCommand,CommandError
from django.contrib.auth.password_validation import validate_password
from django.db import transaction
from django.utils import timezone
from core.fixtures import synthetic_records
from core.models import District, ExpensePolicy, FieldDay, FieldFollowUp, FieldVisit, FranchiseTarget, KWDLead, KWDLeadEvent, KWDStockBalance, KWDStockMovement, LSGD, LSGDContact, LSGDOpportunity, PresentationAppointment, Product, Role, User, VisitContact, VisitPlan
class Command(BaseCommand):
    help='Create isolated synthetic development records; never runs in production.'
    def add_arguments(self,p): p.add_argument('--test-policies',action='store_true')
    def handle(self,*args,**opts):
        if settings.ENVIRONMENT!='development':raise CommandError('Demo fixtures require development environment.')
        password=os.getenv('SCRAPPY_DEMO_PASSWORD')
        if not password: raise CommandError('Set SCRAPPY_DEMO_PASSWORD to a unique development-only password.')
        validate_password(password)
        with transaction.atomic():
            data=synthetic_records(password,opts['test_policies'])
            manager=data['users'][Role.DIGESTER_MANAGER]
            zonal_manager=data['users'][Role.ZONAL_MANAGER]
            tso=data['users'][Role.TSO]
            digester_user=manager
            digester_franchises=[book for book in data['books'] if book.kind=='digester']
            demo_policy={
                'two_wheeler_rate':Decimal('5.00'),'car_rate':Decimal('10.00'),
                'da_local_max':Decimal('500.00'),'da_night_max':Decimal('1000.00'),
                'da_bill_threshold':Decimal('500.00'),'lodging_cap':Decimal('2000.00'),
                'auto_taxi_cap':Decimal('800.00'),'daily_km_limit':200,
                'gps_allowance_percent':25,'road_factor':Decimal('1.35'),'submit_days':7,
            }
            claim_policy=ExpensePolicy.objects.first()
            if claim_policy is None:
                ExpensePolicy.objects.create(**demo_policy)
            else:
                missing_policy_values={key:value for key,value in demo_policy.items() if not getattr(claim_policy,key)}
                if missing_policy_values:
                    for key,value in missing_policy_values.items(): setattr(claim_policy,key,value)
                    claim_policy.version+=1
                    claim_policy.save(update_fields=[*missing_policy_values,'version','updated_at'])
            today=timezone.localdate()
            now=timezone.now()
            samples=[
                ('Demo North Grama Panchayat','grama_panchayat','Ernakulam','Synthetic Digester CSV demo record',Decimal('10.1200000'),Decimal('76.3000000')),
                ('Demo South Grama Panchayat','grama_panchayat','Ernakulam','Synthetic Digester CSV demo record',Decimal('9.9300000'),Decimal('76.2700000')),
                ('Demo Central Municipality','municipality','Thrissur','Synthetic Digester CSV demo record',Decimal('10.5200000'),Decimal('76.2100000')),
                ('Demo Coastal Block Panchayat','block_panchayat','Alappuzha','Synthetic Digester CSV demo record',Decimal('9.5000000'),Decimal('76.3300000')),
            ]
            lsgds=[]
            for index,(name,kind,district_name,address,latitude,longitude) in enumerate(samples):
                district,_=District.objects.get_or_create(name=district_name)
                row,_=LSGD.objects.get_or_create(name=name,district=district,defaults={'kind':kind,'address':address,'latitude':latitude,'longitude':longitude,'created_by':manager})
                if index < 2 and not row.assigned_manager_id:
                    row.assigned_manager=zonal_manager
                    row.manager_assigned_by=manager
                    row.assigned_tso=tso
                    row.tso_assigned_by=zonal_manager
                    row.save(update_fields=['assigned_manager','manager_assigned_by','assigned_tso','tso_assigned_by','updated_at'])
                lsgds.append(row)

            contacts=[]
            for row,name,designation,phone in [
                (lsgds[0],'Anitha Nair','Secretary','9000000101'),
                (lsgds[1],'Suresh Kumar','Assistant Engineer','9000000102'),
                (lsgds[2],'Meera Thomas','Health Standing Committee Chair','9000000103'),
            ]:
                contact,_=LSGDContact.objects.get_or_create(lsgd=row,name=name,defaults={'designation':designation,'phone':phone,'decision_maker':True,'verified_by':zonal_manager})
                contacts.append(contact)

            financial_year=f'{today.year}-{today.year+1}' if today.month>=4 else f'{today.year-1}-{today.year}'
            for row,stage,units,chance,contact_index in [
                (lsgds[0],'first_meeting',12,40,0),
                (lsgds[1],'proposal_submitted',24,70,1),
            ]:
                LSGDOpportunity.objects.get_or_create(lsgd=row,financial_year=financial_year,defaults={
                    'stage':stage,'expected_units':units,'expected_value':Decimal(units*500),
                    'chance_percent':chance,'expected_month':today.replace(day=1),'updated_by':tso,
                })

            planned,_=VisitPlan.objects.get_or_create(lsgd=lsgds[0],assigned_to=tso,planned_date=today,defaults={
                'purpose':'Meet the LSGD team to introduce the digester programme.','created_by':zonal_manager,
            })
            verified_plan,_=VisitPlan.objects.get_or_create(lsgd=lsgds[1],assigned_to=tso,planned_date=today-timedelta(days=1),defaults={
                'purpose':'Review the submitted proposal and confirm next steps.','state':'done','created_by':zonal_manager,
            })
            if verified_plan.state!='done':
                verified_plan.state='done'
                verified_plan.save(update_fields=['state','updated_at'])

            pending_visit,_=FieldVisit.objects.get_or_create(owner=tso,lsgd=lsgds[0],plan=None,defaults={
                'lsgd':lsgds[0],'visited_at':now,'latitude':lsgds[0].latitude,'longitude':lsgds[0].longitude,
                'gps_accuracy':Decimal('18.00'),'distance_m':45,'discussion':'Discussed digester capacity, site readiness, and the local body approval process.',
                'outcome':'The secretary requested a follow-up with the engineering team.','interest':4,'details_at':now,
            })
            verified_visit,_=FieldVisit.objects.get_or_create(owner=tso,lsgd=lsgds[1],plan=verified_plan,defaults={
                'lsgd':lsgds[1],'visited_at':now-timedelta(days=1),'latitude':lsgds[1].latitude,'longitude':lsgds[1].longitude,
                'gps_accuracy':Decimal('12.00'),'distance_m':32,'discussion':'Reviewed the proposal, installation location, and expected annual waste volume with the LSGD team.',
                'outcome':'Proposal accepted for review in the annual plan.','interest':5,'details_at':now-timedelta(days=1),
                'zm_status':'verified','zm_reviewer':zonal_manager,'zm_remark':'Visit evidence and discussion details reviewed.',
            })
            VisitContact.objects.get_or_create(visit=pending_visit,contact=contacts[0])
            VisitContact.objects.get_or_create(visit=verified_visit,contact=contacts[1])
            FieldFollowUp.objects.get_or_create(visit=pending_visit,owner=tso,defaults={
                'lsgd':lsgds[0],'contact':contacts[0],'due_date':today,'mode':'visit',
                'note':'Arrange a site review with the engineering team.',
            })
            kwd_samples=[
                ('Demo KWD Lead - Anjali Menon','9000000201','Kochi','Ernakulam','new','Call to discuss household digester options.'),
                ('Demo KWD Lead - Joseph Mathew','9000000202','Thrissur','Thrissur','contacted','Shared product details; requested a site presentation.'),
                ('Demo KWD Lead - Green Valley Residents Association','9000000203','Alappuzha','Alappuzha','negotiating','Reviewing a community installation proposal.'),
            ]
            kwd_leads=[]
            for index,(name,phone,place,district_name,status,note) in enumerate(kwd_samples):
                franchise=digester_franchises[index % len(digester_franchises)]
                lead,_=KWDLead.objects.get_or_create(franchise=franchise,name=name,defaults={
                    'phone':phone,'place':place,'district':district_name,'head_office_note':'Synthetic KWD demo lead.',
                    'status':status,'discussion':note,'follow_up_date':today+timedelta(days=index+1),
                    'assigned_by':manager,'assigned_at':now-timedelta(days=index),
                })
                kwd_leads.append(lead)
                KWDLeadEvent.objects.get_or_create(lead=lead,actor=manager,action='seeded_demo',defaults={
                    'new_status':lead.status,
                    'note':'Synthetic KWD demo activity.','follow_up_date':lead.follow_up_date,
                })
            for index,franchise in enumerate(digester_franchises):
                FranchiseTarget.objects.get_or_create(franchise=franchise,month=today.replace(day=1),defaults={
                    'target_value':Decimal('250000.00'),'target_units':10,'set_by':manager,
                    'note':'Synthetic monthly KWD demo target.',
                })
                product=Product.objects.get(name='Digester',category__kind='digester')
                balance,_=KWDStockBalance.objects.get_or_create(franchise=franchise,product=product,defaults={
                    'quantity':Decimal('8.000') if index==0 else Decimal('5.000'),
                    'low_stock_threshold':Decimal('2.000'),
                })
                if not KWDStockMovement.objects.filter(franchise=franchise,product=product).exists():
                    KWDStockMovement.objects.create(franchise=franchise,product=product,actor=digester_user,
                        kind='receipt',quantity=balance.quantity,reason='Synthetic opening KWD demo stock.')
            presentation,_=PresentationAppointment.objects.get_or_create(franchise=digester_franchises[0],
                lead=kwd_leads[1],scheduled_at=now+timedelta(days=2),defaults={
                    'venue':'office','person_name':'Joseph Mathew','district':'Thrissur',
                    'address':'Demo Community Hall, Thrissur','contact_person':'Joseph Mathew',
                    'contact_phone':'9000000202','people_expected':'Residents association committee',
                    'status':'upcoming','created_by':digester_user,
                })
            presentation.products.add(product)
        self.stdout.write('Synthetic fixtures created. Digester HQ demo account: '+manager.email+'; assigned LSGDs: 2; visit reviews: 2; opportunities: 2; TA/DA demo policy configured; KWD demo leads: 3; targets and stock: 2 franchises; presentations: 1.')
