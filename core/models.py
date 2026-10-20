import uuid
from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

class Role(models.TextChoices):
    SUPER = 'super_admin', 'Super Admin'
    ADMIN = 'admin', 'Admin'
    APP_MANAGER = 'app_manager', 'App Manager'
    DIGESTER_MANAGER = 'digester_hq_manager', 'Digester HQ Manager'
    ZONAL_MANAGER = 'zonal_manager', 'Zonal Manager'
    TSO = 'tso', 'Territory Sales Officer'
    HQ = 'hq_manager', 'HQ Manager'
    APP = 'app_franchise_manager', 'App Franchise Manager'
    DRIVER = 'delivery_staff', 'Delivery Staff'
    DIGESTER = 'digester_franchise_admin', 'Legacy Digester Franchise Admin'
    STAFF = 'digester_staff', 'Legacy Digester Staff'
    WAREHOUSE = 'warehouse_manager', 'Warehouse Manager'
    COLLECTOR = 'collector', 'Collector'

class Record(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    version = models.PositiveIntegerField(default=1)
    class Meta:
        abstract = True
        ordering = ['-created_at']

class RoleDefinition(Record):
    name = models.CharField(max_length=80, unique=True)
    base_role = models.CharField(max_length=30, choices=Role.choices)
    legacy_base_role = models.CharField(max_length=30, blank=True)
    permissions = models.JSONField(default=list)
    created_by = models.ForeignKey('User', null=True, blank=True, on_delete=models.SET_NULL, related_name='created_role_definitions')

class Franchise(Record):
    name = models.CharField(max_length=160)
    code = models.CharField(max_length=12, unique=True)
    kind = models.CharField(max_length=12, choices=[('scrap','Scrap'),('digester','Digester'),('hq','Head Office')])
    status = models.CharField(max_length=12, choices=[('active','Active'),('pending','Pending'),('suspended','Suspended')], default='active')
    phone = models.CharField(max_length=24, blank=True)
    email = models.EmailField(blank=True)
    address = models.TextField(blank=True)
    district = models.CharField(max_length=80, blank=True)
    state = models.CharField(max_length=80, blank=True)
    gstin = models.CharField(max_length=15, blank=True)
    owner_name = models.CharField(max_length=160, blank=True)
    share_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    class Meta(Record.Meta):
        constraints = [models.UniqueConstraint(fields=['kind'],condition=Q(kind='hq'),name='single_head_office'),models.CheckConstraint(condition=Q(share_percent__gte=0,share_percent__lte=100),name='share_percent_range')]
    def __str__(self): return self.name

class Warehouse(Record):
    name = models.CharField(max_length=160)
    franchise = models.ForeignKey(Franchise,on_delete=models.PROTECT)

class User(AbstractUser):
    email = models.EmailField(unique=True)
    role = models.CharField(max_length=30,choices=Role.choices)
    custom_role = models.ForeignKey(RoleDefinition,null=True,blank=True,on_delete=models.SET_NULL,related_name='users')
    franchise = models.ForeignKey(Franchise,null=True,blank=True,on_delete=models.PROTECT)
    legacy_franchise = models.ForeignKey(Franchise,null=True,blank=True,on_delete=models.PROTECT,related_name='legacy_digester_accounts')
    legacy_digester_role = models.CharField(max_length=32,blank=True)
    manager = models.ForeignKey('self',null=True,blank=True,on_delete=models.PROTECT,related_name='reporting_staff')
    warehouse = models.ForeignKey(Warehouse,null=True,blank=True,on_delete=models.PROTECT)
    permission_version = models.PositiveIntegerField(default=1)
    vehicle = models.CharField(max_length=80,blank=True)
    area = models.CharField(max_length=120,blank=True)
    mfa_secret = models.TextField(blank=True)
    mfa_enabled = models.BooleanField(default=False)
    mfa_last_step = models.BigIntegerField(default=-1)
    recovery_hashes = models.JSONField(default=list,blank=True)
    class Meta:
        constraints = [models.CheckConstraint(condition=(Q(role__in=['super_admin','admin','hq_manager','app_manager','digester_hq_manager','zonal_manager','tso'],franchise__isnull=True,warehouse__isnull=True)|Q(role__in=['app_franchise_manager','delivery_staff','digester_franchise_admin','digester_staff','collector'],franchise__isnull=False,warehouse__isnull=True)|Q(role='warehouse_manager',franchise__isnull=False,warehouse__isnull=False)),name='role_scope_required')]
    def clean(self):
        super().clean()
        if self.custom_role_id and self.custom_role.base_role != self.role:
            raise ValidationError('The assigned custom role must use the account role scope.')
        if self.role in [Role.SUPER,Role.ADMIN,Role.HQ,Role.APP_MANAGER,Role.DIGESTER_MANAGER,Role.ZONAL_MANAGER,Role.TSO]:
            if self.franchise_id or self.warehouse_id: raise ValidationError('Network accounts must not have a fixed franchise.')
            if self.role == Role.TSO and (not self.manager_id or self.manager.role != Role.ZONAL_MANAGER): raise ValidationError('A TSO must report to a Zonal Manager.')
            if self.role != Role.TSO and self.manager_id: raise ValidationError('Only TSOs may have a reporting manager.')
        else:
            if not self.franchise_id: raise ValidationError('A franchise is required.')
            expected = 'digester' if self.role in [Role.DIGESTER,Role.STAFF] else 'scrap'
            if self.franchise.kind != expected: raise ValidationError('Role and franchise type are incompatible.')
            if self.role == Role.WAREHOUSE and (not self.warehouse_id or self.warehouse.franchise_id != self.franchise_id): raise ValidationError('A warehouse in the assigned franchise is required.')

class District(Record):
    name = models.CharField(max_length=80, unique=True)

    def __str__(self): return self.name

class LSGD(Record):
    TYPES = [('grama_panchayat','Grama Panchayat'),('block_panchayat','Block Panchayat'),('district_panchayat','District Panchayat'),('municipality','Municipality'),('corporation','Corporation')]
    name = models.CharField(max_length=180)
    kind = models.CharField(max_length=24, choices=TYPES)
    district = models.ForeignKey(District,on_delete=models.PROTECT,related_name='lsgds')
    latitude = models.DecimalField(max_digits=10,decimal_places=7,null=True,blank=True)
    longitude = models.DecimalField(max_digits=10,decimal_places=7,null=True,blank=True)
    assigned_manager = models.ForeignKey(settings.AUTH_USER_MODEL,null=True,blank=True,on_delete=models.PROTECT,related_name='managed_lsgds')
    assigned_tso = models.ForeignKey(settings.AUTH_USER_MODEL,null=True,blank=True,on_delete=models.PROTECT,related_name='assigned_lsgds')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL,null=True,blank=True,on_delete=models.PROTECT,related_name='created_lsgds')
    manager_assigned_by = models.ForeignKey(settings.AUTH_USER_MODEL,null=True,blank=True,on_delete=models.PROTECT,related_name='assigned_lsgd_managers')
    tso_assigned_by = models.ForeignKey(settings.AUTH_USER_MODEL,null=True,blank=True,on_delete=models.PROTECT,related_name='assigned_lsgd_tso')
    active = models.BooleanField(default=True)
    address = models.TextField(blank=True)
    class Meta(Record.Meta):
        constraints = [models.UniqueConstraint(fields=['name','district'],name='lsgd_name_per_district')]

