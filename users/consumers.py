"""
WebSocket consumers for real-time functionality.
Includes notification updates and ticket board collaboration.
"""

import json
import asyncio
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.contrib.auth.models import AnonymousUser
from tenant_schemas.utils import schema_context
from .models import Notification, UserOnlineStatus, TeamChatConversation, TeamChatMessage


class NotificationConsumer(AsyncWebsocketConsumer):
    """
    WebSocket consumer for real-time notification updates.
    Handles new notifications, read status updates, and unread count changes.
    """

    async def connect(self):
        self.tenant_schema = self.scope['url_route']['kwargs']['tenant_schema']
        self.user = self.scope.get('user', AnonymousUser())

        print(f"[NotificationWS] Connection attempt - User: {self.user}, Tenant: {self.tenant_schema}")

        # Only allow authenticated users to receive notifications
        if self.user.is_anonymous:
            print(f"[NotificationWS] Rejecting connection - User not authenticated")
            await self.accept()
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': 'Authentication required',
                'code': 'UNAUTHENTICATED'
            }))
            await self.close(code=4001)
            return

        # Join the notifications group for this specific user in this tenant
        # This ensures users only receive their own notifications
        self.notifications_group_name = f'notifications_{self.tenant_schema}_{self.user.id}'

        await self.channel_layer.group_add(
            self.notifications_group_name,
            self.channel_name
        )

        await self.accept()

        # Get initial unread count
        unread_count = await self.get_unread_count()

        # Send initial connection confirmation with unread count
        await self.send(text_data=json.dumps({
            'type': 'connection',
            'status': 'connected',
            'tenant': self.tenant_schema,
            'user_id': self.user.id,
            'unread_count': unread_count
        }))

        print(f"[NotificationWS] Connected successfully for user {self.user.email} in tenant {self.tenant_schema}")

    async def disconnect(self, close_code):
        # Leave the notifications group
        if hasattr(self, 'notifications_group_name'):
            try:
                await self.channel_layer.group_discard(
                    self.notifications_group_name,
                    self.channel_name
                )
            except Exception as e:
                # Redis/channel layer can already be gone if the socket dropped abruptly.
                # Avoid bubbling disconnect cleanup errors to Sentry.
                print(f"[NotificationWS] group_discard failed during disconnect: {e}")

            print(f"[NotificationWS] Disconnected user {self.user.id if not self.user.is_anonymous else 'anonymous'}")

    async def receive(self, text_data):
        """Handle incoming WebSocket messages from client"""
        try:
            data = json.loads(text_data)
            message_type = data.get('type')

            if message_type == 'ping':
                # Respond to ping with pong for connection health check
                await self.send(text_data=json.dumps({
                    'type': 'pong',
                    'timestamp': data.get('timestamp')
                }))

            elif message_type == 'mark_read':
                # Mark a notification as read
                notification_id = data.get('notification_id')
                if notification_id:
                    success = await self.mark_notification_read(notification_id)
                    if success:
                        # Get updated unread count
                        unread_count = await self.get_unread_count()

                        # Send confirmation
                        await self.send(text_data=json.dumps({
                            'type': 'notification_read',
                            'notification_id': notification_id,
                            'unread_count': unread_count
                        }))

            elif message_type == 'mark_all_read':
                # Mark all notifications as read
                count = await self.mark_all_notifications_read()

                # Send confirmation
                await self.send(text_data=json.dumps({
                    'type': 'all_notifications_read',
                    'marked_count': count,
                    'unread_count': 0
                }))

            elif message_type == 'get_unread_count':
                # Get current unread count
                unread_count = await self.get_unread_count()

                await self.send(text_data=json.dumps({
                    'type': 'unread_count',
                    'count': unread_count
                }))

        except json.JSONDecodeError:
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': 'Invalid JSON format'
            }))
        except Exception as e:
            print(f"[NotificationWS] Error handling message: {str(e)}")
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': 'Internal server error'
            }))

    # Handlers for messages sent from Django views/signals
    async def notification_created(self, event):
        """Send new notification to WebSocket"""
        await self.send(text_data=json.dumps({
            'type': 'notification_created',
            'notification': event['notification'],
            'unread_count': event.get('unread_count', 1)
        }))

    async def notification_read(self, event):
        """Send notification read status update to WebSocket"""
        await self.send(text_data=json.dumps({
            'type': 'notification_read',
            'notification_id': event['notification_id'],
            'unread_count': event.get('unread_count', 0)
        }))

    async def unread_count_update(self, event):
        """Send unread count update to WebSocket"""
        await self.send(text_data=json.dumps({
            'type': 'unread_count_update',
            'count': event['count']
        }))

    # Database operations
    @database_sync_to_async
    def get_unread_count(self):
        """Get the count of unread notifications for the current user (cached)."""
        with schema_context(self.tenant_schema):
            from users.notification_utils import get_unread_count as _cached_count
            return _cached_count(self.user, self.tenant_schema)

    @database_sync_to_async
    def mark_notification_read(self, notification_id):
        """Mark a single notification as read"""
        try:
            with schema_context(self.tenant_schema):
                from django.utils import timezone
                from users.notification_utils import decrement_unread
                notification = Notification.objects.get(
                    id=notification_id,
                    user=self.user
                )
                if not notification.is_read:
                    notification.is_read = True
                    notification.read_at = timezone.now()
                    notification.save(update_fields=['is_read', 'read_at'])
                    decrement_unread(self.user, self.tenant_schema)
                return True
        except Notification.DoesNotExist:
            return False
        except Exception as e:
            print(f"[NotificationWS] Error marking notification {notification_id} as read: {str(e)}")
            return False

    @database_sync_to_async
    def mark_all_notifications_read(self):
        """Mark all unread notifications as read"""
        try:
            with schema_context(self.tenant_schema):
                from django.utils import timezone
                from users.notification_utils import reset_unread
                count = Notification.objects.filter(
                    user=self.user,
                    is_read=False
                ).update(
                    is_read=True,
                    read_at=timezone.now()
                )
                reset_unread(self.user, self.tenant_schema)
                return count
        except Exception as e:
            print(f"[NotificationWS] Error marking all notifications as read: {str(e)}")
            return 0


