"""Additive workspace read contracts; money is serialized as decimal strings."""
from rest_framework import serializers as s

class SearchResult(s.Serializer):
    id=s.UUIDField()
    screen=s.ChoiceField(choices=['bills','customers'])
    type=s.CharField()
    name=s.CharField()
    detail=s.CharField()
class SearchResponse(s.Serializer):
    results=SearchResult(many=True)
class PricingFranchise(s.Serializer):
    id=s.UUIDField()
    name=s.CharField()
    kind=s.CharField()
class PricingCell(s.Serializer):
    franchise=s.UUIDField()
    amount=s.CharField(allow_null=True)
    override=s.BooleanField()
    eligible=s.BooleanField()
class PricingProduct(s.Serializer):
    id=s.UUIDField()
    name=s.CharField()
    category_name=s.CharField()
    unit=s.CharField()
    default=s.CharField(allow_null=True)
    cells=PricingCell(many=True)
class PricingResponse(s.Serializer):
    franchises=PricingFranchise(many=True)
    results=PricingProduct(many=True)
class WorkspaceSummary(s.Serializer):
    today=s.CharField()
    month=s.CharField()
    incoming_outstanding=s.CharField()
    outgoing_outstanding=s.CharField()
    share_owed=s.CharField()
class WorkspaceFranchise(PricingFranchise):
    today=s.CharField()
    month=s.CharField()
    count=s.IntegerField()
    receivable=s.CharField()
    payable=s.CharField()
    share_owed=s.CharField(allow_null=True)
class DashboardResponse(s.Serializer):
    identity=s.DictField(required=False)
    date=s.DateField(required=False)
    pickup_chart=s.DictField(required=False)
    metrics=s.ListField(child=s.DictField())
    bills=s.ListField(child=s.DictField(),required=False)
    leads=s.ListField(child=s.DictField(),required=False)
    summary=WorkspaceSummary(required=False)
    franchises=WorkspaceFranchise(many=True,required=False)
    cancellations=s.IntegerField(required=False)
class RegisterResponse(s.Serializer):
    count=s.IntegerField()
    page=s.IntegerField()
    page_size=s.IntegerField()
    date_basis=s.CharField()
    summary=s.DictField()
    results=s.ListField(child=s.DictField())
class EarningsFranchise(PricingFranchise):
    kind=None
    share_percent=s.CharField()
    billed=s.CharField()
    collected=s.CharField()
    earned=s.CharField()
    paid=s.CharField()
    balance=s.CharField()
class EarningsResponse(s.Serializer):
    balance=s.CharField()
    can_payout=s.BooleanField()
    results=s.ListField(child=s.DictField())
    franchises=EarningsFranchise(many=True)
    summary=s.DictField(child=s.CharField())