class LSGDContact(Record):
    lsgd = models.ForeignKey(LSGD,on_delete=models.PROTECT,related_name='contacts')
    name = models.CharField(max_length=160)
    designation = models.CharField(max_length=120,blank=True)
    phone = models.CharField(max_length=24,blank=True)
    email = models.EmailField(blank=True)
    decision_maker = models.BooleanField(default=False)
    verified_by = models.ForeignKey(settings.AUTH_USER_MODEL,null=True,blank=True,on_delete=models.PROTECT,related_name='verified_lsgd_contacts')

class LSGDOpportunity(Record):
    STAGES = [('new','New'),('first_meeting','First meeting'),('demo_done','Demo done'),('proposal_submitted','Proposal submitted'),('in_annual_plan','In annual plan'),('dpc_approved','DPC approved'),('work_order','Work order'),('supplied','Supplied'),('payment_received','Payment received'),('lost','Lost')]
    lsgd = models.ForeignKey(LSGD,on_delete=models.PROTECT,related_name='opportunities')
    financial_year = models.CharField(max_length=9)
    stage = models.CharField(max_length=24,choices=STAGES,default='new')
    expected_units = models.PositiveIntegerField(default=0)
    expected_value = models.DecimalField(max_digits=14,decimal_places=2,default=0)
    chance_percent = models.PositiveSmallIntegerField(default=10)
    expected_month = models.DateField(null=True,blank=True)
    lost_reason = models.CharField(max_length=500,blank=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)
    class Meta(Record.Meta):
        constraints = [models.UniqueConstraint(fields=['lsgd','financial_year'],name='one_lsgd_opportunity_per_fy'),models.CheckConstraint(condition=Q(chance_percent__gte=0,chance_percent__lte=100),name='opportunity_chance_range')]

