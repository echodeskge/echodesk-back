"""
Per-recipient notification texts (English / Georgian).

Notification titles and messages are rendered in the *recipient's* language,
not the sender's or the server's. Callers hand ``create_notification`` a
:class:`LocalizedText` (catalog key + params) or a :class:`LazyText`
(``func(language) -> str``) instead of a finished string; ``create_notification``
renders it once per recipient (``user_language(user)``), so a fan-out to a
Georgian and an English colleague stores and pushes each one in their own
language. Plain strings (free text written by people: chat messages, ticket
titles, error details, …) pass through unchanged.

Language resolution (``user_language``):
    User.preferred_language ('en' | 'ka')
    → the tenant's preferred_language (when 'en' or 'ka')
    → 'ka'

This is an in-code catalog on purpose: the deploy does not compile gettext
``.mo`` files, and the texts are few. Georgian wording follows the web app's
``echodesk-frontend/src/i18n/messages/ka.json`` where it has an equivalent.
"""
from django.db import connection

SUPPORTED_LANGUAGES = ('en', 'ka')
DEFAULT_LANGUAGE = 'ka'


CATALOG = {
    # --- Generic -----------------------------------------------------------
    'common.batch_more': {'en': '(+{count} more)', 'ka': '(+{count} სხვა)'},
    'common.someone': {'en': 'Someone', 'ka': 'უცნობი მომხმარებელი'},

    # --- Tickets -----------------------------------------------------------
    'ticket.assigned.title': {'en': 'Assigned to: {ticket}', 'ka': 'დაგენიშნათ ტიკეტი: {ticket}'},
    'ticket.assigned.message': {
        'en': '{assigner} assigned you to this ticket as {role}',
        'ka': '{assigner} — დაგნიშნათ ამ ტიკეტზე როლით „{role}“',
    },
    'ticket.role.primary': {'en': 'Primary Assignee', 'ka': 'მთავარი შემსრულებელი'},
    'ticket.role.collaborator': {'en': 'Collaborator', 'ka': 'თანაშემსრულებელი'},
    'ticket.role.reviewer': {'en': 'Reviewer', 'ka': 'შემმოწმებელი'},
    'ticket.role.observer': {'en': 'Observer', 'ka': 'დამკვირვებელი'},
    'ticket.mentioned.title': {'en': 'Mentioned in: {ticket}', 'ka': 'მოგნიშნეს: {ticket}'},
    'ticket.mentioned.message': {
        'en': '{commenter} mentioned you in a comment',
        'ka': '{commenter} — მოგნიშნათ კომენტარში',
    },
    'ticket.commented.title': {'en': 'New comment on: {ticket}', 'ka': 'ახალი კომენტარი: {ticket}'},
    'ticket.commented.message': {
        'en': '{commenter} added a comment',
        'ka': '{commenter} — დაამატა კომენტარი',
    },
    'ticket.status_changed.title': {'en': 'Status changed: {ticket}', 'ka': 'სტატუსი შეიცვალა: {ticket}'},
    'ticket.status_changed.message': {
        'en': 'Ticket status changed from "{old}" to "{new}"',
        'ka': 'ტიკეტის სტატუსი შეიცვალა: „{old}“ → „{new}“',
    },
    'ticket.department_assigned.title': {
        'en': 'Department assigned: {ticket}',
        'ka': 'ტიკეტი გადაეცა დეპარტამენტს: {ticket}',
    },
    'ticket.department_assigned.message': {
        'en': 'Ticket assigned to your department "{department}"',
        'ka': 'ტიკეტი გადაეცა თქვენს დეპარტამენტს „{department}“',
    },

    # --- Bug reports (same wording as the web's notificationList.bugReport) -
    'bug_report.received.title': {'en': 'Bug report received', 'ka': 'ბაგ რეპორტი მიღებულია'},
    'bug_report.received.message': {
        'en': 'Your bug report has been received and is scheduled for development.',
        'ka': 'თქვენი ბაგ რეპორტი მიღებულია და დაგეგმილია გამოსწორებისთვის.',
    },
    'bug_report.in_progress.title': {'en': 'Bug fix in progress', 'ka': 'ბაგის გამოსწორება მიმდინარეობს'},
    'bug_report.in_progress.message': {
        'en': 'We are currently working on fixing your reported bug.',
        'ka': 'ჩვენ ამჟამად ვმუშაობთ თქვენი ბაგის გამოსწორებაზე.',
    },
    'bug_report.fixed.title': {'en': 'Bug report fixed', 'ka': 'ბაგი გამოსწორებულია'},
    'bug_report.fixed.message': {
        'en': 'Your reported bug has been fixed. Thank you for the report!',
        'ka': 'თქვენი ბაგი გამოსწორებულია. მადლობა რეპორტისთვის!',
    },
    'bug_report.update.title': {'en': 'Bug report update', 'ka': 'ბაგ რეპორტის განახლება'},
    'bug_report.update.message': {
        'en': 'Your bug report status has been updated.',
        'ka': 'თქვენი ბაგ რეპორტის სტატუსი განახლდა.',
    },

    # --- Invoices ----------------------------------------------------------
    'invoice.a_client': {'en': 'a client', 'ka': 'კლიენტი'},
    'invoice.client': {'en': 'Client', 'ka': 'კლიენტი'},
    'invoice.created.title': {'en': 'Invoice {number} created', 'ka': 'ინვოისი {number} შეიქმნა'},
    'invoice.created.message': {
        'en': 'New invoice for {client} totalling {total} {currency}',
        'ka': 'ახალი ინვოისი — {client}, ჯამი: {total} {currency}',
    },
    'invoice.paid.title': {'en': 'Invoice {number} paid', 'ka': 'ინვოისი {number} გადახდილია'},
    'invoice.paid.message': {
        'en': '{client} has paid invoice {number}',
        'ka': '{client} — ინვოისი {number} გადახდილია',
    },
    'invoice.overdue.title': {'en': 'Invoice {number} overdue', 'ka': 'ინვოისი {number} ვადაგადაცილებულია'},
    'invoice.overdue.message': {
        'en': 'Invoice for {client} is now overdue',
        'ka': 'ინვოისი ({client}) ვადაგადაცილებულია',
    },

    # --- Leave -------------------------------------------------------------
    'leave.submitted.title': {'en': 'New leave request', 'ka': 'შვებულების ახალი მოთხოვნა'},
    'leave.submitted.message': {
        'en': '{employee} submitted a leave request ({leave_type})',
        'ka': '{employee} — გამოაგზავნა შვებულების მოთხოვნა ({leave_type})',
    },
    # "approved by {stage}"
    'leave.stage.manager': {'en': 'manager', 'ka': 'მენეჯერმა'},
    'leave.stage.hr': {'en': 'HR', 'ka': 'HR-მა'},
    # "awaits {next} approval" / "awaiting {next} approval"
    'leave.next.manager': {'en': 'manager', 'ka': 'მენეჯერის დამტკიცებას'},
    'leave.next.hr': {'en': 'HR', 'ka': 'HR-ის დამტკიცებას'},
    'leave.next.admin': {'en': 'admin', 'ka': 'ადმინისტრატორის დამტკიცებას'},
    'leave.next.final': {'en': 'final', 'ka': 'საბოლოო დამტკიცებას'},
    'leave.progress.title': {
        'en': 'Leave request partially approved',
        'ka': 'შვებულების მოთხოვნა ნაწილობრივ დამტკიცდა',
    },
    'leave.progress.message': {
        'en': 'Your leave request for {period} was approved by {stage}; it now awaits {next} approval',
        'ka': 'თქვენი შვებულების მოთხოვნა ({period}) დაამტკიცა {stage}; ახლა ელოდება {next}',
    },
    'leave.awaiting.title': {
        'en': 'Leave request awaiting {next} approval',
        'ka': 'შვებულების მოთხოვნა ელოდება {next}',
    },
    'leave.awaiting.message': {
        'en': '{employee} has a leave request ({leave_type}) for {period} waiting for your approval',
        'ka': '{employee} — შვებულების მოთხოვნა ({leave_type}, {period}) ელოდება თქვენს დამტკიცებას',
    },
    'leave.approved.title': {'en': 'Leave request approved', 'ka': 'შვებულების მოთხოვნა დამტკიცდა'},
    'leave.approved.message': {
        'en': 'Your leave request for {period} has been approved',
        'ka': 'თქვენი შვებულების მოთხოვნა ({period}) დამტკიცდა',
    },
    'leave.rejected.title': {'en': 'Leave request rejected', 'ka': 'შვებულების მოთხოვნა უარყოფილია'},
    'leave.rejected.message': {
        'en': 'Your leave request for {period} has been rejected',
        'ka': 'თქვენი შვებულების მოთხოვნა ({period}) უარყოფილია',
    },
    'leave.cancelled.title': {'en': 'Leave request cancelled', 'ka': 'შვებულების მოთხოვნა გაუქმდა'},
    'leave.cancelled.message': {
        'en': 'Your leave request for {period} has been cancelled',
        'ka': 'თქვენი შვებულების მოთხოვნა ({period}) გაუქმდა',
    },
    'leave.cancelled.message_approver': {
        'en': "{employee}'s leave request for {period} has been cancelled",
        'ka': '{employee} — შვებულების მოთხოვნა ({period}) გაუქმდა',
    },

    # --- Calls -------------------------------------------------------------
    'call.missed.title': {'en': 'Missed call', 'ka': 'გამოტოვებული ზარი'},
    'call.missed.message': {
        'en': 'Missed inbound call from {number}',
        'ka': 'გამოტოვებული შემომავალი ზარი ნომრიდან {number}',
    },

    # --- E-commerce --------------------------------------------------------
    'order.a_customer': {'en': 'A customer', 'ka': 'მომხმარებელი'},
    'order.created.title': {'en': 'New order {number}', 'ka': 'ახალი შეკვეთა {number}'},
    'order.created.message': {
        'en': '{name} ordered {amount} {currency} (cash on delivery)',
        'ka': '{name} — შეკვეთა {amount} {currency} (ადგილზე გადახდა)',
    },
    'order.paid.title': {'en': 'Order {number} paid', 'ka': 'შეკვეთა {number} გადახდილია'},
    'order.paid.message': {
        'en': '{name} paid {amount} {currency}',
        'ka': '{name} — გადახდილია {amount} {currency}',
    },
    'stock.low_one.title': {'en': 'Low stock: {name}', 'ka': 'დაბალი მარაგი: {name}'},
    'stock.low_one.message': {
        'en': '{sku} has {quantity} left (alert at {threshold})',
        'ka': '{sku} — დარჩა {quantity} (ზღვარი: {threshold})',
    },
    'stock.low_many.title': {
        'en': '{count} products are low on stock',
        'ka': '{count} პროდუქტს დაბალი მარაგი აქვს',
    },
    'stock.low_many.message': {'en': '{names}', 'ka': '{names}'},
    'stock.low_many.message_more': {
        'en': '{names} and {count} more',
        'ka': '{names} და კიდევ {count}',
    },

    # --- Social inbox ------------------------------------------------------
    'social.platform.Widget': {'en': 'Widget', 'ka': 'ვებსაიტის ვიჯეტი'},
    'social.platform.Email': {'en': 'Email', 'ka': 'ელ-ფოსტა'},
    'social.message.title': {
        'en': 'New {platform} message from {sender}',
        'ka': 'ახალი შეტყობინება ({platform}): {sender}',
    },
    'social.widget.visitor': {'en': 'Website visitor {visitor}', 'ka': 'ვებსაიტის ვიზიტორი {visitor}'},
    'social.widget.attachment': {'en': 'Sent an attachment', 'ka': 'გამოგზავნა ფაილი'},
    'social.a_customer': {'en': 'a customer', 'ka': 'მომხმარებელი'},
    'social.ai_handoff.title': {
        'en': 'AI needs a human: {platform} chat with {customer}',
        'ka': 'AI-ს ოპერატორი სჭირდება: {platform} ჩატი — {customer}',
    },
    'social.ai_handoff.default_reason': {
        'en': 'The AI assistant asked for a human to take over.',
        'ka': 'AI ასისტენტმა ოპერატორის ჩართვა მოითხოვა.',
    },
    'auto_post.draft.title': {
        'en': 'New AI post waiting for approval',
        'ka': 'ახალი AI პოსტი ელოდება დამტკიცებას',
    },
    'auto_post.draft.default': {
        'en': 'An auto-generated post needs your review.',
        'ka': 'ავტომატურად შექმნილი პოსტი თქვენს განხილვას საჭიროებს.',
    },
    'auto_post.failed.title': {
        'en': 'Auto-post failed to publish',
        'ka': 'ავტო-პოსტის გამოქვეყნება ვერ მოხერხდა',
    },
    'auto_post.failed.default': {'en': 'Publishing failed.', 'ka': 'გამოქვეყნება ვერ მოხერხდა.'},
    'whatsapp.template.title': {'en': 'WhatsApp template reviewed', 'ka': 'WhatsApp შაბლონი განხილულია'},
    'whatsapp.template.message': {
        'en': 'Template "{name}" ({language}) is now {status}.',
        'ka': 'შაბლონის „{name}“ ({language}) სტატუსი: {status}.',
    },
    'whatsapp.template.message_reason': {
        'en': 'Template "{name}" ({language}) is now {status}. Reason: {reason}',
        'ka': 'შაბლონის „{name}“ ({language}) სტატუსი: {status}. მიზეზი: {reason}',
    },
    'whatsapp.status.approved': {'en': 'approved', 'ka': 'დამტკიცებული'},
    'whatsapp.status.rejected': {'en': 'rejected', 'ka': 'უარყოფილი'},
    'whatsapp.status.pending': {'en': 'pending', 'ka': 'განხილვაში'},
    'whatsapp.status.paused': {'en': 'paused', 'ka': 'შეჩერებული'},
    'whatsapp.status.disabled': {'en': 'disabled', 'ka': 'გამორთული'},
    'whatsapp.status.in appeal': {'en': 'in appeal', 'ka': 'გასაჩივრებული'},
    'email_sync.disabled.title': {
        'en': 'Email sync disabled: {email}',
        'ka': 'ელ-ფოსტის სინქრონიზაცია გაითიშა: {email}',
    },
    'email_sync.disabled.message': {
        'en': (
            'Automatic IMAP sync for {email} was disabled after an error. Go to '
            'email settings to fix credentials and reactivate. Error: {error}'
        ),
        'ka': (
            '{email} — ავტომატური IMAP სინქრონიზაცია შეცდომის გამო გაითიშა. '
            'გადადით ელ-ფოსტის პარამეტრებში, შეასწორეთ მონაცემები და ხელახლა '
            'ჩართეთ. შეცდომა: {error}'
        ),
    },
    'telegram.disconnected.title': {
        'en': 'Telegram account disconnected',
        'ka': 'Telegram ანგარიში გაითიშა',
    },
    'telegram.disconnected.message': {
        'en': (
            'The Telegram account {label} was disconnected ({reason}). '
            'Reconnect it from Settings → Social → Telegram.'
        ),
        'ka': (
            'Telegram ანგარიში {label} გაითიშა ({reason}). ხელახლა დააკავშირეთ: '
            'პარამეტრები → სოციალური მედია → Telegram.'
        ),
    },

    # --- Team chat ---------------------------------------------------------
    'team_chat.photo': {'en': 'Sent a photo', 'ka': 'გამოგზავნა ფოტო'},
    'team_chat.voice': {'en': 'Sent a voice message', 'ka': 'გამოგზავნა ხმოვანი შეტყობინება'},
    'team_chat.file': {'en': 'Sent a file', 'ka': 'გამოგზავნა ფაილი'},
    'team_chat.file_named': {'en': 'Sent a file: {name}', 'ka': 'გამოგზავნა ფაილი: {name}'},
}


