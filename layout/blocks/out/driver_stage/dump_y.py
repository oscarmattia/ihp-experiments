from layout.blocks.driver_stage import build_driver_stage, snap, rule, route_width, min_space, ESD_OUTBOARD_GAP, ROW_GAP, _PAD_KEEPOUT, PAD_FEED_METAL
from layout.common.sizing import read_params
from layout.common.devices import build
from layout.devices.catalog import esd_devices, driver_devices

p = read_params("circuits/ctle56n/spice/driver_params.inc")
cat = {s.name: s for s in driver_devices(p) + esd_devices()}
b = build_driver_stage(params=p)
ring = b.ring
print("ring vss_top", ring.ports["vss"][0].center[1], "vdd_top", ring.ports["vdd"][0].center[1])
print("ring left vdd", ring.ports["vdd"][2].center, "vss", ring.ports["vss"][2].center)
print("bbox", b.layout.top_cells()[0].dbbox())
