"""Word (.docx) import — walks paragraphs looking for a documented
question/option/answer pattern (numbered questions, lettered options,
"Answer:"/"Explanation:" markers). Preserves superscript/subscript (critical
for Physics/Chemistry notation — x², H₂O) and bold/italic by walking run
properties; extracts embedded images via the paragraph's own drawing
relationships. This is a documented-convention parser, not free-form NLP —
faculty following the template's structure get reliable results; documents
that don't follow the pattern degrade to "no questions found," not silent
corruption.

Subject, Chapter, Topic and Course are NOT part of the file — the admin
selects them for the whole batch on the Preview & Validate screen. Marks and
negative marks aren't part of the file either — those are set later in Exam
Management.

Expected structure (also shown in the downloadable .docx template):

    Q1. A ball is thrown vertically upward. What is its acceleration
    at the highest point?
    A) Zero
    B) g, downward
    C) g, upward
    D) Depends on mass
    Answer: B
    Explanation: At the highest point velocity is zero but gravity
    still acts downward.
"""
import re

from docx import Document
from docx.oxml.ns import qn

from .base import ParsedQuestion, save_temp_image

QUESTION_RE = re.compile(r'^\s*(Q\.?\s*)?(\d+)\s*[.):]\s*')
OPTION_RE = re.compile(r'^\s*([A-Da-d])\s*[.):]\s*')
ANSWER_RE = re.compile(r'^\s*Answer\s*:?\s*([A-Da-d])', re.IGNORECASE)
EXPLANATION_RE = re.compile(r'^\s*Explanation\s*:?\s*')


def _run_html(run, text, keep_bold=True):
    if not text:
        return ''
    escaped = text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    if run.font.superscript:
        escaped = f'<sup>{escaped}</sup>'
    elif run.font.subscript:
        escaped = f'<sub>{escaped}</sub>'
    if run.italic:
        escaped = f'<em>{escaped}</em>'
    if run.bold and keep_bold:
        escaped = f'<strong>{escaped}</strong>'
    return escaped


def _paragraph_html(para, strip_prefix_len=0):
    return _paragraph_html_variants(para, strip_prefix_len)[0]


def _paragraph_html_variants(para, strip_prefix_len=0):
    """Returns `(html, plain_html, all_bold)` for one paragraph.

    `plain_html` is identical to `html` except every run's bold is
    suppressed (sup/sub/italic are untouched) — used to recover an
    option's real text when its bold turns out to be nothing but the
    correct-answer marking convention (see `parse_docx`'s ANSWER_RE
    branch), never for questions or explanations.

    `all_bold` is True only when EVERY run that contributes actual
    (non-whitespace) text to this paragraph has `run.bold` set — i.e. the
    paragraph reads as a single fully-bold clause, not a bold word inside
    otherwise plain text. A paragraph with no qualifying run (blank, or
    entirely consumed by `strip_prefix_len`) is never considered
    "all bold" — there's nothing there to have been marked."""
    parts = []
    plain_parts = []
    consumed = 0
    saw_text_run = False
    all_bold = True
    for run in para.runs:
        text = run.text or ''
        if consumed < strip_prefix_len:
            cut = min(strip_prefix_len - consumed, len(text))
            text = text[cut:]
            consumed += cut
        if text:
            parts.append(_run_html(run, text))
            plain_parts.append(_run_html(run, text, keep_bold=False))
            if text.strip():
                saw_text_run = True
                all_bold = all_bold and bool(run.bold)
    inline = ''.join(parts).strip()
    plain_inline = ''.join(plain_parts).strip()
    html = f'<p>{inline}</p>' if inline else ''
    plain_html = f'<p>{plain_inline}</p>' if plain_inline else ''
    return html, plain_html, saw_text_run and all_bold


def _paragraph_image(para, document, batch_id, slot):
    if not batch_id:
        return ''
    for run in para.runs:
        for blip in run._element.findall('.//' + qn('a:blip')):  # noqa: SLF001 - python-docx has no public run-image accessor
            rid = blip.get(qn('r:embed'))
            if not rid:
                continue
            try:
                blob = document.part.related_parts[rid].blob
            except KeyError:
                continue
            return save_temp_image(batch_id, slot, blob, ext='png')
    return ''


