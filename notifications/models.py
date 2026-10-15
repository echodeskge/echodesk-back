from django.db import models
from django.conf import settings


class PushSubscription(models.Model):
    """Store push notification subscriptions for users."""
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='push_subscriptions'
    )
    endpoint = models.TextField(unique=True)
    p256dh = models.TextField()  # Public key
    auth = models.TextField()  # Authentication secret
    user_agent = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Push Subscription'
        verbose_name_plural = 'Push Subscriptions'
        indexes = [
            models.Index(fields=['user', 'is_active']),
            models.Index(fields=['endpoint']),
        ]

    def __str__(self):
        return f"{self.user.email} - {self.endpoint[:50]}..."


class DevicePushToken(models.Model):
    """Expo push token registered by the native mobile app (iOS / Android).

    One row per physical device install. The token is globally unique, so a
    re-registration from a different user (shared phone, re-login) simply
    re-points the row to the new owner.
    """
    PLATFORM_CHOICES = [
        ('ios', 'iOS'),
        ('android', 'Android'),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='device_push_tokens'
    )
    token = models.CharField(max_length=255, unique=True)  # ExponentPushToken[...]
    platform = models.CharField(max_length=10, choices=PLATFORM_CHOICES)
    device_id = models.CharField(max_length=255, blank=True, db_index=True)
    device_name = models.CharField(max_length=255, blank=True)
    app_version = models.CharField(max_length=50, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-last_seen_at']
        verbose_name = 'Device Push Token'
        verbose_name_plural = 'Device Push Tokens'
        indexes = [
            models.Index(fields=['user', 'is_active']),
        ]

    def __str__(self):
        return f"{self.user.email} - {self.platform} - {self.token[:40]}..."


class NotificationLog(models.Model):
    """Log sent notifications for debugging and analytics."""
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('sent', 'Sent'),
        ('failed', 'Failed'),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='notification_logs'
    )
    subscription = models.ForeignKey(
        PushSubscription,
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )
    device_token = models.ForeignKey(
        DevicePushToken,
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )
    title = models.CharField(max_length=255)
    body = models.TextField()
    data = models.JSONField(default=dict, blank=True)
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='pending'
    )
    error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Notification Log'
        verbose_name_plural = 'Notification Logs'

    def __str__(self):
        return f"{self.user.email} - {self.title} ({self.status})"
