"""SEO service functions for the public /question/{slug}/ page.

Plain functions, no new models — the public serializer/view (Phase 3) and the
sitemap generator (Phase 5) both import from here so title/description/
breadcrumb/related-question/structured-data logic has one source of truth.
Slug generation itself stays on Question.save() (Backend/academics/models.py)
since it needs to run at save time against Question.objects for uniqueness.
"""

import re

SHORT_EXPLANATION_FALLBACK_LENGTH = 220
SEO_TITLE_SNIPPET_LENGTH = 70
SEO_DESCRIPTION_SNIPPET_LENGTH = 155


def strip_tags(html):
    plain = re.sub(r'<[^>]+>', ' ', html or '')
    return re.sub(r'\s+', ' ', plain).strip()


def get_seo_title(question):
    if question.seo_title:
        return question.seo_title
    snippet = strip_tags(question.text)[:SEO_TITLE_SNIPPET_LENGTH].rstrip()
    return f'{snippet} — {question.subject.name} MCQ | Dr. Gutka'


def get_seo_description(question):
    if question.seo_description:
        return question.seo_description
    snippet = strip_tags(question.text)[:SEO_DESCRIPTION_SNIPPET_LENGTH].rstrip()
    return f'{snippet} — practice with answer and explanation on Dr. Gutka.'


def get_short_explanation(question):
    """Free teaser shown on the public page — falls back to a truncated
    excerpt of the full (premium-gated) explanation when no dedicated
    short_explanation has been authored, so existing imported questions are
    publishable immediately without a content-backfill effort."""
    if question.short_explanation:
        return question.short_explanation
    plain = strip_tags(question.explanation)
    if not plain:
        return ''
    if len(plain) <= SHORT_EXPLANATION_FALLBACK_LENGTH:
        return plain
    return plain[:SHORT_EXPLANATION_FALLBACK_LENGTH].rsplit(' ', 1)[0] + '…'


def get_breadcrumbs(question):
    """Ordered Subject → Chapter → Topic → Question trail. Used both to
    render the page's breadcrumb UI and to seed BreadcrumbList structured
    data — one computation, two consumers."""
    crumbs = [{'label': question.subject.name, 'type': 'subject', 'slug': question.subject.slug}]
    if question.chapter:
        crumbs.append({
            'label': question.chapter.name, 'type': 'chapter',
            'slug': question.chapter.slug, 'subject_slug': question.subject.slug,
        })
    if question.topic:
        crumbs.append({'label': question.topic.name, 'type': 'topic', 'id': question.topic.id})
    crumbs.append({'label': f'Q{question.public_id}', 'type': 'question', 'slug': question.slug})
    return crumbs


def get_related_questions(question, limit=6):
    """A handful of published, same-subject questions (preferring the same
    chapter) for the 'Related Questions' internal-linking block."""
    from .models import Question

    qs = Question.objects.filter(is_published=True, subject=question.subject).exclude(pk=question.pk)
    picked = list(qs.filter(chapter=question.chapter)[:limit]) if question.chapter else []
    if len(picked) < limit:
        picked_ids = [q.pk for q in picked]
        picked += list(qs.exclude(pk__in=picked_ids)[: limit - len(picked)])
    return picked


def get_similar_questions_for_practice(question, limit=50):
    """Broader same-subject/chapter pool for 'Practice N Similar Questions'.
    Deliberately not restricted to is_published — practice mode is gated the
    normal qbank way (has_qbank_access), not by the public-page publish flag."""
    from .models import Question

    qs = Question.objects.filter(subject=question.subject).exclude(pk=question.pk)
    if question.chapter:
        qs = qs.filter(chapter=question.chapter)
    return qs[:limit]


def build_structured_data(question, page_url=None):
    """schema.org QAPage/Question/Answer JSON-LD for the public page."""
    correct = next((o for o in question.options.all() if o.is_correct), None)
    main_entity = {
        '@type': 'Question',
        'name': strip_tags(question.text)[:300],
        'text': strip_tags(question.text),
        'answerCount': 1,
        'acceptedAnswer': {
            '@type': 'Answer',
            'text': strip_tags(correct.text) if correct else '',
        },
    }
    data = {'@context': 'https://schema.org', '@type': 'QAPage', 'mainEntity': main_entity}
    if page_url:
        data['url'] = page_url
        main_entity['url'] = page_url
    breadcrumbs = get_breadcrumbs(question)
    data['breadcrumb'] = {
        '@type': 'BreadcrumbList',
        'itemListElement': [
            {'@type': 'ListItem', 'position': i + 1, 'name': c['label']}
            for i, c in enumerate(breadcrumbs)
        ],
    }
    return data