def parse_docx(file_obj, batch_id=None):
    try:
        document = Document(file_obj)
    except Exception as exc:  # noqa: BLE001 - surface any python-docx failure as a clean validation error
        raise ValueError(f'Could not read that Word document: {exc}') from exc

    questions = []
    current = None
    mode = None

    def flush():
        nonlocal current
        if current is not None and current['text_html']:
            # `_bold_plain_html`/`_all_bold` are internal bookkeeping only
            # (see the ANSWER_RE branch below) — never part of the
            # documented option shape in base.ParsedQuestion's docstring.
            for opt in current['options']:
                opt.pop('_bold_plain_html', None)
                opt.pop('_all_bold', None)
            questions.append(current)
        current = None

    for para in document.paragraphs:
        raw_text = para.text.strip()
        if not raw_text:
            continue

        m = QUESTION_RE.match(raw_text)
        # A bare "12." (no "Q" prefix) is ambiguous with a decimal value like
        # "0.5 N — ..." that a rich explanation's per-option breakdown often
        # starts a line with — only trust it as a genuine new question when
        # it's explicitly "Q12." or we're not already inside an explanation
        # (a real new question never starts mid-explanation without a marker).
        if m and (m.group(1) or mode != 'explanation'):
            flush()
            current = ParsedQuestion()
            mode = 'question'
            current['text_html'] = _paragraph_html(para, strip_prefix_len=m.end())
            img = _paragraph_image(para, document, batch_id, f'q{len(questions) + 1}_question')
            if img:
                current['question_image_path'] = img
            continue

        if current is None:
            continue  # stray text before the first recognized question marker

        m = OPTION_RE.match(raw_text)
        # Same ambiguity as above: a "Detailed Option Analysis" breakdown
        # inside the explanation commonly repeats "A) ... / B) ... / C) ..."
        # to justify each choice — that must stay part of the explanation,
        # never be read as a second set of real options for the question.
        if m and mode != 'explanation':
            mode = 'option'
            html_val, plain_val, all_bold = _paragraph_html_variants(para, strip_prefix_len=m.end())
            img = _paragraph_image(para, document, batch_id, f'q{len(questions) + 1}_option{len(current["options"]) + 1}')
            current['options'].append({
                'text_html': html_val, 'is_correct': False, 'image_path': img,
                '_bold_plain_html': plain_val, '_all_bold': all_bold,
            })
            continue

        m = ANSWER_RE.match(raw_text)
        if m and mode != 'explanation':
            letter = m.group(1).upper()
            for i, opt in enumerate(current['options']):
                is_correct = chr(ord('A') + i) == letter
                opt['is_correct'] = is_correct
                # Some faculty bold the whole correct option in the source
                # document as their own visual convention while typing —
                # the parser never reads that bold as the answer signal
                # (the explicit "Answer: <letter>" line above is the only
                # source of truth for is_correct), but it must not leak
                # into the option students see either. Only strip it once
                # this option is independently confirmed correct by that
                # line, and only when the ENTIRE option text came out
                # bold — a partially-bold option (e.g. one word bolded for
                # genuine emphasis) is left exactly as authored, since the
                # importer has no way to tell that apart from the marker
                # convention and must not guess.
                if is_correct and opt.get('_all_bold') and opt.get('_bold_plain_html'):
                    opt['text_html'] = opt['_bold_plain_html']
            mode = None
            continue

        m = EXPLANATION_RE.match(raw_text)
        if m and mode != 'explanation' and raw_text.lower().startswith('explanation'):
            mode = 'explanation'
            current['explanation_html'] = _paragraph_html(para, strip_prefix_len=m.end())
            img = _paragraph_image(para, document, batch_id, f'q{len(questions) + 1}_explanation')
            if img:
                current['explanation_image_path'] = img
            continue

        # plain continuation of whichever section is currently open
        if mode == 'option' and current['options']:
            html_val, plain_val, all_bold = _paragraph_html_variants(para)
            if not html_val:
                continue
            opt = current['options'][-1]
            opt['text_html'] += html_val
            opt['_bold_plain_html'] = opt.get('_bold_plain_html', '') + plain_val
            opt['_all_bold'] = opt.get('_all_bold', True) and all_bold
            continue

        html_val = _paragraph_html(para)
        if not html_val:
            continue
        if mode == 'question':
            current['text_html'] += html_val
        elif mode == 'explanation':
            current['explanation_html'] += html_val

    flush()
    return questions


def build_template_docx():
    document = Document()
    document.add_paragraph('Q1. A ball is thrown vertically upward. What is its acceleration at the highest point?')
    document.add_paragraph('A) Zero')
    document.add_paragraph('B) g, downward')
    document.add_paragraph('C) g, upward')
    document.add_paragraph('D) Depends on mass')
    document.add_paragraph('Answer: B')
    document.add_paragraph('Explanation: At the highest point velocity is zero but gravity still acts downward.')
    document.add_paragraph('')
    document.add_paragraph('Q2. Which ion is formed when sulfuric acid (H2SO4) fully dissociates, alongside SO4²⁻?')
    document.add_paragraph('A) H⁺')
    document.add_paragraph('B) OH⁻')
    document.add_paragraph('C) NH4⁺')
    document.add_paragraph('D) Fe³⁺')
    document.add_paragraph('Answer: A')
    document.add_paragraph('Explanation: H2SO4 -> 2H+ + SO4^2-')
    return document
