"""Utility helpers for resolving OSCAL mapping collections into concrete catalog controls."""

from __future__ import annotations

import logging
import pathlib
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

from trestle.common.list_utils import as_list
from trestle.core.catalog.catalog_interface import CatalogInterface
from trestle.core.profile_resolver import ProfileResolver
from trestle.oscal import catalog as oscat
from trestle.oscal import mapping as osmap

from .utils import DEFAULT_TRESTLE_ROOT, find_trestle_root

logger = logging.getLogger(__name__)


def _trestle_href_to_path(trestle_root: pathlib.Path, href: str) -> pathlib.Path:
    """Translate trestle:// URLs into filesystem paths relative to the workspace root."""
    if href.startswith("trestle://"):
        relative = href[len("trestle://"):]
        return trestle_root / relative
    path = pathlib.Path(href)
    if path.is_absolute():
        return path
    return trestle_root / path


def _normalize_catalog_href(trestle_root: pathlib.Path, href: str) -> str:
    """Normalize catalog hrefs for stable comparisons."""
    path = _trestle_href_to_path(trestle_root, href)
    return str(path.resolve(strict=False))


@dataclass(frozen=True)
class _MappingEdge:
    """Mapping edge from a source catalog to a target catalog with the corresponding maps."""

    target_key: str
    target_href: str
    maps: List[osmap.Map]


@dataclass(frozen=True)
class TransitiveControlTarget:
    """Typed result for transitive control resolution."""

    catalog_href: str
    control: oscat.Control


class MappingResolver:
    """Resolve source controls in a mapping collection to their target catalog controls."""

    def __init__(self, mapping_href: str, trestle_root: Optional[pathlib.Path] = None) -> None:
        self._trestle_root = trestle_root or find_trestle_root(DEFAULT_TRESTLE_ROOT)
        self._mapping_href = mapping_href
        self._mapping_collection = self._load_mapping_collection()
        self._catalog_cache: Dict[str, CatalogInterface] = {}

    def _load_mapping_collection(self) -> osmap.MappingCollection:
        mapping_path = _trestle_href_to_path(self._trestle_root, self._mapping_href)
        mapping_collection = osmap.MappingCollection.oscal_read(mapping_path)
        if mapping_collection is None:
            raise ValueError(f"Unable to read mapping collection at {mapping_path}")
        return mapping_collection

    def _get_catalog_interface(self, href: str) -> CatalogInterface:
        """Return a cached CatalogInterface for the given catalog href."""
        if href not in self._catalog_cache:
            catalog = ProfileResolver.get_resolved_profile_catalog(self._trestle_root, href)
            self._catalog_cache[href] = CatalogInterface(catalog)
        return self._catalog_cache[href]

    def resolve_target_controls(self, source_control_id: str) -> List[oscat.Control]:
        """Resolve target controls for a given source control identifier."""
        resolved_controls: List[oscat.Control] = []
        raw_mappings = self._mapping_collection.mappings
        if raw_mappings is None:
            return resolved_controls
        if isinstance(raw_mappings, osmap.Mapping):
            mapping_iterable = [raw_mappings]
        else:
            mapping_iterable = list(raw_mappings)

        for mapping in mapping_iterable:
            catalog_interface = None
            try:
                catalog_interface = self._get_catalog_interface(mapping.target_resource.href)
            except Exception as exc:  # pragma: no cover - defensive logging
                logger.error("Failed to resolve target catalog %s: %s", mapping.target_resource.href, exc)
                continue
            for map_entry in as_list(mapping.maps):
                if not any(item.id_ref == source_control_id for item in as_list(map_entry.sources)):
                    continue
                for target_item in as_list(map_entry.targets):
                    control = catalog_interface.get_control(target_item.id_ref)
                    if control is None:
                        logger.warning(
                            "Target control %s referenced by mapping %s not found in %s",
                            target_item.id_ref,
                            map_entry.uuid,
                            mapping.target_resource.href,
                        )
                        continue
                    resolved_controls.append(control)
        return resolved_controls

    def get_mapping_collection(self) -> osmap.MappingCollection:
        """Expose the loaded mapping collection."""
        return self._mapping_collection