class VisitPlan(Record):
    lsgd = models.ForeignKey(LSGD,on_delete=models.PROTECT,related_name='visit_plans')
    assigned_to = models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name='visit_plans')
    planned_date = models.DateField()
    purpose = models.CharField(max_length=500,blank=True)
    state = models.CharField(max_length=16,choices=[('planned','Planned'),('done','Done'),('cancelled','Cancelled')],default='planned')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name='created_visit_plans')

class FieldVisit(Record):
    lsgd = models.ForeignKey(LSGD,on_delete=models.PROTECT,related_name='visits')
    owner = models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name='field_visits')
    plan = models.ForeignKey(VisitPlan,null=True,blank=True,on_delete=models.PROTECT,related_name='visits')
    visited_at = models.DateTimeField()
    latitude = models.DecimalField(max_digits=10,decimal_places=7,null=True,blank=True)
    longitude = models.DecimalField(max_digits=10,decimal_places=7,null=True,blank=True)
    gps_accuracy = models.DecimalField(max_digits=8,decimal_places=2,null=True,blank=True)
    distance_m = models.PositiveIntegerField(null=True,blank=True)
    photo_path = models.CharField(max_length=500,blank=True)
    photo_captured_at = models.DateTimeField(null=True,blank=True)
    discussion = models.TextField(blank=True)
    outcome = models.TextField(blank=True)
    interest = models.PositiveSmallIntegerField(null=True,blank=True)
    flags = models.JSONField(default=list)
    details_at = models.DateTimeField(null=True,blank=True)
    zm_status = models.CharField(max_length=16,choices=[('pending','Pending'),('verified','Verified'),('flagged','Flagged'),('rejected','Rejected')],default='pending')
    zm_reviewer = models.ForeignKey(settings.AUTH_USER_MODEL,null=True,blank=True,on_delete=models.PROTECT,related_name='reviewed_field_visits')
    zm_remark = models.TextField(blank=True)
    ho_remark = models.TextField(blank=True)
    ho_signed_at = models.DateTimeField(null=True,blank=True)
    ho_reviewer = models.ForeignKey(settings.AUTH_USER_MODEL,null=True,blank=True,on_delete=models.PROTECT,related_name='signed_field_visits')

class VisitContact(Record):
    visit = models.ForeignKey(FieldVisit,on_delete=models.PROTECT,related_name='people_met')
    contact = models.ForeignKey(LSGDContact,on_delete=models.PROTECT)

class FieldFollowUp(Record):
    MODES = [('visit','Visit'),('call','Call'),('whatsapp','WhatsApp'),('email','Email')]
    lsgd = models.ForeignKey(LSGD,on_delete=models.PROTECT,related_name='followups')
    visit = models.ForeignKey(FieldVisit,null=True,blank=True,on_delete=models.PROTECT,related_name='followups')
    owner = models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name='field_followups')
    contact = models.ForeignKey(LSGDContact,null=True,blank=True,on_delete=models.PROTECT)
    due_date = models.DateField()
    mode = models.CharField(max_length=12,choices=MODES)
    note = models.TextField()
    completed_at = models.DateTimeField(null=True,blank=True)
    completion_note = models.TextField(blank=True)

