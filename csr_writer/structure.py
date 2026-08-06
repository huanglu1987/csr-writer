"""Strict, order-preserving loader for the verified ICH E3 chapter tree."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

from .errors import StructureError
from .io_utils import load_yaml


ICH_OFFICIAL = "ich-official"
INDUSTRY_CONVENTION = "industry-convention"
SPONSOR_CUSTOM = "sponsor-custom"
ALLOWED_LAYERS = frozenset({ICH_OFFICIAL, INDUSTRY_CONVENTION, SPONSOR_CUSTOM})


@dataclass(frozen=True)
class SectionNode:
    id: str
    layer: str
    en: str
    zh: str
    level: int
    children: tuple["SectionNode", ...]

    def title(self, language: str = "zh") -> str:
        if language == "zh":
            return self.zh or self.en
        if language == "en":
            return self.en or self.zh
        if language == "bilingual":
            if self.zh and self.en:
                return f"{self.zh}（{self.en}）"
            return self.zh or self.en
        raise StructureError(f"Unsupported title language: {language}")

    def walk(self) -> Iterator["SectionNode"]:
        yield self
        for child in self.children:
            yield from child.walk()


@dataclass(frozen=True)
class ChapterTree:
    sections: tuple[SectionNode, ...]
    metadata: dict[str, Any]

    def walk(self) -> Iterator[SectionNode]:
        for section in self.sections:
            yield from section.walk()

    def select_layers(self, layers: Iterable[str]) -> tuple[SectionNode, ...]:
        selected = frozenset(layers)
        unknown = selected - ALLOWED_LAYERS
        if unknown:
            raise StructureError(f"Unknown skeleton layer(s): {sorted(unknown)}")

        def select(node: SectionNode) -> SectionNode | None:
            if node.layer not in selected:
                return None
            children = tuple(child for raw in node.children if (child := select(raw)) is not None)
            return SectionNode(node.id, node.layer, node.en, node.zh, node.level, children)

        result = tuple(node for raw in self.sections if (node := select(raw)) is not None)
        for root in result:
            _validate_numeric_child_sequence(root)
        return result


def _validate_numeric_child_sequence(node: SectionNode) -> None:
    numeric_suffixes = [
        int(child.id.rsplit(".", 1)[-1])
        for child in node.children
        if child.id.rsplit(".", 1)[-1].isdigit()
    ]
    if numeric_suffixes and numeric_suffixes != list(range(1, len(numeric_suffixes) + 1)):
        raise StructureError(
            f"Selected numeric children of {node.id} must remain contiguous from 1; "
            f"got {numeric_suffixes}. Select the intervening layer or change the extension ids."
        )
    for child in node.children:
        _validate_numeric_child_sequence(child)


def _require_mapping(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise StructureError(f"{context} must be a mapping")
    return value


def _parse_node(
    raw_value: Any,
    inherited_layer: str | None,
    default_layer: str | None,
    parent_id: str | None,
) -> SectionNode:
    raw = _require_mapping(raw_value, f"section under {parent_id or 'root'}")
    section_id = raw.get("id")
    if not isinstance(section_id, str) or not section_id.strip():
        raise StructureError(f"Section id under {parent_id or 'root'} must be a non-empty string")
    section_id = section_id.strip()
    for component in section_id.split("."):
        if component.isdigit() and str(int(component)) != component:
            raise StructureError(
                f"Section {section_id} has non-canonical numeric component {component!r}"
            )
    if parent_id and section_id.rsplit(".", 1)[0] != parent_id:
        raise StructureError(f"Section {section_id} is not a direct child of {parent_id}")

    layer = raw.get("layer", default_layer or inherited_layer)
    if layer not in ALLOWED_LAYERS:
        raise StructureError(f"Section {section_id} has unknown or missing layer {layer!r}")

    level = len(section_id.split("."))
    if level > 5:
        raise StructureError(f"Section {section_id} exceeds the supported five heading levels")
    en = raw.get("en", "")
    zh = raw.get("zh", "")
    if not isinstance(en, str) or not isinstance(zh, str) or not (en.strip() or zh.strip()):
        raise StructureError(f"Section {section_id} needs an English or Chinese title")

    child_specs = (
        ("children", None),
        ("children_official", ICH_OFFICIAL),
        ("children_convention", INDUSTRY_CONVENTION),
        ("children_sponsor", SPONSOR_CUSTOM),
    )
    children: list[SectionNode] = []
    for field, child_default_layer in child_specs:
        raw_children = raw.get(field, [])
        if raw_children is None:
            raw_children = []
        if not isinstance(raw_children, list):
            raise StructureError(f"{section_id}.{field} must be a list")
        for raw_child in raw_children:
            children.append(
                _parse_node(
                    raw_child,
                    inherited_layer=layer,
                    default_layer=child_default_layer,
                    parent_id=section_id,
                )
            )

    return SectionNode(
        id=section_id,
        layer=layer,
        en=en.strip(),
        zh=zh.strip(),
        level=level,
        children=tuple(children),
    )


def _validate_tree(tree: ChapterTree) -> None:
    top_ids = [node.id for node in tree.sections]
    expected = [str(number) for number in range(1, 17)]
    if top_ids != expected:
        raise StructureError(f"ICH official top level must be exactly 1-16 in order; got {top_ids}")

    seen: dict[str, str] = {}
    for node in tree.walk():
        if node.id in seen:
            raise StructureError(
                f"Duplicate section id {node.id} in layers {seen[node.id]} and {node.layer}"
            )
        seen[node.id] = node.layer
        if node.layer == ICH_OFFICIAL and (
            node.id.startswith("14.1.") or node.id.startswith("14.2.")
        ):
            raise StructureError(
                f"{node.id} cannot be ich-official: ICH E3 defines no official children under 14.1/14.2"
            )
        if node.layer == ICH_OFFICIAL and node.id.startswith("16.1."):
            tail = node.id.split(".")[-1]
            if tail.isdigit() and int(tail) > 12:
                raise StructureError(
                    f"{node.id} cannot be ich-official: official 16.1 ends at 16.1.12"
                )
    for root in tree.sections:
        _validate_numeric_child_sequence(root)


def load_chapter_tree(path: str | Path) -> ChapterTree:
    payload = load_yaml(path)
    if not isinstance(payload, dict) or not isinstance(payload.get("sections"), list):
        raise StructureError("Chapter YAML must contain a sections list")
    sections = tuple(
        _parse_node(raw, inherited_layer=None, default_layer=None, parent_id=None)
        for raw in payload["sections"]
    )
    tree = ChapterTree(sections=sections, metadata=dict(payload.get("meta") or {}))
    _validate_tree(tree)
    return tree


def flatten_sections(sections: Iterable[SectionNode]) -> list[SectionNode]:
    flattened: list[SectionNode] = []
    for section in sections:
        flattened.extend(section.walk())
    return flattened
