"""
Substitution pay and the monthly payroll report.

Rules follow INSERVICE (app/server/policy.ts updateCoverage, app/ui.tsx calc):
- substitution pay per month = amount x days worked (per-day rate) or
  amount x days worked / working days in the month (per-month rate);
- base pay = salary x (scheduled days - unpaid days) / working days, where
  scheduled = working days inside the employment dates and unpaid = days
  covered by approved leave of an unpaid type;
- gross = base + substitution pay; net mode subtracts the pension
  contribution and income tax on (gross - pension).
Money is rounded half-up to 0.01 (like JS Math.round, unlike Python round).
"""
import csv
import io
from calendar import monthrange
from collections import defaultdict
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from django.contrib.auth import get_user_model
from django.db.models import Q
from django.utils import timezone

from .models import (
    LeaveRequest, LeaveRequestSubstitute, EmployeeCompensation, PayrollRun,
)
from .utils import working_dates, employment_start, employment_end, get_leave_settings

User = get_user_model()
CENT = Decimal('0.01')
ZERO = Decimal('0')


def round2(value):
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def parse_period(period):
    try:
        year, month = map(int, str(period).split('-'))
        first = date(year, month, 1)
    except (TypeError, ValueError):
        raise ValueError('Use YYYY-MM.')
    if len(str(period)) != 7:
        raise ValueError('Use YYYY-MM.')
    return first, date(year, month, monthrange(year, month)[1])


def period_of(day):
    return day.strftime('%Y-%m')


def locked_periods(tenant):
    return set(PayrollRun.objects.filter(tenant=tenant, is_locked=True).values_list('period', flat=True))


# ── substitution pay ────────────────────────────────────────────────────────

class WorkedDaysError(ValueError):
    pass


def _approved_days(user, start, end, tenant):
    days = set()
    for req in LeaveRequest.objects.filter(
        tenant=tenant, employee=user, status='approved',
        start_date__lte=end, end_date__gte=start,
    ).only('start_date', 'end_date'):
        current = max(req.start_date, start)
        last = min(req.end_date, end)
        while current <= last:
            days.add(current)
            current = date.fromordinal(current.toordinal() + 1)
    return days


def workable_days(row, tenant, today=None):
    """Days of the request a substitute could have covered (tickable days)."""
    req = row.leave_request
    today = today or timezone.localdate()
    end = min(req.end_date, today)
    if end < req.start_date:
        return []
    candidates = working_dates(req.start_date, end, tenant)
    sub = row.substitute
    start_emp, end_emp = employment_start(sub), employment_end(sub)
    own_leave = _approved_days(sub, req.start_date, end, tenant)
    return [
        d for d in candidates
        if (start_emp is None or d >= start_emp)
        and (end_emp is None or d <= end_emp)
        and d not in own_leave
    ]


def compute_totals(row, days, tenant):
    """{YYYY-MM: '12.34'} for the given worked days and the row's rate."""
    if row.rate_amount is None:
        return {}
    by_month = defaultdict(int)
    for d in days:
        by_month[period_of(d)] += 1
    totals = {}
    for month, count in sorted(by_month.items()):
        amount = Decimal(row.rate_amount) * count
        if row.rate_mode == 'month':
            first, last = parse_period(month)
            divisor = len(working_dates(first, last, tenant))
            amount = amount / divisor if divisor else ZERO
        totals[month] = str(round2(amount))
    return totals


def set_worked_days(row, raw_days, actor, tenant, today=None):
    """Validate and store the days a substitute actually worked."""
    from .audit import log_event

    if row.leave_request.status != 'approved':
        raise WorkedDaysError('Worked days can only be confirmed on approved requests.')
    if row.rate_amount is None:
        raise WorkedDaysError('Set a substitution rate for this task first.')
    if not isinstance(raw_days, (list, tuple)) or len(raw_days) > 366:
        raise WorkedDaysError('Send a list of dates (at most 366).')
    try:
        days = sorted({date.fromisoformat(str(d)) for d in raw_days})
    except ValueError:
        raise WorkedDaysError('Dates must be YYYY-MM-DD.')
    allowed = set(workable_days(row, tenant, today))
    bad = [str(d) for d in days if d not in allowed]
    if bad:
        raise WorkedDaysError(
            'These days cannot be confirmed (outside the leave, in the future, '
            'not a working day, or the substitute was away): ' + ', '.join(bad)
        )
    locked = locked_periods(tenant)
    old_days = {date.fromisoformat(d) for d in (row.worked_days or [])}
    touched = {period_of(d) for d in old_days.symmetric_difference(days)}
    if touched & locked:
        raise WorkedDaysError(
            'Payroll is locked for ' + ', '.join(sorted(touched & locked)) + '.'
        )
    before = {'days': row.worked_days, 'totals': row.totals_by_month}
    row.worked_days = [str(d) for d in days]
    row.totals_by_month = compute_totals(row, days, tenant)
    row.worked_confirmed_by = actor
    row.worked_confirmed_at = timezone.now()
    row.save(update_fields=[
        'worked_days', 'totals_by_month', 'worked_confirmed_by', 'worked_confirmed_at', 'updated_at'
    ])
    log_event(
        tenant, 'substitute', 'worked_days_set', actor=actor, leave_request=row.leave_request,
        subject_user=row.substitute, object_id=row.pk,
        data={'before': before, 'days': row.worked_days, 'totals': row.totals_by_month},
    )
    return row


