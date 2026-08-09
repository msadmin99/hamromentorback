"""CSV import — same header-name convention as the Excel template, minus
embedded images (CSV can't hold binary): an optional *-image-url column per
image slot lets the source reference a hosted image by URL instead, which
gets downloaded and stored locally (never just linked externally).

Subject, Chapter, Topic and Course are NOT columns in this file — the admin
selects them for the whole batch on the Preview & Validate screen. Marks and
negative marks aren't columns either — those are set later in Exam
Management."""
import csv
import html
import io
import urllib.error
import urllib.request

from .base import ParsedQuestion, save_temp_image


def _wrap_html(value):
    text = (value or '').strip()
    if not text:
        return ''
    return f'<p>{html.escape(text)}</p>'

MAX_IMAGE_BYTES = 8 * 1024 * 1024  # 8MB — a sane ceiling for a single question/option image
FETCH_TIMEOUT = 8

REQUIRED_HEADERS = ['Question']
OPTION_TEXT_HEADERS = [f'Option{i}' for i in range(1, 5)]


def _fetch_image(url, batch_id, slot):
    if not url or not url.strip():
        return ''
    try:
        req = urllib.request.Request(url.strip(), headers={'User-Agent': 'HamromentorImporter/1.0'})
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
            raw = resp.read(MAX_IMAGE_BYTES + 1)
            if len(raw) > MAX_IMAGE_BYTES:
                return ''
            ext = url.strip().rsplit('.', 1)[-1][:4] if '.' in url else 'png'
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError):
        return ''
    return save_temp_image(batch_id, slot, raw, ext=ext)


def parse_csv(file_obj, batch_id=None):
    text = file_obj.read().decode('utf-8-sig')
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise ValueError('The CSV file appears to be empty.')
    missing = [h for h in REQUIRED_HEADERS if h not in reader.fieldnames]
    if missing:
        raise ValueError(f'Missing required column(s): {", ".join(missing)}. Download the CSV template for the expected headers.')

    parsed = []
    for i, row in enumerate(reader, start=1):
        options = []
        for n, header in enumerate(OPTION_TEXT_HEADERS, start=1):
            text_val = (row.get(header) or '').strip()
            if not text_val:
                continue
            image_path = _fetch_image(row.get(f'Option{n}-image-url', ''), batch_id, f'row{i}_option{n}') if batch_id else ''
            options.append({
                'text_html': _wrap_html(text_val),
                'is_correct': str(row.get('Correct Option', '')).strip() == str(n),
                'image_path': image_path,
            })

        parsed.append(ParsedQuestion(
            text_html=_wrap_html(row.get('Question')),
            options=options,
            explanation_html=_wrap_html(row.get('Explanation')),
            year=row.get('Year') or None,
            past_exam_years=(row.get('Past Exam Years') or '').strip(),
            explanation_video_url=(row.get('Explanation Video URL') or '').strip(),
            question_image_path=_fetch_image(row.get('Question-image-url', ''), batch_id, f'row{i}_question') if batch_id else '',
        ))
    return parsed


def build_template_csv():
    headers = [
        'Question', 'Question-image-url',
        'Option1', 'Option1-image-url', 'Option2', 'Option2-image-url',
        'Option3', 'Option3-image-url', 'Option4', 'Option4-image-url',
        'Correct Option', 'Explanation', 'Year', 'Past Exam Years',
        'Explanation Video URL',
    ]
    sample = [
        'A ball is thrown vertically upward. What is its acceleration at the highest point?', '',
        'Zero', '', 'g, downward', '', 'g, upward', '', 'Depends on mass', '',
        '2', 'At the highest point velocity is zero but gravity still acts downward.', '', '2078,2080', '',
    ]
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(headers)
    writer.writerow(sample)
    return buf.getvalue()
