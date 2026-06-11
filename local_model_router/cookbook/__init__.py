"""Cookbook: hardware-aware model recommendations.

Inspired by github.com/Andyyyy64/whichllm — VRAM fit modeling with
evidence-graded confidence — adapted to this router's world: the models
are already on disk, the roles (chat/utility/embed/scribe) come from the
fleet config, and a recommendation can be applied by starting a slot.
"""
