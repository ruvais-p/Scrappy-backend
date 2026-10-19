from django.urls import path
from drf_spectacular.views import SpectacularAPIView
from core.auth import AuthView
from core.views import RegisterView,CommandView,CashView,DashboardView,OptionsView,SettingsView,TeamView,RoleDefinitionsView,ReconciliationView,PayoutView,TimelineView
from core.events import stream
from core.documents import BillPDF,QuotationPDF,ShareLink,CustomerPDF,CustomerBillPage,PDFJobView,RevokeCustomerLinks
from core.jobs import ExportView,DownloadView,JobStatusView,AuditDeliveryView
from core.health import live,ready
from core.views import ReconciliationQueueView
from core.inventory_reports import InventoryReportView,InventoryAdjustmentView
from core.presentation import SearchView,PricingView
urlpatterns=[
 path('api/v1/search/',SearchView.as_view()),
 path('api/v1/pricing/',PricingView.as_view()),
 path('api/v1/inventory-report/',InventoryReportView.as_view()),
 path('api/v1/inventory-adjustments/',InventoryAdjustmentView.as_view()),
 path('api/health/live/',live),
 path('api/health/ready/',ready),
 path('api/v1/exports/',ExportView.as_view()),
 path('api/v1/audit-delivery/',AuditDeliveryView.as_view()),
 path('api/v1/jobs/<uuid:pk>/download/',DownloadView.as_view()),
 path('api/v1/jobs/<uuid:pk>/',JobStatusView.as_view()),
 path('api/v1/auth/<str:action>/',AuthView.as_view()),
 path('api/v1/schema/',SpectacularAPIView.as_view()),
 path('api/v1/events',stream),
 path('api/v1/dashboard/',DashboardView.as_view()),
 path('api/v1/reconciliation/<uuid:pk>/',ReconciliationView.as_view()),
 path('api/v1/reconciliation-queue/',ReconciliationQueueView.as_view()),
 path('api/v1/timeline/<str:feature>/<uuid:pk>/',TimelineView.as_view()),
 path('api/v1/payouts/',PayoutView.as_view()),
 path('api/v1/team/',TeamView.as_view()),
 path('api/v1/role-definitions/',RoleDefinitionsView.as_view()),
 path('api/v1/options/',OptionsView.as_view()),
 path('api/v1/driver-cash/',CashView.as_view()),
 path('api/v1/settings/<str:section>/save/',SettingsView.as_view()),
 path('api/v1/documents/<uuid:pk>/pdf/',BillPDF.as_view()),
 path('api/v1/quotations/<uuid:pk>/pdf/',QuotationPDF.as_view()),
 path('api/v1/documents/<uuid:pk>/pdf-jobs/',PDFJobView.as_view()),
 path('api/v1/documents/<uuid:pk>/share/',ShareLink.as_view()),
 path('api/v1/documents/<uuid:pk>/revoke-links/',RevokeCustomerLinks.as_view()),
 path('api/v1/customer-bill/<str:token>/',CustomerPDF.as_view()),
 path('api/v1/customer-bill/<str:token>/details/',CustomerBillPage.as_view()),
 path('api/v1/customer-bill/<str:token>/agree/',CustomerBillPage.as_view()),
 path('api/v1/<str:feature>/<uuid:pk>/<str:action>/',CommandView.as_view()),
 path('api/v1/<str:feature>/<uuid:pk>/',RegisterView.as_view()),
 path('api/v1/<str:feature>/',RegisterView.as_view()),
]
