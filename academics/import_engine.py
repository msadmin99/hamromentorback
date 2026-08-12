"""Turns a validated ImportRow's raw_data into real Question/Option rows —
the actual DB-writing step, run from the background thread once a batch is
confirmed. Validation and dedup have already run by the time a row gets here.

Subject/Chapter/Topic and Courses come from the ImportBatch itself (chosen
once by the admin on the Preview & Validate screen) rather than being
resolved per-row — this is a direct FK assignment to existing taxonomy rows,
never a get-or-create-by-name, so importing can never create a duplicate
Subject/Chapter/Topic."""
from django.core.exceptions import ValidationError

from media_library.service import create_media_asset_from_file
from tests_app.models import Answer, TestQuestion

from .importers.base import load_temp_image
from .models import Option, Question


def _attach_image(file_obj, image_type, uploaded_by, question_id=None, option_id=None):
    """Routes an import-extracted image through the same validate/dedup/
    optimize pipeline as a direct upload — the same diagram embedded in
    5 different teachers' CSVs only ever gets stored/processed once.
    Returns the created MediaAsset, or None if the file fails validation
    (logged as an import warning by the caller, never crashes the batch)."""
    if not file_obj:
        return None
    try:
        return create_media_asset_from_file(
            file_obj, image_type, owner=uploaded_by, owner_role='teacher', category='other',
            question_id=question_id, option_id=option_id, original_filename=getattr(file_obj, 'name', ''),
        )
    except ValidationError:
        return None


def question_is_referenced(question):
    """True if a question has already been used in a live Test or answered
    in a submitted attempt — the line rollback/replace must not cross."""
    return TestQuestion.objects.filter(question=question).exists() or Answer.objects.filter(question=question).exists()


def create_question_from_row(data, batch, course_list):
    try:
        year = int(data['year']) if data.get('year') else None
    except (TypeError, ValueError):
        year = None

    question = Question.objects.create(
        subject=batch.subject, chapter=batch.chapter, topic=batch.topic,
        text=data.get('text_html', ''),
        explanation=data.get('explanation_html', ''),
        explanation_video_url=data.get('explanation_video_url', ''),
        remarks=data.get('remarks', ''),
        year=year,
        past_exam_years=data.get('past_exam_years', ''),
        references=data.get('references') or [],
    )

    q_image = load_temp_image(data.get('question_image_path'))
    exp_image = load_temp_image(data.get('explanation_image_path'))
    if q_image or exp_image:
        # Keep the legacy ImageField as a same-request fallback (so the
        # question always has *something* to show even if async variant
        # processing later fails), while also routing through the new
        # validated/deduped/optimized pipeline via image_asset.
        if q_image:
            question.image_asset = _attach_image(q_image, 'question_image', batch.uploaded_by, question_id=question.id)
            q_image.seek(0)
            question.image = q_image
        if exp_image:
            question.explanation_image_asset = _attach_image(
                exp_image, 'explanation_image', batch.uploaded_by, question_id=question.id,
            )
            exp_image.seek(0)
            question.explanation_image = exp_image
        question.save()

    for i, opt in enumerate(data.get('options') or []):
        if not (opt.get('text_html') or '').strip():
            continue
        opt_image = load_temp_image(opt.get('image_path'))
        opt_asset = _attach_image(opt_image, 'option_image', batch.uploaded_by) if opt_image else None
        if opt_image:
            opt_image.seek(0)
        option = Option.objects.create(
            question=question, text=opt.get('text_html', ''),
            image=opt_image, image_asset=opt_asset,
            order=i, is_correct=bool(opt.get('is_correct')),
        )
        if opt_asset:
            opt_asset.option_id = option.id
            opt_asset.save(update_fields=['option_id'])

    if course_list:
        question.courses.set(course_list)

    return question