class FieldDay(Record):
    MODES = [('two_wheeler','Two wheeler'),('car','Car'),('public_transport','Public transport')]
    STATUSES = [('open','Open'),('submitted','Submitted'),('zm_approved','Verified by ZM'),('ho_approved','Approved by HQ'),('rejected','Rejected'),('paid','Paid')]
    owner = models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name='expense_days')
    day = models.DateField()
    status = models.CharField(max_length=16,choices=STATUSES,default='open')
    travel_mode = models.CharField(max_length=20,choices=MODES)
    vehicle_number = models.CharField(max_length=40,blank=True)
    start_odometer = models.PositiveIntegerField(null=True,blank=True)
    end_odometer = models.PositiveIntegerField(null=True,blank=True)
    start_photo_path = models.CharField(max_length=500,blank=True)
    end_photo_path = models.CharField(max_length=500,blank=True)
    start_latitude = models.DecimalField(max_digits=10,decimal_places=7,null=True,blank=True)
    start_longitude = models.DecimalField(max_digits=10,decimal_places=7,null=True,blank=True)
    end_latitude = models.DecimalField(max_digits=10,decimal_places=7,null=True,blank=True)
    end_longitude = models.DecimalField(max_digits=10,decimal_places=7,null=True,blank=True)
    started_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True,blank=True)
    night_halt = models.BooleanField(default=False)
    halt_place = models.CharField(max_length=180,blank=True)
    note = models.TextField(blank=True)
    da_requested = models.DecimalField(max_digits=12,decimal_places=2,default=0)
    ta_km = models.DecimalField(max_digits=10,decimal_places=2,default=0)
    gps_km = models.DecimalField(max_digits=10,decimal_places=2,null=True,blank=True)
    ta_amount = models.DecimalField(max_digits=12,decimal_places=2,default=0)
    total_claimed = models.DecimalField(max_digits=12,decimal_places=2,default=0)
    approved_amount = models.DecimalField(max_digits=12,decimal_places=2,null=True,blank=True)
    flags = models.JSONField(default=list)
    submitted_at = models.DateTimeField(null=True,blank=True)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL,null=True,blank=True,on_delete=models.PROTECT,related_name='reviewed_expense_days')
    review_note = models.TextField(blank=True)
    ho_approved_by = models.ForeignKey(settings.AUTH_USER_MODEL,null=True,blank=True,on_delete=models.PROTECT,related_name='approved_expense_days')
    ho_note = models.TextField(blank=True)
    paid_at = models.DateTimeField(null=True,blank=True)
    class Meta(Record.Meta):
        constraints = [models.UniqueConstraint(fields=['owner','day'],name='one_field_day_per_owner_date')]

class ExpenseItem(Record):
    CATEGORIES = [('da_bill','DA bill'),('bus_train','Bus or train'),('auto_taxi','Auto or taxi'),('parking_toll','Parking or toll'),('lodging','Lodging'),('phone_internet','Phone or internet'),('printing_courier','Printing or courier'),('other','Other')]
    field_day = models.ForeignKey(FieldDay,on_delete=models.PROTECT,related_name='items')
    category = models.CharField(max_length=24,choices=CATEGORIES)
    amount = models.DecimalField(max_digits=12,decimal_places=2,default=0)
    description = models.CharField(max_length=500,blank=True)
    bill_number = models.CharField(max_length=100,blank=True)
    photo_path = models.CharField(max_length=500)
    photo_digest = models.CharField(max_length=64)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)

class ExpensePayment(Record):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name='expense_payments')
    month = models.CharField(max_length=7)
    amount = models.DecimalField(max_digits=14,decimal_places=2)
    reference = models.CharField(max_length=120)
    note = models.TextField(blank=True)
    paid_by = models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name='issued_expense_payments')
    paid_at = models.DateTimeField()
    class Meta(Record.Meta):
        constraints = [models.UniqueConstraint(fields=['owner','month'],name='one_expense_payment_per_owner_month')]

class ClaimEvent(Record):
    field_day = models.ForeignKey(FieldDay,on_delete=models.PROTECT,related_name='events')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)
    action = models.CharField(max_length=32)
    old_status = models.CharField(max_length=16,blank=True)
    new_status = models.CharField(max_length=16,blank=True)
    amount = models.DecimalField(max_digits=12,decimal_places=2,null=True,blank=True)
    note = models.TextField(blank=True)

class ExpensePolicy(Record):
    two_wheeler_rate = models.DecimalField(max_digits=8,decimal_places=2,default=0)
    car_rate = models.DecimalField(max_digits=8,decimal_places=2,default=0)
    da_local_max = models.DecimalField(max_digits=10,decimal_places=2,default=0)
    da_night_max = models.DecimalField(max_digits=10,decimal_places=2,default=0)
    da_bill_threshold = models.DecimalField(max_digits=10,decimal_places=2,default=0)
    lodging_cap = models.DecimalField(max_digits=10,decimal_places=2,default=0)
    auto_taxi_cap = models.DecimalField(max_digits=10,decimal_places=2,default=0)
    daily_km_limit = models.PositiveIntegerField(default=0)
    gps_allowance_percent = models.PositiveSmallIntegerField(default=0)
    road_factor = models.DecimalField(max_digits=5,decimal_places=2,default='1.35')
    submit_days = models.PositiveSmallIntegerField(default=0)