class MappingReachability:
    """Build a graph of mapping collections to discover reachable catalogs."""

    def __init__(
        self,
        mapping_root: Optional[pathlib.Path] = None,
        trestle_root: Optional[pathlib.Path] = None,
    ) -> None:
        self._trestle_root = trestle_root or find_trestle_root(DEFAULT_TRESTLE_ROOT)
        self._mapping_root = mapping_root or self._trestle_root / "mapping-collections"
        self._adjacency: Dict[str, Set[str]] = {}
        self._href_lookup: Dict[str, str] = {}
        self._load_mapping_graph()

    def _load_mapping_graph(self) -> None:
        """Load all mapping collections into an adjacency list keyed by normalized href."""
        if not self._mapping_root.exists():
            raise FileNotFoundError(f"Mapping root {self._mapping_root} does not exist")

        mapping_files = sorted(self._mapping_root.glob("*/mapping-collection.json"))
        if not mapping_files:
            logger.warning("No mapping collections found under %s", self._mapping_root)

        for mapping_path in mapping_files:
            try:
                collection = osmap.MappingCollection.oscal_read(mapping_path)
            except Exception as exc:  # pragma: no cover - defensive logging
                logger.error("Failed to load mapping collection %s: %s", mapping_path, exc)
                continue
            if collection is None or collection.mappings is None:
                continue

            raw_mappings = collection.mappings
            mapping_iterable = [raw_mappings] if isinstance(raw_mappings, osmap.Mapping) else list(raw_mappings)
            for mapping in mapping_iterable:
                source_resource = getattr(mapping, "source_resource", None)
                target_resource = getattr(mapping, "target_resource", None)
                if not source_resource or not target_resource:
                    logger.warning("Mapping in %s missing source or target resource", mapping_path)
                    continue

                self._record_edge(source_resource.href, target_resource.href)

    def _record_edge(self, source_href: str, target_href: str) -> None:
        source_key = _normalize_catalog_href(self._trestle_root, source_href)
        target_key = _normalize_catalog_href(self._trestle_root, target_href)
        self._href_lookup.setdefault(source_key, source_href)
        self._href_lookup.setdefault(target_key, target_href)
        self._adjacency.setdefault(source_key, set()).add(target_key)

    def reachable_catalogs(self, source_catalog_href: str) -> Set[str]:
        """Return all target catalogs reachable from the given source, following mappings transitively."""
        start_key = _normalize_catalog_href(self._trestle_root, source_catalog_href)
        self._href_lookup.setdefault(start_key, source_catalog_href)

        visited: Set[str] = set()
        reachable: Set[str] = set()
        to_visit = [start_key]

        while to_visit:
            current = to_visit.pop(0)
            if current in visited:
                continue
            visited.add(current)

            for neighbor in self._adjacency.get(current, set()):
                if neighbor in visited:
                    continue
                reachable.add(self._href_lookup.get(neighbor, neighbor))
                to_visit.append(neighbor)

        return reachable


