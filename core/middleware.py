import time
from asgiref.sync import iscoroutinefunction, markcoroutinefunction, sync_to_async
from django.contrib.auth import logout
from django.http import JsonResponse

class AccessMiddleware:
    sync_capable=True
    async_capable=True
    def __init__(self,get_response):
        self.get_response=get_response
        self.is_async=iscoroutinefunction(get_response)
        if self.is_async: markcoroutinefunction(self)
    def validate(self,request):
        user=request.user
        if user.is_authenticated:
            now=time.time()
            idle=900 if user.role in ('super_admin','admin','hq_manager') else 1800
            if request.session.get('permission_version') != user.permission_version or now-request.session.get('last_activity',0)>idle or now-request.session.get('signed_in_at',0)>43200:
                logout(request)
                return JsonResponse({'error':{'code':'session_expired','detail':'Please sign in again.'}},status=401)
            if request.method not in ('GET','HEAD','OPTIONS'):
                request.session['last_activity']=now
        return None
    def secure(self,response):
        response['Cache-Control']='private, no-store'
        response['X-Frame-Options']='DENY'
        return response
    def __call__(self,request):
        if self.is_async: return self.acall(request)
        denied=self.validate(request)
        return self.secure(denied or self.get_response(request))
    async def acall(self,request):
        denied=await sync_to_async(self.validate,thread_sensitive=True)(request)
        response=denied if denied is not None else await self.get_response(request)
        return self.secure(response)