class Customer(Record):
    franchise = models.ForeignKey(Franchise,on_delete=models.PROTECT)
    name = models.CharField(max_length=160)
    phone = models.CharField(max_length=24)
    address = models.TextField(blank=True)
    area = models.CharField(max_length=120,blank=True)
    state = models.CharField(max_length=80,blank=True)
    gstin = models.CharField(max_length=15,blank=True)
    active = models.BooleanField(default=True)
    class Meta(Record.Meta):
        constraints = [models.UniqueConstraint(fields=['franchise','phone'],name='customer_phone_per_book')]

class Category(Record):
    name = models.CharField(max_length=100)
    kind = models.CharField(max_length=12,choices=[('scrap','Scrap'),('digester','Digester')])
    default_billing_mode = models.CharField(max_length=16,choices=[('franchise','Franchise Billing'),('company','Company Billing')],default='franchise')
    active = models.BooleanField(default=True)

class Product(Record):
    name = models.CharField(max_length=160)
    category = models.ForeignKey(Category,on_delete=models.PROTECT)
    unit = models.CharField(max_length=12,choices=[('kg','KG'),('piece','Piece'),('litre','Litre'),('pack','Pack'),('set','Set')])
    active = models.BooleanField(default=True)
    tracks_stock = models.BooleanField(default=True)
    hsn = models.CharField(max_length=16,blank=True)
    tax_percent = models.DecimalField(max_digits=5,decimal_places=2,default=0)
    franchises = models.ManyToManyField(Franchise,related_name='products',blank=True)

class Rate(Record):
    product = models.ForeignKey(Product,on_delete=models.PROTECT)
    franchise = models.ForeignKey(Franchise,null=True,blank=True,on_delete=models.PROTECT)
    kind = models.CharField(max_length=16,choices=[('collection','Collection'),('b2b','B2B'),('sale','Sale')])
    amount = models.DecimalField(max_digits=14,decimal_places=2)
    discount_percent = models.DecimalField(max_digits=5,decimal_places=2,default=0)
    actor = models.ForeignKey(User,on_delete=models.PROTECT)
    reason = models.CharField(max_length=500,blank=True)
    class Meta(Record.Meta):
        constraints = [models.CheckConstraint(condition=Q(amount__gt=0),name='rate_positive'),models.CheckConstraint(condition=Q(discount_percent__gte=0,discount_percent__lte=100),name='rate_discount_range')]

class Policy(Record):
    key = models.CharField(max_length=80,unique=True)
    config = models.JSONField(default=dict)
    decision_reference = models.CharField(max_length=500)
    test_only = models.BooleanField(default=False)

class CompanySetting(Record):
    section = models.CharField(max_length=30,unique=True)
    values = models.JSONField(default=dict)

class Load(Record):
    franchise = models.ForeignKey(Franchise,on_delete=models.PROTECT)
    driver = models.ForeignKey(User,on_delete=models.PROTECT)
    warehouse = models.ForeignKey(Warehouse,on_delete=models.PROTECT)
    state = models.CharField(max_length=20,default='on_route')
    vehicle_full_at = models.DateTimeField(null=True,blank=True)
    returned_at = models.DateTimeField(null=True,blank=True)
    unloaded_at = models.DateTimeField(null=True,blank=True)
    eligibility_reference = models.CharField(max_length=500,blank=True)
    reconciliation_state = models.CharField(max_length=24,default='pending')
    reweigh_version = models.PositiveIntegerField(default=1)
    resolution_action = models.CharField(max_length=24,blank=True)
    resolution_note = models.CharField(max_length=1000,blank=True)
    resolved_by = models.ForeignKey('User',null=True,blank=True,on_delete=models.PROTECT,related_name='resolved_loads')
    resolved_at = models.DateTimeField(null=True,blank=True)

