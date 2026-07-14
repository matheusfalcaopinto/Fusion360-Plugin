import adsk.core


def run(_context: str):
    wrist_length = targets["eco_wrist_length"]
    forearm_length = targets["eco_forearm_length"]
    upper_arm_length = targets["eco_upper_arm_length"]
    wrist_length.expression = "85 mm"
    forearm_length.expression = "155 mm"
    upper_arm_length.expression = "195 mm"