# Utility functions for sending WebSocket messages from Django views/signals
async def send_notification_to_user(tenant_schema, user_id, notification_data, unread_count=None):
    """
    Send new notification to a specific user via WebSocket.
    Call this from signals when a new notification is created.

    Args:
        tenant_schema: The tenant schema name
        user_id: The user ID to send notification to
        notification_data: Dictionary containing notification data
        unread_count: Optional unread count to include in the message
    """
    from channels.layers import get_channel_layer

    channel_layer = get_channel_layer()

    # Send to user's notification group
    await channel_layer.group_send(
        f'notifications_{tenant_schema}_{user_id}',
        {
            'type': 'notification_created',
            'notification': notification_data,
            'unread_count': unread_count
        }
    )


async def send_notification_read_update(tenant_schema, user_id, notification_id, unread_count=None):
    """
    Send notification read status update to a specific user via WebSocket.

    Args:
        tenant_schema: The tenant schema name
        user_id: The user ID
        notification_id: The notification ID that was read
        unread_count: Optional updated unread count
    """
    from channels.layers import get_channel_layer

    channel_layer = get_channel_layer()

    await channel_layer.group_send(
        f'notifications_{tenant_schema}_{user_id}',
        {
            'type': 'notification_read',
            'notification_id': notification_id,
            'unread_count': unread_count
        }
    )


async def send_unread_count_update(tenant_schema, user_id, count):
    """
    Send unread count update to a specific user via WebSocket.

    Args:
        tenant_schema: The tenant schema name
        user_id: The user ID
        count: The updated unread count
    """
    from channels.layers import get_channel_layer

    channel_layer = get_channel_layer()

    await channel_layer.group_send(
        f'notifications_{tenant_schema}_{user_id}',
        {
            'type': 'unread_count_update',
            'count': count
        }
    )


