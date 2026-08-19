crashbackups stop
drc off
gds read /workspace/layout/blocks/out/ctle_stage/ctle_stage.gds
load ctle_stage -dereference
select top cell
extract path .
extract all
ext2spice lvs
ext2spice cthresh 0
ext2spice rthresh 0
ext2spice subcircuit on
ext2spice -o /workspace/layout/blocks/out/ctle_stage/pex_run_nores/ctle_stage_pex.spice
quit -noprompt