def normalize_language(language):
    """'ka' / 'en' (also from 'ka-GE', 'en-US'), or None."""
    if not language:
        return None
    code = str(language).strip().lower().replace('_', '-').split('-')[0]
    return code if code in SUPPORTED_LANGUAGES else None


class LocalizedText:
    """A catalog key + params, rendered later in the recipient's language.

    Param values may themselves be LocalizedText / LazyText / {'en','ka'}
    dicts; they are rendered in the same language.
    """

    __slots__ = ('key', 'params')

    def __init__(self, key, **params):
        if key not in CATALOG:
            raise KeyError(f'Unknown notification text key: {key}')
        self.key = key
        self.params = params

    def render(self, language):
        entry = CATALOG[self.key]
        template = entry.get(language) or entry['en']
        params = {k: render(v, language) if _is_localizable(v) else v
                  for k, v in self.params.items()}
        return template.format(**params) if params else template

    def __repr__(self):
        return f'LocalizedText({self.key!r}, **{self.params!r})'


class LazyText:
    """``func(language) -> str``, for texts built from language-dependent
    pieces outside the catalog (e.g. booking texts, multilingual names)."""

    __slots__ = ('func',)

    def __init__(self, func):
        self.func = func

    def render(self, language):
        return self.func(language)


def T(key, **params):
    """Shorthand for ``LocalizedText(key, **params)``."""
    return LocalizedText(key, **params)


