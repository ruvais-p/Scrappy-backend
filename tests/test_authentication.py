import hashlib
from unittest.mock import patch
import pytest
from django.core.management import call_command,CommandError
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework.exceptions import PermissionDenied
from core.models import User,Role,AccessToken
from core.services.common import command

PASSWORD='Synthetic-test-password-only-942!'
def post(client,action,data):
    csrf=client.get('/api/v1/auth/csrf/').data['csrfToken']
    return client.post('/api/v1/auth/'+action+'/',data,format='json',HTTP_X_CSRFTOKEN=csrf)

@pytest.mark.django_db
def test_bootstrap_closes_and_password_is_hashed():
    with patch('getpass.getpass',return_value=PASSWORD):
        call_command('bootstrap_admin',email='first@example.test')
        with pytest.raises(CommandError):call_command('bootstrap_admin',email='second@example.test')
    user=User.objects.get();assert user.check_password(PASSWORD) and user.password!=PASSWORD

@pytest.mark.django_db
@pytest.mark.parametrize('role', Role.values)
def test_password_login_csrf_and_logout(data, role):
    user=data['users'][role];user.set_password(PASSWORD)
    # Previously enrolled accounts also use email/password only.
    user.mfa_enabled=True;user.mfa_secret='legacy-encrypted-secret';user.save()
    client=APIClient(enforce_csrf_checks=True)
    assert client.post('/api/v1/auth/login/',{'email':user.email,'password':PASSWORD}).status_code==403
    assert post(client,'login',{'email':user.email,'password':'incorrect'}).status_code==400
    assert client.get('/api/v1/auth/me/').status_code==401
    result=post(client,'login',{'email':user.email,'password':PASSWORD})
    assert result.status_code==200 and result.data['id']==user.pk
    assert 'mfa_required' not in result.data
    assert client.get('/api/v1/auth/me/').status_code==200
    assert post(client,'logout',{}).status_code==200
    assert client.get('/api/v1/auth/me/').status_code==401
    session=client.session
    session['challenge']={'user':user.id,'expires':9999999999,'permission_version':user.permission_version}
    session.save()
    for action in ('mfa-setup','mfa-verify'):
        assert post(client,action,{'code':'123456'}).status_code==400
    assert client.get('/api/v1/auth/me/').status_code==401

@pytest.mark.django_db
def test_reset_single_use_and_session_revocation(data,client_for):
    user=data['users'][Role.COLLECTOR];old=client_for(user);client=APIClient(enforce_csrf_checks=True)
    raw='synthetic-reset-token'
    AccessToken.objects.create(user=user,purpose='reset',digest=hashlib.sha256(raw.encode()).hexdigest(),expires_at=timezone.now()+timezone.timedelta(minutes=10))
    assert post(client,'reset-confirm',{'token':raw,'password':PASSWORD}).status_code==200
    assert post(client,'reset-confirm',{'token':raw,'password':PASSWORD}).status_code==400
    assert old.get('/api/v1/auth/me/').status_code==401

@pytest.mark.django_db
def test_reset_request_is_generic_and_team_invitation_is_queued(data,client_for):
    client=APIClient(enforce_csrf_checks=True)
    unknown=post(client,'reset-request',{'email':'nobody@example.test'})
    known=post(client,'reset-request',{'email':data['users'][Role.COLLECTOR].email})
    assert unknown.status_code==200 and known.status_code==200
    assert unknown.data==known.data
    hq=data['users'][Role.HQ]
    response=client_for(hq).post('/api/v1/team/',{'name':'Invited Collector','email':'invite@example.test','role':Role.COLLECTOR,'franchise':str(data['books'][0].pk)},format='json',HTTP_IDEMPOTENCY_KEY='invite-auth-test')
    assert response.status_code==200
    from core.models import Job
    assert Job.objects.filter(kind='account_invitation',payload__user=response.data['id']).exists()

@pytest.mark.django_db
def test_command_rechecks_permission_version_after_lock(data):
    actor=data['users'][Role.HQ]
    User.objects.filter(pk=actor.pk).update(permission_version=actor.permission_version+1)
    with pytest.raises(PermissionDenied):command(actor,'revoked-key','example',{},lambda u:{'unexpected':True})
