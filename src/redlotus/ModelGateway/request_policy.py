"""Request-scoped selection and budgets, attached through Pydantic AI's public hooks."""

from pydantic_ai.capabilities import AbstractCapability

from redlotus.ModelGateway.model_factory import ModelTarget, create_model
from redlotus.ModelGateway.input_policy import ModelInputPolicy, InputLimitError


class RequestPolicy(AbstractCapability):
    def __init__(self, role, target, model, *, follow_config=False, task_state=None):
        self.role, self.target, self.model = role, target, model
        self.follow_config = follow_config
        self.task_state = task_state

    async def before_model_request(self, ctx, request_context):
        target = ModelTarget.for_role(self.role) if self.follow_config else self.target
        if target != self.target:
            model = create_model(target)
        else:
            model = self.model
        parameters = request_context.model_request_parameters
        tool_definitions = [*parameters.function_tools, *parameters.output_tools]
        if self.role in ("coordinator", "manager", "worker"):
            from redlotus.ModelGateway.ModelChecker import compact_request_messages

            request_context.messages = await compact_request_messages(
                request_context.messages,
                role=self.role,
                target=target,
                task_state=self.task_state() if self.task_state else "",
                tools=tool_definitions,
            )
        else:
            from redlotus.ModelGateway.ModelChecker import (
                get_effective_max_context_async,
                estimate_context_tokens,
            )

            limit = await get_effective_max_context_async(
                target.name, role=self.role, context=target.context
            )
            if (
                estimate_context_tokens(
                    request_context.messages, tools=tool_definitions
                )
                + int(target.settings.get("max_tokens") or 0)
                >= limit
            ):
                raise InputLimitError(
                    "Input and configured output budget exceed the target context capacity."
                )
        self.target, self.model = target, model
        ModelInputPolicy.from_limits(target.limits).check_messages(
            request_context.messages
        )
        request_context.model = model
        request_context.model_settings = model.settings or {}
        return request_context

    async def after_model_request(self, ctx, *, request_context, response):
        response.metadata = {
            **(response.metadata or {}),
            "model_target": {
                "name": self.target.name,
                "protocol": self.target.protocol,
            },
        }
        return response

    async def on_model_request_error(self, ctx, *, request_context, error):
        cause = error
        while cause is not None:
            if isinstance(cause, InputLimitError):
                raise cause
            cause = cause.__cause__
        raise error
