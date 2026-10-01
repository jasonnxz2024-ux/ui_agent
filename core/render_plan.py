"""Render-plan facade for callers that keep the six pipeline stages explicit."""

from .capability import plan_capabilities
from .model import RenderPlan, RenderPlanItem


def build_render_plan(model, browser_evidence=None) -> RenderPlan:
    plan_capabilities(model, browser_evidence)
    return model.render_plan


__all__ = ["RenderPlan", "RenderPlanItem", "build_render_plan"]

