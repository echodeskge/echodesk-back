# `onboarding` — per-module setup wizard (backend)

Tenant-schema app. One `TenantOnboarding` row per tenant holds the progress
and answers of every wizard step; the steps themselves are registered by the
module apps and filtered by the tenant's enabled subscription features.

- API: `GET /api/onboarding/`, `POST /api/onboarding/<key>/{apply,skip,reset}/`
  (tenant admins only — staff, superuser or `manage_settings`, the same check
  as the workspace settings endpoints). Full contract in
  `~/Echodesk/.wt/onb/RULES.md`.
- Triggers: `process_pending_tenants` (after `sync_features`) creates the row,
  marks every shown step pending and calls `seed_defaults()` of every enabled
  module's step; `SubscriptionService.sync_tenant_features` marks a newly
  enabled feature's step pending and removes the entry of a disabled one.

## How to add a step (module packages)

1. Create `<app>/onboarding.py` (the app must be in `TENANT_APPS`; it is
   autodiscovered by `OnboardingConfig.ready()`). Document the answers
   schema at the top of the file — the web depends on the keys, keep them
   snake_case and stable. The canonical list of what each step asks/writes is
   in `~/Echodesk/.wt/onb/PLAN.md` (sections 3–9).
2. Subclass `onboarding.base.OnboardingStep` and register it:

   ```python
   from rest_framework import serializers
   from onboarding.base import OnboardingStep, empty_result
   from onboarding.registry import register

   class TicketsAnswersSerializer(serializers.Serializer):
       kind = serializers.ChoiceField(choices=['support', 'tasks', 'jobs'])
       ...

   @register
   class TicketsStep(OnboardingStep):
       key = 'tickets'               # step key; feature key of the module
       feature_key = 'tickets'       # None = always shown (only workspace)
       order = 10                    # workspace 0, tickets 10, social 20, bookings 30,
                                     # shop 40, invoices 50, leave 60, calls 70
       serializer_class = TicketsAnswersSerializer

       def context(self, tenant, request) -> dict: ...   # read data for the page
       def prefill(self, tenant, state) -> dict: ...     # state = TenantOnboarding row
       def apply(self, tenant, answers, user) -> dict:   # answers = validated_data
           result = empty_result()                       # {"created": {}, "updated": [], "notes": []}
           ...
           return result
       def seed_defaults(self, tenant) -> None: ...      # provisioning fallback (decision B)
   ```

3. Rules for `apply`: it runs inside the tenant schema, in one transaction
   (the view opens it), must be **idempotent** (match existing rows by natural
   keys — board name, column name, service name, leave type code, email,
   DID…), and never deletes. The view marks the step `done` when it returns.
   `serializer_class` gets `context={"request", "tenant", "state"}`.
4. `seed_defaults(tenant)` runs at provisioning for enabled modules before any
   answer exists; it must be safe to run twice and must not raise (errors are
   logged and swallowed, but keep it clean).
5. `prefill(tenant, state)` can read other steps' answers through
   `state.answers_of('workspace')` and the industry through `state.industry`.
6. Exposing users in `context`: follow the workspace step —
   `[{"id", "email", "name", "role", "is_active"}]` — so the web can reuse one
   people picker.
7. Inviting users from a step: `users.invitations.invite_user(user,
   invited_by=acting_user, tenant=tenant)` after `User.objects.create(...)`
   (temporary password + forced change + invitation e-mail task, the same
   path as `POST /api/users/`). In tests patch
   `users.invitations.send_user_invitation_email_task`.
8. Tests: subclass `users.tests.conftest.EchoDeskTenantTestCase`; enable a
   feature for the test tenant by creating a `Feature` + `TenantSubscription`
   (see `onboarding/tests/helpers.py`). Run
   `python3 -m pytest onboarding <your app> tenants -q -p no:cacheprovider --reuse-db`.
