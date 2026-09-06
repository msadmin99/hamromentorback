import re

from django.db import migrations

_PUBLIC_ID_RE = re.compile(r'^(.+?)(\d+)$')


def seed_question_public_id_counters(apps, schema_editor):
    """Seeds QuestionPublicIdCounter from every existing Question.public_id
    so the new atomic-sequence save() (see Question.save() / academics.
    models._next_public_id_number) continues numbering from exactly where
    the old COUNT(*)-based scheme left off, instead of restarting at 1 and
    colliding with already-issued public_ids.

    Splits each public_id into (prefix, number) via a general "everything
    up to the trailing digit run is the prefix" regex — not hardcoded to
    the current 1-2-uppercase-letter shape the prefix-generation logic
    happens to produce today, since that logic (Question.save(), untouched
    by this fix) derives prefixes from subject names and could in
    principle produce something outside that shape (e.g. a subject name
    containing '&' or a digit). Purely additive: only ever creates/updates
    QuestionPublicIdCounter rows, never touches Question data.
    """
    Question = apps.get_model('academics', 'Question')
    QuestionPublicIdCounter = apps.get_model('academics', 'QuestionPublicIdCounter')

    max_by_prefix = {}
    unparseable = []
    for public_id in Question.objects.exclude(public_id='').values_list('public_id', flat=True):
        m = _PUBLIC_ID_RE.match(public_id)
        if not m:
            unparseable.append(public_id)
            continue
        prefix, number = m.group(1), int(m.group(2))
        if number > max_by_prefix.get(prefix, 0):
            max_by_prefix[prefix] = number

    if unparseable:
        # Never seen in production as of writing (every existing public_id
        # matched), but if some legacy row ever doesn't end in digits at
        # all, it simply can't collide with a future {prefix}{number}
        # value — skip it rather than fail the migration.
        print(f'\nAudit: {len(unparseable)} Question.public_id value(s) had no trailing digits and were '
              f'skipped when seeding QuestionPublicIdCounter (they cannot collide with future generated ids): '
              f'{unparseable[:10]}{"..." if len(unparseable) > 10 else ""}\n')

    if not max_by_prefix:
        print('\nAudit: no existing Question.public_id values to seed QuestionPublicIdCounter from.\n')
        return

    for prefix, last_number in sorted(max_by_prefix.items()):
        QuestionPublicIdCounter.objects.update_or_create(prefix=prefix, defaults={'last_number': last_number})
    print(f'\nAudit: seeded QuestionPublicIdCounter for {len(max_by_prefix)} prefix(es) from existing '
          f'Question.public_id data: {dict(sorted(max_by_prefix.items()))}\n')


def unseed_question_public_id_counters(apps, schema_editor):
    QuestionPublicIdCounter = apps.get_model('academics', 'QuestionPublicIdCounter')
    QuestionPublicIdCounter.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('academics', '0021_questionpublicidcounter'),
    ]

    operations = [
        migrations.RunPython(seed_question_public_id_counters, unseed_question_public_id_counters),
    ]
