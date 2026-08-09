"""Turns a validated ImportRow's raw_data into real Question/Option rows —
the actual DB-writing step, run from the background thread once a batch is
confirmed. Validation and dedup have already run by the time a row gets here.

Subject/Chapter/Topic and Courses come from the ImportBatch itself (chosen
once by the admin on the Preview & Validate screen) rather than being
resolved per-row — this is a direct FK assignment to existing taxonomy rows,
never a get-or-create-by-name, so importing can never create a duplicate
Subject/Chapter/Topic."""
from tests_app.models import Answer, TestQuestion

from .importers.base import load_temp_image
from .models import Option, Question


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
        if q_image:
            question.image = q_image
        if exp_image:
            question.explanation_image = exp_image
        question.save()

    for i, opt in enumerate(data.get('options') or []):
        if not (opt.get('text_html') or '').strip():
            continue
        Option.objects.create(
            question=question, text=opt.get('text_html', ''),
            image=load_temp_image(opt.get('image_path')),
            order=i, is_correct=bool(opt.get('is_correct')),
        )

    if course_list:
        question.courses.set(course_list)

    return question
