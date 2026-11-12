"""Response / request shapes for the API schema. The step list itself is
built by :mod:`onboarding.services`; these serializers only document it."""
from rest_framework import serializers

from .models import INDUSTRY_KEYS, STATUSES


class OnboardingStepSerializer(serializers.Serializer):
    key = serializers.CharField()
    feature_key = serializers.CharField(allow_null=True)
    order = serializers.IntegerField()
    status = serializers.ChoiceField(choices=list(STATUSES))
    answers = serializers.JSONField(help_text='Last applied answers ({} if none).')
    prefill = serializers.JSONField(help_text='Suggested answers computed from existing data.')
    context = serializers.JSONField(help_text='Step-specific read data the wizard page needs.')
    completed_at = serializers.DateTimeField(allow_null=True)


class OnboardingStateSerializer(serializers.Serializer):
    industry = serializers.ChoiceField(choices=INDUSTRY_KEYS, allow_null=True)
    started_at = serializers.DateTimeField(allow_null=True)
    finished_at = serializers.DateTimeField(allow_null=True)
    pending_count = serializers.IntegerField()
    total = serializers.IntegerField()
    steps = OnboardingStepSerializer(many=True)


class ApplyRequestSerializer(serializers.Serializer):
    answers = serializers.JSONField(help_text="The step's answers (see the step's answers schema).")


class ApplyResultSerializer(serializers.Serializer):
    created = serializers.JSONField(help_text='Counts / ids of rows created.')
    updated = serializers.ListField(child=serializers.CharField(), help_text='Field names updated.')
    notes = serializers.ListField(child=serializers.CharField())


class ApplyResponseSerializer(serializers.Serializer):
    step = OnboardingStepSerializer()
    result = ApplyResultSerializer()


class StepResponseSerializer(serializers.Serializer):
    step = OnboardingStepSerializer()


class ErrorsSerializer(serializers.Serializer):
    errors = serializers.JSONField(help_text='DRF field errors: {"<field>": ["message"]}.')
