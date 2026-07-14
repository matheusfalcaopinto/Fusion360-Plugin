import adsk.core


def run(_context: str):
    flange_od = targets["eco_flange_od"]
    bolt_circle = targets["eco_bolt_circle"]
    bolt_count = targets["eco_bolt_count"]
    meridian_count = targets["eco_meridian_count"]
    dome_radius = targets["eco_dome_radius"]
    flange_od.expression = "230 mm"
    bolt_circle.expression = "210 mm"
    bolt_count.expression = "16"
    meridian_count.expression = "16"
    dome_radius.expression = "105 mm"
