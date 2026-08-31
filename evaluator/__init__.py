"""Trellis workflow adherence evaluator.

Wave 1 scaffold slice: behavior catalog (behaviors.yaml) and probe suite
loaders. Driver, trace, grader, judge, report, and CLI land in later steps.
"""

from evaluator.catalog import Behavior, BehaviorCatalog, load_catalog
from evaluator.probes import Probe, load_probes

__all__ = ["Behavior", "BehaviorCatalog", "Probe", "load_catalog", "load_probes"]
