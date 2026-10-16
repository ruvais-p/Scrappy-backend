from django.conf import settings
from django.db.models import Q
from rest_framework.exceptions import PermissionDenied, NotFound, APIException
from .models import Role, Policy, HandoverRoute
NETWORK = {Role.SUPER, Role.ADMIN, Role.HQ}
OWN = {Role.DRIVER,Role.STAFF,Role.COLLECTOR}
BILLING = {Role.SUPER,Role.ADMIN,Role.APP,Role.DRIVER,Role.DIGESTER,Role.STAFF}
GRANTS = {
 'dashboard':set(Role.values), 'franchises':{Role.SUPER,Role.ADMIN,Role.HQ,Role.APP,Role.DRIVER,Role.DIGESTER,Role.STAFF},
 'customers':BILLING, 'bills':BILLING, 'payments':BILLING, 'earnings':{Role.SUPER,Role.ADMIN,Role.DIGESTER},
 'products':{Role.SUPER,Role.ADMIN}, 'categories':{Role.SUPER,Role.ADMIN},
 'rates':{Role.SUPER,Role.ADMIN,Role.HQ,Role.APP,Role.DIGESTER}, 'audit':{Role.SUPER,Role.HQ}, 'settings':{Role.SUPER},
 'team':{Role.SUPER,Role.ADMIN,Role.HQ,Role.APP,Role.DIGESTER},
 'leads':{Role.SUPER,Role.ADMIN,Role.HQ,Role.APP,Role.DRIVER,Role.COLLECTOR,Role.WAREHOUSE},
 'pickups':{Role.SUPER,Role.ADMIN,Role.HQ,Role.APP,Role.DRIVER,Role.COLLECTOR},
 'loads':{Role.SUPER,Role.ADMIN,Role.HQ,Role.APP,Role.DRIVER,Role.COLLECTOR,Role.WAREHOUSE},
 'receipts':{Role.SUPER,Role.ADMIN,Role.HQ,Role.APP,Role.WAREHOUSE},
 'inventory':{Role.SUPER,Role.ADMIN,Role.HQ,Role.APP,Role.WAREHOUSE},
 'requirements':{Role.HQ,Role.WAREHOUSE},'cash':{Role.HQ,Role.WAREHOUSE,Role.APP,Role.DRIVER,Role.COLLECTOR},
 'imports':{Role.HQ},
 'franchise.create':{Role.SUPER,Role.HQ}, 'franchise.edit':{Role.SUPER,Role.HQ},'franchise.suspend':{Role.SUPER},
 'customer.create':BILLING, 'customer.edit':{Role.SUPER,Role.ADMIN,Role.APP,Role.DIGESTER},
 'product.write':{Role.SUPER},'product.assign':{Role.SUPER},'rate.write':{Role.SUPER,Role.HQ},
'lead.assign':{Role.HQ,Role.APP},'lead.schedule':{Role.HQ,Role.APP},'pickup.write':{Role.DRIVER,Role.COLLECTOR},
 'bill.write':BILLING, 'receipt.write':{Role.WAREHOUSE},'requirement.write':{Role.HQ},'release.write':{Role.WAREHOUSE},
 'float.write':{Role.HQ,Role.WAREHOUSE}, 'count.write':{Role.WAREHOUSE},'payout.write':{Role.SUPER,Role.ADMIN},
}
class PolicyUnavailable(APIException):
    status_code=409
    default_code='policy_unavailable'

def require(user, action):
    if not user.is_authenticated or not user.is_active or user.role not in GRANTS.get(action,set()):
        raise PermissionDenied('This action is not available for your role.')

def policy(key):
    p=Policy.objects.filter(key=key).first()
    if not p or not p.decision_reference or (p.test_only and settings.ENVIRONMENT not in ('test','development')):
        raise PolicyUnavailable(f'{key.replace("_"," ").capitalize()} policy must be recorded before this action is available.')
    return p.config

def scope(user, qs, feature):
    require(user,feature)
    if user.role in NETWORK: return qs
    model=qs.model.__name__
    paths={'Payment':'bill__franchise_id','StockBalance':'warehouse__franchise_id','StockMovement':'warehouse__franchise_id','Receipt':'warehouse__franchise_id','Rate':'franchise_id','Franchise':'id','Product':'franchises__id','Category':None,'DriverAccount':'driver__franchise_id','HandoverRoute':'handover__franchise_id'}
    field=paths.get(model,'franchise_id')
    if field:
        qs=qs.filter(Q(franchise_id=user.franchise_id)|Q(franchise__isnull=True)) if model=='Rate' else qs.filter(**{field:user.franchise_id})
    if model=='Rate': qs=qs.filter(product__franchises=user.franchise)
    if user.role==Role.WAREHOUSE:
        if model in ('StockBalance','StockMovement','Receipt','Load'): qs=qs.filter(warehouse_id=user.warehouse_id)
        if model=='Handover':
            local=HandoverRoute.objects.filter(load__warehouse_id=user.warehouse_id).values('handover_id')
            foreign=HandoverRoute.objects.exclude(load__warehouse_id=user.warehouse_id).values('handover_id')
            qs=qs.filter(pk__in=local).exclude(pk__in=foreign)
    if model=='ImportBatch' and user.role not in NETWORK:
        qs=qs.filter(actor=user)
    if user.role in OWN:
        if model=='Bill': qs=qs.filter(creator=user)
        elif model=='Payment': qs=qs.filter(bill__creator=user)
        elif model in ('Lead','Load','CashMovement','Handover','DriverAccount'): qs=qs.filter(driver=user)
        elif model=='EarningsEntry': return qs.none()
    return qs.distinct() if model in ('Rate','Product') else qs

def scoped_get(user,qs,feature,pk):
    try: return scope(user,qs,feature).get(pk=pk)
    except (qs.model.DoesNotExist,ValueError): raise NotFound()

def book_allowed(user,book):
    if user.role not in NETWORK and user.franchise_id!=book.id: raise NotFound()

def eligible_product(user,product,book):
    book_allowed(user,book)
    return product.active and product.category.active and product.franchises.filter(pk=book.pk).exists()
