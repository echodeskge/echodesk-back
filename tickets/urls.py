from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    TicketViewSet, TagViewSet, TicketCommentViewSet, TicketColumnViewSet,
    ChecklistItemViewSet, TicketAssignmentViewSet,
    TicketTimeLogViewSet, BoardViewSet, TicketPaymentViewSet,
    ItemListViewSet, ListItemViewSet, TicketFormViewSet, TicketFormSubmissionViewSet,
    TicketAttachmentViewSet
)
from .support_views import report_bug
from .views_timer import TicketTimerViewSet, TimeEntryViewSet

# Create router and register viewsets
router = DefaultRouter()
router.register(r'tickets', TicketViewSet, basename='ticket')
router.register(r'boards', BoardViewSet, basename='board')
router.register(r'columns', TicketColumnViewSet, basename='ticketcolumn')
router.register(r'tags', TagViewSet, basename='tag')
router.register(r'comments', TicketCommentViewSet, basename='ticketcomment')
router.register(r'checklist-items', ChecklistItemViewSet, basename='checklistitem')
router.register(r'time-logs', TicketTimeLogViewSet, basename='tickettimelog')
router.register(r'payments', TicketPaymentViewSet, basename='ticketpayment')

# New registrations for dynamic lists and forms
router.register(r'item-lists', ItemListViewSet, basename='itemlist')
router.register(r'list-items', ListItemViewSet, basename='listitem')
router.register(r'ticket-forms', TicketFormViewSet, basename='ticketform')
router.register(r'form-submissions', TicketFormSubmissionViewSet, basename='formsubmission')
router.register(r'attachments', TicketAttachmentViewSet, basename='ticketattachment')
router.register(r'time-entries', TimeEntryViewSet, basename='tickettimeentry')

app_name = 'tickets'

urlpatterns = [
    # Personal timer (manual nested routes, like assignments below)
    path('api/tickets/<int:id>/timer/', TicketTimerViewSet.as_view({'get': 'retrieve'}), name='ticket-timer'),
    path('api/tickets/<int:id>/timer/start/', TicketTimerViewSet.as_view({'post': 'start'}), name='ticket-timer-start'),
    path('api/tickets/<int:id>/timer/stop/', TicketTimerViewSet.as_view({'post': 'stop'}), name='ticket-timer-stop'),
    path('api/', include(router.urls)),
    # Manual nested URLs for assignments
    path('api/tickets/<int:ticket_pk>/assignments/', TicketAssignmentViewSet.as_view({'get': 'list', 'post': 'create'}), name='ticket-assignments-list'),
    path('api/tickets/<int:ticket_pk>/assignments/<int:pk>/', TicketAssignmentViewSet.as_view({'get': 'retrieve', 'put': 'update', 'patch': 'partial_update', 'delete': 'destroy'}), name='ticket-assignments-detail'),
    path('api/tickets/<int:ticket_pk>/assignments/bulk_assign/', TicketAssignmentViewSet.as_view({'post': 'bulk_assign'}), name='ticket-assignments-bulk-assign'),
    path('api/tickets/<int:ticket_pk>/assignments/bulk_unassign/', TicketAssignmentViewSet.as_view({'delete': 'bulk_unassign'}), name='ticket-assignments-bulk-unassign'),
    path('api/support/report-bug/', report_bug, name='report-bug'),
]

# Example URLConf for including in main router:
# In your main urls.py, include this app like:
# path('tickets/', include('tickets.urls')),
#
# This will create the following endpoints:
# - GET/POST /tickets/api/tickets/ - List/Create tickets
# - GET/PUT/PATCH/DELETE /tickets/api/tickets/{id}/ - Retrieve/Update/Delete ticket
# - POST /tickets/api/tickets/{id}/add_comment/ - Add comment to ticket
# - GET /tickets/api/tickets/{id}/comments/ - Get ticket comments
# - GET /tickets/api/tickets/my_tickets/ - Get current user's tickets
# - GET /tickets/api/tickets/assigned_to_me/ - Get tickets assigned to current user
# - PATCH /tickets/api/tickets/{id}/assign/ - Assign ticket (staff only)
# - GET/POST /tickets/api/tags/ - List/Create tags
# - GET/PUT/PATCH/DELETE /tickets/api/tags/{id}/ - Retrieve/Update/Delete tag
# - GET/POST /tickets/api/comments/ - List/Create comments
# - GET/PUT/PATCH/DELETE /tickets/api/comments/{id}/ - Retrieve/Update/Delete comment