class TicketBoardConsumer(AsyncWebsocketConsumer):
    """
    WebSocket consumer for real-time ticket board collaboration.
    Handles ticket movements, updates, and user presence on Kanban boards.
    """

    async def connect(self):
        self.tenant_schema = self.scope['url_route']['kwargs']['tenant_schema']
        self.board_id = self.scope['url_route']['kwargs'].get('board_id', 'default')
        self.user = self.scope.get('user', AnonymousUser())

        print(f"[TicketBoardWS] Connection attempt - User: {self.user}, Board: {self.board_id}, Tenant: {self.tenant_schema}")

        # Only allow authenticated users
        if self.user.is_anonymous:
            print(f"[TicketBoardWS] Rejecting connection - User not authenticated")
            await self.accept()
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': 'Authentication required',
                'code': 'UNAUTHENTICATED'
            }))
            await self.close(code=4001)
            return

        # Join the board group for this specific board in this tenant
        self.board_group_name = f'board_{self.tenant_schema}_{self.board_id}'

        await self.channel_layer.group_add(
            self.board_group_name,
            self.channel_name
        )

        await self.accept()

        # Add user to presence tracking
        await self.add_user_presence()

        # Get current board state
        board_data = await self.get_board_data()
        active_users = await self.get_active_users()

        # Send initial connection confirmation with board data
        await self.send(text_data=json.dumps({
            'type': 'connection',
            'status': 'connected',
            'tenant': self.tenant_schema,
            'board_id': self.board_id,
            'user_id': self.user.id,
            'user_name': self.user.get_full_name() or self.user.email,
            'active_users': active_users
        }))

        # Notify other users that this user joined
        await self.channel_layer.group_send(
            self.board_group_name,
            {
                'type': 'user_joined',
                'user_id': self.user.id,
                'user_name': self.user.get_full_name() or self.user.email,
                'user_email': self.user.email,
                'exclude_channel': self.channel_name
            }
        )

        print(f"[TicketBoardWS] Connected successfully for user {self.user.email} to board {self.board_id}")

    async def disconnect(self, close_code):
        # Remove user from presence tracking
        if hasattr(self, 'board_group_name'):
            try:
                await self.remove_user_presence()

                # Notify other users that this user left
                await self.channel_layer.group_send(
                    self.board_group_name,
                    {
                        'type': 'user_left',
                        'user_id': self.user.id if not self.user.is_anonymous else None,
                        'user_name': self.user.get_full_name() or self.user.email if not self.user.is_anonymous else 'Anonymous'
                    }
                )

                # Leave the board group
                await self.channel_layer.group_discard(
                    self.board_group_name,
                    self.channel_name
                )
            except (ConnectionError, ConnectionResetError, OSError) as e:
                # Socket already gone — nothing to do.
                print(f"[TicketBoardWS] disconnect cleanup failed (connection reset): {e}")

            print(f"[TicketBoardWS] Disconnected user {self.user.id if not self.user.is_anonymous else 'anonymous'}")

    async def receive(self, text_data):
        """Handle incoming WebSocket messages from client"""
        try:
            data = json.loads(text_data)
            message_type = data.get('type')

            if message_type == 'ping':
                # Respond to ping for connection health check
                await self.send(text_data=json.dumps({
                    'type': 'pong',
                    'timestamp': data.get('timestamp')
                }))

            elif message_type == 'ticket_moving':
                # User is dragging a ticket
                ticket_id = data.get('ticket_id')
                from_column = data.get('from_column')

                # Broadcast to other users
                await self.channel_layer.group_send(
                    self.board_group_name,
                    {
                        'type': 'ticket_being_moved',
                        'ticket_id': ticket_id,
                        'from_column': from_column,
                        'user_id': self.user.id,
                        'user_name': self.user.get_full_name() or self.user.email,
                        'exclude_channel': self.channel_name
                    }
                )

            elif message_type == 'ticket_editing':
                # User started editing a ticket
                ticket_id = data.get('ticket_id')

                # Broadcast to other users
                await self.channel_layer.group_send(
                    self.board_group_name,
                    {
                        'type': 'ticket_being_edited',
                        'ticket_id': ticket_id,
                        'user_id': self.user.id,
                        'user_name': self.user.get_full_name() or self.user.email,
                        'exclude_channel': self.channel_name
                    }
                )

            elif message_type == 'ticket_editing_stopped':
                # User stopped editing a ticket
                ticket_id = data.get('ticket_id')

                # Broadcast to other users
                await self.channel_layer.group_send(
                    self.board_group_name,
                    {
                        'type': 'ticket_editing_stopped',
                        'ticket_id': ticket_id,
                        'user_id': self.user.id,
                        'exclude_channel': self.channel_name
                    }
                )

            elif message_type == 'get_active_users':
                # Get list of active users on this board
                active_users = await self.get_active_users()
                await self.send(text_data=json.dumps({
                    'type': 'active_users',
                    'users': active_users
                }))

        except json.JSONDecodeError:
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': 'Invalid JSON format'
            }))
        except Exception as e:
            print(f"[TicketBoardWS] Error handling message: {str(e)}")
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': 'Internal server error'
            }))

    # Handlers for messages sent from Django views/signals
    async def ticket_moved(self, event):
        """Broadcast ticket movement to all users on the board"""
        # Don't send to the user who initiated the move (optional)
        if event.get('exclude_channel') == self.channel_name:
            return

        await self.send(text_data=json.dumps({
            'type': 'ticket_moved',
            'ticket_id': event['ticket_id'],
            'from_column_id': event.get('from_column_id'),
            'to_column_id': event['to_column_id'],
            'position': event.get('position'),
            'updated_by_id': event.get('updated_by_id'),
            'updated_by_name': event.get('updated_by_name')
        }))

    async def ticket_updated(self, event):
        """Broadcast ticket field updates to all users on the board"""
        if event.get('exclude_channel') == self.channel_name:
            return

        await self.send(text_data=json.dumps({
            'type': 'ticket_updated',
            'ticket_id': event['ticket_id'],
            'changes': event.get('changes', {}),
            'updated_by_id': event.get('updated_by_id'),
            'updated_by_name': event.get('updated_by_name')
        }))

    async def ticket_created(self, event):
        """Broadcast new ticket creation to all users on the board"""
        if event.get('exclude_channel') == self.channel_name:
            return

        await self.send(text_data=json.dumps({
            'type': 'ticket_created',
            'ticket': event['ticket'],
            'created_by_id': event.get('created_by_id'),
            'created_by_name': event.get('created_by_name')
        }))

    async def ticket_deleted(self, event):
        """Broadcast ticket deletion to all users on the board"""
        if event.get('exclude_channel') == self.channel_name:
            return

        await self.send(text_data=json.dumps({
            'type': 'ticket_deleted',
            'ticket_id': event['ticket_id'],
            'deleted_by_id': event.get('deleted_by_id'),
            'deleted_by_name': event.get('deleted_by_name')
        }))

    async def ticket_commented(self, event):
        """Broadcast a new comment (and the fresh comment count) to all users on the board"""
        if event.get('exclude_channel') == self.channel_name:
            return

        await self.send(text_data=json.dumps({
            'type': 'ticket_commented',
            'ticket_id': event['ticket_id'],
            'comment': event.get('comment'),
            'comments_count': event.get('comments_count'),
            'user_id': event.get('user_id'),
            'user_name': event.get('user_name')
        }))

    async def ticket_being_moved(self, event):
        """Notify users that someone is dragging a ticket"""
        if event.get('exclude_channel') == self.channel_name:
            return

        await self.send(text_data=json.dumps({
            'type': 'ticket_being_moved',
            'ticket_id': event['ticket_id'],
            'from_column': event.get('from_column'),
            'user_id': event['user_id'],
            'user_name': event['user_name']
        }))

    async def ticket_being_edited(self, event):
        """Notify users that someone is editing a ticket"""
        if event.get('exclude_channel') == self.channel_name:
            return

        await self.send(text_data=json.dumps({
            'type': 'ticket_being_edited',
            'ticket_id': event['ticket_id'],
            'user_id': event['user_id'],
            'user_name': event['user_name']
        }))

    async def ticket_editing_stopped(self, event):
        """Notify users that someone stopped editing a ticket"""
        if event.get('exclude_channel') == self.channel_name:
            return

        await self.send(text_data=json.dumps({
            'type': 'ticket_editing_stopped',
            'ticket_id': event['ticket_id'],
            'user_id': event['user_id']
        }))

    async def user_joined(self, event):
        """Notify users that someone joined the board"""
        if event.get('exclude_channel') == self.channel_name:
            return

        await self.send(text_data=json.dumps({
            'type': 'user_joined',
            'user_id': event['user_id'],
            'user_name': event['user_name'],
            'user_email': event['user_email']
        }))

    async def user_left(self, event):
        """Notify users that someone left the board"""
        await self.send(text_data=json.dumps({
            'type': 'user_left',
            'user_id': event['user_id'],
            'user_name': event['user_name']
        }))

    # Database operations and presence tracking
    @database_sync_to_async
    def get_board_data(self):
        """Get basic board data"""
        try:
            with schema_context(self.tenant_schema):
                from tickets.models import Board
                board = Board.objects.get(id=self.board_id)
                return {
                    'id': board.id,
                    'name': board.name,
                    'description': board.description
                }
        except Exception as e:
            print(f"[TicketBoardWS] Error getting board data: {str(e)}")
            return None

    @database_sync_to_async
    def add_user_presence(self):
        """Track user presence on the board using Django cache"""
        from django.core.cache import cache
        cache_key = f'board_presence_{self.tenant_schema}_{self.board_id}'

        # Get current users
        users = cache.get(cache_key, {})

        # Add this user
        users[str(self.user.id)] = {
            'user_id': self.user.id,
            'user_name': self.user.get_full_name() or self.user.email,
            'user_email': self.user.email,
            'channel_name': self.channel_name
        }

        # Store with 5 minute expiry (refreshed on each ping)
        cache.set(cache_key, users, 300)

        print(f"[TicketBoardWS] User {self.user.email} added to board presence")

    @database_sync_to_async
    def remove_user_presence(self):
        """Remove user from presence tracking"""
        from django.core.cache import cache
        cache_key = f'board_presence_{self.tenant_schema}_{self.board_id}'

        users = cache.get(cache_key, {})
        users.pop(str(self.user.id), None)

        cache.set(cache_key, users, 300)

        print(f"[TicketBoardWS] User {self.user.id} removed from board presence")

    @database_sync_to_async
    def get_active_users(self):
        """Get list of currently active users on this board"""
        from django.core.cache import cache
        cache_key = f'board_presence_{self.tenant_schema}_{self.board_id}'

        users = cache.get(cache_key, {})

        # Return list of users (excluding self)
        return [
            {
                'user_id': user_data['user_id'],
                'user_name': user_data['user_name'],
                'user_email': user_data['user_email']
            }
            for user_id, user_data in users.items()
            if user_id != str(self.user.id)
        ]


