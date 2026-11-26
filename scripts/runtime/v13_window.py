"""Retain existing windows and associate them with the v12-r3 reference."""
from v11_window import replace_once
from v12_window import HOST_COUNTERS as earlier, MILESTONES, patch_drive as v12

HOST_COUNTERS = (*earlier, "spatial_members", "spatial_unsplittable", "spatial_accepted")


def patch_drive(source):
    source = v12(source)
    source = replace_once(source, "from v12_window import MILESTONES, HOST_COUNTERS",
                          "from v13_window import MILESTONES, HOST_COUNTERS")
    return replace_once(source, "summary = dict(target=v11_next, v11=old, v12=prefix,",
                        "summary = dict(target=v11_next, v12=old, v13=prefix,")
