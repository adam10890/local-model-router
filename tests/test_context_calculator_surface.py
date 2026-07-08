from local_model_router.helpers import context_calculator


def test_context_calculator_exposes_only_active_planning_surface():
    assert hasattr(context_calculator, "read_gguf_metadata")
    assert hasattr(context_calculator, "ContextUtilization")
    assert not hasattr(context_calculator, "ExternalTokenBudget")
    assert not hasattr(context_calculator, "calculate_optimal_context")
    assert not hasattr(context_calculator, "estimate_vram_detailed")
