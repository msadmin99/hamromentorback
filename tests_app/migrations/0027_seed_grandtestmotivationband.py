"""Grand Test 3.0 / GT3-6 — seed the 6 default score-band motivational
rows from the approved GT3-6 spec §16, verbatim. Admin can edit/add/
remove bands afterward (GrandTestMotivationBand is a plain CRUD table,
not a fixed enum) — this migration only establishes the day-one defaults
so a fresh Grand Test result has real, meaningful motivation copy instead
of an empty table forcing every score into the generic fallback message.

Reversible: the reverse operation deletes exactly these 6 rows by title
(the only identifying data this migration itself created)."""
from django.db import migrations

BANDS = [
    {
        'min_percent': 90, 'max_percent': 100, 'title': 'Outstanding Performance',
        'message': (
            'Excellent performance. Your preparation is producing strong results. Keep your strongest areas '
            'consistent while targeting the few remaining areas where you can recover additional marks.'
        ),
        'recommended_practice_hint': 'Advanced practice / difficult questions / timed Mock Test', 'order': 0,
    },
    {
        'min_percent': 75, 'max_percent': 89.99, 'title': 'Excellent Progress',
        'message': (
            'You have built a strong performance base. Your next improvement should come from analysing '
            'mistakes and strengthening the areas where you lost marks.'
        ),
        'recommended_practice_hint': 'Weak-topic practice + timed Mock Test', 'order': 1,
    },
    {
        'min_percent': 60, 'max_percent': 74.99, 'title': 'Good Progress',
        'message': (
            'You are making good progress. Your next opportunity is to convert weaker topics into reliable '
            'marks before the next Grand Test.'
        ),
        'recommended_practice_hint': 'Subject/Chapter/Topic practice', 'order': 2,
    },
    {
        'min_percent': 40, 'max_percent': 59.99, 'title': 'Keep Improving',
        'message': (
            'This result gives you a clear roadmap for improvement. Focus on the subjects and topics where '
            'you lost the most marks rather than simply taking more full-length exams.'
        ),
        'recommended_practice_hint': 'Targeted topic/chapter practice', 'order': 3,
    },
    {
        'min_percent': 20, 'max_percent': 39.99, 'title': 'Build the Foundation',
        'message': (
            'Use this Grand Test as a diagnostic tool. Strengthen your weaker concepts first, then return '
            'to timed full-length practice.'
        ),
        'recommended_practice_hint': 'Topic + Chapter + Subject practice', 'order': 4,
    },
    {
        'min_percent': 0, 'max_percent': 19.99, 'title': 'Start Step by Step',
        'message': (
            'One Grand Test does not define your preparation. Use this result to identify your biggest '
            'learning gaps and work through them systematically.'
        ),
        'recommended_practice_hint': 'Foundational practice + subject tests + revision', 'order': 5,
    },
]

BAND_TITLES = [b['title'] for b in BANDS]


def seed_bands(apps, schema_editor):
    GrandTestMotivationBand = apps.get_model('tests_app', 'GrandTestMotivationBand')
    for band in BANDS:
        GrandTestMotivationBand.objects.get_or_create(title=band['title'], defaults=band)


def remove_bands(apps, schema_editor):
    GrandTestMotivationBand = apps.get_model('tests_app', 'GrandTestMotivationBand')
    GrandTestMotivationBand.objects.filter(title__in=BAND_TITLES).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('tests_app', '0026_grandtestmotivationband'),
    ]

    operations = [
        migrations.RunPython(seed_bands, remove_bands),
    ]
