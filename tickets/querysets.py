"""Shared ticket querysets and annotations.

Counts are scalar ``Subquery`` expressions (``SELECT COUNT(id) FROM child
WHERE ticket_id = outer.id``) instead of joined ``Count()`` aggregates, so the
outer query never fans out across child tables and has no ``GROUP BY`` (R9).

``lite_card_queryset()`` is the single definition of what a kanban card
loads; board broadcasts (BE-08 ``build_card``) reuse it.
"""
import html
import re

from django.contrib.auth import get_user_model
from django.db.models import F, Func, IntegerField, OuterRef, Prefetch, Subquery, Value, TextField
from django.db.models.functions import Substr

from .models import ChecklistItem, Ticket, TicketAttachment, TicketComment

# How much of the (tag-stripped) description the database hands back for the
# card preview, and how long the preview itself is.
DESCRIPTION_HEAD_CHARS = 1000
DESCRIPTION_PREVIEW_CHARS = 200

_TAG_RE = re.compile(r'<[^>]*>')
_UNCLOSED_TAG_RE = re.compile(r'<[^>]*$')
_WS_RE = re.compile(r'\s+')


class _Count(Func):
    """COUNT(...) that Django does not treat as an aggregate, so a correlated
    subquery using it gets no GROUP BY."""
    function = 'COUNT'
    template = '%(function)s(%(expressions)s)'
    output_field = IntegerField()


def child_count_subquery(model, fk='ticket', **filters):
    """Scalar subquery counting ``model`` rows that point at the outer row."""
    qs = model.objects.filter(**{fk: OuterRef('pk')}, **filters).order_by()
    return Subquery(
        qs.annotate(_n=_Count(F('pk'))).values('_n')[:1],
        output_field=IntegerField(),
    )


def ticket_count_annotations(*, attachments=True, checklist=True, comments=True):
    """The card/list count annotations, all as scalar subqueries."""
    ann = {}
    if comments:
        ann['comments_count'] = child_count_subquery(TicketComment)
    if attachments:
        ann['attachments_count'] = child_count_subquery(TicketAttachment)
    if checklist:
        ann['checklist_items_count'] = child_count_subquery(ChecklistItem)
        ann['completed_checklist_items_count'] = child_count_subquery(ChecklistItem, is_checked=True)
    return ann


def description_head_expression():
    """First DESCRIPTION_HEAD_CHARS characters of the description with HTML
    tags removed in the database, so a huge description (e.g. an inline
    base64 image) never travels to Python (R8)."""
    stripped = Func(
        F('description'), Value('<[^>]*>'), Value(' '), Value('g'),
        function='regexp_replace', output_field=TextField(),
    )
    return Substr(stripped, 1, DESCRIPTION_HEAD_CHARS)


def make_description_preview(text):
    """Plain-text preview (<= DESCRIPTION_PREVIEW_CHARS) of an HTML or plain
    description, or of its database-side head."""
    if not text:
        return ''
    text = _TAG_RE.sub(' ', text)
    text = _UNCLOSED_TAG_RE.sub(' ', text)
    text = html.unescape(text)
    text = _WS_RE.sub(' ', text).strip()
    if len(text) > DESCRIPTION_PREVIEW_CHARS:
        text = text[:DESCRIPTION_PREVIEW_CHARS].rstrip()
    return text


def lite_card_queryset(base_qs=None):
    """Tickets shaped for ``TicketCardSerializer``: assignee/tag prefetches,
    the four subquery counts, the description head annotation, and the full
    description columns deferred."""
    User = get_user_model()
    qs = Ticket.objects.all() if base_qs is None else base_qs
    return (
        qs
        .select_related('assigned_to', 'column')
        .prefetch_related(
            Prefetch('assigned_users', queryset=User.objects.only('id', 'first_name', 'last_name', 'email')),
            'tags',
        )
        .annotate(
            _description_head=description_head_expression(),
            **ticket_count_annotations(),
        )
        .defer('description', 'rich_description')
    )
