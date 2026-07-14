import adsk.core


def run(_context: str):
    machine_width = targets["eco_machine_width"]
    belt_width = targets["eco_belt_width"]
    door_width = targets["eco_door_width"]
    machine_width.expression = "760 mm"
    belt_width.expression = "400 mm"
    door_width.expression = "460 mm"
