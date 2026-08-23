from django.core.management.base import BaseCommand
from django.db.models import Sum
from django.utils import timezone

from academics.models import Question, QuestionAttempt, QuestionBankConfig


class Command(BaseCommand):
    """Batch-computes Question.actual_difficulty from real student
    performance (QuestionAttempt.attempts_count/correct_count) — never
    live, per the product brief's performance requirement (recomputing
    per-question difficulty on every page load doesn't scale). Run on a
    schedule (e.g. daily via Cloud Scheduler), same pattern as the existing
    subscription-expiry cron jobs. Never touches instructor_difficulty."""

    help = 'Recompute Question.actual_difficulty from aggregated QuestionAttempt data.'

    def handle(self, *args, **options):
        config = QuestionBankConfig.load()
        min_attempts = config.min_attempts_for_difficulty
        now = timezone.now()

        per_question = (
            QuestionAttempt.objects.values('question_id')
            .annotate(total_attempts=Sum('attempts_count'), total_correct=Sum('correct_count'))
            .filter(total_attempts__gte=min_attempts)
        )
        questions = {q.id: q for q in Question.objects.filter(id__in=[r['question_id'] for r in per_question])}

        updated = 0
        for row in per_question:
            question = questions.get(row['question_id'])
            if not question:
                continue
            pct_correct = round(row['total_correct'] / row['total_attempts'] * 100, 1)

            if pct_correct >= config.easy_min_pct:
                difficulty = 'easy'
            elif pct_correct >= config.medium_min_pct:
                difficulty = 'medium'
            elif pct_correct >= config.hard_min_pct:
                difficulty = 'hard'
            else:
                difficulty = 'very_hard'

            question.actual_difficulty = difficulty
            question.actual_difficulty_sample_size = row['total_attempts']
            question.actual_difficulty_updated_at = now
            question.save(update_fields=['actual_difficulty', 'actual_difficulty_sample_size', 'actual_difficulty_updated_at'])
            updated += 1

        self.stdout.write(self.style.SUCCESS(
            f'Recomputed actual_difficulty for {updated} question(s) with >= {min_attempts} attempts.'
        ))