def truncate(value, length):
    """``value`` cut to ``length`` characters after rendering."""
    if isinstance(value, str):
        return value[:length]
    return LazyText(lambda language: render(value, language)[:length])


def _is_localizable(value):
    return isinstance(value, (LocalizedText, LazyText, dict))


def render(value, language):
    """Render a notification text in ``language``. Plain strings pass through."""
    if value is None:
        return ''
    if isinstance(value, (LocalizedText, LazyText)):
        return value.render(language)
    if isinstance(value, dict):
        # A multilingual model value ({"en": ..., "ka": ...}).
        for key in (language, 'ka', 'en'):
            if value.get(key):
                return str(value[key])
        return next((str(v) for v in value.values() if v), '')
    return str(value)


def tenant_language(schema_name=None):
    """The current tenant's preferred_language when it is 'en'/'ka', else 'ka'."""
    schema_name = schema_name or getattr(connection, 'schema_name', None)
    language = None
    # Always read it fresh (one indexed query): a cached connection.tenant
    # may predate a language change made in the same process.
    if schema_name and schema_name != 'public':
        try:
            from tenants.models import Tenant
            language = (
                Tenant.objects.filter(schema_name=schema_name)
                .values_list('preferred_language', flat=True).first()
            )
        except Exception:  # noqa: BLE001 - never fail a notification over this
            language = None
    return normalize_language(language) or DEFAULT_LANGUAGE


def user_language(user, schema_name=None):
    """Language a notification to ``user`` is written in."""
    return (
        normalize_language(getattr(user, 'preferred_language', None))
        or tenant_language(schema_name)
    )
