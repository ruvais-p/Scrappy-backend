from django.conf import settings
from django.db.models import Q
from rest_framework.exceptions import PermissionDenied, NotFound, APIException
from .models import Role, Policy, HandoverRoute, Rate
NETWORK = {Role.SUPER, Role.ADMIN, Role.HQ}
SEGMENT_NETWORK = {Role.APP_MANAGER: 'scrap'}
OWN = {Role.DRIVER,Role.STAFF,Role.COLLECTOR}
# General Billing workspace is reserved for Admin and Super Admin. Assigned
# pickup billing remains a separate operational permission on leads/pickups.
BILLING = {Role.SUPER,Role.ADMIN}
GRANTS = {
 'dashboard':set(Role.values), 'franchises':{Role.SUPER,Role.ADMIN,Role.HQ,Role.APP,Role.DRIVER,Role.DIGESTER,Role.STAFF},
 'kwd_leads':{Role.DIGESTER_MANAGER,Role.DIGESTER}, 'kwd_sales':{Role.DIGESTER},
 'kwd_stock':{Role.DIGESTER}, 'kwd_targets':{Role.DIGESTER_MANAGER,Role.DIGESTER},
 'kwd_presentations':{Role.DIGESTER},
 'lsgds':{Role.DIGESTER_MANAGER,Role.ZONAL_MANAGER,Role.TSO},
 'opportunities':{Role.DIGESTER_MANAGER,Role.ZONAL_MANAGER,Role.TSO},
 'visit_plans':{Role.DIGESTER_MANAGER,Role.ZONAL_MANAGER,Role.TSO},
 'visits':{Role.DIGESTER_MANAGER,Role.ZONAL_MANAGER,Role.TSO},
 'followups':{Role.DIGESTER_MANAGER,Role.ZONAL_MANAGER,Role.TSO},
 'field_days':{Role.DIGESTER_MANAGER,Role.ZONAL_MANAGER,Role.TSO},
 'claims':{Role.DIGESTER_MANAGER,Role.ZONAL_MANAGER,Role.TSO},
 'expense_items':{Role.DIGESTER_MANAGER,Role.ZONAL_MANAGER,Role.TSO},
 'settlements':{Role.DIGESTER_MANAGER},
 'customers':BILLING|{Role.DIGESTER}, 'bills':BILLING|{Role.DIGESTER}, 'quotations':BILLING, 'payments':BILLING, 'earnings':BILLING,
 'products':{Role.SUPER,Role.ADMIN}, 'categories':{Role.SUPER,Role.ADMIN},
 'rates':{Role.SUPER,Role.ADMIN,Role.HQ,Role.APP,Role.DIGESTER}, 'audit':{Role.SUPER,Role.HQ}, 'settings':{Role.SUPER},
 'team':{Role.SUPER,Role.ADMIN,Role.HQ,Role.APP,Role.DIGESTER,Role.DIGESTER_MANAGER,Role.ZONAL_MANAGER},
 'warehouses':{Role.SUPER,Role.ADMIN,Role.HQ},
 'leads':{Role.SUPER,Role.ADMIN,Role.HQ,Role.APP,Role.DRIVER,Role.COLLECTOR,Role.WAREHOUSE},
 'pickups':{Role.SUPER,Role.ADMIN,Role.HQ,Role.APP,Role.DRIVER,Role.COLLECTOR},
 'loads':{Role.SUPER,Role.ADMIN,Role.HQ,Role.APP,Role.DRIVER,Role.COLLECTOR,Role.WAREHOUSE},
 'receipts':{Role.SUPER,Role.ADMIN,Role.HQ,Role.APP,Role.WAREHOUSE},
 'inventory':{Role.SUPER,Role.ADMIN,Role.HQ,Role.APP,Role.WAREHOUSE},
 'requirements':{Role.HQ,Role.WAREHOUSE,Role.APP},'cash':{Role.HQ,Role.WAREHOUSE,Role.APP,Role.DRIVER,Role.COLLECTOR},
 'imports':{Role.HQ},'inventory.adjust':{Role.SUPER,Role.ADMIN,Role.HQ},
 'role.write':{Role.SUPER,Role.ADMIN},
 'company.billing':BILLING,
 'company.billing.manage':{Role.SUPER,Role.ADMIN},
 'franchise.create':{Role.SUPER,Role.HQ}, 'franchise.edit':{Role.SUPER,Role.HQ},'franchise.suspend':{Role.SUPER},
 'customer.create':BILLING|{Role.DIGESTER}, 'customer.edit':BILLING,
'lsgd.create':{Role.DIGESTER_MANAGER},'lsgd.assign.manager':{Role.DIGESTER_MANAGER},'lsgd.assign.tso':{Role.ZONAL_MANAGER},
'opportunity.write':{Role.DIGESTER_MANAGER,Role.ZONAL_MANAGER,Role.TSO},'lsgd.contact.write':{Role.DIGESTER_MANAGER,Role.ZONAL_MANAGER,Role.TSO},
'visit.plan':{Role.DIGESTER_MANAGER,Role.ZONAL_MANAGER},'visit.write':{Role.ZONAL_MANAGER,Role.TSO},
'visit.review':{Role.DIGESTER_MANAGER,Role.ZONAL_MANAGER},'followup.write':{Role.DIGESTER_MANAGER,Role.ZONAL_MANAGER,Role.TSO},
'claim.write':{Role.ZONAL_MANAGER,Role.TSO},'claim.review':{Role.DIGESTER_MANAGER,Role.ZONAL_MANAGER},'claim.pay':{Role.DIGESTER_MANAGER},'expense.policy.write':{Role.DIGESTER_MANAGER},
'product.write':{Role.SUPER},'product.assign':{Role.SUPER},'rate.write':{Role.SUPER,Role.ADMIN,Role.HQ},
'lead.assign':{Role.HQ,Role.APP},'lead.schedule':{Role.HQ,Role.APP},'pickup.write':{Role.DRIVER,Role.COLLECTOR},
 'bill.write':BILLING|{Role.DIGESTER}, 'quotation.write':BILLING, 'receipt.write':{Role.WAREHOUSE,Role.HQ},'requirement.write':{Role.HQ},'release.write':{Role.APP,Role.HQ},
 'float.write':{Role.HQ,Role.WAREHOUSE}, 'count.write':{Role.HQ,Role.WAREHOUSE},'handover.review':{Role.APP,Role.HQ},'payout.write':{Role.SUPER,Role.ADMIN},
 'load.unload':{Role.WAREHOUSE,Role.HQ},'load.resolve':{Role.HQ},'load.vehicle_full':{Role.DRIVER,Role.COLLECTOR},'receipt.explain':{Role.WAREHOUSE},
}
def effective_permissions(user):
    role=user.role
    if role==Role.DIGESTER:
        allowed={'dashboard','kwd_leads','kwd_sales','kwd_stock','kwd_targets','kwd_presentations',
                 'bills','bill.write','customers','customer.create'}
        if getattr(user,'custom_role_id',None): return allowed.intersection(user.custom_role.permissions or [])
        return allowed
    if role in SEGMENT_NETWORK:
        allowed={key for key,roles in GRANTS.items() if role in roles or Role.ADMIN in roles}
        # App managers inherit network permissions except for the Admin-only
        # general Billing workspace; assigned pickup operations stay separate.
        if role==Role.APP_MANAGER:
            allowed.difference_update({'bills','quotations','payments','earnings','customers','bill.write',
                                       'quotation.write','customer.create','customer.edit','company.billing','payout.write'})
    else:
        allowed={key for key,roles in GRANTS.items() if role in roles}
    if getattr(user,'custom_role_id',None):
        return allowed.intersection(user.custom_role.permissions or [])
    return allowed

