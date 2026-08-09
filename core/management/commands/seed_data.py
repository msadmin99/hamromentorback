import random
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.utils.text import slugify

from academics.models import Chapter, Option, Question, Subject, Topic
from core.models import Announcement, Banner, HomeFeature, MCQOfTheDay, SiteLink, SiteSettings
from courses.models import (
    Course,
    CoursePackage,
    Enrollment,
    EnrollmentRequest,
)
from tests_app.models import Test, TestQuestion
from videos_app.models import Video, VideoCategory

User = get_user_model()

SUBJECTS = [
    ('Anatomy', '🦴', ['Cardiovascular and Respiratory Systems', 'Upper Limb', 'Neuroanatomy']),
    ('Biochemistry', '🧪', ['Enzymes', 'Carbohydrate Metabolism', 'Molecular Biology']),
    ('Physiology', '🫀', ['Cardiovascular Physiology', 'Renal Physiology', 'Nerve-Muscle Physiology']),
    ('Pharmacology', '💊', ['Autonomic Pharmacology', 'Antimicrobials', 'Cardiovascular Drugs']),
    ('Microbiology', '🦠', ['Bacteriology', 'Virology', 'Immunology']),
    ('Pathology', '🔬', ['Cell Injury', 'Neoplasia', 'Hematology']),
    ('Community Medicine', '🏘️', ['Epidemiology', 'Biostatistics', 'Nutrition']),
    ('Forensic Medicine', '🕵️', ['Thanatology', 'Toxicology', 'Medical Jurisprudence']),
    ('Ophthalmology', '👁️', ['Cornea', 'Glaucoma', 'Retina']),
    ('ENT', '👂', ['Ear', 'Nose', 'Throat']),
    ('Anaesthesia', '💉', ['General Anaesthesia', 'Regional Anaesthesia', 'Critical Care']),
    ('Dermatology', '🩹', ['Infections', 'Papulosquamous Disorders', 'Pigmentary Disorders']),
    ('Psychiatry', '🧠', ['Mood Disorders', 'Psychosis', 'Anxiety Disorders']),
    ('Radiology', '📡', ['Chest Imaging', 'Abdominal Imaging', 'Neuroimaging']),
    ('Medicine', '🩺', ['Cardiology', 'Endocrinology', 'Nephrology']),
    ('Surgery', '⚕️', ['GI Surgery', 'Trauma', 'Urology']),
    ('Orthopaedics', '🦵', ['Fractures', 'Joint Disorders', 'Spine']),
    ('Paediatrics', '🧒', ['Neonatology', 'Growth & Development', 'Immunization']),
    ('Obstetrics & Gynaecology', '🤰', ['Antenatal Care', 'Labour', 'Gynaecological Oncology']),
    ('Previous Year Question Papers', '📄', ['CEE-PG 2023', 'CEE-PG 2024', 'CEE-PG 2025']),
]

GENERIC_QUESTION_BANK = [
    "Which of the following is the most likely diagnosis in {chapter}?",
    "A patient presents with a classic finding related to {chapter}. What is the next best step?",
    "Which structure/mechanism is primarily involved in {chapter}?",
    "The best investigation of choice for a condition under {chapter} is:",
]

OPTION_POOL = ["Option A finding", "Option B mechanism", "Option C investigation", "Option D structure"]


