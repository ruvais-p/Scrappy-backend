import pytest
from types import SimpleNamespace
from unittest.mock import patch
from core.models import Policy
from core.policy import PolicyUnavailable,policy

def test_missing_policy_returns_actionable_gate():
    with patch.object(Policy.objects,'filter',return_value=SimpleNamespace(first=lambda:None)):
        with pytest.raises(PolicyUnavailable,match='Billing issue policy'):
            policy('billing_issue')

def test_test_only_policy_never_activates_in_production(settings):
    synthetic=SimpleNamespace(decision_reference='Synthetic only',test_only=True,config={})
    settings.ENVIRONMENT='production'
    with patch.object(Policy.objects,'filter',return_value=SimpleNamespace(first=lambda:synthetic)):
        with pytest.raises(PolicyUnavailable):
            policy('synthetic_gate')