# Utility functions for sending WebSocket messages from Django views/signals
async def broadcast_ticket_moved(tenant_schema, board_id, ticket_id, from_column_id, to_column_id, position, updated_by, exclude_channel=None):
    """
    Broadcast ticket movement to all users viewing the board.

    Args:
        tenant_schema: The tenant schema name
        board_id: The board ID
        ticket_id: The ticket that was moved
        from_column_id: Source column ID
        to_column_id: Destination column ID
        position: New position in column
        updated_by: User who moved the ticket
        exclude_channel: Optional channel name to exclude from broadcast
    """
    from channels.layers import get_channel_layer

    channel_layer = get_channel_layer()

    await channel_layer.group_send(
        f'board_{tenant_schema}_{board_id}',
        {
            'type': 'ticket_moved',
            'ticket_id': ticket_id,
            'from_column_id': from_column_id,
            'to_column_id': to_column_id,
            'position': position,
            'updated_by_id': updated_by.id if updated_by else None,
            'updated_by_name': updated_by.get_full_name() or updated_by.email if updated_by else 'System',
            'exclude_channel': exclude_channel
        }
    )


async def broadcast_ticket_updated(tenant_schema, board_id, ticket_id, changes, updated_by, exclude_channel=None):
    """
    Broadcast ticket field changes to all users viewing the board.

    Args:
        tenant_schema: The tenant schema name
        board_id: The board ID
        ticket_id: The ticket that was updated
        changes: Dictionary of field changes
        updated_by: User who updated the ticket
        exclude_channel: Optional channel name to exclude from broadcast
    """
    from channels.layers import get_channel_layer

    channel_layer = get_channel_layer()

    await channel_layer.group_send(
        f'board_{tenant_schema}_{board_id}',
        {
            'type': 'ticket_updated',
            'ticket_id': ticket_id,
            'changes': changes,
            'updated_by_id': updated_by.id if updated_by else None,
            'updated_by_name': updated_by.get_full_name() or updated_by.email if updated_by else 'System',
            'exclude_channel': exclude_channel
        }
    )