def role_permissions(role):
    if role==Role.DIGESTER:
        return {'dashboard','kwd_leads','kwd_sales','kwd_stock','kwd_targets','kwd_presentations',
                'bills','bill.write','customers','customer.create'}
    if role in SEGMENT_NETWORK:
        allowed={key for key,roles in GRANTS.items() if role in roles or Role.ADMIN in roles}
        if role==Role.APP_MANAGER:
            allowed.difference_update({'bills','quotations','payments','earnings','customers','bill.write',
                                       'quotation.write','customer.create','customer.edit','company.billing','payout.write'})
        return allowed
    return {key for key,roles in GRANTS.items() if role in roles}

def has_permission(user, action):
    return action in effective_permissions(user)

class PolicyUnavailable(APIException):
    status_code=409
    default_code='policy_unavailable'

def require(user, action):
    if not user.is_authenticated or not user.is_active or not has_permission(user,action):
        raise PermissionDenied('This action is not available for your role.')

def lsgd_scope(user, qs):
    require(user, 'lsgds')
    prefix='' if qs.model.__name__=='LSGD' else 'lsgd__'
    if user.role == Role.DIGESTER_MANAGER:
        return qs
    if user.role == Role.ZONAL_MANAGER:
        return qs.filter(**{prefix+'assigned_manager_id':user.pk})
    if user.role == Role.TSO:
        return qs.filter(**{prefix+'assigned_tso_id':user.pk})
    return qs.none()

