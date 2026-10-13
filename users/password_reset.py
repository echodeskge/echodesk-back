"""
"Forgot password" for dashboard users.

1. POST /api/users/password-reset/          {email}
   Emails a 6-digit code (valid 30 minutes). Same answer whether or not the
   email has an account, so it can't be used to discover who works here.
2. POST /api/users/password-reset/confirm/  {email, code, new_password}
   Sets the new password. Five wrong codes and the code is void.

Codes live in the cache (Redis), keyed by tenant schema + email.
"""
import logging
import secrets
from html import escape

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.cache import cache
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import connection
from django.utils.crypto import constant_time_compare
from drf_spectacular.utils import extend_schema
from rest_framework import permissions, serializers, status
from rest_framework.decorators import api_view, authentication_classes, permission_classes, throttle_classes
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle

logger = logging.getLogger(__name__)

CODE_TTL_SECONDS = 30 * 60
MAX_ATTEMPTS = 5


class PasswordResetThrottle(ScopedRateThrottle):
    scope = 'auth'


class PasswordResetRequestSerializer(serializers.Serializer):
    email = serializers.EmailField()


class PasswordResetConfirmSerializer(serializers.Serializer):
    email = serializers.EmailField()
    code = serializers.CharField(max_length=6, min_length=6)
    new_password = serializers.CharField(write_only=True, min_length=8)


def _key(email):
    return f'staff_password_reset:{connection.schema_name}:{email.strip().lower()}'


def _find_user(email):
    return get_user_model().objects.filter(email__iexact=email.strip(), is_active=True).first()


def _send_code_email(user, code):
    from tenants.email_service import email_service
    from tenants.models import Tenant

    try:
        tenant = Tenant.objects.get(schema_name=connection.schema_name)
        tenant_name, language = tenant.name, tenant.preferred_language
    except Tenant.DoesNotExist:
        tenant_name, language = 'EchoDesk', 'en'
    if language == 'ka':
        subject = f'პაროლის აღდგენის კოდი — {tenant_name}'
        lines = ['თქვენი პაროლის აღდგენის კოდი:', 'კოდი მოქმედებს 30 წუთის განმავლობაში. თუ ეს თქვენ არ მოგითხოვიათ, უბრალოდ უგულებელყავით ეს წერილი.']
    else:
        subject = f'Your password reset code — {tenant_name}'
        lines = ['Your password reset code is:', 'The code is valid for 30 minutes. If you did not ask for it, you can ignore this email.']
    html = (
        f'<div style="font-family:sans-serif;font-size:15px">'
        f'<p>{escape(lines[0])}</p>'
        f'<p style="font-size:28px;font-weight:bold;letter-spacing:4px">{code}</p>'
        f'<p>{escape(lines[1])}</p></div>'
    )
    return email_service._send_email(user.email, subject, html, f'{lines[0]} {code}\n\n{lines[1]}')


@extend_schema(tags=['Auth'], request=PasswordResetRequestSerializer)
@api_view(['POST'])
@authentication_classes([])
@permission_classes([permissions.AllowAny])
@throttle_classes([PasswordResetThrottle])
def password_reset_request(request):
    serializer = PasswordResetRequestSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    email = serializer.validated_data['email']

    user = _find_user(email)
    if user is not None:
        code = f'{secrets.randbelow(1_000_000):06d}'
        cache.set(_key(email), {'code': code, 'attempts': 0}, CODE_TTL_SECONDS)
        if not _send_code_email(user, code):
            logger.error('Password reset email to %s (%s) failed', user.email, connection.schema_name)
    return Response({'message': 'If an account exists for this email, a reset code has been sent.'})


@extend_schema(tags=['Auth'], request=PasswordResetConfirmSerializer)
@api_view(['POST'])
@authentication_classes([])
@permission_classes([permissions.AllowAny])
@throttle_classes([PasswordResetThrottle])
def password_reset_confirm(request):
    serializer = PasswordResetConfirmSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data

    invalid = Response({'code': 'invalid_code', 'detail': 'The code is wrong or has expired.'},
                       status=status.HTTP_400_BAD_REQUEST)
    key = _key(data['email'])
    stored = cache.get(key)
    if not stored:
        return invalid
    if not constant_time_compare(stored['code'], data['code']):
        stored['attempts'] += 1
        if stored['attempts'] >= MAX_ATTEMPTS:
            cache.delete(key)
        else:
            cache.set(key, stored, CODE_TTL_SECONDS)
        return invalid

    user = _find_user(data['email'])
    if user is None:
        cache.delete(key)
        return invalid
    try:
        validate_password(data['new_password'], user)
    except DjangoValidationError as exc:
        return Response({'new_password': list(exc.messages)}, status=status.HTTP_400_BAD_REQUEST)

    user.set_password(data['new_password'])
    user.password_change_required = False
    user.save(update_fields=['password', 'password_change_required'])
    cache.delete(key)
    return Response({'message': 'Password changed. You can sign in now.'})
