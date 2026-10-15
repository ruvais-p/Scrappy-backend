import uuid
from django.core.exceptions import ValidationError as DjangoValidationError, ObjectDoesNotExist
from django.db import IntegrityError
from rest_framework.exceptions import ValidationError, NotFound
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_handler

def exception_handler(exc,context):
    if isinstance(exc,DjangoValidationError): exc=ValidationError(exc.messages)
    if isinstance(exc,ObjectDoesNotExist): exc=NotFound()
    response=drf_handler(exc,context)
    if response is not None:
        response.data={'error':{'code':getattr(exc,'default_code','invalid'),'detail':response.data},'request_id':str(uuid.uuid4())}
    elif isinstance(exc,IntegrityError):
        response=Response({'error':{'code':'conflict','detail':'The record conflicts with an existing record or constraint.'}},status=409)
    return response
