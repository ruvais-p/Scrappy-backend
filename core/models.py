import uuid
from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

class Role(models.TextChoices):
    SUPER = 'super_admin', 'Super Admin'
    ADMIN = 'admin', 'Admin'
    HQ = 'hq_manager', 'HQ Manager'
    APP = 'app_franchise_manager', 'App Franchise Manager'
    DRIVER = 'delivery_staff', 'Delivery Staff'
    DIGESTER = 'digester_franchise_admin', 'Digester Franchise Admin'
    STAFF = 'digester_staff', 'Digester Staff'
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
    franchise = models.ForeignKey(Franchise,null=True,blank=True,on_delete=models.PROTECT)
    warehouse = models.ForeignKey(Warehouse,null=True,blank=True,on_delete=models.PROTECT)
    permission_version = models.PositiveIntegerField(default=1)
    vehicle = models.CharField(max_length=80,blank=True)
    area = models.CharField(max_length=120,blank=True)
    mfa_secret = models.TextField(blank=True)
    mfa_enabled = models.BooleanField(default=False)
    mfa_last_step = models.BigIntegerField(default=-1)
    recovery_hashes = models.JSONField(default=list,blank=True)
    class Meta:
        constraints = [models.CheckConstraint(condition=(Q(role__in=['super_admin','admin','hq_manager'],franchise__isnull=True,warehouse__isnull=True)|Q(role__in=['app_franchise_manager','delivery_staff','digester_franchise_admin','digester_staff','collector'],franchise__isnull=False,warehouse__isnull=True)|Q(role='warehouse_manager',franchise__isnull=False,warehouse__isnull=False)),name='role_scope_required')]
    def clean(self):
        super().clean()
        if self.role in [Role.SUPER,Role.ADMIN,Role.HQ]:
            if self.franchise_id or self.warehouse_id: raise ValidationError('Network accounts must not have a fixed franchise.')
        else:
            if not self.franchise_id: raise ValidationError('A franchise is required.')
            expected = 'digester' if self.role in [Role.DIGESTER,Role.STAFF] else 'scrap'
            if self.franchise.kind != expected: raise ValidationError('Role and franchise type are incompatible.')
            if self.role == Role.WAREHOUSE and (not self.warehouse_id or self.warehouse.franchise_id != self.franchise_id): raise ValidationError('A warehouse in the assigned franchise is required.')

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
    actor = models.ForeignKey(User,on_delete=models.PROTECT)
    reason = models.CharField(max_length=500,blank=True)
    class Meta(Record.Meta):
        constraints = [models.CheckConstraint(condition=Q(amount__gt=0),name='rate_positive')]

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
    returned_at = models.DateTimeField(null=True,blank=True)
    unloaded_at = models.DateTimeField(null=True,blank=True)
    eligibility_reference = models.CharField(max_length=500,blank=True)

class Lead(Record):
    franchise = models.ForeignKey(Franchise,on_delete=models.PROTECT)
    customer = models.ForeignKey(Customer,on_delete=models.PROTECT)
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
    submitted_at = models.DateTimeField(null=True,blank=True)
    flagged = models.BooleanField(default=False)

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
    requirement = models.ForeignKey(Requirement,null=True,blank=True,on_delete=models.PROTECT)
    class Meta(Record.Meta):
        constraints = [models.UniqueConstraint(fields=['receipt','product'],condition=Q(receipt__isnull=False),name='once_per_receipt_product')]

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
