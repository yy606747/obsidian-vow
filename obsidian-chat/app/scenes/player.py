"""Clock-driven loop player. No provider calls, threads or physical outputs."""

from copy import deepcopy


class MockDevices:
    def __init__(self):
        self.connected = set()
        self.outputs = {}
        self.write_count = {}

    def connect(self, device_id):
        self.connected.add(device_id)

    def disconnect(self, device_id):
        self.stop(device_id)
        self.connected.discard(device_id)

    def write(self, device_id, values):
        if device_id not in self.connected:
            raise ValueError("scene_device_disconnected")
        self.outputs[device_id] = dict(values)
        self.write_count[device_id] = self.write_count.get(device_id, 0) + 1

    def stop(self, device_id):
        self.outputs[device_id] = {}


class LoopPlayer:
    def __init__(self, devices, *, now):
        self.devices, self.now = devices, now
        self.sequences = {}
        self.accepted = {}
        self.started = {}
        self.positions = {}
        self.versions = {}

    def apply(self, device_id, sequence):
        action = sequence["action"]
        if action == "hold":
            return
        if action == "stop":
            self.stop(device_id)
            return
        self.versions[device_id] = self.versions.get(device_id, 0) + 1
        self.accepted[device_id] = deepcopy(sequence)
        self.sequences[device_id] = deepcopy(sequence)
        self.started[device_id] = self.now()
        self.positions.pop(device_id, None)
        self.tick(device_id)

    def position(self, device_id):
        sequence = self.sequences.get(device_id)
        if not sequence:
            return {"playing": False, "step": None, "elapsed_ms": 0}
        elapsed = max(0, int((self.now() - self.started[device_id]) * 1000))
        cycle = sum(step["duration_ms"] for step in sequence["steps"])
        offset = elapsed % cycle
        for index, step in enumerate(sequence["steps"]):
            if offset < step["duration_ms"]:
                return {"playing": True, "step": index, "elapsed_ms": elapsed,
                        "cycle": elapsed // cycle, "step_elapsed_ms": offset}
            offset -= step["duration_ms"]

    def tick(self, device_id=None):
        for key in ([device_id] if device_id else list(self.sequences)):
            if key not in self.sequences:
                continue
            position = self.position(key)
            marker = (position["cycle"], position["step"])
            if self.positions.get(key) != marker:
                self.devices.write(key, self.sequences[key]["steps"][position["step"]]["values"])
                self.positions[key] = marker

    def stop(self, device_id):
        self.versions[device_id] = self.versions.get(device_id, 0) + 1
        self.sequences.pop(device_id, None)
        self.positions.pop(device_id, None)
        self.devices.stop(device_id)

    def stop_all(self):
        for key in set(self.sequences) | set(self.accepted):
            self.stop(key)