class Command(BaseCommand):
    help = 'Seed the database with demo Dr. Gutka content (subjects, chapters, questions, tests, videos).'

    def handle(self, *args, **options):
        random.seed(42)
        self.stdout.write('Seeding subjects, chapters, topics, questions...')
        all_questions_by_subject = {}

        for order, (name, icon, chapters) in enumerate(SUBJECTS):
            subject, _ = Subject.objects.update_or_create(
                slug=slugify(name), defaults={'name': name, 'icon': icon, 'order': order},
            )
            all_questions_by_subject[subject.slug] = []

            for c_order, chapter_name in enumerate(chapters):
                chapter, _ = Chapter.objects.update_or_create(
                    subject=subject, slug=slugify(chapter_name),
                    defaults={'name': chapter_name, 'order': c_order},
                )
                Topic.objects.get_or_create(chapter=chapter, name=f'{chapter_name} - Key Concepts', defaults={'order': 0})
                Topic.objects.get_or_create(chapter=chapter, name=f'{chapter_name} - Frequently Asked', defaults={'order': 1})

                for q_index in range(4):
                    template = GENERIC_QUESTION_BANK[q_index % len(GENERIC_QUESTION_BANK)]
                    text = template.format(chapter=chapter_name)
                    question, created = Question.objects.get_or_create(
                        subject=subject, chapter=chapter, text=text,
                        defaults={
                            'explanation': f'This tests core knowledge of {chapter_name} within {name}, a frequently '
                                           f'repeated concept in national licensing exams.',
                            'marks': 1,
                            'negative_marks': 0.25,
                        },
                    )
                    if created:
                        options = OPTION_POOL[:]
                        random.shuffle(options)
                        correct_index = random.randint(0, 3)
                        for i, opt_text in enumerate(options):
                            Option.objects.create(
                                question=question, text=opt_text, order=i,
                                is_correct=(i == correct_index),
                                pick_percentage=random.randint(10, 40),
                            )
                    all_questions_by_subject[subject.slug].append(question)

        # A richer, hand-written Anatomy question (mirrors the embryology example in the reference app)
        anatomy = Subject.objects.get(slug='anatomy')
        cv_chapter = Chapter.objects.get(subject=anatomy, slug=slugify('Cardiovascular and Respiratory Systems'))
        featured_q, created = Question.objects.get_or_create(
            subject=anatomy, chapter=cv_chapter,
            text='Which of the following forms the trabeculated part of the right ventricle?',
            defaults={
                'explanation': (
                    'The trabeculated part of the right ventricle is formed from the proximal 1/3rd of the '
                    'bulbus cordis. The heart tube begins to bend by day 23, creating the cardiac loop that '
                    'gives rise to the chambers of the definitive heart.'
                ),
                'marks': 1,
                'negative_marks': 0.25,
            },
        )
        if created:
            opts = [
                ('Primitive ventricle', False),
                ('Distal 1/3 of bulbus cordis', False),
                ('Middle 1/3 of bulbus cordis', False),
                ('Proximal 1/3 of bulbus cordis', True),
            ]
            for i, (text, correct) in enumerate(opts):
                Option.objects.create(question=featured_q, text=text, order=i, is_correct=correct, pick_percentage=[25, 28, 10, 37][i])

        self.stdout.write('Seeding videos...')
        lecture_category, _ = VideoCategory.objects.get_or_create(
            name='Chapter Lecture', defaults={'slug': 'chapter-lecture', 'order': 0},
        )
        for subject in Subject.objects.exclude(slug='previous-year-question-papers'):
            for i, chapter in enumerate(subject.chapters.all()[:2]):
                video, _ = Video.objects.get_or_create(
                    subject=subject, chapter=chapter, title=f'{chapter.name} - Concept Walkthrough',
                    defaults={
                        'category': lecture_category,
                        'instructor_name': ['Dr. Aakash Rai', 'Dr. Sunita Shrestha', 'Dr. Bibek Koirala'][i % 3],
                        'source_type': 'youtube',
                        'video_url': 'https://www.youtube.com/watch?v=dQw4w9WgXcQ',
                        'duration_seconds': random.choice([28, 46, 57, 72]) * 60,
                        'access_level': 'registered' if i == 0 else 'premium',
                        'order': i,
                    },
                )
                video.courses.set(subject.courses.all())

        self.stdout.write('Seeding tests...')
        now = timezone.now()
        subject_pool = [s for s in Subject.objects.exclude(slug='previous-year-question-papers')]

        # Grand tests
        for i in range(1, 4):
            questions = list(Question.objects.order_by('?')[:40])
            test, _ = Test.objects.get_or_create(
                title=f'Grand Test {17 + i} - New NEET Pattern',
                group='grand',
                defaults={
                    'duration_minutes': 210,
                    'is_pro': True,
                    'is_new': (i == 3),
                    'academic_year': '2025-26',
                    'scheduled_start': now - timedelta(days=10 - i * 3),
                    'scheduled_end': now - timedelta(days=9 - i * 3),
                },
            )
            for order, q in enumerate(questions):
                TestQuestion.objects.get_or_create(test=test, question=q, defaults={'order': order})

        Test.objects.get_or_create(
            title="National CEE-PG Mock '26 (New NEET Pattern)",
            group='grand',
            defaults={
                'duration_minutes': 210,
                'is_pro': True,
                'is_new': True,
                'price': 999,
                'academic_year': '2025-26',
                'scheduled_start': now + timedelta(days=3),
                'scheduled_end': now + timedelta(days=3, hours=4),
            },
        )

        # Mini tests (High yield)
        mini_topics = ['ECGs', 'Instruments & Surgical Procedures', 'Radiographs', 'Recent Updates']
        for i, topic in enumerate(mini_topics, start=3):
            questions = list(Question.objects.order_by('?')[:20])
            test, _ = Test.objects.get_or_create(
                title=f'High Yield Mini Test {i} - {topic}',
                group='mini',
                defaults={
                    'duration_minutes': 21,
                    'is_pro': True,
                    'academic_year': '2025-26',
                    'scheduled_start': now - timedelta(days=(7 - i) * 7),
                    'scheduled_end': now - timedelta(days=(7 - i) * 7 - 1),
                },
            )
            for order, q in enumerate(questions):
                TestQuestion.objects.get_or_create(test=test, question=q, defaults={'order': order})

        # Subject-wise tests
        for subject in subject_pool:
            questions = all_questions_by_subject.get(subject.slug, [])[:15]
            if not questions:
                continue
            test, _ = Test.objects.get_or_create(
                title=f'{subject.name} Subject Test',
                group='subject', subject=subject,
                defaults={'duration_minutes': 30, 'academic_year': '2025-26'},
            )
            for order, q in enumerate(questions):
                TestQuestion.objects.get_or_create(test=test, question=q, defaults={'order': order})

        self.stdout.write('Seeding home banner, MCQ of the day, announcement...')
        Banner.objects.get_or_create(
            title='National CEE-PG Mock \'26', defaults={
                'subtitle': 'Starts soon - know more',
                'tag': 'NEW PATTERN',
                'background_color': '#1447E6',
                'order': 0,
            },
        )
        MCQOfTheDay.objects.get_or_create(date=timezone.localdate(), defaults={'question': featured_q})
        Announcement.objects.get_or_create(
            message='Your plan is expiring soon.', defaults={'coupon_code': 'RENEW'},
        )

        self.stdout.write('Seeding public homepage content...')
        SiteSettings.load()  # creates the singleton with defaults if missing

        for order, (number, title, body) in enumerate([
            ('01', 'Concept-first QBank',
             'Every module is organised by subject, chapter and high-yield topic, so you build the concept '
             'before you drill the MCQs.'),
            ('02', "Solve, don't just read",
             'Since CEE-PG and NMCLE are computer-based, practising MCQs sharpens your educated guessing '
             'far more than passive reading does.'),
            ('03', 'Instant, detailed explanations',
             "Every question comes with a clear explanation — text, images and tables — right after you "
             "answer, not buried in a separate book."),
            ('04', 'Exam-pattern Test Series',
             "Grand, Mini and Subject tests replicate the real exam: timed, negatively marked, and shuffled "
             "so you're never memorising order."),
            ('05', 'Track your weak topics',
             'Subject and chapter-wise progress is visible right on your dashboard, so you always know what '
             'to revise next.'),
            ('06', "Built for Nepal's aspirants",
             'Course tracks for CEE-UG, CEE-PG and NMCLE — MBBS and BDS — with content mapped to the exams '
             "you're actually sitting."),
        ]):
            HomeFeature.objects.get_or_create(number=number, defaults={'title': title, 'body': body, 'order': order})

        for order, (label, url) in enumerate([
            ('Courses', '#courses'),
            ('Plans', '#courses'),
            ('Careers', 'mailto:atech1627@gmail.com?subject=Careers'),
            ('Contact', 'mailto:atech1627@gmail.com?subject=Contact'),
        ]):
            SiteLink.objects.get_or_create(section='nav', label=label, defaults={'url': url, 'order': order})

        for order, (label, url) in enumerate([
            ('Join as faculty', 'mailto:atech1627@gmail.com?subject=Faculty%20application'),
            ('Careers', 'mailto:atech1627@gmail.com?subject=Careers'),
            ('FAQs & Feedback', 'mailto:atech1627@gmail.com?subject=Feedback'),
            ('Contact Us', 'mailto:atech1627@gmail.com'),
        ]):
            SiteLink.objects.get_or_create(section='footer', label=label, defaults={'url': url, 'order': order})

        self.stdout.write('Seeding courses...')
        course_defs = [
            ('CEE-UG MBBS', 'MBBS', 'CEE-UG', '🩺', '#f59e0b',
             'Undergraduate entrance exam for MBBS admission, with the full QBank and subject-wise tests.'),
            ('CEE-UG BDS', 'BDS', 'CEE-UG', '🦷', '#14b8a6',
             'Undergraduate entrance exam for BDS admission, sharing the same high-yield question bank.'),
            ('CEE-PG MD/MS', 'MD', 'CEE-PG', '⚕️', '#8b5cf6',
             'Postgraduate entrance prep for MD/MS seats, with Grand Tests that mirror the real pattern.'),
            ('NMCLE (MBBS)', 'NMBBS', 'NMCLE', '📜', '#ec4899',
             'Nepal Medical Council Licensing Exam preparation for MBBS graduates.'),
            ('NMCLE (BDS)', 'NBDS', 'NMCLE', '📜', '#06b6d4',
             'Nepal Medical Council Licensing Exam preparation for BDS graduates.'),
        ]
        courses_by_prefix = {}
        for order, (name, prefix, group, icon, color, desc) in enumerate(course_defs):
            course, _ = Course.objects.get_or_create(
                prefix=prefix,
                defaults={
                    'name': name, 'program_group': group, 'order': order,
                    'icon': icon, 'color': color, 'description': desc,
                },
            )
            courses_by_prefix[prefix] = course
            CoursePackage.objects.get_or_create(
                course=course, name='Full Access Package (3 Months)',
                defaults={'price': 2999, 'duration_days': 90},
            )
            CoursePackage.objects.get_or_create(
                course=course, name='Full Access Package (Lifetime)',
                defaults={'price': 7999, 'duration_days': None},
            )

        mbbs_course = courses_by_prefix['MBBS']
        md_course = courses_by_prefix['MD']
        self.stdout.write('  linking existing questions to CEE-UG MBBS and CEE-PG MD/MS...')
        for question in Question.objects.all():
            question.courses.add(mbbs_course, md_course)


        self.stdout.write('Seeding role permissions...')
        from accounts.models import ALL_FEATURES, EDITOR_ALLOWED_FEATURES, RolePermission
        RolePermission.objects.get_or_create(role='admin', defaults={'features': ALL_FEATURES})
        RolePermission.objects.get_or_create(role='editor', defaults={'features': EDITOR_ALLOWED_FEATURES})

        self.stdout.write('Creating demo accounts...')
        if not User.objects.filter(email='admin@hamromentor.com').exists():
            User.objects.create_superuser(
                username='admin', email='admin@hamromentor.com', password='Admin@12345',
            )
            self.stdout.write(self.style.SUCCESS('  superuser: admin@hamromentor.com / Admin@12345'))
        User.objects.filter(email='admin@hamromentor.com').update(admin_role='super_admin')

        if not User.objects.filter(email='manager@hamromentor.com').exists():
            User.objects.create_user(
                username='manager', email='manager@hamromentor.com', password='Manager@123',
                first_name='Manager', is_staff=True, admin_role='admin',
            )
            self.stdout.write(self.style.SUCCESS('  admin: manager@hamromentor.com / Manager@123'))

        if not User.objects.filter(email='editor@hamromentor.com').exists():
            User.objects.create_user(
                username='content_editor', email='editor@hamromentor.com', password='Editor@123',
                first_name='Editor', is_staff=True, admin_role='editor',
            )
            self.stdout.write(self.style.SUCCESS('  editor: editor@hamromentor.com / Editor@123'))

        if not User.objects.filter(email='student@hamromentor.com').exists():
            student = User.objects.create_user(
                username='demo_student', email='student@hamromentor.com', password='Student@123',
                first_name='Demo', last_name='Student', program='CEE-PG', course='CEE-PG-MD',
            )
            from accounts.models import StudentProfile
            StudentProfile.objects.get_or_create(user=student, defaults={
                'college': 'Tribhuvan University Teaching Hospital',
                'district': 'Kathmandu', 'province': 'Bagmati', 'exam_target': 'CEE-PG', 'batch': '2026',
            })
            self.stdout.write(self.style.SUCCESS('  student: student@hamromentor.com / Student@123'))

        student = User.objects.filter(email='student@hamromentor.com').first()
        if student:
            self.stdout.write('Seeding enrollment + enrollment request for the demo student...')
            full_package = md_course.packages.filter(duration_days=90).first()
            Enrollment.objects.get_or_create(
                user=student, course=md_course,
                defaults={'package': full_package, 'access_type': 'package'},
            )
            EnrollmentRequest.objects.get_or_create(
                user=student, course=md_course, package=full_package,
                defaults={'status': 'approved', 'decided_at': timezone.now()},
            )
            mbbs_package = mbbs_course.packages.filter(duration_days=90).first()
            EnrollmentRequest.objects.get_or_create(
                user=student, course=mbbs_course, package=mbbs_package, defaults={'status': 'pending'},
            )

        self.stdout.write(self.style.SUCCESS('Done.'))
