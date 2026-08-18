# Passive device characterization (placeholder)

R / L / C LUT characterization for IHP SG13G2 will live here.

Planned (later):

| Class | Models (examples) | LUT idea |
| --- | --- | --- |
| Resistors | `rsil`, `rhigh`, `rppd`, … | R(W,L,T), mismatch hooks |
| Capacitors | `cmim`, `cmom`, MOSCAP | C(V), Q proxies |
| Inductors | RF spiral / line (as available) | L(f), Q(f) |

Format will match `char/common/lut.py` (`.npz` + metadata) so a common
browser can load MOS / BJT / passives the same way.

```bash
# not implemented yet
./char/passive/run_all.sh
```
