"""Authoritative user/device/global credential checks and audited state changes.

Lock order: credential policy, users ordered by pk, devices. No runtime creation
of missing policy state, no process-local revocation cache, no destructive reset.
"""
from django.conf import settings
from django.contrib.auth.hashers import check_password
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import APIException, AuthenticationFailed, PermissionDenied, ValidationError

from .models import CredentialPolicy, Device, RolePermission, User

ADMIN_ROLES = {'super_admin', 'admin', 'editor', 'teacher'}
CAPABILITIES = {'restore': 'restore_student_access', 'block': 'block_student_account',
                'revoke': 'revoke_student_device'}


def save_profile_fields(instance, changes, allowed):
    """Write only allowed profile columns, never a stale full account row.

    Disjoint column updates preserve concurrent security decisions in either
    ordering. Re-read afterward so responses do not publish stale security state.
    Role/status/password changes must use their dedicated locked workflows.
    """
    if set(changes) - allowed:
        raise ValidationError('Use the dedicated authorized security action.')
    for name, value in changes.items():
        setattr(instance, name, value)
    if changes:
        instance.save(update_fields=list(changes))
    instance.refresh_from_db()
    return instance


class SecurityUnavailable(APIException):
    status_code = 503
    default_detail = 'Authentication policy is unavailable. Please retry later.'


class StateConflict(APIException):
    status_code = 409
    default_detail = 'Account state changed. Reload and review before trying again.'


def role_policy(user):
    if user.is_superuser and not user.is_staff:
        return 'review'
    if user.is_staff:
        if user.admin_role not in ADMIN_ROLES:
            return 'review'
        return 'exempt'
    if user.admin_role:
        return 'review'
    return 'student'


def is_security_superadmin(user):
    return bool(user.is_active and role_policy(user) == 'exempt'
                and (user.is_superuser or user.admin_role == 'super_admin'))


def can_manage(user, operation, target=None):
    if not user.is_active or role_policy(user) != 'exempt':
        return False
    if target is not None and (target.pk == user.pk or role_policy(target) != 'student'):
        return is_security_superadmin(user) and target.pk != user.pk
    if is_security_superadmin(user):
        return True
    explicit = RolePermission.objects.filter(role='admin').values_list('features', flat=True).first() or []
    if not isinstance(explicit, list):
        return False
    return (
        user.admin_role == 'admin' and CAPABILITIES[operation] in explicit)


def policy(lock=False):
    try:
        qs = CredentialPolicy.objects.select_for_update() if lock else CredentialPolicy.objects
        return qs.get(pk=1)
    except CredentialPolicy.DoesNotExist:
        raise SecurityUnavailable() from None


def rejected(code, message):
    raise AuthenticationFailed({'code': code, 'detail': message, 'message': message})


def validate_credential(claims, user, current=None):
    current = current or policy()
    if getattr(user, 'staff_onboarding_pending', False):
        rejected('staff_onboarding_required', 'Complete staff account setup before signing in.')
    if not user.is_active:
        rejected('account_blocked', 'This account has been blocked. Contact support.')
    if role_policy(user) == 'review':
        rejected('privileged_configuration_review', 'Account privilege configuration requires administrator review.')
    generation = claims.get('credential_generation')
    version = claims.get('account_auth_version')
    row_id = claims.get('device_row_id')
    required = (generation, version, row_id)
    complete = all(type(v) is int for v in required)
    if not complete:
        # Partial/malformed new-format claims never count as legacy credentials.
        legacy = generation is None and version is None
        if not legacy or current.strict_enforcement or user.auth_version != 0:
            rejected('session_revoked', 'Your session has ended. Please sign in again.')
    if generation is not None and generation != current.generation:
        rejected('session_revoked', 'Your session has ended. Please sign in again.')
    if version is not None and version != user.auth_version:
        rejected('session_revoked', 'Your session has ended. Please sign in again.')
    if row_id is not None:
        if type(row_id) is not int or not Device.objects.filter(
                pk=row_id, user_id=user.pk, revoked_at__isnull=True).exists():
            rejected('device_revoked', 'This device was removed. Please sign in again.')
    elif current.strict_enforcement:
        rejected('session_revoked', 'Your session has ended. Please sign in again.')


