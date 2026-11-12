from django.urls import path

from .views import OnboardingStateView, OnboardingStepActionView

app_name = 'onboarding'

urlpatterns = [
    path('', OnboardingStateView.as_view(), name='state'),
    path('<slug:key>/apply/', OnboardingStepActionView.as_view(action='apply'), name='apply'),
    path('<slug:key>/skip/', OnboardingStepActionView.as_view(action='skip'), name='skip'),
    path('<slug:key>/reset/', OnboardingStepActionView.as_view(action='reset'), name='reset'),
]
