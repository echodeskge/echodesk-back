from django.shortcuts import render
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
import json
import logging

logger = logging.getLogger(__name__)


def privacy_policy(request):
    """Privacy Policy page for Facebook App compliance"""
    return render(request, 'legal/privacy_policy.html')


def terms_of_service(request):
    """Terms of Service page for Facebook App compliance"""
    return render(request, 'legal/terms_of_service.html')


def _signed_request_from(request):
    if (request.content_type or '').startswith('application/json'):
        try:
            data = json.loads(request.body or b'{}')
        except ValueError:
            data = {}
    else:
        data = request.POST.dict()
    return (data or {}).get('signed_request')


def _verified_user_id(request):
    """Return (user_id, error_response) for a Meta signed_request POST."""
    from .meta_compliance import (
        SignedRequestError, SignedRequestUnverifiable, parse_signed_request,
    )

    signed_request = _signed_request_from(request)
    if not signed_request:
        return None, JsonResponse({'error': 'Missing signed_request parameter'}, status=400)
    try:
        payload = parse_signed_request(signed_request)
    except SignedRequestUnverifiable:
        logger.error('Meta signed_request received but no app secret is configured')
        return None, JsonResponse({'error': 'signed_request verification is not configured'}, status=503)
    except SignedRequestError as exc:
        logger.warning('Rejected Meta signed_request: %s', exc)
        return None, JsonResponse({'error': 'Invalid signed_request'}, status=400)
    user_id = str(payload.get('user_id') or '').strip()
    if not user_id:
        return None, JsonResponse({'error': 'signed_request has no user_id'}, status=400)
    return user_id, None


@csrf_exempt
@require_http_methods(["GET", "POST"])
def user_data_deletion(request):
    """Meta "Data Deletion Request Callback".

    GET renders the human-readable deletion instructions page. POST is Meta's
    callback: verify the signed_request, record a FacebookDataDeletionRequest,
    remove what can be mapped to the app-scoped user id (see
    ``social_integrations.meta_compliance`` for the PSID vs ASID limits) and
    answer ``{"url": <status page>, "confirmation_code": <code>}``.
    """
    if request.method == 'GET':
        return render(request, 'legal/data_deletion.html')

    from django.utils import timezone
    from tenant_schemas.utils import schema_context
    from tenants.models import FacebookDataDeletionRequest
    from .meta_compliance import delete_user_data, deletion_status_url, new_confirmation_code

    user_id, error = _verified_user_id(request)
    if error:
        return error

    with schema_context('public'):
        record = FacebookDataDeletionRequest.objects.create(
            confirmation_code=new_confirmation_code(), user_id=user_id,
        )
    try:
        ok, details = delete_user_data(user_id)
    except Exception as exc:
        logger.exception('Data deletion for a Meta user failed')
        ok, details = False, {'errors': {'_': str(exc)}}
    with schema_context('public'):
        record.status = (
            FacebookDataDeletionRequest.STATUS_COMPLETED if ok else FacebookDataDeletionRequest.STATUS_FAILED
        )
        record.completed_at = timezone.now() if ok else None
        record.details = details
        record.save(update_fields=['status', 'completed_at', 'details'])

    return JsonResponse({
        'url': deletion_status_url(record.confirmation_code),
        'confirmation_code': record.confirmation_code,
    })


def _deletion_status_response(request, confirmation_code):
    from tenant_schemas.utils import schema_context
    from tenants.models import FacebookDataDeletionRequest

    record = None
    if confirmation_code:
        with schema_context('public'):
            record = FacebookDataDeletionRequest.objects.filter(confirmation_code=confirmation_code).first()

    wants_json = (
        request.GET.get('format') == 'json'
        or 'application/json' in (request.headers.get('Accept') or '')
    )
    if wants_json:
        if not record:
            return JsonResponse({'error': 'Unknown confirmation code'}, status=404)
        return JsonResponse({
            'confirmation_code': record.confirmation_code,
            'status': record.status,
            'created_at': record.created_at.isoformat(),
            'completed_at': record.completed_at.isoformat() if record.completed_at else None,
        })
    return render(request, 'legal/data_deletion_status.html', {
        'confirmation_code': confirmation_code,
        'record': record,
    }, status=200 if record or not confirmation_code else 404)


def data_deletion_status(request):
    """Legacy status page: /legal/data-deletion-status/?code=<code>"""
    return _deletion_status_response(request, request.GET.get('code'))


@require_http_methods(["GET"])
def data_deletion_status_by_code(request, confirmation_code):
    """Status page linked from the data-deletion callback response:
    /api/social/facebook/data-deletion/status/<code>/ (HTML; JSON with
    ?format=json or Accept: application/json)."""
    return _deletion_status_response(request, confirmation_code)


@csrf_exempt
@require_http_methods(["POST"])
def deauthorize_callback(request):
    """Meta "Deauthorize Callback": the user removed the app.

    Verifies the signed_request and deactivates the Page / Instagram
    connections that app-scoped user granted (tokens wiped) in every tenant.
    """
    from .meta_compliance import deauthorize_user

    user_id, error = _verified_user_id(request)
    if error:
        return error
    try:
        summary = deauthorize_user(user_id)
    except Exception:
        logger.exception('Deauthorize callback processing failed')
        return JsonResponse({'error': 'Failed to process deauthorization'}, status=500)
    logger.info('Meta deauthorize processed; affected schemas: %s', list(summary))
    return JsonResponse({'status': 'success'})