def issue_tokens(user, device, current):
    from rest_framework_simplejwt.tokens import RefreshToken
    if device is None:
        raise ValueError('New credentials require a device')
    refresh = RefreshToken.for_user(user)
    refresh['credential_generation'] = current.generation
    refresh['account_auth_version'] = user.auth_version
    refresh['device_row_id'] = device.pk
    validate_credential(refresh.payload, user, current)
    return {'access': str(refresh.access_token), 'refresh': str(refresh)}


def audit(actor, target, action, reason, before, after, outcome='success', target_id=None):
    from core.models import AdminEditAuditLog
    return AdminEditAuditLog.objects.create(
        actor=actor, actor_email=actor.email if actor else '',
        resource_type='CredentialPolicy' if action == 'global_credential_revocation' else 'AccountSecurity',
        resource_id=str(target.pk if target else (1 if action == 'global_credential_revocation' else target_id or 'unknown')),
        action=action, reason=reason,
        outcome=outcome, changed_fields={'state': {'old': before, 'new': after}})


def account_state(user):
    return {'is_active': user.is_active, 'auth_version': user.auth_version,
            'security_revision': user.security_revision,
            'block_reason': user.block_reason, 'block_source': user.block_source}


def block_locked(user, actor, reason, source):
    before = account_state(user)
    user.is_active = False
    user.auth_version += 1
    user.security_revision += 1
    user.block_reason, user.block_source = reason, source
    user.blocked_at, user.blocked_by = timezone.now(), actor
    user.save(update_fields=['is_active', 'auth_version', 'security_revision', 'block_reason', 'block_source',
                             'blocked_at', 'blocked_by'])
    audit(actor, user, 'automatic_fourth_device_block' if source == 'device_limit' else 'block_account',
          reason, before, account_state(user))


def security_input(data):
    reason = data.get('reason')
    version = data.get('expected_version')
    if data.get('confirm') is not True or not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
        raise ValidationError({'detail': 'Confirmation and a nonblank reason (maximum 500 characters) are required.'})
    if type(version) is not int or version < 0:
        raise ValidationError({'expected_version': 'A current integer security revision is required.'})
    return reason.strip(), version


