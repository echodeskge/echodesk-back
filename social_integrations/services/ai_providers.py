"""
Provider-agnostic structured LLM calls for the AI companion.

One entry point — ``generate_structured`` — that forces the model to
answer through a tool/function call matching a JSON schema, so callers
never parse free text. The Anthropic branch mirrors
``blog/services/ai_post_generator.py`` (server-validated tool use); the
OpenAI branch uses function calling with the same contract.

SDKs are imported lazily so the app boots without them in environments
that never run AI features.
"""
import json
import logging

from django.conf import settings

logger = logging.getLogger(__name__)

# Cheaper OpenAI models to fall back to when the account can't access the
# requested one (mirrors ai_content_service.FALLBACK_MODELS).
OPENAI_FALLBACK_MODELS = ['gpt-4o-mini', 'gpt-3.5-turbo']

REQUEST_TIMEOUT_SECONDS = 60


class AICompanionError(Exception):
    """Raised when a provider call fails or can't be interpreted."""


def default_model(provider):
    if provider == 'anthropic':
        return settings.BLOG_AI_MODEL
    return settings.OPENAI_MODEL


def default_api_key(provider):
    if provider == 'anthropic':
        return settings.ANTHROPIC_API_KEY
    return settings.OPENAI_API_KEY


def generate_structured(provider, model, api_key, system, messages,
                        tool_name, tool_schema, max_tokens=1024):
    """
    Run one structured completion.

    Args:
        provider: 'anthropic' | 'openai'
        model: model id ('' = provider default)
        api_key: key to use ('' = platform default from env)
        system: system prompt string
        messages: [{'role': 'user'|'assistant', 'content': str}, ...]
        tool_name: name of the forced tool
        tool_schema: JSON schema for the tool input

    Returns:
        (payload: dict, usage: {'prompt_tokens': int, 'completion_tokens': int})

    Raises:
        AICompanionError on missing key, API failure, or malformed response.
    """
    model = model or default_model(provider)
    api_key = api_key or default_api_key(provider)
    if not api_key:
        raise AICompanionError(
            f"No API key configured for provider '{provider}'."
        )

    if provider == 'anthropic':
        return _anthropic_structured(
            model, api_key, system, messages, tool_name, tool_schema, max_tokens
        )
    if provider == 'openai':
        return _openai_structured(
            model, api_key, system, messages, tool_name, tool_schema, max_tokens
        )
    raise AICompanionError(f"Unknown AI provider: {provider}")


def _anthropic_structured(model, api_key, system, messages,
                          tool_name, tool_schema, max_tokens):
    from anthropic import Anthropic

    client = Anthropic(api_key=api_key, timeout=REQUEST_TIMEOUT_SECONDS)
    try:
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            tools=[{
                'name': tool_name,
                'description': f'Submit the {tool_name} result.',
                'input_schema': tool_schema,
            }],
            tool_choice={'type': 'tool', 'name': tool_name},
            messages=messages,
        )
    except Exception as exc:
        raise AICompanionError(f"Anthropic call failed: {exc}") from exc

    tool_block = None
    for block in response.content or []:
        if getattr(block, 'type', None) == 'tool_use' and getattr(block, 'name', '') == tool_name:
            tool_block = block
            break
    if tool_block is None or not isinstance(getattr(tool_block, 'input', None), dict):
        raise AICompanionError(
            f"Anthropic did not return a {tool_name} tool call."
        )

    usage = {
        'prompt_tokens': getattr(response.usage, 'input_tokens', 0) or 0,
        'completion_tokens': getattr(response.usage, 'output_tokens', 0) or 0,
    }
    return tool_block.input, usage


def _openai_structured(model, api_key, system, messages,
                       tool_name, tool_schema, max_tokens):
    import openai
    from openai import OpenAI

    client = OpenAI(api_key=api_key, timeout=REQUEST_TIMEOUT_SECONDS)
    chat_messages = [{'role': 'system', 'content': system}] + messages
    tools = [{
        'type': 'function',
        'function': {
            'name': tool_name,
            'description': f'Submit the {tool_name} result.',
            'parameters': tool_schema,
        },
    }]

    models_to_try = [model] + [m for m in OPENAI_FALLBACK_MODELS if m != model]
    last_error = None
    for candidate in models_to_try:
        try:
            response = client.chat.completions.create(
                model=candidate,
                messages=chat_messages,
                tools=tools,
                tool_choice={'type': 'function', 'function': {'name': tool_name}},
                max_tokens=max_tokens,
            )
            break
        except openai.PermissionDeniedError as exc:
            # Account can't use this model — try the next cheaper one.
            logger.warning("OpenAI model %s unavailable, trying fallback: %s", candidate, exc)
            last_error = exc
        except Exception as exc:
            raise AICompanionError(f"OpenAI call failed: {exc}") from exc
    else:
        raise AICompanionError(f"OpenAI call failed: {last_error}")

    try:
        tool_calls = response.choices[0].message.tool_calls or []
        call = next(c for c in tool_calls if c.function.name == tool_name)
        payload = json.loads(call.function.arguments)
    except (StopIteration, IndexError, AttributeError, json.JSONDecodeError) as exc:
        raise AICompanionError(
            f"OpenAI did not return a valid {tool_name} tool call: {exc}"
        ) from exc

    usage = {
        'prompt_tokens': getattr(response.usage, 'prompt_tokens', 0) or 0,
        'completion_tokens': getattr(response.usage, 'completion_tokens', 0) or 0,
    }
    return payload, usage
