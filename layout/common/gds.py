"""Writing device and block layouts to GDS, with optional LVS net labels.

The PDK's own LVS testcases carry no net-name text at all: ``sg13g2.lvs``
extracts devices and nets from connectivity and the runner is invoked with
``--ignore_top_ports_mismatch``. Labels are therefore not required for a
single-device compare, but they make the compare stronger — named nets let LVS
check that our port mapping is what we think it is — and they become necessary
once several devices share a cell. Net names go on the ``*_text`` layer of the
metal the pin sits on, which is what ``layers_definitions.lvs`` reads.
"""

from __future__ import annotations

from pathlib import Path

from layout.common.layers import ROUTING_METALS, layer_map
from layout.common.pdk import pya_module
from layout.common.spec import Terminal

#: Pin layer -> text layer, for the metals that carry LVS net names.
_PIN_TO_TEXT: dict[str, tuple[int, int]] = {
    f"{metal.lower()}_pin": spec["text"]  # type: ignore[misc]
    for metal, spec in ROUTING_METALS.items()
}

#: Plate layers used as terminals by capacitors map to their metal's text layer.
_DRW_TO_TEXT: dict[str, tuple[int, int]] = {
    f"{metal.lower()}_drw": spec["text"]  # type: ignore[misc]
    for metal, spec in ROUTING_METALS.items()
}


def text_layer_for(pin_layer: str) -> tuple[int, int] | None:
    """Text layer that names nets on the metal a pin sits on.

    Returns ``None`` for non-metal pins. ``gatpoly_pin`` is the case that
    matters: the PDK defines no ``gatpoly_text`` layer, so a gate net can only
    be named where the gate reaches metal.
    """
    if pin_layer in _PIN_TO_TEXT:
        return _PIN_TO_TEXT[pin_layer]
    return _DRW_TO_TEXT.get(pin_layer)


def stamp_net_labels(
    layout,
    cell,
    terminals: list[Terminal],
    net_names: dict[str, str] | None = None,
) -> list[str]:
    """Write net-name text at each terminal; returns the terminals skipped.

    ``net_names`` maps terminal name to net name; terminals absent from the
    mapping are labelled with their own name.
    """
    pya = pya_module()
    names = net_names or {}
    skipped: list[str] = []
    for terminal in terminals:
        target = text_layer_for(terminal.layer)
        if target is None:
            skipped.append(terminal.name)
            continue
        net = names.get(terminal.name, terminal.name)
        layer_index = layout.layer(target[0], target[1])
        x, y = terminal.center
        text = pya.DText(net, x, y)
        cell.shapes(layer_index).insert(text)
    return skipped


def export_cell(layout, cell, name: str, flatten: bool = False):
    """Copy ``cell`` into a fresh layout whose single top cell is ``name``.

    A PCell variant is a library proxy, and ``SaveLayoutOptions.select_cell``
    on a proxy writes an empty file — the PDK DRC runner then reports "No
    topcell found in layout". Copying the tree into a plain cell materializes
    the geometry and gives DRC and LVS exactly one, predictably named top cell.
    """
    pya = pya_module()
    out = pya.Layout()
    out.dbu = layout.dbu
    target = out.create_cell(name)
    target.copy_tree(cell)
    if flatten:
        target.flatten(-1, True)
    return out, target


def write_gds(layout, cell, path: Path, name: str | None = None, flatten: bool = False) -> Path:
    """Write ``cell`` as the sole top cell of a GDS file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    out, _ = export_cell(layout, cell, name or path.stem, flatten=flatten)
    out.write(str(path))
    return path


def write_for_magic(gds_in: Path, gds_out: Path, cell: str | None = None) -> tuple[Path, str]:
    """Rewrite a GDS so Magic can read it; returns ``(path, top_cell_name)``.

    Two things upset Magic about a gdsfactory-written GDS:

    * kfactory stores KLayout PCell state in a ``$$$CONTEXT_INFO$$$`` cell, and
      Magic stops with "Unknown layer/datatype in boundary" when it hits it.
    * a component built as ``gf.Component()`` without a name lands as
      ``Unnamed_1``, so ``load <cell>`` fails.

    The output is flattened as well, since Magic has no notion of the library
    proxies a PCell hierarchy carries.
    """
    pya = pya_module()
    gds_in = Path(gds_in)
    gds_out = Path(gds_out)
    gds_out.parent.mkdir(parents=True, exist_ok=True)

    layout = pya.Layout()
    layout.read(str(gds_in))

    if cell:
        source = layout.cell(cell)
        if source is None:
            raise RuntimeError(f"no cell {cell!r} in {gds_in}")
    else:
        tops = layout.top_cells()
        if len(tops) != 1:
            raise RuntimeError(f"{gds_in} has {len(tops)} top cells; name one explicitly")
        source = tops[0]

    name = cell or (gds_out.stem if source.name.startswith("Unnamed") else source.name)
    out, target = export_cell(layout, source, name, flatten=True)

    options = pya.SaveLayoutOptions()
    options.format = "GDS2"
    options.select_all_layers()
    options.write_context_info = False
    out.write(str(gds_out), options)
    return gds_out, target.name


def read_gds(path: Path):
    """Read a GDS file; returns ``(layout, top_cell)``."""
    pya = pya_module()
    layout = pya.Layout()
    layout.read(str(path))
    tops = layout.top_cells()
    if len(tops) != 1:
        raise RuntimeError(f"{path} has {len(tops)} top cells, expected exactly 1")
    return layout, tops[0]


def flatten_copy(layout, cell, name: str | None = None):
    """A flattened copy of ``cell`` in a fresh layout.

    Magic's GDS reader and some DRC checks behave more predictably on flat
    geometry, and PCell variants carry library references that do not survive
    a round trip through other tools.
    """
    pya = pya_module()
    out = pya.Layout()
    out.dbu = layout.dbu
    target = out.create_cell(name or cell.name)
    target.copy_tree(cell)
    target.flatten(-1, True)
    return out, target


def layer_summary(layout, cell) -> dict[str, int]:
    """Polygon count per named layer, for manifests and quick sanity checks."""
    lm = layer_map()
    summary: dict[str, int] = {}
    for layer_index in layout.layer_indexes():
        info = layout.get_info(layer_index)
        count = 0
        it = cell.begin_shapes_rec(layer_index)
        while not it.at_end():
            if not it.shape().is_text():
                count += 1
            it.next()
        if count:
            name = lm.name_for(info.layer, info.datatype) or f"{info.layer}/{info.datatype}"
            summary[name] = count
    return dict(sorted(summary.items()))