def manage_account(actor, target_id, operation, data, device_id=None):
    if not can_manage(actor, operation):
        raise PermissionDenied('You do not have permission for this account action.')
    reason, expected = security_input(data)
    with transaction.atomic():
        policy(lock=True)
        users = {u.pk: u for u in User.objects.select_for_update().filter(
            pk__in=[actor.pk, target_id]).order_by('pk')}
        actor, target = users.get(actor.pk), users.get(target_id)
        if actor is None or target is None:
            raise PermissionDenied('Account is unavailable.')
        if not can_manage(actor, operation, target):
            raise PermissionDenied('You do not have permission for this account action.')
        if target.security_revision != expected:
            raise StateConflict()
        if getattr(target, 'staff_onboarding_pending', False):
            raise StateConflict('Use the dedicated invitation actions for this pending staff account.')
        if operation == 'restore':
            if role_policy(target) == 'review':
                raise StateConflict('Privileged role/flag configuration requires separate review before restoration.')
            if target.is_active:
                audit(actor, target, 'restore_access', reason, account_state(target), account_state(target), 'no_change')
                target.security_outcome = 'no_change'
                return target
            if not target.is_active and target.block_source != 'device_limit':
                if not is_security_superadmin(actor) or data.get('reviewed_unknown_reason') is not True:
                    raise PermissionDenied('A Super Admin must explicitly review this historical or manual suspension.')
            before = account_state(target)
            target.is_active = True
            target.auth_version += 1
            target.security_revision += 1
            target.save(update_fields=['is_active', 'auth_version', 'security_revision'])
            audit(actor, target, 'restore_access', reason, before, account_state(target))
        elif operation == 'block':
            if not target.is_active:
                audit(actor, target, 'block_account', reason, account_state(target), account_state(target), 'no_change')
                target.security_outcome = 'no_change'
                return target
            if is_security_superadmin(target) and not User.objects.filter(
                    Q(is_superuser=True) | Q(admin_role='super_admin'), is_active=True,
                    is_staff=True, admin_role__in=ADMIN_ROLES).exclude(pk=target.pk).exists():
                raise PermissionDenied('Do not block the last available Super Admin.')
            block_locked(target, actor, reason, 'administrator')
        else:
            device = Device.objects.select_for_update().filter(pk=device_id, user=target).first()
            if device is None:
                raise ValidationError({'detail': 'Device not found for this account.'})
            before = {'device_row_id': device.pk, 'revoked': device.revoked_at is not None}
            if device.revoked_at is None:
                device.revoked_at, device.revoked_by = timezone.now(), actor
                device.save(update_fields=['revoked_at', 'revoked_by'])
            # Also supplies stale-state protection for simultaneous device decisions.
            target.security_revision += 1
            target.save(update_fields=['security_revision'])
            audit(actor, target, 'revoke_device', reason, before,
                  {'device_row_id': device.pk, 'revoked': True})
        return target


def activate_global(actor, data):
    if not getattr(settings, 'GLOBAL_REVOCATION_ENABLED', False):
        raise PermissionDenied('Global revocation execution is disabled.')
    reason, _ = security_input(dict(data, expected_version=0))
    with transaction.atomic():
        current = policy(lock=True)
        actor = User.objects.select_for_update().get(pk=actor.pk)
        if not is_security_superadmin(actor):
            raise PermissionDenied()
        # No setter: fresh verification must not silently upgrade a stored hash.
        if not isinstance(data.get('password'), str) or not check_password(data['password'], actor.password):
            raise PermissionDenied('Fresh password verification failed.')
        if type(data.get('expected_generation')) is not int or data['expected_generation'] != current.generation:
            raise StateConflict()
        if current.strict_enforcement:
            raise StateConflict('The one-time global cutoff has already been activated.')
        before = {'generation': current.generation, 'strict_enforcement': current.strict_enforcement}
        current.generation += 1
        current.strict_enforcement = True
        current.activated_at, current.activated_by = timezone.now(), actor
        current.save(update_fields=['generation', 'strict_enforcement', 'activated_at', 'activated_by'])
        audit(actor, None, 'global_credential_revocation', reason, before,
              {'generation': current.generation, 'strict_enforcement': True})
        return current


def change_staff_role(actor, target_id, role, data):
    reason, expected = security_input(data)
    if role not in ADMIN_ROLES:
        raise ValidationError({'admin_role': 'An explicit recognized staff role is required.'})
    with transaction.atomic():
        policy(lock=True)
        users = {u.pk: u for u in User.objects.select_for_update().filter(
            pk__in=[actor.pk, target_id]).order_by('pk')}
        actor, target = users.get(actor.pk), users.get(target_id)
        if actor is None or target is None or not is_security_superadmin(actor) or actor.pk == target.pk:
            raise PermissionDenied('A different verified Super Admin must review role changes.')
        if not target.is_staff or target.security_revision != expected:
            raise StateConflict()
        before = {'admin_role': target.admin_role, 'auth_version': target.auth_version}
        target.admin_role = role
        target.auth_version += 1
        target.security_revision += 1
        target.save(update_fields=['admin_role', 'auth_version', 'security_revision'])
        audit(actor, target, 'change_staff_role', reason, before,
              {'admin_role': role, 'auth_version': target.auth_version})
        return target
