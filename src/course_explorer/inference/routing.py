"""Route one whole-target inference call through the fit-only local keys to the evidence writer.

The module prepares the rendered request and its route-bound work identity, counts the prompt tokens
through the first ordered local key's pinned template and tokenize endpoints, and selects the first
key that fits. The same first key generates when it fits, and only the selected larger no-MTP key is
opened when only it fits. A caller may supply a :class:`GenerationRuntimeControl` to load and unload
the selected local key, and without one the externally prepared bounded endpoints run with no
process action. A packet fitting neither key raises :class:`TargetContextError` unless the caller
injects an :class:`OversizeRouteFactory` for that whole-target call. Every route shares the one
HTTP, retry, validation and evidence path of :func:`run_operational_target`, receives the identical
live admission and request-scope callbacks, and reports the planned work identity through
``on_identity`` immediately before generation. A tokenizer or generation failure is never a fit
condition, and a refused admission raises the cooperative :class:`RequestNotStarted` signal.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from course_explorer.config import InferenceLocalVariantSettings, InferenceSettings
from course_explorer.http_retry import RequestNotStarted
from course_explorer.inference.evidence import (
    InferenceExecutionProfile,
    InferenceTargetWorkRecord,
)
from course_explorer.inference.evidence_repository import InferenceEvidenceRepository
from course_explorer.inference.failures import TargetContextError
from course_explorer.inference.operational import (
    OperationalCatalogue,
    OperationalTargetInput,
    materialize_selection_packet,
    run_operational_target,
)
from course_explorer.inference.prompts import PromptPack
from course_explorer.inference.provider import StructuredJsonProvider
from course_explorer.inference.target_pass import (
    TargetGenerationProvider,
    TargetWorkIdentity,
    prepare_target_request,
    target_work_identity,
)

if TYPE_CHECKING:
    from course_explorer.inference.scheduling import ProcessingTargetRecord, TargetFailureRecord
    from course_explorer.inference.scheduling_repository import InferenceSchedulingRepository


class GenerationRuntimeControl(Protocol):
    """One supplied runtime's load/unload control around the fit-only local routes."""

    def activate(self, variant: InferenceLocalVariantSettings) -> None: ...

    def stop(self) -> None: ...

    def active_variant(self) -> InferenceLocalVariantSettings | None: ...


class OversizeRouteFactory(Protocol):
    """One injected alternative route for a packet fitting neither ordered local key.

    The caller supplies it explicitly, so this package names no provider, module or credential. When
    called it yields the opened provider together with that route's execution profile for
    exactly one :func:`run_operational_target` call.
    """

    def __call__(
        self,
        settings: InferenceSettings,
        *,
        before_request: Callable[[], bool] | None,
        request_scope: Callable[[float], AbstractContextManager[None]] | None,
    ) -> AbstractContextManager[tuple[TargetGenerationProvider, InferenceExecutionProfile]]: ...


def local_execution(
    settings: InferenceSettings, variant: InferenceLocalVariantSettings
) -> InferenceExecutionProfile:
    """Derive the execution profile for one selected local fit key."""
    return InferenceExecutionProfile(
        route="local",
        model_identity=settings.local_model_identity(variant),
        runtime_identity=settings.local_runtime_identity(variant),
    )


def select_fitting_variant(
    settings: InferenceSettings, prompt_tokens: int
) -> InferenceLocalVariantSettings | None:
    """Return the first ordered key whose real prompt plus output and margin fits, else None.

    Fit is decided only from the tokenizer's exact prompt count against each key's configured
    context and achieved maximum. A local model or tokenizer failure is never a fit failure.
    """
    required = (
        prompt_tokens + settings.inference_max_tokens + settings.inference_fit_headroom_tokens
    )
    for variant in settings.inference_local_variants:
        if required <= variant.context_size and required <= variant.achieved_max_context:
            return variant
    return None