class Lead(Record):
    franchise = models.ForeignKey(Franchise,on_delete=models.PROTECT)
    customer = models.ForeignKey(Customer,on_delete=models.PROTECT)
    franchise_manager = models.ForeignKey(User,null=True,blank=True,on_delete=models.PROTECT,related_name='managed_leads')
    driver = models.ForeignKey(User,null=True,blank=True,on_delete=models.PROTECT)
    load = models.ForeignKey(Load,null=True,blank=True,on_delete=models.PROTECT)
    pickup_date = models.DateField()
    pickup_time = models.TimeField()
    items = models.CharField(max_length=1000,blank=True)
    upi_id = models.CharField(max_length=160,blank=True)
    latitude = models.DecimalField(max_digits=10,decimal_places=7,null=True,blank=True)
    longitude = models.DecimalField(max_digits=10,decimal_places=7,null=True,blank=True)
    state = models.CharField(max_length=24,default='unassigned')
    failure_reason = models.CharField(max_length=500,blank=True)
    started_at = models.DateTimeField(null=True,blank=True)
    sent_at = models.DateTimeField(null=True,blank=True)
    agreed_at = models.DateTimeField(null=True,blank=True)
    closed_at = models.DateTimeField(null=True,blank=True)

class Bill(Record):
    franchise = models.ForeignKey(Franchise,on_delete=models.PROTECT)
    creator = models.ForeignKey(User,on_delete=models.PROTECT)
    customer = models.ForeignKey(Customer,on_delete=models.PROTECT)
    lead = models.OneToOneField(Lead,null=True,blank=True,on_delete=models.PROTECT,related_name='bill')
    kind = models.CharField(max_length=20,choices=[('collection','Collection'),('sale','Sale'),('b2b','B2B')])
    transaction_type = models.CharField(max_length=3,choices=[('B2B','B2B'),('B2C','B2C'),('B2G','B2G')],default='B2C')
    mode = models.CharField(max_length=8,choices=[('purchase','Purchase'),('sales','Sales')],default='purchase')
    billing_mode = models.CharField(max_length=16,choices=[('franchise','Franchise Billing'),('company','Company Billing')],default='franchise')
    state = models.CharField(max_length=24,default='draft')
    number = models.CharField(max_length=80,unique=True,null=True,blank=True)
    lines = models.JSONField(default=list)
    snapshot = models.JSONField(default=dict)
    subtotal = models.DecimalField(max_digits=16,decimal_places=2,default=0)
    tax = models.DecimalField(max_digits=16,decimal_places=2,default=0)
    total = models.DecimalField(max_digits=16,decimal_places=2,default=0)
    settled = models.DecimalField(max_digits=16,decimal_places=2,default=0)
    share_percent = models.DecimalField(max_digits=5,decimal_places=2,default=0)
    notes = models.CharField(max_length=1000,blank=True)
    issued_at = models.DateTimeField(null=True,blank=True)
    class Meta(Record.Meta):
        constraints = [models.CheckConstraint(condition=Q(total__gte=0,settled__gte=0,settled__lte=models.F('total')),name='bill_settlement_bounds')]

class Quotation(Record):
    franchise = models.ForeignKey(Franchise,on_delete=models.PROTECT)
    creator = models.ForeignKey(User,on_delete=models.PROTECT)
    customer = models.ForeignKey(Customer,on_delete=models.PROTECT)
    kind = models.CharField(max_length=20,choices=[('collection','Collection'),('sale','Sale'),('b2b','B2B')])
    billing_mode = models.CharField(max_length=16,choices=[('franchise','Franchise Billing'),('company','Company Billing')],default='franchise')
    number = models.CharField(max_length=32,unique=True)
    state = models.CharField(max_length=16,choices=[('draft','Draft'),('sent','Sent'),('accepted','Accepted'),('rejected','Rejected'),('converted','Converted')],default='draft')
    lines = models.JSONField(default=list)
    subtotal = models.DecimalField(max_digits=16,decimal_places=2,default=0)
    tax = models.DecimalField(max_digits=16,decimal_places=2,default=0)
    total = models.DecimalField(max_digits=16,decimal_places=2,default=0)
    notes = models.CharField(max_length=1000,blank=True)
    sent_at = models.DateTimeField(null=True,blank=True)
    accepted_at = models.DateTimeField(null=True,blank=True)
    rejected_at = models.DateTimeField(null=True,blank=True)
    converted_at = models.DateTimeField(null=True,blank=True)
    converted_bill = models.OneToOneField(Bill,null=True,blank=True,on_delete=models.PROTECT,related_name='source_quotation')

class InvoiceSequence(models.Model):
    key = models.CharField(max_length=80,primary_key=True)
    next_number = models.PositiveBigIntegerField(default=1)

