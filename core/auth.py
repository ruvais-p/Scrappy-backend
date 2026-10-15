import hashlib
import secrets
import time
from datetime import timedelta
from django.conf import settings
from django.contrib.auth import authenticate,login,logout
from django.contrib.auth.password_validation import validate_password
from django.core.mail import send_mail
from django.db import transaction
from django.middleware.csrf import get_token
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_protect
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError,PermissionDenied,Throttled
from drf_spectacular.utils import extend_schema
from .models import User,Role,AccessToken,LoginAttempt,AuditEvent
from .policy import GRANTS

def identity(user):
    return {'id':user.id,'name':user.get_full_name() or user.email,'email':user.email,'role':user.role,'role_label':user.get_role_display(),'franchise':str(user.franchise_id) if user.franchise_id else None,'franchise_name':user.franchise.name if user.franchise_id else 'All franchises','warehouse':str(user.warehouse_id) if user.warehouse_id else None,'permission_version':user.permission_version,'capabilities':[k for k,v in GRANTS.items() if user.role in v]}

def throttle(key,limit=5):
    digest=hashlib.sha256(key.encode()).hexdigest()
    with transaction.atomic():
        row,_=LoginAttempt.objects.get_or_create(key=digest,defaults={'window_start':timezone.now()})
        row=LoginAttempt.objects.select_for_update().get(pk=digest)
        if timezone.now()-row.window_start>timedelta(minutes=15): row.attempts=0;row.window_start=timezone.now()
        if row.attempts>=limit: raise Throttled(wait=900)
        row.attempts+=1;row.save()

def establish(request,user):
    login(request,user,backend='django.contrib.auth.backends.ModelBackend')
    request.session.update({'permission_version':user.permission_version,'signed_in_at':time.time(),'last_activity':time.time(),'reauthenticated_at':time.time()})
    request.session.pop('challenge',None)
    AuditEvent.objects.create(actor=user,franchise_id=user.franchise_id,action='session.login',resource=str(user.pk))
    return identity(user)

def issue_token(user,purpose):
    raw=secrets.token_urlsafe(32)
    with transaction.atomic():
        AccessToken.objects.filter(user=user,purpose=purpose,consumed_at__isnull=True).update(consumed_at=timezone.now())
        AccessToken.objects.create(user=user,purpose=purpose,digest=hashlib.sha256(raw.encode()).hexdigest(),expires_at=timezone.now()+timedelta(minutes=30 if purpose=='reset' else 1440))
    send_mail('Your Scrappy account',f'Open {settings.PUBLIC_ORIGIN}/?access_token={raw} to set your password. This link is single use.',settings.DEFAULT_FROM_EMAIL,[user.email])

@method_decorator(csrf_protect,name='dispatch')
class AuthView(APIView):
    permission_classes=[AllowAny]
    @extend_schema(responses=dict)
    def get(self,request,action):
        if action=='csrf': return Response({'csrfToken':get_token(request)})
        user=request.user
        if action=='me' and user.is_authenticated: return Response(identity(user))
        return Response({'error':{'code':'unauthenticated','detail':'Please sign in.'}},status=401)
    @extend_schema(request=dict,responses=dict)
    def post(self,request,action):
        if action=='logout':
            logout(request);return Response({'ok':True})
        if action=='login':
            email=str(request.data.get('email','')).strip().lower()
            throttle('login:'+email);throttle('source:'+request.META.get('REMOTE_ADDR',''),30)
            user=authenticate(request,username=email,password=request.data.get('password',''))
            if not user: raise ValidationError('Email or password is incorrect.')
            return Response(establish(request,user))
        if action=='reset-request':
            email=str(request.data.get('email','')).strip().lower();throttle('reset:'+email,3)
            user=User.objects.filter(email=email,is_active=True).first()
            if user: issue_token(user,'reset')
            return Response({'detail':'If this account is eligible, recovery instructions will be sent.'})
        if action=='reset-confirm':
            raw=str(request.data.get('token',''));password=str(request.data.get('password',''))
            throttle('reset-token:'+request.META.get('REMOTE_ADDR',''),10)
            with transaction.atomic():
                token=AccessToken.objects.select_for_update().filter(digest=hashlib.sha256(raw.encode()).hexdigest(),consumed_at__isnull=True,expires_at__gt=timezone.now()).first()
                if not token: raise ValidationError('This link is invalid or expired.')
                user=User.objects.select_for_update().get(pk=token.user_id)
                if not user.is_active: raise ValidationError('This link is invalid or expired.')
                validate_password(password,user);user.set_password(password);user.permission_version+=1;user.save()
                token.consumed_at=timezone.now();token.save()
                AuditEvent.objects.create(actor=user,action='account.password_reset',resource=str(user.pk))
            logout(request);return Response({'detail':'Password updated. Sign in to continue.'})
        raise ValidationError('Unknown authentication operation.')
