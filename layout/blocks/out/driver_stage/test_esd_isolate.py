from pathlib import Path
import layout.blocks.driver_stage as ds
from layout.common.lvs import run_lvs
from layout.common.netlist import write_block_cdl
from layout.common.sizing import read_params

# Monkeypatch build to skip ESD bus routing after placement
orig_build = ds.build_driver_stage

def build_no_esd_route(*a, **k):
    import layout.blocks.driver_stage as mod
    orig_loop = None
    src = Path(mod.__file__).read_text()
    return orig_build(*a, **k)

b = ds.build_driver_stage(params=read_params("circuits/ctle56n/spice/driver_params.inc"), with_esd=False)
d = Path("/workspace/layout/blocks/out/driver_stage/no_esd_test")
d.mkdir(parents=True, exist_ok=True)
gds = b.write(d)
cdl = write_block_cdl("driver_dut", b.port_nets, b.instances, d / "t.cdl")
lvs = run_lvs(gds=gds, cdl=cdl, run_dir=d / "lvs", topcell="driver_dut", disable_tap_extraction=True)
txt = Path(lvs.extracted_netlist).read_text()
print("no esd mega", "|" in txt)

b2 = ds.build_driver_stage(params=read_params("circuits/ctle56n/spice/driver_params.inc"), with_esd=True, with_pad_feed=False)
d2 = Path("/workspace/layout/blocks/out/driver_stage/esd_nofeed_test")
d2.mkdir(parents=True, exist_ok=True)
gds2 = b2.write(d2)
cdl2 = write_block_cdl("driver_dut", b2.port_nets, b2.instances, d2 / "t.cdl")
lvs2 = run_lvs(gds=gds2, cdl=cdl2, run_dir=d2 / "lvs", topcell="driver_dut", disable_tap_extraction=True)
txt2 = Path(lvs2.extracted_netlist).read_text()
print("esd no pad feed mega", "|" in txt2)
for line in txt2.splitlines():
    if line.startswith("D"):
        print(line)