async def broadcast_ticket_created(tenant_schema, board_id, ticket_data, created_by, exclude_channel=None):
    """
    Broadcast new ticket creation to all users viewing the board.

    Args:
        tenant_schema: The tenant schema name
        board_id: The board ID
        ticket_data: Dictionary containing ticket data
        created_by: User who created the ticket
        exclude_channel: Optional channel name to exclude from broadcast
    """
    from channels.layers import get_channel_layer

    channel_layer = get_channel_layer()

    await channel_layer.group_send(
        f'board_{tenant_schema}_{board_id}',
        {
            'type': 'ticket_created',
            'ticket': ticket_data,
            'created_by_id': created_by.id if created_by else None,
            'created_by_name': created_by.get_full_name() or created_by.email if created_by else 'System',
            'exclude_channel': exclude_channel
        }
    )


async def broadcast_ticket_deleted(tenant_schema, board_id, ticket_id, deleted_by, exclude_channel=None):
    """
    Broadcast ticket deletion to all users viewing the board.

    Args:
        tenant_schema: The tenant schema name
        board_id: The board ID
        ticket_id: The ticket that was deleted
        deleted_by: User who deleted the ticket
        exclude_channel: Optional channel name to exclude from broadcast
    """
    from channels.layers import get_channel_layer

    channel_layer = get_channel_layer()

    await channel_layer.group_send(
        f'board_{tenant_schema}_{board_id}',
        {
            'type': 'ticket_deleted',
            'ticket_id': ticket_id,
            'deleted_by_id': deleted_by.id if deleted_by else None,
            'deleted_by_name': deleted_by.get_full_name() or deleted_by.email if deleted_by else 'System',
            'exclude_channel': exclude_channel
        }
    )


async def broadcast_ticket_commented(tenant_schema, board_id, ticket_id, comment, comments_count, user, exclude_channel=None):
    """
    Broadcast a new ticket comment to all users viewing the board.

    Args:
        tenant_schema: The tenant schema name
        board_id: The board ID
        ticket_id: The ticket that was commented on
        comment: JSON-safe dict describing the comment (id, comment, created_at, user)
        comments_count: Total comments on the ticket after this one
        user: User who wrote the comment
        exclude_channel: Optional channel name to exclude from broadcast
    """
    from channels.layers import get_channel_layer

    channel_layer = get_channel_layer()

    await channel_layer.group_send(
        f'board_{tenant_schema}_{board_id}',
        {
            'type': 'ticket_commented',
            'ticket_id': ticket_id,
            'comment': comment,
            'comments_count': comments_count,
            'user_id': user.id if user else None,
            'user_name': user.get_full_name() or user.email if user else 'System',
            'exclude_channel': exclude_channel
        }
    )


