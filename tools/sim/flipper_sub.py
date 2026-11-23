"""[merge-v3] Flipper Zero SubGhz RAW .sub: the documented format as a checker,
with a reference file in that format.

Rules, from Flipper's file-format documentation (SubGhz RAW File, version 1):
  Filetype: Flipper SubGhz RAW File      Version: 1
  Frequency: <Hz>                        Preset: <FuriHalSubGhzPreset...>
  Protocol: RAW
  RAW_Data: timings in microseconds; every value non-zero, the first one
            positive, signs alternating (positive = carrier on), at most 512
            values on a line; the key may repeat.

REFERENCE is a file written to those rules (the layout of the documentation's
own example). It is not a capture read back from a real Flipper: that check
needs the device.
"""

REFERENCE = """Filetype: Flipper SubGhz RAW File
Version: 1
Frequency: 433920000
Preset: FuriHalSubGhzPresetOok650Async
Protocol: RAW
RAW_Data: 29262 -361 68 -2635 66 -24113 66 -11 400 -1200 400 -1200 1200 -400
RAW_Data: 361 -723 361 -361 723 -361 361 -11300
"""
HEADER_KEYS = ["Filetype", "Version", "Frequency", "Preset", "Protocol"]
MAX_VALUES_PER_LINE = 512
KNOWN_PRESETS = ("FuriHalSubGhzPresetOok270Async", "FuriHalSubGhzPresetOok650Async",
                 "FuriHalSubGhzPreset2FSKDev238Async", "FuriHalSubGhzPreset2FSKDev476Async")


def parse(text: str):
    """(header dict in file order, [timings]) or raises ValueError saying why."""
    header, timings, keys = {}, [], []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        key, separator, value = line.partition(": ")
        if not separator:
            raise ValueError("line %d has no 'Key: value': %r" % (number, line[:40]))
        if key == "RAW_Data":
            try:
                row = [int(item) for item in value.split()]
            except ValueError:
                raise ValueError("line %d: RAW_Data holds something that is not an integer" % number) from None
            if not row:
                raise ValueError("line %d: empty RAW_Data" % number)
            if len(row) > MAX_VALUES_PER_LINE:
                raise ValueError("line %d: %d values on one RAW_Data line (max %d)"
                                 % (number, len(row), MAX_VALUES_PER_LINE))
            timings.extend(row)
        else:
            if timings:
                raise ValueError("line %d: %s after RAW_Data" % (number, key))
            header[key] = value
            keys.append(key)
    if keys != HEADER_KEYS:
        raise ValueError("header keys are %s, expected %s" % (keys, HEADER_KEYS))
    return header, timings


def validate(text: str) -> list:
    """The timings of a valid RAW .sub; raises ValueError otherwise."""
    header, timings = parse(text)
    if header["Filetype"] != "Flipper SubGhz RAW File":
        raise ValueError("Filetype is %r" % header["Filetype"])
    if header["Version"] != "1" or header["Protocol"] != "RAW":
        raise ValueError("Version/Protocol are %r/%r" % (header["Version"], header["Protocol"]))
    if not header["Frequency"].isdigit() or not 280_000_000 <= int(header["Frequency"]) <= 930_000_000:
        raise ValueError("Frequency %r is not a sub-GHz frequency in Hz" % header["Frequency"])
    if header["Preset"] not in KNOWN_PRESETS:
        raise ValueError("unknown Preset %r" % header["Preset"])
    if not timings:
        raise ValueError("no RAW_Data")
    if timings[0] <= 0:
        raise ValueError("RAW_Data must start with a positive (carrier on) value")
    for index, value in enumerate(timings):
        if value == 0:
            raise ValueError("RAW_Data value %d is zero" % index)
        if index and (value > 0) == (timings[index - 1] > 0):
            raise ValueError("RAW_Data values %d and %d have the same sign" % (index - 1, index))
    return timings
