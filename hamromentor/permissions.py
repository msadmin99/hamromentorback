from rest_framework.permissions import SAFE_METHODS, BasePermission


class IsStaffOrReadOnly(BasePermission):
    """Anyone (even anonymous) can read catalog data; only staff can write."""

    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return True
        return bool(request.user and request.user.is_authenticated and request.user.is_staff)


class IsStaffOrReadOnlyExcludingTeacherWrites(BasePermission):
    """Same as IsStaffOrReadOnly, but Teacher-role accounts can't write here.
    Used for Subject/Unit(Chapter)/Chapter(Topic) taxonomy management, which is
    Subjects Management — out of scope for a Teacher's Question Entry access."""

    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return True
        user = request.user
        if not (user and user.is_authenticated and user.is_staff):
            return False
        return getattr(user, 'admin_role', None) != 'teacher'


class IsAdminRoleOrAbove(BasePermission):
    """Staff-only endpoint that additionally blocks Editor-role accounts.
    Matches the reference admin panel's 'Admin role access' feature list —
    Editors are limited to content/exam areas and can't reach this."""

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated and user.is_staff):
            return False
        if user.is_superuser or getattr(user, 'admin_role', None) in (None, '', 'super_admin', 'admin'):
            return True
        return False


class IsAdminRoleOrAboveOrReadOnly(BasePermission):
    """Public read; write requires staff AND not an Editor-role account."""

    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return True
        user = request.user
        if not (user and user.is_authenticated and user.is_staff):
            return False
        return bool(user.is_superuser or getattr(user, 'admin_role', None) in (None, '', 'super_admin', 'admin'))


class IsSuperAdmin(BasePermission):
    """Super-Admin-only: managing other admin accounts, package pricing."""

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated and user.is_staff):
            return False
        return bool(user.is_superuser or getattr(user, 'admin_role', None) in (None, '', 'super_admin'))


class IsApprovedTeacher(BasePermission):
    """Marketplace-teacher-only write access. Deliberately unrelated to
    admin_role='teacher' (the internal-staff concept) — this checks the
    separate marketplace.TeacherProfile approval status instead."""

    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return True
        user = request.user
        if not (user and user.is_authenticated):
            return False
        profile = getattr(user, 'teacher_profile', None)
        return bool(profile and profile.status == 'approved')


class IsCourseOwnerOrAdmin(BasePermission):
    """A marketplace course (or its sections/lessons, via `get_course(obj)`) is
    only writable by the owning teacher or staff. Reads are public for
    approved courses, otherwise restricted to the owner/staff — drafts and
    pending/rejected courses are private, matching 'a teacher must not access
    another teacher's private course data.'"""

    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return True
        user = request.user
        if not (user and user.is_authenticated):
            return False
        if user.is_staff:
            return True
        profile = getattr(user, 'teacher_profile', None)
        return bool(profile and profile.status == 'approved')

    def has_object_permission(self, request, view, obj):
        user = request.user
        course = view.get_course(obj) if hasattr(view, 'get_course') else obj
        is_owner = bool(user and user.is_authenticated and course.teacher_id == user.id)
        is_staff = bool(user and user.is_authenticated and user.is_staff)
        if request.method in SAFE_METHODS:
            return is_owner or is_staff or course.status == 'approved'
        return is_owner or is_staff
