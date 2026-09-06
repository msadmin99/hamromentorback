# Entitlement Data Model — Phase 2 Design

Produced before any schema change, per the Phase 2 spec's Step 24. Covers current models (unchanged, reused), the two small fixes to existing behavior this phase makes, and the one genuinely new model set.

---

## Decision: no central `Entitlement` table replaces existing models

Per Step 3 ("do not duplicate existing data... if an appropriate existing entitlement structure exists, reuse/refactor it... do NOT create EntitlementV2/ExamEntitlement/SubscriptionEntitlement as unrelated parallel systems"): `courses.Enrollment`, `billing.Subscription`, `billing.Scholarship`, `billing.GrandTestAccess`, and `tests_app.Test.assigned_students`/`assigned_batches` **remain the systems of record** for their respective sources. Copying them into a new unified table would itself be the kind of duplication the spec explicitly forbids, and would immediately create two places that could disagree.

**The one genuinely new concept** is Free Starter access — confirmed in `ENTITLEMENT_CURRENT_STATE.md` to have no existing model at all. That is the only new schema this phase adds. Everything else is a **decision-layer** (a Python service module, `entitlements.services`) that reads the existing models live and composes one uniform, explainable answer — not a new data store.

This satisfies Step 4's request for "the conceptual entitlement" (`user, resource_type, resource_id, source_type, source_id, valid_from, expires_at, quantity, used_quantity, unlimited, status`) as a **read-time composition**, not a write-time table, for every source except Free Starter, which genuinely needs its own row (there is no other record of "how much free QBank has this student used").

---

## New app: `entitlements`

### `FreeStarterPolicy`

Admin-configurable quota per resource type — the actual, editable policy. **No rows are seeded by this phase's migration** — the table starts empty, meaning zero Free Starter entitlements are granted until an admin explicitly configures at least one active policy row. This is deliberate: seeding literal example numbers (the spec's own "100 QBank questions" example) into a migration would itself be exactly the hardcoding the spec's Absolute Rule #10 forbids, even as a "default."

```python
class FreeStarterPolicy(models.Model):
    resource_type = models.CharField(max_length=20, choices=RESOURCE_TYPE_CHOICES, unique=True)
    quantity = models.PositiveIntegerField(default=0)
    unlimited = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
```