class TeamChatConsumer(AsyncWebsocketConsumer):
    """
    WebSocket consumer for real-time team chat messaging.

    Handles:
    - User online/offline status
    - Real-time messaging between team members
    - Typing indicators
    - Read receipts

    WebSocket URL: /ws/team-chat/{tenant_schema}/?token={auth_token}

    Client -> Server messages:
    - {"type": "message", "recipient_id": 123, "text": "Hello", "message_type": "text"}
    - {"type": "typing", "recipient_id": 123, "is_typing": true}
    - {"type": "read", "message_ids": [1, 2, 3]}

    Server -> Client messages:
    - {"type": "new_message", "message": {...}}
    - {"type": "typing_indicator", "user_id": 123, "is_typing": true}
    - {"type": "read_receipt", "message_ids": [...], "read_by": 123}
    - {"type": "user_online", "user_id": 123, "is_online": true}
    """

    async def connect(self):
        self.tenant_schema = self.scope['url_route']['kwargs']['tenant_schema']
        self.user = self.scope.get('user', AnonymousUser())

        print(f"[TeamChatWS] Connection attempt - User: {self.user}, Tenant: {self.tenant_schema}")

        # Only allow authenticated users
        if self.user.is_anonymous:
            print(f"[TeamChatWS] Rejecting connection - User not authenticated")
            await self.accept()
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': 'Authentication required',
                'code': 'UNAUTHENTICATED'
            }))
            await self.close(code=4001)
            return

        try:
            # Join personal channel group for receiving messages
            self.personal_group_name = f'team_chat_{self.tenant_schema}_{self.user.id}'

            # Join tenant-wide group for online status updates
            self.tenant_group_name = f'team_chat_presence_{self.tenant_schema}'

            await self.channel_layer.group_add(
                self.personal_group_name,
                self.channel_name
            )

            await self.channel_layer.group_add(
                self.tenant_group_name,
                self.channel_name
            )

            await self.accept()

            # Update online status (wrapped in try/catch to not fail connection)
            try:
                await self.set_online_status(True)
            except Exception as e:
                print(f"[TeamChatWS] Error setting online status: {e}")

            # Notify other users that this user is online
            try:
                await self.channel_layer.group_send(
                    self.tenant_group_name,
                    {
                        'type': 'user_status_changed',
                        'user_id': self.user.id,
                        'is_online': True,
                        'user_name': self.user.get_full_name() or self.user.email,
                        'exclude_channel': self.channel_name
                    }
                )
            except Exception as e:
                print(f"[TeamChatWS] Error broadcasting online status: {e}")

            # Get online users list
            online_users = []
            try:
                online_users = await self.get_online_users()
            except Exception as e:
                print(f"[TeamChatWS] Error getting online users: {e}")

            # Send connection confirmation
            await self.send(text_data=json.dumps({
                'type': 'connection',
                'status': 'connected',
                'tenant': self.tenant_schema,
                'user_id': self.user.id,
                'online_users': online_users
            }))

            print(f"[TeamChatWS] Connected successfully for user {self.user.email} in tenant {self.tenant_schema}")
        except Exception as e:
            print(f"[TeamChatWS] Connection error: {e}")
            import traceback
            traceback.print_exc()
            await self.close(code=4000)

    async def disconnect(self, close_code):
        if hasattr(self, 'personal_group_name'):
            try:
                # Update online status
                await self.set_online_status(False)

                # Notify other users that this user is offline
                if hasattr(self, 'tenant_group_name'):
                    await self.channel_layer.group_send(
                        self.tenant_group_name,
                        {
                            'type': 'user_status_changed',
                            'user_id': self.user.id,
                            'is_online': False,
                            'user_name': self.user.get_full_name() or self.user.email
                        }
                    )

                # Leave groups
                await self.channel_layer.group_discard(
                    self.personal_group_name,
                    self.channel_name
                )

                if hasattr(self, 'tenant_group_name'):
                    await self.channel_layer.group_discard(
                        self.tenant_group_name,
                        self.channel_name
                    )
            except (ConnectionError, ConnectionResetError, OSError) as e:
                # Socket already gone — nothing to do.
                print(f"[TeamChatWS] disconnect cleanup failed (connection reset): {e}")

            print(f"[TeamChatWS] Disconnected user {self.user.id if not self.user.is_anonymous else 'anonymous'}")

    async def receive(self, text_data):
        """Handle incoming WebSocket messages from client"""
        try:
            data = json.loads(text_data)
            message_type = data.get('type')

            if message_type == 'ping':
                await self.send(text_data=json.dumps({
                    'type': 'pong',
                    'timestamp': data.get('timestamp')
                }))

            elif message_type == 'message':
                # Send a new message
                await self.handle_send_message(data)

            elif message_type == 'typing':
                # Handle typing indicator
                await self.handle_typing(data)

            elif message_type == 'read':
                # Handle read receipts
                await self.handle_read_receipt(data)

            elif message_type == 'get_online_users':
                # Get list of online users
                online_users = await self.get_online_users()
                await self.send(text_data=json.dumps({
                    'type': 'online_users',
                    'users': online_users
                }))

        except json.JSONDecodeError:
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': 'Invalid JSON format'
            }))
        except Exception as e:
            print(f"[TeamChatWS] Error handling message: {str(e)}")
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': 'Internal server error'
            }))

    async def handle_send_message(self, data):
        """Handle sending a new message"""
        recipient_id = data.get('recipient_id')
        conversation_id = data.get('conversation_id')
        text = data.get('text', '')
        message_type = data.get('message_type', 'text')

        if not recipient_id and not conversation_id:
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': 'recipient_id or conversation_id required'
            }))
            return

        # Create message in database
        message_data = await self.create_message(
            recipient_id=recipient_id,
            conversation_id=conversation_id,
            text=text,
            message_type=message_type
        )

        if message_data:
            # Send to recipient
            recipient_group = f"team_chat_{self.tenant_schema}_{message_data['recipient_id']}"

            await self.channel_layer.group_send(
                recipient_group,
                {
                    'type': 'chat_message',
                    'message': message_data['message'],
                    'conversation': message_data['conversation_id'],
                    'conversation_id': message_data['conversation_id']
                }
            )

            # Confirm to sender
            await self.send(text_data=json.dumps({
                'type': 'message_sent',
                'message': message_data['message'],
                'conversation': message_data['conversation_id'],
                'conversation_id': message_data['conversation_id']
            }))
        else:
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': 'Message could not be sent'
            }))

    async def handle_typing(self, data):
        """Handle typing indicator"""
        recipient_id = data.get('recipient_id')
        is_typing = data.get('is_typing', False)

        if recipient_id:
            recipient_group = f"team_chat_{self.tenant_schema}_{recipient_id}"

            await self.channel_layer.group_send(
                recipient_group,
                {
                    'type': 'typing_indicator',
                    'user_id': self.user.id,
                    'user_name': self.user.get_full_name() or self.user.email,
                    'is_typing': is_typing
                }
            )

    async def handle_read_receipt(self, data):
        """Handle read receipt"""
        message_ids = data.get('message_ids', [])
        conversation_id = data.get('conversation_id')

        if message_ids:
            # Mark messages as read in database
            await self.mark_messages_read(message_ids)

            # Get the other participant to notify them
            other_user_id = await self.get_conversation_other_participant(conversation_id)

            if other_user_id:
                other_user_group = f"team_chat_{self.tenant_schema}_{other_user_id}"

                await self.channel_layer.group_send(
                    other_user_group,
                    {
                        'type': 'read_receipt',
                        'message_ids': message_ids,
                        'read_by': self.user.id,
                        'conversation_id': conversation_id
                    }
                )

    # Event handlers for messages from channel layer
    async def chat_message(self, event):
        """Send new message to WebSocket"""
        conversation_id = event.get('conversation_id', event.get('conversation'))
        await self.send(text_data=json.dumps({
            'type': 'new_message',
            'message': event['message'],
            'conversation': conversation_id,
            'conversation_id': conversation_id
        }))

    async def typing_indicator(self, event):
        """Send typing indicator to WebSocket"""
        await self.send(text_data=json.dumps({
            'type': 'typing_indicator',
            'user_id': event['user_id'],
            'user_name': event['user_name'],
            'is_typing': event['is_typing']
        }))

    async def read_receipt(self, event):
        """Send read receipt to WebSocket"""
        await self.send(text_data=json.dumps({
            'type': 'read_receipt',
            'message_ids': event['message_ids'],
            'read_by': event['read_by'],
            'conversation_id': event['conversation_id']
        }))

    async def user_status_changed(self, event):
        """Send user online status change to WebSocket"""
        if event.get('exclude_channel') == self.channel_name:
            return

        await self.send(text_data=json.dumps({
            'type': 'user_online',
            'user_id': event['user_id'],
            'is_online': event['is_online'],
            'user_name': event['user_name']
        }))

    # Database operations
    @database_sync_to_async
    def set_online_status(self, is_online):
        """Update user's online status"""
        try:
            with schema_context(self.tenant_schema):
                status, created = UserOnlineStatus.objects.get_or_create(
                    user=self.user,
                    defaults={'is_online': is_online}
                )
                if not created:
                    status.is_online = is_online
                    status.save(update_fields=['is_online', 'last_seen'])
                return True
        except Exception as e:
            print(f"[TeamChatWS] Error setting online status: {e}")
            return False

    @database_sync_to_async
    def get_online_users(self):
        """Get list of online users"""
        try:
            with schema_context(self.tenant_schema):
                online_statuses = UserOnlineStatus.objects.filter(
                    is_online=True
                ).exclude(user=self.user).select_related('user')

                return [
                    {
                        'user_id': status.user.id,
                        'user_name': status.user.get_full_name() or status.user.email,
                        'email': status.user.email
                    }
                    for status in online_statuses
                ]
        except Exception as e:
            print(f"[TeamChatWS] Error getting online users: {e}")
            return []

    @database_sync_to_async
    def create_message(self, recipient_id=None, conversation_id=None, text='', message_type='text'):
        """
        Create a new message in the database.

        The websocket path only carries text (attachments go through
        ``POST /api/team-chat/messages/``), so the type is forced to ``text``
        and an empty body is rejected. The message is serialized with
        ``TeamChatMessageSerializer`` — the same shape the REST API and the
        REST-triggered ``new_message`` frames use — and the recipient gets the
        same in-app/push notification as for a REST-created message.
        """
        try:
            with schema_context(self.tenant_schema):
                from django.contrib.auth import get_user_model
                from users.notification_utils import create_team_chat_message_notification
                from users.serializers import TeamChatMessageSerializer
                User = get_user_model()

                text = (text or '').strip()
                if not text:
                    print("[TeamChatWS] Rejecting empty websocket message")
                    return None

                # Get or create conversation
                if conversation_id:
                    conversation = TeamChatConversation.objects.get(
                        id=conversation_id,
                        participants=self.user
                    )
                    recipient = conversation.get_other_participant(self.user)
                    if recipient is None:
                        print(f"[TeamChatWS] Conversation {conversation_id} has no other participant")
                        return None
                else:
                    recipient = User.objects.get(id=recipient_id)
                    if recipient.id == self.user.id:
                        print("[TeamChatWS] Rejecting message to self")
                        return None

                    # Find existing conversation
                    conversation = TeamChatConversation.objects.filter(
                        participants=self.user
                    ).filter(
                        participants=recipient
                    ).first()

                    if not conversation:
                        conversation = TeamChatConversation.objects.create()
                        conversation.participants.add(self.user, recipient)

                # Create message (websocket messages are text-only)
                message = TeamChatMessage.objects.create(
                    conversation=conversation,
                    sender=self.user,
                    message_type='text',
                    text=text
                )

                # Update conversation timestamp
                conversation.save()

                # Serialize message with the REST serializer (one shape for all
                # clients); keep `conversation_id` as an alias of `conversation`
                # for clients written against the old websocket payload.
                message_data = dict(TeamChatMessageSerializer(message).data)
                message_data.setdefault('conversation_id', conversation.id)

                # In-app notification + push for the recipient; never fail the send
                try:
                    create_team_chat_message_notification(message, recipient=recipient)
                except Exception as notify_err:
                    print(f"[TeamChatWS] Error creating team chat notification: {notify_err}")

                return {
                    'message': message_data,
                    'conversation_id': conversation.id,
                    'recipient_id': recipient.id
                }

        except Exception as e:
            print(f"[TeamChatWS] Error creating message: {e}")
            return None

    @database_sync_to_async
    def mark_messages_read(self, message_ids):
        """Mark messages as read"""
        try:
            with schema_context(self.tenant_schema):
                from django.utils import timezone
                TeamChatMessage.objects.filter(
                    id__in=message_ids,
                    conversation__participants=self.user,
                    is_read=False
                ).exclude(
                    sender=self.user
                ).update(
                    is_read=True,
                    read_at=timezone.now()
                )
                return True
        except Exception as e:
            print(f"[TeamChatWS] Error marking messages read: {e}")
            return False

    @database_sync_to_async
    def get_conversation_other_participant(self, conversation_id):
        """Get the other participant in a conversation"""
        try:
            with schema_context(self.tenant_schema):
                conversation = TeamChatConversation.objects.get(
                    id=conversation_id,
                    participants=self.user
                )
                other = conversation.get_other_participant(self.user)
                return other.id if other else None
        except Exception as e:
            print(f"[TeamChatWS] Error getting other participant: {e}")
            return None


# Utility function for sending team chat messages from REST views
async def send_team_chat_message(tenant_schema, recipient_id, message_data, conversation_id):
    """
    Send a team chat message notification via WebSocket.

    Args:
        tenant_schema: The tenant schema name
        recipient_id: The recipient user ID
        message_data: Serialized message data
        conversation_id: The conversation ID
    """
    from channels.layers import get_channel_layer

    channel_layer = get_channel_layer()
    if not channel_layer:
        return

    await channel_layer.group_send(
        f'team_chat_{tenant_schema}_{recipient_id}',
        {
            'type': 'chat_message',
            'message': message_data,
            'conversation': conversation_id,
            'conversation_id': conversation_id
        }
    )