class Payment(Record):
    bill = models.ForeignKey(Bill,on_delete=models.PROTECT,related_name='payments')
    actor = models.ForeignKey(User,on_delete=models.PROTECT)
    amount = models.DecimalField(max_digits=16,decimal_places=2)
    method = models.CharField(max_length=24)
    reference = models.CharField(max_length=160,blank=True)
    paid_at = models.DateTimeField()
    class Meta(Record.Meta):
        constraints = [models.CheckConstraint(condition=Q(amount__gt=0),name='payment_positive')]

class EarningsEntry(Record):
    franchise = models.ForeignKey(Franchise,on_delete=models.PROTECT)
    bill = models.OneToOneField(Bill,null=True,blank=True,on_delete=models.PROTECT)
    actor = models.ForeignKey(User,on_delete=models.PROTECT)
    amount = models.DecimalField(max_digits=16,decimal_places=2)
    kind = models.CharField(max_length=20)
    reference = models.CharField(max_length=160,blank=True)

class DriverAccount(models.Model):
    driver = models.OneToOneField(User,primary_key=True,on_delete=models.PROTECT)
    balance = models.DecimalField(max_digits=16,decimal_places=2,default=0)
    version = models.PositiveIntegerField(default=1)

class Handover(Record):
    driver = models.ForeignKey(User,on_delete=models.PROTECT)
    franchise = models.ForeignKey(Franchise,on_delete=models.PROTECT)
    counter = models.ForeignKey(User,on_delete=models.PROTECT,related_name='counts')
    expected = models.DecimalField(max_digits=16,decimal_places=2)
    received = models.DecimalField(max_digits=16,decimal_places=2)
    difference = models.DecimalField(max_digits=16,decimal_places=2)
    note = models.CharField(max_length=1000,blank=True)
    state = models.CharField(max_length=30,default='awaiting_supervisor')
    cutoff = models.DateTimeField()
    reviewed_by = models.ForeignKey(User,null=True,blank=True,on_delete=models.PROTECT,related_name='reviewed_handovers')
    reviewed_at = models.DateTimeField(null=True,blank=True)
    review_note = models.CharField(max_length=1000,blank=True)

class HandoverRoute(models.Model):
    handover = models.ForeignKey(Handover,on_delete=models.PROTECT,related_name='routes')
    load = models.OneToOneField(Load,on_delete=models.PROTECT,related_name='counted_in')

class CashMovement(Record):
    driver = models.ForeignKey(User,on_delete=models.PROTECT,related_name='cash_movements')
    franchise = models.ForeignKey(Franchise,on_delete=models.PROTECT)
    actor = models.ForeignKey(User,on_delete=models.PROTECT)
    amount = models.DecimalField(max_digits=16,decimal_places=2)
    kind = models.CharField(max_length=20)
    payment = models.OneToOneField(Payment,null=True,blank=True,on_delete=models.PROTECT)
    handover = models.OneToOneField(Handover,null=True,blank=True,on_delete=models.PROTECT)

class Receipt(Record):
    lead = models.OneToOneField(Lead,on_delete=models.PROTECT,related_name='receipt')
    warehouse = models.ForeignKey(Warehouse,on_delete=models.PROTECT)
    actor = models.ForeignKey(User,on_delete=models.PROTECT)
    lines = models.JSONField(default=list)
    note = models.CharField(max_length=1000,blank=True)
    explanation = models.CharField(max_length=1000,blank=True)
    submitted_at = models.DateTimeField(null=True,blank=True)
    flagged = models.BooleanField(default=False)
    revision = models.PositiveIntegerField(default=1)

class ReceiptRevision(Record):
    receipt = models.ForeignKey(Receipt,on_delete=models.PROTECT,related_name='revisions')
    actor = models.ForeignKey(User,on_delete=models.PROTECT)
    revision = models.PositiveIntegerField()
    lines = models.JSONField(default=list)
    note = models.CharField(max_length=1000,blank=True)
    submitted_at = models.DateTimeField(null=True,blank=True)
    flagged = models.BooleanField(default=False)
    superseded_at = models.DateTimeField(null=True,blank=True)

class StockBalance(models.Model):
    warehouse = models.ForeignKey(Warehouse,on_delete=models.PROTECT)
    product = models.ForeignKey(Product,on_delete=models.PROTECT)
    quantity = models.DecimalField(max_digits=18,decimal_places=3,default=0)
    version = models.PositiveIntegerField(default=1)
    class Meta:
        constraints = [models.UniqueConstraint(fields=['warehouse','product'],name='stock_balance_unique'),models.CheckConstraint(condition=Q(quantity__gte=0),name='stock_nonnegative')]

