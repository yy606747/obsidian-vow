"""Opt-in, task-local provider diagnostics without request/response prose."""

from contextlib import contextmanager
from contextvars import ContextVar

private_provider_scope = ContextVar("private_provider_scope", default=None)


@contextmanager
def private_provider_call(scope):
    token = private_provider_scope.set(scope)
    try:
        yield
    finally:
        private_provider_scope.reset(token)