`resource_type` choices: `qbank`, `mock_test`, `daily_test`, `grand_test`, `pyq` — matching the spec's own Step 5 resource list and the exam-category vocabulary already used platform-wide (`SubscriptionPlan.PRODUCT_CHOICES`/`Test.exam_type`). Deliberately **type-level, not resource_id-level** granularity — the spec's own examples ("100 QBank questions", "1 Mock Test") are platform-wide counts, not per-specific-question/test allocations, so `resource_id` is not a field on this model (see Step 5's own caution: "do not automatically make every database object an entitlement — define product/resource boundaries intentionally").

**Constraints:** `unique=True` on `resource_type` — one policy row per resource type, admin edits it in place (matches `RolePermission`'s existing `unique=True` on `role` for the same "one configurable row per key" shape already in this codebase).

**Indexes:** none beyond the unique constraint — this table is tiny (5 rows max) and read once per registration, not a hot query path.

### `FreeStarterEntitlement`

The actual per-student grant and running usage.

```python
class FreeStarterEntitlement(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='free_starter_entitlements')
    resource_type = models.CharField(max_length=20, choices=FreeStarterPolicy.RESOURCE_TYPE_CHOICES)
    quantity = models.PositiveIntegerField(default=0)
    unlimited = models.BooleanField(default=False)
    used = models.PositiveIntegerField(default=0)
    valid_from = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='active')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('user', 'resource_type')
```

**Relationships:** `user` FK, `CASCADE` (matches `Subscription`/`Enrollment`'s own convention — a deleted user's entitlement rows are meaningless to keep).

**Unique constraint:** `(user, resource_type)` — one row per student per resource type. This is both the schema-level idempotency guard for provisioning (Step 14's "avoid duplicate starter entitlements if registration callbacks/retries happen more than once") and the row `select_for_update()` locks for atomic consumption (Step 12).

**`quantity`/`used` snapshotted at provisioning time, not read live from `FreeStarterPolicy`** — matches the platform's own existing "historical rights are not silently rewritten by a later config change" pattern (`PurchaseComboItem.price` snapshots at purchase time; the exam audit confirmed this as a sound, deliberate pattern elsewhere). A student provisioned under a 100-question policy keeps their 100 even if an admin later changes the policy to 50 — only *future* provisioning picks up the new value. This is the same choice `EXAM_MANAGEMENT_FEATURES`/`RolePermission` already implicitly makes for feature grants and matches Step "IMPORTANT COMMERCIAL RULE" ("historical purchases must not be destroyed because... product edited") applied to the free tier by the same logic.

**`effective_status` property** (not a stored field) computes live validity — matches `Subscription.is_current`'s existing correct pattern exactly, applied here for consistency rather than inventing a new convention:
```python
@property
def effective_status(self):
    if self.status == 'revoked':
        return 'revoked'
    if self.expires_at and self.expires_at < timezone.now():
        return 'expired'
    if not self.unlimited and self.used >= self.quantity:
        return 'exhausted'
    return 'active'
```

**Deletion behavior:** hard delete cascades from `User` deletion (matches every other user-owned commercial record in this codebase — `Subscription`, `Enrollment` are the same). No soft-delete introduced (consistent with the audit's finding that no model in this codebase uses soft-delete — not introducing a new, inconsistent convention here).

### `EntitlementEventLog`

Append-only audit trail (Step 23). Mirrors the existing `DeletionAuditLog`/`PaymentAuditLog`/`AdminEditAuditLog` pattern already used in this codebase — not a new logging convention.

```python
class EntitlementEventLog(models.Model):
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    resource_type = models.CharField(max_length=30, blank=True)
    event = models.CharField(max_length=15, choices=EVENT_CHOICES)  # created/consumed/exhausted/expired/revoked/restored
    detail = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=['user', 'resource_type'])]
```

`user`/`actor` are `SET_NULL` (not `CASCADE`) — matches `DeletionAuditLog`'s own convention of keeping the audit trail even if the referenced account is later deleted. No sensitive personal data logged beyond the user reference itself (Step 23's "do not expose secrets" / Step 27's "avoid logging sensitive personal data unnecessarily").

---

## Fix 1: `courses/access.py` — honor `expires_at`

**Current (bug, confirmed unchanged since the master audit):**
```python
def eligible_course_ids(user):
    return set(Enrollment.objects.filter(user=user, is_active=True).values_list('course_id', flat=True))
```

**Fix:** add the same `Q(expires_at__isnull=True) | Q(expires_at__gte=now)` filter already used correctly by `billing.access._active_subscriptions` and `has_video_access`'s course branch — for consistency, not a new pattern.

**Why this is safe (re-verified this phase, not assumed):** grepped every consumer (`tests_app.access`, `academics.access`, `videos_app.views`, `tests_app.performance`, `tests_app.views`'s `SubjectPerformanceDetailView`) and every existing test that touches `Enrollment.expires_at` (`billing/tests.py`) — zero tests create an enrollment with a past `expires_at` and assert it stays visible. The change can only **remove** access from rows that are already, definitionally, expired — it cannot grant new access to anyone. This directly satisfies the Phase 2 spec's Step 9 mandatory requirement ("access must not remain valid merely because is_active=true if expires_at < current_time") for the one place in the whole access graph where it was previously not true.

**Blast radius:** every consumer of `eligible_course_ids`/`eligible_batch_ids` (tests_app, academics, videos_app) — full regression run required and performed (see completion report).

## Fix 2: `billing/payment_service.py` — scholarship/paid subscriptions never share a row

**Current (bug, confirmed unchanged):** `_extend_or_create_subscription(user, course, product_type, duration, plan, mock_test_quota)` looks up *any* existing active `Subscription` for `(user, course, product_type)`, regardless of whether it originated from a scholarship or a real purchase, and extends it. A scholarship grant issued after a student already has a paid subscription for the same course+product silently attaches to (and later, on revocation, deactivates) that same paid row.

**Fix:** add an `is_scholarship=False` parameter. The lookup for an "existing" row to extend is now scoped by origin:
```python
existing_qs = Subscription.objects.filter(
    user=user, course=course, product_type=product_type, is_active=True,
).filter(Q(expires_at__isnull=True) | Q(expires_at__gte=now))
existing_qs = existing_qs.filter(scholarship__isnull=not is_scholarship)
existing = existing_qs.first()
```
(`scholarship__isnull=False` when `is_scholarship=True` — only extend an existing *scholarship-linked* row; `scholarship__isnull=True` otherwise — only extend an existing *non-scholarship* row.)

**Effect:** a scholarship grant for a student who already has separate paid access now always creates a **second, independent** `Subscription` row rather than merging into the paid one. Both rows are simultaneously valid — `_active_subscriptions()`/`has_*_access()` already correctly treat "any matching active row" as sufficient, so this requires **no change** to any access-check function, only to which row gets extended vs. created. This directly implements Step 10 ("multiple valid entitlements coexist... an expired/revoked entitlement must not cancel an unrelated valid entitlement") and Step 17 ("revoking scholarship access must NOT revoke independently purchased access") for the one concrete mechanism that could previously violate both.

**Renewal behavior preserved:** a second scholarship grant for the same (user, course, product_type) still correctly extends the *first* scholarship's row (found via `scholarship__isnull=False`), not creating a third row per renewal — matching the existing, correct "extend don't duplicate" behavior for same-origin renewals.

**Call sites updated:** `GrantAccessView.post` (`billing/views.py`) now passes `is_scholarship=bool(request.data.get('is_scholarship'))`. `_activate_product()`'s `subscription`/`combo` branches (real purchases) pass no flag (default `False`) — unchanged behavior for the by-far-most-common path.

**Not changed:** `ScholarshipViewSet.revoke()` itself — it still only deactivates the `Scholarship` and its linked `Subscription`, never touching `Enrollment` (see `ENTITLEMENT_CURRENT_STATE.md` §4 — flagged as a separate, larger business decision, not addressed by this fix).

---

## Migration strategy

- `entitlements` app: one new migration (`0001_initial`), pure `CREATE TABLE` — no data migration, no existing table touched, zero rows created by the migration itself (`FreeStarterPolicy` starts empty by design).
- `courses`/`billing`: **no migration** — both fixes are pure Python logic changes (a query filter, a function parameter), no field/schema change to either app.
- **Backfill (optional, not run by this phase):** a new idempotent management command, `backfill_free_starter_entitlements`, provisions Free Starter for every existing student against whatever `FreeStarterPolicy` rows are active at run time. `--dry-run` by default (matches the existing `audit_exam_course_assignment` command's convention exactly), `--apply` to actually write. Safe to re-run any number of times (the same `(user, resource_type)` uniqueness that makes registration-time provisioning idempotent makes this idempotent too — a second run creates zero new rows for anyone already provisioned). **Not executed as part of this phase** — whether/when to backfill existing students (vs. only new registrations going forward) is a product decision, not resolved here; the tool is ready when that decision is made.

## Rollback strategy

- `entitlements` app: `python manage.py migrate entitlements zero` cleanly drops all three new tables — nothing else references them yet (not wired into any existing endpoint's request path), so this is a zero-blast-radius rollback.
- Fix 1 (`courses/access.py`): revert the filter addition — a one-line, easily-reverted change; no data was altered, only a query's filter set.
- Fix 2 (`billing/payment_service.py`, `billing/views.py`): revert the `is_scholarship` parameter and its call site — no data migration to undo; any `Subscription` rows already created under the fixed behavior (i.e., a scholarship that got its own separate row instead of merging) remain valid, correctly-shaped rows even if the code is rolled back — rollback only stops the fix from applying to *future* grants, it does not corrupt anything already written.
- Registration hook (`accounts/serializers.py`): revert the `provision_free_starter(user)` call — future registrations simply stop getting Free Starter rows; no cleanup needed for already-provisioned students (their rows remain valid and harmless).