def run_routed_operational_target(
    value: OperationalTargetInput,
    pack: PromptPack,
    settings: InferenceSettings,
    repository: InferenceEvidenceRepository,
    *,
    catalogue: OperationalCatalogue,
    scheduling_repository: InferenceSchedulingRepository,
    cycle_id: UUID,
    runtime: GenerationRuntimeControl | None = None,
    on_identity: Callable[[TargetWorkIdentity], None] | None = None,
    retry_failed: bool = False,
    before_request: Callable[[], bool] | None = None,
    on_failure: Callable[[ProcessingTargetRecord, TargetFailureRecord], None] | None = None,
    request_scope: Callable[[float], AbstractContextManager[None]] | None = None,
    oversize_route: OversizeRouteFactory | None = None,
) -> InferenceTargetWorkRecord:
    """Run or reuse one whole-target call through the one fit-only route and sole writer.

    Admission is checked before any runtime activation. The compact selection references are
    materialized once into the transient packet, and its request is rendered and hashed once before
    any tokenizer or runtime work. Prompt tokens are counted from that prepared request through the
    already-loaded local key when one is loaded, so a loaded runtime is not reloaded to count. The
    first ordered key is loaded only on genuine first use. The first key generates when it fits, and
    only the selected larger no-MTP key is switched to when only it fits. A loaded local runtime
    is loaded across an injected oversize call, so the next local target needs no reload. Every
    selected route binds one work identity from that prepared request and passes it to the writer.

    A packet fitting neither key has no local route: the fixed :class:`TargetContextError` is
    raised, or an explicitly supplied ``oversize_route`` opens its provider/profile and the same
    :func:`run_operational_target` writer runs once with the identical packet, durable scheduling
    owner/cycle and live admission/request-scope and failure callbacks, reporting the exact planned
    work identity through ``on_identity`` immediately before generation. A tokenizer or generation
    failure is never a fit condition and never invokes the factory, and a refused admission is the
    fixed cooperative :class:`RequestNotStarted` signal.
    """
    if before_request is not None and not before_request():
        raise RequestNotStarted("inference target request not started")
    packet = materialize_selection_packet(value.references, catalogue)
    prepared = prepare_target_request(value, pack, settings, packet=packet)
    first_variant = settings.inference_local_variants[0]
    counting_variant = None if runtime is None else runtime.active_variant()
    if counting_variant is None:
        if runtime is not None:
            runtime.activate(first_variant)
        counting_variant = first_variant
    tokenizer = StructuredJsonProvider(
        settings, counting_variant, before_request=before_request, request_scope=request_scope
    )
    selected: InferenceLocalVariantSettings | None = None
    try:
        prompt_tokens = tokenizer.count_prompt_tokens(prepared.request)
        selected = select_fitting_variant(settings, prompt_tokens)
        if selected == counting_variant:
            execution = local_execution(settings, selected)
            identity = target_work_identity(
                value, settings, execution, packet=packet, prepared=prepared
            )
            _notify_identity(identity, on_identity)
            return run_operational_target(
                value,
                settings,
                repository,
                tokenizer,
                execution,
                packet=packet,
                identity=identity,
                scheduling_repository=scheduling_repository,
                cycle_id=cycle_id,
                retry_failed=retry_failed,
                before_request=before_request,
                on_failure=on_failure,
            )
    finally:
        tokenizer.close()
    if selected is None:
        # The loaded local runtime is left loaded across the injected oversize call, so
        # the next local target reuses it without a reload.
        if oversize_route is None:
            raise TargetContextError()
        with oversize_route(
            settings, before_request=before_request, request_scope=request_scope
        ) as (provider, execution):
            identity = target_work_identity(
                value, settings, execution, packet=packet, prepared=prepared
            )
            _notify_identity(identity, on_identity)
            return run_operational_target(
                value,
                settings,
                repository,
                provider,
                execution,
                packet=packet,
                identity=identity,
                scheduling_repository=scheduling_repository,
                cycle_id=cycle_id,
                retry_failed=retry_failed,
                before_request=before_request,
                on_failure=on_failure,
            )
    if before_request is not None and not before_request():
        raise RequestNotStarted("inference target request not started")
    if runtime is not None:
        runtime.activate(selected)
    execution = local_execution(settings, selected)
    identity = target_work_identity(value, settings, execution, packet=packet, prepared=prepared)
    _notify_identity(identity, on_identity)
    with StructuredJsonProvider(
        settings, selected, before_request=before_request, request_scope=request_scope
    ) as provider:
        return run_operational_target(
            value,
            settings,
            repository,
            provider,
            execution,
            packet=packet,
            identity=identity,
            scheduling_repository=scheduling_repository,
            cycle_id=cycle_id,
            retry_failed=retry_failed,
            before_request=before_request,
            on_failure=on_failure,
        )


def _notify_identity(
    identity: TargetWorkIdentity,
    on_identity: Callable[[TargetWorkIdentity], None] | None,
) -> None:
    """Call the optional planned-identity callback with the supplied work identity."""
    if on_identity is None:
        return
    on_identity(identity)