def backfill_rate(assignment, rate):
    """Give the rate to the owner's request rows for this task that have none yet."""
    return LeaveRequestSubstitute.objects.filter(
        leave_request__tenant=assignment.tenant,
        leave_request__employee=assignment.owner,
        substitute=assignment.substitute, task=assignment.task,
        rate_amount__isnull=True,
    ).update(rate_mode=rate.mode, rate_amount=rate.amount, rate_currency=rate.currency)


# ── payroll report ──────────────────────────────────────────────────────────

def salary_for(user, last_day, compensations=None):
    """(amount, currency) effective on ``last_day``, or (None, None)."""
    rows = compensations if compensations is not None else EmployeeCompensation.objects.filter(user=user)
    best = None
    for comp in rows:
        if comp.user_id == user.pk and comp.effective_from <= last_day:
            if best is None or comp.effective_from > best.effective_from:
                best = comp
    return (best.monthly_salary, best.currency) if best else (None, None)


def build_report(tenant, period, user_ids=None, today=None):
    """The payroll report for ``period`` (computed live; see report_for)."""
    first, last = parse_period(period)
    today = today or timezone.localdate()
    settings_obj = get_leave_settings(tenant)
    mode = getattr(settings_obj, 'payroll_mode', 'gross') or 'gross'
    default_currency = getattr(settings_obj, 'payroll_currency', 'GEL') or 'GEL'
    tax_rate = Decimal(getattr(settings_obj, 'income_tax_rate', 20) or 0)
    pension_rate = Decimal(getattr(settings_obj, 'pension_rate', 2) or 0)
    working = working_dates(first, last, tenant)

    comps = list(EmployeeCompensation.objects.filter(tenant=tenant, effective_from__lte=last))
    sub_rows = list(
        LeaveRequestSubstitute.objects.filter(
            leave_request__tenant=tenant, leave_request__status='approved',
            totals_by_month__has_key=period,
        ).select_related('leave_request')
    )
    pay_by_user = defaultdict(lambda: defaultdict(Decimal))
    for row in sub_rows:
        currency = row.rate_currency or default_currency
        pay_by_user[row.substitute_id][currency] += Decimal(row.totals_by_month[period])

    wanted = {c.user_id for c in comps} | set(pay_by_user)
    if user_ids is not None:
        wanted &= set(user_ids)
    users = User.objects.filter(pk__in=wanted).filter(
        Q(employment_end_date__isnull=True) | Q(employment_end_date__gte=first)
    ).order_by('first_name', 'last_name', 'email')

    approved = defaultdict(list)
    for req in LeaveRequest.objects.filter(
        tenant=tenant, employee_id__in=wanted, status='approved',
        start_date__lte=last, end_date__gte=first,
    ).select_related('leave_type'):
        approved[req.employee_id].append(req)

    rows = []
    totals = defaultdict(lambda: defaultdict(Decimal))
    for user in users:
        start_emp, end_emp = employment_start(user), employment_end(user)
        scheduled = [
            d for d in working
            if (start_emp is None or d >= start_emp) and (end_emp is None or d <= end_emp)
        ]
        paid_leave = unpaid = 0
        covered = set()
        for req in approved[user.pk]:
            for d in scheduled:
                if req.start_date <= d <= req.end_date and d not in covered:
                    covered.add(d)
                    if req.leave_type.is_paid:
                        paid_leave += 1
                    else:
                        unpaid += 1
        worked = sum(1 for d in scheduled if d < today and d not in covered)
        salary, currency = salary_for(user, last, comps)
        currency = currency or default_currency
        base = (
            round2(Decimal(salary) * (len(scheduled) - unpaid) / len(working))
            if salary is not None and working else ZERO
        )
        user_pay = pay_by_user.get(user.pk, {})
        substitution_pay = round2(user_pay.get(currency, ZERO))
        other_pay = {c: str(round2(v)) for c, v in user_pay.items() if c != currency and v}
        gross = round2(base + substitution_pay)
        row = {
            'user': user.pk,
            'name': user.get_full_name() or user.email,
            'email': user.email,
            'job_title': user.job_title,
            'currency': currency,
            'monthly_salary': str(salary) if salary is not None else None,
            'working_days': len(working),
            'scheduled_days': len(scheduled),
            'worked_to_date': worked,
            'paid_leave_days': paid_leave,
            'unpaid_days': unpaid,
            'base_pay': str(base),
            'substitution_pay': str(substitution_pay),
            'other_currency_substitution_pay': other_pay,
            'gross': str(gross),
        }
        if mode == 'net':
            pension = round2(gross * pension_rate / 100)
            tax = round2((gross - pension) * tax_rate / 100)
            row.update({
                'pension': str(pension), 'income_tax': str(tax),
                'net': str(round2(gross - pension - tax)),
            })
        rows.append(row)
        for key in ('base_pay', 'substitution_pay', 'gross') + (
            ('pension', 'income_tax', 'net') if mode == 'net' else ()
        ):
            totals[currency][key] += Decimal(row[key])

    return {
        'period': period,
        'mode': mode,
        'currency': default_currency,
        'income_tax_rate': str(tax_rate),
        'pension_rate': str(pension_rate),
        'working_days': len(working),
        'locked': False,
        'generated_at': timezone.now().isoformat(),
        'rows': rows,
        'totals': {c: {k: str(round2(v)) for k, v in t.items()} for c, t in totals.items()},
    }


