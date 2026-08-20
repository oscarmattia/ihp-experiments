from pathlib import Path
import layout.blocks.driver_stage as ds
from layout.common.lvs import run_lvs
from layout.common.netlist import write_block_cdl
from layout.common.sizing import read_params

orig = ds._supply_ring_tie

def noop(*a, **k):
    pass

ds._supply_ring_tie = noop
try:
    b = ds.build_driver_stage(params=read_params("circuits/ctle56n/spice/driver_params.inc"))
    d = Path("/workspace/layout/blocks/out/driver_stage/notie_test")
    d.mkdir(parents=True, exist_ok=True)
    gds = b.write(d)
    cdl = write_block_cdl("driver_dut", b.port_nets, b.instances, d / "t.cdl")
    lvs = run_lvs(gds=gds, cdl=cdl, run_dir=d / "lvs", topcell="driver_dut", disable_tap_extraction=True)
    txt = Path(lvs.extracted_netlist).read_text()
    print("mega", "|" in txt.splitlines()[2], "clean", lvs.clean)
    for line in txt.splitlines():
        if line.startswith("D"):
            print(line)
finally:
    ds._supply_ring_tie = orig
