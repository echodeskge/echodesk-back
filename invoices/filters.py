"""
django-filter FilterSets for the invoice API.

Declaring the list filters here (instead of ad-hoc ``request.query_params``
reads in the view) makes every parameter show up in the OpenAPI schema with
the right type, so the generated TypeScript clients know about them.
"""
import django_filters

from ecommerce_crm.models import EcommerceClient
from tickets.models import ListItem

from .models import Invoice


class InvoiceFilterSet(django_filters.FilterSet):
    status = django_filters.ChoiceFilter(
        choices=Invoice.STATUS_CHOICES,
        method='filter_status',
        help_text=(
            "Filter by status. `overdue` is computed, not stored: it returns open "
            "invoices (sent / viewed / partially_paid) whose due_date is in the "
            "past, exactly like `overdue=true`."
        ),
    )
    client = django_filters.ModelChoiceFilter(
        queryset=EcommerceClient.objects.all(),
        help_text="Legacy e-commerce client id (invoices created from the client ItemList have client=null).",
    )
    client_itemlist_item = django_filters.ModelChoiceFilter(
        queryset=ListItem.objects.all(),
        help_text="Client ListItem id (for tenants whose invoice clients come from an ItemList).",
    )
    currency = django_filters.CharFilter(help_text="ISO 4217 code, e.g. GEL, USD.")
    date_from = django_filters.DateFilter(
        field_name='issue_date', lookup_expr='gte',
        help_text="Issue date lower bound (YYYY-MM-DD, inclusive).",
    )
    date_to = django_filters.DateFilter(
        field_name='issue_date', lookup_expr='lte',
        help_text="Issue date upper bound (YYYY-MM-DD, inclusive).",
    )
    overdue = django_filters.BooleanFilter(
        method='filter_overdue',
        help_text=(
            "`true` keeps only overdue invoices: status in sent / viewed / "
            "partially_paid and due_date < today. Same result as `status=overdue`. "
            "`false` is ignored."
        ),
    )

    class Meta:
        model = Invoice
        fields = ['status', 'client', 'client_itemlist_item', 'currency', 'date_from', 'date_to', 'overdue']

    def filter_status(self, queryset, name, value):
        if not value:
            return queryset
        if value == 'overdue':
            return queryset.filter(Invoice.overdue_q())
        return queryset.filter(status=value)

    def filter_overdue(self, queryset, name, value):
        if value is True:
            return queryset.filter(Invoice.overdue_q())
        return queryset