def report_for(tenant, period, today=None):
    """Locked months are served from their snapshot."""
    run = PayrollRun.objects.filter(tenant=tenant, period=period, is_locked=True).first()
    if run:
        data = dict(run.snapshot)
        data['locked'] = True
        data['locked_at'] = run.locked_at.isoformat() if run.locked_at else None
        data['locked_by'] = run.locked_by_id
        return data
    return build_report(tenant, period, today=today)


CSV_HEADERS = {
    'en': {
        'name': 'Employee', 'period': 'Month', 'currency': 'Currency',
        'monthly_salary': 'Monthly salary', 'working_days': 'Working days',
        'scheduled_days': 'Scheduled days', 'worked_to_date': 'Worked (to date)',
        'paid_leave_days': 'Paid leave', 'unpaid_days': 'Unpaid',
        'base_pay': 'Base pay', 'substitution_pay': 'Substitution pay', 'gross': 'Gross',
        'pension': 'Pension', 'income_tax': 'Income tax', 'net': 'Net',
    },
    'ka': {
        'name': 'თანამშრომელი', 'period': 'თვე', 'currency': 'ვალუტა',
        'monthly_salary': 'თვიური ხელფასი', 'working_days': 'სამუშაო დღეები',
        'scheduled_days': 'დაგეგმილი დღეები', 'worked_to_date': 'ნამუშევარი (დღევანდლამდე)',
        'paid_leave_days': 'ანაზღაურებადი შვებულება', 'unpaid_days': 'არაანაზღაურებადი',
        'base_pay': 'ძირითადი ხელფასი', 'substitution_pay': 'ჩანაცვლების ანაზღაურება',
        'gross': 'დარიცხული (ბრუტო)', 'pension': 'საპენსიო', 'income_tax': 'საშემოსავლო',
        'net': 'ხელზე (ნეტო)',
    },
}


def csv_bytes(report, lang='en'):
    """UTF-8 with BOM, every field quoted, CRLF (opens cleanly in Excel)."""
    headers = CSV_HEADERS.get(lang, CSV_HEADERS['en'])
    keys = ['name', 'period', 'currency', 'monthly_salary', 'working_days', 'scheduled_days',
            'worked_to_date', 'paid_leave_days', 'unpaid_days', 'base_pay',
            'substitution_pay', 'gross']
    if report.get('mode') == 'net':
        keys += ['pension', 'income_tax', 'net']
    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_ALL, lineterminator='\r\n')
    writer.writerow([headers[k] for k in keys])
    for row in report['rows']:
        values = dict(row, period=report['period'])
        writer.writerow(['' if values.get(k) is None else values.get(k) for k in keys])
    return ('﻿' + buf.getvalue()).encode('utf-8')