def digester_team_scope(user, qs):
    require(user, 'team')
    if user.role == Role.DIGESTER_MANAGER:
        return qs.filter(role__in=[Role.ZONAL_MANAGER, Role.TSO])
    if user.role == Role.ZONAL_MANAGER:
        return qs.filter(role=Role.TSO, manager_id=user.pk)
    return qs.none()

def policy(key):
    p=Policy.objects.filter(key=key).first()
    if not p or not p.decision_reference or (p.test_only and settings.ENVIRONMENT not in ('test','development')):
        raise PolicyUnavailable(f'{key.replace("_"," ").capitalize()} policy must be recorded before this action is available.')
    return p.config

def scope(user, qs, feature):
    require(user,feature)
    if user.role in SEGMENT_NETWORK:
        model=qs.model.__name__
        segment=SEGMENT_NETWORK[user.role]
        if model=='Franchise': return qs.filter(kind=segment)
        paths={
            'Customer':'franchise__kind','Bill':'franchise__kind','Quotation':'franchise__kind',
            'EarningsEntry':'franchise__kind','Payment':'bill__franchise__kind',
            'Lead':'franchise__kind','Load':'franchise__kind','Requirement':'franchise__kind',
            'CashMovement':'franchise__kind','Handover':'franchise__kind',
            'Warehouse':'franchise__kind','Receipt':'warehouse__franchise__kind',
            'StockBalance':'warehouse__franchise__kind','StockMovement':'warehouse__franchise__kind',
            'DriverAccount':'driver__franchise__kind','HandoverRoute':'handover__franchise__kind',
            'AuditEvent':'franchise__kind',
        }
        if model=='Category': return qs.filter(kind=segment)
        if model=='Product': return qs.filter(category__kind=segment)
        if model=='Rate': return qs.filter(product__category__kind=segment).filter(Q(franchise__kind=segment)|Q(franchise__isnull=True))
        if model=='AuditEvent':
            rate_ids=Rate.objects.filter(product__category__kind=segment).values('pk')
            return qs.filter(Q(franchise__kind=segment)|Q(action='rate.changed',resource__in=rate_ids))
        path=paths.get(model)
        return qs.filter(**{path:segment}) if path else qs.none()
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
        if model in ('Bill','Quotation'): qs=qs.filter(creator=user)
        elif model=='Payment': qs=qs.filter(bill__creator=user)
        elif model in ('Lead','Load','CashMovement','Handover','DriverAccount'): qs=qs.filter(driver=user)
        elif model=='EarningsEntry': return qs.none()
    return qs.distinct() if model in ('Rate','Product') else qs

def scoped_get(user,qs,feature,pk):
    try: return scope(user,qs,feature).get(pk=pk)
    except (qs.model.DoesNotExist,ValueError): raise NotFound()

def book_allowed(user,book):
    if user.role in SEGMENT_NETWORK:
        if book.kind!=SEGMENT_NETWORK[user.role]: raise NotFound()
    elif user.role not in NETWORK and user.franchise_id!=book.id: raise NotFound()

def eligible_product(user,product,book):
    book_allowed(user,book)
    return product.active and product.category.active and product.franchises.filter(pk=book.pk).exists()
