"""JSON import — the highest-fidelity format, since it carries pre-formatted
HTML/LaTeX directly with zero lossy parsing. Documented schema:

[
  {
    "text": "<p>What is <strong>Newton's</strong> second law?</p>",
    "options": [
      {"text": "F = ma", "is_correct": true},
      {"text": "F = mv", "is_correct": false}
    ],
    "explanation": "<p>...</p>",                                          # optional
    "year": 2080, "past_exam_years": "2078,2080",                         # optional
    "explanation_video_url": "https://...",                               # optional
    "references": [{"type": "book", "label": "...", "url": "..."}]        # optional
  }
]

Subject, Chapter, Topic and Course are NOT part of the file — the admin
selects them for the whole batch on the Preview & Validate screen. Marks and
negative marks are not part of the file either — those are set later in Exam
Management.
"""
import json

from .base import ParsedQuestion


def parse_json(file_obj):
    try:
        data = json.loads(file_obj.read().decode('utf-8'))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f'Not valid JSON: {exc}') from exc

    if not isinstance(data, list):
        raise ValueError('Expected a JSON array of question objects at the top level.')

    parsed = []
    for item in data:
        if not isinstance(item, dict):
            parsed.append(None)
            continue
        options = [
            {'text_html': str(o.get('text', '')), 'is_correct': bool(o.get('is_correct')), 'image_path': ''}
            for o in item.get('options', []) if isinstance(o, dict)
        ]
        parsed.append(ParsedQuestion(
            text_html=str(item.get('text', '')),
            options=options,
            explanation_html=str(item.get('explanation', '')),
            year=item.get('year'),
            past_exam_years=str(item.get('past_exam_years', '')),
            explanation_video_url=str(item.get('explanation_video_url', '')),
            references=item.get('references', []),
        ))
    return parsed
