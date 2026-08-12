"""Role → allowed image_types + default visibility, matching this project's
existing admin_role system (student/teacher/admin/editor/super_admin) rather
than inventing a parallel one."""

STUDENT_ALLOWED_TYPES = {'student_avatar'}
TEACHER_ALLOWED_TYPES = {
    'teacher_avatar', 'course_thumbnail', 'question_image', 'option_image', 'explanation_image', 'rich_text',
}
ADMIN_ALLOWED_TYPES = TEACHER_ALLOWED_TYPES | STUDENT_ALLOWED_TYPES | {'banner', 'logo', 'other'}

# SVG is never accepted through the general image validator — only the
# admin-only logo path (not implemented in stage 1) may special-case it.
ADMIN_ROLES = {'admin', 'super_admin', 'editor'}


def get_owner_role(user):
    if getattr(user, 'admin_role', None) in ADMIN_ROLES or getattr(user, 'is_superuser', False):
        return 'admin'
    if getattr(user, 'admin_role', None) == 'teacher':
        return 'teacher'
    if getattr(user, 'is_staff', False):
        return 'admin'
    return 'student'


def allowed_image_types_for(owner_role):
    return {
        'student': STUDENT_ALLOWED_TYPES,
        'teacher': TEACHER_ALLOWED_TYPES,
        'admin': ADMIN_ALLOWED_TYPES,
    }.get(owner_role, set())


# image_type -> default visibility for its variants (originals are ALWAYS
# private regardless of this — see tasks.process_media_asset).
DEFAULT_VISIBILITY = {
    'student_avatar': 'public',
    'teacher_avatar': 'public',
    'course_thumbnail': 'public',
    'question_image': 'public',
    'option_image': 'public',
    'explanation_image': 'public',
    'banner': 'public',
    'logo': 'public',
    'rich_text': 'public',
    'other': 'private',
}