class TransitiveControlResolver:
    """Resolve target controls reachable transitively across mapping collections."""

    def __init__(
        self,
        mapping_root: Optional[pathlib.Path] = None,
        trestle_root: Optional[pathlib.Path] = None,
    ) -> None:
        self._trestle_root = trestle_root or find_trestle_root(DEFAULT_TRESTLE_ROOT)
        self._mapping_root = mapping_root or self._trestle_root / "mapping-collections"
        self._edges: Dict[str, List[_MappingEdge]] = {}
        self._href_lookup: Dict[str, str] = {}
        self._catalog_cache: Dict[str, CatalogInterface] = {}
        self._load_edges()

    def _load_edges(self) -> None:
        if not self._mapping_root.exists():
            raise FileNotFoundError(f"Mapping root {self._mapping_root} does not exist")

        mapping_files = sorted(self._mapping_root.glob("*/mapping-collection.json"))
        if not mapping_files:
            logger.warning("No mapping collections found under %s", self._mapping_root)

        for mapping_path in mapping_files:
            try:
                collection = osmap.MappingCollection.oscal_read(mapping_path)
            except Exception as exc:  # pragma: no cover - defensive logging
                logger.error("Failed to load mapping collection %s: %s", mapping_path, exc)
                continue

            if collection is None or collection.mappings is None:
                continue

            raw_mappings = collection.mappings
            mapping_iterable = [raw_mappings] if isinstance(raw_mappings, osmap.Mapping) else list(raw_mappings)
            for mapping in mapping_iterable:
                maps = as_list(getattr(mapping, "maps", []))
                source_res = getattr(mapping, "source_resource", None)
                target_res = getattr(mapping, "target_resource", None)
                if not source_res or not target_res or not maps:
                    logger.warning("Mapping in %s missing source, target, or maps", mapping_path)
                    continue
                self._record_edge(source_res.href, target_res.href, maps)

    def _record_edge(self, source_href: str, target_href: str, maps: List[osmap.Map]) -> None:
        source_key = _normalize_catalog_href(self._trestle_root, source_href)
        target_key = _normalize_catalog_href(self._trestle_root, target_href)
        self._href_lookup.setdefault(source_key, source_href)
        self._href_lookup.setdefault(target_key, target_href)
        edge = _MappingEdge(target_key=target_key, target_href=target_href, maps=maps)
        self._edges.setdefault(source_key, []).append(edge)

    def _get_catalog_interface(self, href: str) -> CatalogInterface:
        if href not in self._catalog_cache:
            catalog = ProfileResolver.get_resolved_profile_catalog(self._trestle_root, href)
            self._catalog_cache[href] = CatalogInterface(catalog)
        return self._catalog_cache[href]

    def resolve_transitive_targets(
        self, source_catalog_href: str, source_control_id: str
    ) -> List[TransitiveControlTarget]:
        """Return typed results reachable from a given control across mapping chains."""
        start_key = _normalize_catalog_href(self._trestle_root, source_catalog_href)
        self._href_lookup.setdefault(start_key, source_catalog_href)

        visited: Set[Tuple[str, str]] = set()
        results: Dict[Tuple[str, str], TransitiveControlTarget] = {}
        queue: List[Tuple[str, str]] = [(start_key, source_control_id)]

        while queue:
            current_catalog_key, control_id = queue.pop(0)
            if (current_catalog_key, control_id) in visited:
                continue
            visited.add((current_catalog_key, control_id))

            for edge in self._edges.get(current_catalog_key, []):
                for map_entry in edge.maps:
                    if not any(item.id_ref == control_id for item in as_list(map_entry.sources)):
                        continue
                    for target_item in as_list(map_entry.targets):
                        target_control_id = target_item.id_ref
                        target_key = edge.target_key
                        state_key = (target_key, target_control_id)
                        if state_key not in visited:
                            queue.append(state_key)
                        try:
                            catalog_interface = self._get_catalog_interface(edge.target_href)
                            control = catalog_interface.get_control(target_control_id)
                        except Exception as exc:  # pragma: no cover - defensive logging
                            logger.error(
                                "Failed to resolve control %s in catalog %s: %s",
                                target_control_id,
                                edge.target_href,
                                exc,
                            )
                            continue
                        if control is None:
                            logger.warning(
                                "Target control %s referenced in mapping to %s not found",
                                target_control_id,
                                edge.target_href,
                            )
                            continue
                        results.setdefault(
                            state_key,
                            TransitiveControlTarget(catalog_href=edge.target_href, control=control),
                        )

        # Return controls sorted for deterministic output.
        return [results[key] for key in sorted(results)]