class Requirement(Record):
    franchise = models.ForeignKey(Franchise,on_delete=models.PROTECT)
    product = models.ForeignKey(Product,on_delete=models.PROTECT)
    actor = models.ForeignKey(User,on_delete=models.PROTECT)
    quantity = models.DecimalField(max_digits=18,decimal_places=3)
    released = models.DecimalField(max_digits=18,decimal_places=3,default=0)
    due_date = models.DateField()
    note = models.CharField(max_length=1000,blank=True)
    state = models.CharField(max_length=20,default='active')
    class Meta(Record.Meta):
        constraints = [models.CheckConstraint(condition=Q(quantity__gt=0,released__gte=0,released__lte=models.F('quantity')),name='requirement_quantity_bounds')]

class StockMovement(Record):
    warehouse = models.ForeignKey(Warehouse,on_delete=models.PROTECT)
    product = models.ForeignKey(Product,on_delete=models.PROTECT)
    actor = models.ForeignKey(User,on_delete=models.PROTECT)
    quantity = models.DecimalField(max_digits=18,decimal_places=3)
    kind = models.CharField(max_length=20)
    receipt = models.ForeignKey(Receipt,null=True,blank=True,on_delete=models.PROTECT)
    receipt_revision = models.ForeignKey(ReceiptRevision,null=True,blank=True,on_delete=models.PROTECT)
    requirement = models.ForeignKey(Requirement,null=True,blank=True,on_delete=models.PROTECT)
    class Meta(Record.Meta):
        constraints = [models.UniqueConstraint(fields=['receipt','product'],condition=Q(receipt__isnull=False),name='once_per_receipt_product'),models.UniqueConstraint(fields=['receipt_revision','product'],condition=Q(receipt_revision__isnull=False),name='once_per_reweigh_product')]

class AuditEvent(Record):
    actor = models.ForeignKey(User,null=True,on_delete=models.PROTECT)
    franchise = models.ForeignKey(Franchise,null=True,on_delete=models.PROTECT)
    action = models.CharField(max_length=100)
    resource = models.CharField(max_length=100)
    details = models.JSONField(default=dict)

class Outbox(Record):
    actor = models.ForeignKey(User,on_delete=models.PROTECT)
    franchise = models.ForeignKey(Franchise,null=True,on_delete=models.PROTECT)
    feature = models.CharField(max_length=40)
    resource_id = models.CharField(max_length=80)
    owner_id = models.IntegerField(null=True)
    warehouse_id = models.UUIDField(null=True)
    published_at = models.DateTimeField(null=True)

class CommandResult(Record):
    actor = models.ForeignKey(User,on_delete=models.PROTECT)
    key = models.CharField(max_length=100)
    action = models.CharField(max_length=100)
    digest = models.CharField(max_length=64)
    result = models.JSONField(default=dict)
    class Meta(Record.Meta):
        constraints = [models.UniqueConstraint(fields=['actor','key'],name='idempotency_actor_key')]

class AccessToken(Record):
    user = models.ForeignKey(User,on_delete=models.CASCADE)
    digest = models.CharField(max_length=64,unique=True)
    purpose = models.CharField(max_length=24)
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True)

class CustomerLink(Record):
    bill = models.ForeignKey(Bill,on_delete=models.PROTECT)
    digest = models.CharField(max_length=64,unique=True)
    expires_at = models.DateTimeField()
    revoked = models.BooleanField(default=False)

class Job(Record):
    actor = models.ForeignKey(User,on_delete=models.PROTECT)
    kind = models.CharField(max_length=30)
    payload = models.JSONField(default=dict)
    state = models.CharField(max_length=20,default='pending')
    attempts = models.PositiveIntegerField(default=0)
    error = models.CharField(max_length=300,blank=True)
    file_name = models.CharField(max_length=200,blank=True)
    expires_at = models.DateTimeField(null=True)

class ImportBatch(Record):
    actor = models.ForeignKey(User,on_delete=models.PROTECT)
    digest = models.CharField(max_length=64)
    rows = models.JSONField(default=list)
    committed = models.BooleanField(default=False)

class LoginAttempt(models.Model):
    key = models.CharField(max_length=64,primary_key=True)
    attempts = models.PositiveIntegerField(default=0)
    window_start = models.DateTimeField()

class DataRevision(models.Model):
    key = models.CharField(max_length=30,primary_key=True,default='global')
    value = models.PositiveBigIntegerField(default=1)
